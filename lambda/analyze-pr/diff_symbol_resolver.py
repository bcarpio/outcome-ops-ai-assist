"""Diff-driven symbol resolver for analyze-pr (plan-gkg chunk 06).

Walks a unified diff and the post-diff file content to identify which
canonical symbols a PR mutates. Each mutated symbol is tagged with a
``change_kind`` of either ``"signature"`` (the change touches the
declaration line itself — callers likely need updates) or ``"body"``
(the change is inside the body only — behavioral change but the
contract is preserved).

The canonical symbol IDs are produced by the chunk-01 extractor
(``process-batch-summary.extractor``) so they line up exactly with the
IDs the chunk-02 writer stored in DynamoDB. That alignment is what
lets the chunk-04 reader find callers.

Plan-gkg/06 "Symbol resolution from a diff" specifies the high-level
behavior. The implementation here is intentionally **language-agnostic
on the diff side** — we treat the unified diff as a stream of hunks
with file paths and (start_line, count) ranges. The language-specific
symbol extraction is delegated to the existing extractor's
``extract_facts_from_bytes`` so we don't re-implement parsing logic.

Failure modes (per plan-gkg/06):

- File with no hunks            : no targets emitted for that file.
- Removed/binary/null file      : no targets (the writer's graph has
                                  no callers for a file that's gone;
                                  surfacing them is meaningful but is
                                  not implemented in v1 — see "Removed
                                  files" comment in resolve_targets).
- Unsupported language          : silently skipped; the extractor's
                                  registry tells us when this is the
                                  case.
- Extractor exception           : logged and skipped for that file;
                                  other files still produce targets.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# sys.path bootstrap so we can import the chunk-01 extractor without the
# Lambda zip having to special-case it. The deploy package layout uses
# ``source_path = lambda/analyze-pr`` and we side-load shared/ via the
# handler's existing ``sys.path.insert`` (see handler.py top). The
# extractor lives in ``lambda/process-batch-summary/extractor/``; we
# *do not* ship it inside the analyze-pr deploy zip in v1 because the
# diff-side symbol resolver is the only consumer. Instead, the live
# Lambda runs the import lazily and falls back to "no targets" if the
# module isn't reachable — tests put the extractor on sys.path so the
# fixture-driven coverage continues to work.
# ---------------------------------------------------------------------------


_REPO_ROOT_GUESS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_EXTRACTOR_DIR_GUESS = os.path.join(
    _REPO_ROOT_GUESS, "lambda", "process-batch-summary"
)


def _try_import_extractor():
    """Return ``extract_facts_from_bytes`` or None when unavailable.

    Lazy import + path-shim because the analyze-pr Lambda's deploy zip
    doesn't include the extractor today. For local tests we put the
    extractor's parent dir on sys.path so the import succeeds; for the
    live Lambda we just degrade gracefully (the structural-impact
    section is best-effort).
    """

    try:
        from extractor import extract_facts_from_bytes  # type: ignore

        return extract_facts_from_bytes
    except Exception:
        if os.path.isdir(_EXTRACTOR_DIR_GUESS) and _EXTRACTOR_DIR_GUESS not in sys.path:
            sys.path.insert(0, _EXTRACTOR_DIR_GUESS)
            try:
                from extractor import extract_facts_from_bytes  # type: ignore

                return extract_facts_from_bytes
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "[diff_symbol_resolver] extractor import failed after "
                    "path-shim: %s",
                    exc,
                )
                return None
        return None


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


# We type ``change_kind`` as plain strings (no Enum) so the dataclass
# stays frozen-hashable and JSON-serializable without a custom encoder
# -- same shape choice as ``extractor.base.Fact.kind``.
CHANGE_KINDS: Tuple[str, ...] = ("signature", "body")


@dataclass(frozen=True)
class Hunk:
    """One hunk from a unified diff, in post-diff coordinates.

    ``new_start`` is the first line in the post-diff file (1-indexed).
    ``new_count`` is the number of lines the hunk occupies after the
    change. ``signature_lines`` is the set of post-diff line numbers
    whose content adds/removes a declaration-shaped fragment (``def``,
    ``function``, ``class``, Java method headers, etc.) — those lines
    flip the symbol to ``signature``-change.
    """

    new_start: int
    new_count: int
    signature_lines: Tuple[int, ...]


@dataclass(frozen=True)
class MutatedSymbol:
    """One canonical symbol the PR mutates.

    ``symbol_id`` is the chunk-01 canonical ID (e.g.
    ``"python:lambda.shared.outcomeops_license::report_repos_snapshot"``)
    so the chunk-04 reader will find its callers.

    ``change_kind`` distinguishes signature-changes (callers likely
    need updates) from body-changes (behavioral change only). Per the
    plan's failure-modes lean: when ambiguous, treat as signature so
    the reviewer-model emits the stronger warning.

    ``file_path`` / ``definition_line`` / ``language`` anchor the
    symbol for the rendered impact block.
    """

    symbol_id: str
    change_kind: str
    file_path: str
    definition_line: int
    language: str


# ---------------------------------------------------------------------------
# Unified-diff parser
# ---------------------------------------------------------------------------


# ``@@ -old_start,old_count +new_start,new_count @@`` — counts default
# to 1 when omitted (per the unified-diff format). We capture the new-
# side start + count because PR review runs against the post-diff file
# content.
_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-\d+(?:,\d+)?\s+\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)

# Match declaration-shaped fragments across the languages the chunk-01
# extractor covers. False positives here are cheap (we err on the side
# of "this is a signature change" per plan-gkg/06 "Symbol resolution
# from a diff"). False negatives mean a signature change gets the
# softer "body" prompt -- the failure mode we're explicitly avoiding.
_SIGNATURE_RE = re.compile(
    r"^[+\-]\s*("
    # Python
    r"def\s+\w+|"
    r"async\s+def\s+\w+|"
    r"class\s+\w+|"
    # TypeScript / JavaScript
    r"export\s+(?:async\s+)?function\s+\w+|"
    r"export\s+default\s+(?:async\s+)?function\s+\w+|"
    r"(?:async\s+)?function\s+\w+|"
    r"export\s+(?:abstract\s+)?class\s+\w+|"
    r"export\s+interface\s+\w+|"
    r"interface\s+\w+|"
    # Java / C#  (e.g. "public static void foo(...)")
    r"(?:public|private|protected|internal|static|final|virtual|"
    r"override|abstract|async|synchronized)\s+[\w<>\[\],\s]*?\w+\s*\("
    r")"
)


def parse_diff_hunks(patch: str) -> List[Hunk]:
    """Parse a unified diff's ``patch`` into a list of ``Hunk``.

    ``patch`` is the text GitHub returns in the ``patch`` field of
    ``/pulls/{n}/files`` entries. It can be empty (binary diffs, very
    large patches) -- we return ``[]`` in that case.

    Signature-line detection: for each hunk, we walk the body lines
    (those starting with ``+``, ``-``, or `` ``) tracking the
    post-diff line number, and record the line numbers whose content
    matches ``_SIGNATURE_RE``. Context-only matches (lines starting
    with `` ``) are NOT recorded as signature lines -- they didn't
    change.
    """

    if not patch:
        return []

    hunks: List[Hunk] = []
    cur_new_start: Optional[int] = None
    cur_new_count: int = 0
    cur_line: int = 0
    sig_lines: List[int] = []

    def _flush():
        if cur_new_start is None:
            return
        hunks.append(
            Hunk(
                new_start=cur_new_start,
                new_count=cur_new_count,
                signature_lines=tuple(sig_lines),
            )
        )

    for raw in patch.splitlines():
        # Hunk header
        match = _HUNK_HEADER_RE.match(raw)
        if match:
            _flush()
            cur_new_start = int(match.group("new_start"))
            cur_new_count = int(match.group("new_count") or "1")
            cur_line = cur_new_start
            sig_lines = []
            continue

        if cur_new_start is None:
            # Pre-header preamble (``diff --git ...`` / ``+++ ...``).
            continue

        if not raw:
            # Blank line inside a hunk body counts as a context line
            # in unified-diff parlance, but real diffs almost never have
            # them. Treat as context and advance the post-diff cursor.
            cur_line += 1
            continue

        marker = raw[0]
        if marker == "+":
            # Addition -> occupies a post-diff line, may be a signature.
            if _SIGNATURE_RE.match(raw):
                sig_lines.append(cur_line)
            cur_line += 1
        elif marker == "-":
            # Deletion -> doesn't occupy a post-diff line. We still
            # record it as a signature-change signal if the removed
            # line looked like a declaration, because that means the
            # PR removed (or rewrote) a symbol's declaration. The
            # signature line we record is the post-diff cursor (which
            # is where the NEXT added line will land), so any caller-
            # affecting deletion still flips the surrounding symbol to
            # signature-change.
            if _SIGNATURE_RE.match(raw):
                sig_lines.append(cur_line)
        elif marker == "\\":
            # ``\ No newline at end of file`` -- ignore.
            continue
        else:
            # Context line (`` ``) or any other marker -- advance.
            cur_line += 1

    _flush()
    return hunks


# ---------------------------------------------------------------------------
# Symbol-range computation
# ---------------------------------------------------------------------------


def _language_for_path(file_path: str) -> Optional[str]:
    """Best-effort file-extension -> language mapping.

    Mirrors the chunk-01 extractor's coverage (Python / TS / Java /
    C# / Go / ABAP). Unknown extensions return None and the resolver
    short-circuits for that file -- the structural-impact block
    simply omits those files (failure-modes table: "Symbol resolution
    fails on a hunk -> that symbol omitted from impact list").
    """

    lower = file_path.lower()
    if lower.endswith(".py"):
        return "python"
    if lower.endswith(".ts") or lower.endswith(".tsx"):
        return "typescript"
    if lower.endswith(".java"):
        return "java"
    if lower.endswith(".cs"):
        return "csharp"
    if lower.endswith(".go"):
        return "go"
    if lower.endswith(".abap"):
        return "abap"
    return None


def _build_symbol_ranges(
    file_path: str,
    language: str,
    post_diff_source: bytes,
    extract_fn,
) -> List[Tuple[str, int, int]]:
    """Return ``[(symbol_id, start_line, end_line), ...]`` from the
    extractor's ``defines`` facts.

    The extractor only emits each define's ``start_line``, so we
    derive each symbol's end as ``(next_define_start - 1)`` after
    sorting by start-line. This is an over-approximation: a symbol's
    actual body ends before the next top-level construct, but we don't
    know exactly where without re-parsing. For the diff -> symbol map
    purpose it works because:

      * A hunk inside a function body falls between that function's
        start_line and the next function/class's start_line.
      * A hunk that touches the function's declaration line itself is
        flagged separately by ``Hunk.signature_lines`` (not by range
        membership), so the signature-change path doesn't depend on
        the range upper bound.

    The fallback for the last symbol uses a sentinel large line number
    so trailing hunks still attribute to it.
    """

    try:
        facts = list(extract_fn(file_path, language, post_diff_source))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[diff_symbol_resolver] extractor failed for %s: %s", file_path, exc
        )
        return []

    defines = [f for f in facts if f.kind == "defines"]
    if not defines:
        return []

    # Sort by start_line so we can compute (start, next_start - 1).
    defines.sort(key=lambda f: f.line)

    ranges: List[Tuple[str, int, int]] = []
    for i, fact in enumerate(defines):
        if i + 1 < len(defines):
            end = max(fact.line, defines[i + 1].line - 1)
        else:
            # Sentinel: trailing hunks attribute to the last define.
            end = 10**9
        ranges.append((fact.target_symbol_id, fact.line, end))
    return ranges


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilePatch:
    """One file's patch + post-diff content, fed to ``resolve_targets``.

    ``file_path`` is the repo-relative path GitHub returns. ``patch``
    is the unified-diff body (the ``patch`` field on GitHub's
    ``/pulls/.../files`` response). ``post_diff_source`` is the file's
    content at the PR's head SHA -- we run the extractor over it so
    the canonical IDs match what's in the graph for the SAME shape
    (the writer uses the head's content too, modulo the rebuild lag
    documented in plan-gkg/06 "Staleness handling for in-flight PRs").

    ``status`` mirrors GitHub's per-file ``status`` (``"added"``,
    ``"modified"``, ``"removed"``, ``"renamed"``) -- we filter out
    ``"removed"`` because the post-diff source doesn't exist by
    definition and v1 doesn't surface removed-symbol callers (plan-gkg/
    06 "Removed files" notes this is on the deferred list).
    """

    file_path: str
    status: str
    patch: Optional[str]
    post_diff_source: Optional[bytes]


def resolve_targets(
    patches: Sequence[FilePatch],
) -> List[MutatedSymbol]:
    """Compute the MutatedSymbol list for the PR.

    Returns symbols in stable order (sorted by canonical ID) so the
    rendered impact block is reproducible across runs. Duplicate
    symbols across hunks/files collapse to a single entry, with
    ``change_kind`` upgraded to ``"signature"`` if any contributing
    hunk flagged a signature change.
    """

    extract_fn = _try_import_extractor()
    if extract_fn is None:
        logger.warning(
            "[diff_symbol_resolver] extractor module unavailable; "
            "structural impact block will be empty"
        )
        return []

    # symbol_id -> (change_kind, file_path, definition_line, language)
    seen: Dict[str, MutatedSymbol] = {}

    for patch in patches:
        if patch.status == "removed":
            # plan-gkg/06 "Removed files" -- deferred for v1.
            continue
        if not patch.patch or not patch.post_diff_source:
            continue
        language = _language_for_path(patch.file_path)
        if language is None:
            continue

        hunks = parse_diff_hunks(patch.patch)
        if not hunks:
            continue
        ranges = _build_symbol_ranges(
            patch.file_path, language, patch.post_diff_source, extract_fn
        )
        if not ranges:
            continue

        for hunk in hunks:
            hunk_start = hunk.new_start
            hunk_end = hunk.new_start + max(0, hunk.new_count - 1)
            sig_set = set(hunk.signature_lines)

            for sym_id, sym_start, sym_end in ranges:
                # Range membership test: does the hunk overlap the
                # symbol's body? We use inclusive ranges on both sides.
                overlap = not (hunk_end < sym_start or hunk_start > sym_end)
                # Direct decl-line hit even if hunk_end < sym_start
                # (e.g. a deletion immediately before the symbol still
                # records a signature_line equal to sym_start when the
                # removed line was the previous symbol's declaration).
                # The conservative read is: only attribute to this
                # symbol when range overlap is true.
                if not overlap:
                    continue

                # Signature-change if any signature_line falls inside
                # this symbol's range. Defaults to body-change otherwise.
                touches_signature = any(
                    sym_start <= ln <= sym_end for ln in sig_set
                )
                # Extra-conservative leaning: a hunk that begins ON or
                # within 1 line of the symbol's start_line is also
                # treated as a signature change. Real-world diffs often
                # mark the line above the def (decorator, type
                # annotation) which counts as a contract change for
                # the reviewer-model's purposes.
                if hunk_start <= sym_start <= hunk_start + 1:
                    touches_signature = True

                kind = "signature" if touches_signature else "body"
                prev = seen.get(sym_id)
                if prev is None:
                    seen[sym_id] = MutatedSymbol(
                        symbol_id=sym_id,
                        change_kind=kind,
                        file_path=patch.file_path,
                        definition_line=sym_start,
                        language=language,
                    )
                else:
                    # Upgrade to signature if any hunk flagged it.
                    if kind == "signature" and prev.change_kind != "signature":
                        seen[sym_id] = MutatedSymbol(
                            symbol_id=sym_id,
                            change_kind="signature",
                            file_path=prev.file_path,
                            definition_line=prev.definition_line,
                            language=prev.language,
                        )

    return sorted(seen.values(), key=lambda m: m.symbol_id)


__all__ = (
    "CHANGE_KINDS",
    "FilePatch",
    "Hunk",
    "MutatedSymbol",
    "parse_diff_hunks",
    "resolve_targets",
)
