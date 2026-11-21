# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Client-side unified-diff generator.

GitHub exposes a server-side diff endpoint (``Accept:
application/vnd.github.v3.diff`` on a PR URL). Azure DevOps does not
have an equivalent — its REST API exposes base and head file
contents via the items API but leaves diff computation to the client.

The PR-check pipeline's retrieval-heavy handlers (ADR_COMPLIANCE,
ARCHITECTURAL_DUPLICATION) work off unified-diff strings. To keep the
handlers provider-agnostic below the fetch layer, this module produces
the same shape of diff string from base + head bytes as GitHub would
have served up.

Design decisions:

- **Bytes in, str out.** ``shared.source_fetch.fetch_file_at_sha``
  returns bytes because file contents may be binary. This module owns
  the decode step so each handler doesn't reimplement it.
- **UTF-8 with errors="replace"** for text decoding. A single stray
  byte should not fail the whole PR-check turn.
- **Binary detection heuristic.** A NUL byte in the first 1024 bytes
  of either side triggers a ``(binary file changed)`` stub rather
  than an attempted decode. Matches GitHub's diff output for binaries.
- **Added / deleted file support.** ``base_content=None`` means the
  file did not exist in the base commit (added). ``head_content=None``
  means it does not exist in head (deleted). Both cases produce a
  legal unified-diff block that the downstream LLM analyses can parse.
"""

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


_BINARY_SNIFF_BYTES = 1024


def _looks_binary(content: Optional[bytes]) -> bool:
    if content is None:
        return False
    sniff = content[:_BINARY_SNIFF_BYTES]
    return b"\x00" in sniff


def _decode(content: Optional[bytes]) -> str:
    if content is None:
        return ""
    return content.decode("utf-8", errors="replace")


def compute_file_diff(
    base_content: Optional[bytes],
    head_content: Optional[bytes],
    filename: str,
    context_lines: int = 3,
) -> str:
    """Return a unified-diff string in the GitHub ``diff --git`` shape.

    Args:
        base_content: File bytes at the PR base commit, or ``None`` if
            the file did not exist there (i.e. the PR adds this file).
        head_content: File bytes at the PR head commit, or ``None`` if
            the file has been deleted in the PR.
        filename: Repo-relative path to embed in the diff header. Both
            "a/" and "b/" sides use the same filename per Git convention.
        context_lines: Number of unchanged lines of context around each
            hunk. Matches ``git diff -U{n}``; default 3.

    Returns:
        A unified-diff string. Contains the ``diff --git`` header on
        every non-empty result so ADR/duplication handlers'
        section-extractors can locate per-file boundaries.
        Returns the empty string only when base == head AND both are
        non-None (unchanged file — no diff).
    """
    if base_content is None and head_content is None:
        # Nothing to diff — file mentioned in changed_files but no
        # content on either side. Return an empty header so downstream
        # code doesn't crash.
        return f"diff --git a/{filename} b/{filename}\n(no content on either side)\n"

    if _looks_binary(base_content) or _looks_binary(head_content):
        return (
            f"diff --git a/{filename} b/{filename}\n"
            f"(binary file changed)\n"
        )

    base_text = _decode(base_content)
    head_text = _decode(head_content)

    if base_content is not None and head_content is not None and base_text == head_text:
        # Same content -- not really changed. Return empty so callers
        # can skip cleanly.
        return ""

    base_lines = base_text.splitlines(keepends=True)
    head_lines = head_text.splitlines(keepends=True)

    # Git-metadata prefix. Downstream consumers grep the diff string for
    # markers like ``"new file mode"`` (see
    # lambda/process-pr-check/check_handlers/license_compliance.py:is_new_file)
    # -- GitHub's server-side diff includes these lines and any diff we
    # produce needs to match. Live-burned 2026-07-01: without the
    # ``new file mode`` line, LICENSE_COMPLIANCE mis-classified an
    # added file as "existing" and skipped the copyright-header check.
    if base_content is None:
        base_label = "/dev/null"
        head_label = f"b/{filename}"
        git_meta = "new file mode 100644\n"
    elif head_content is None:
        base_label = f"a/{filename}"
        head_label = "/dev/null"
        git_meta = "deleted file mode 100644\n"
    else:
        base_label = f"a/{filename}"
        head_label = f"b/{filename}"
        git_meta = ""

    diff_iter = difflib.unified_diff(
        base_lines,
        head_lines,
        fromfile=base_label,
        tofile=head_label,
        n=context_lines,
        lineterm="",
    )

    header = f"diff --git a/{filename} b/{filename}\n{git_meta}"
    body = "\n".join(diff_iter)
    if body and not body.endswith("\n"):
        body += "\n"
    return header + body


def added_line_numbers(file_diff: str) -> List[int]:
    """1-based RIGHT-side line numbers of added ("+") lines in a unified diff.

    Walks the hunk headers (``@@ -a,b +c,d @@``) tracking the post-change line
    counter, collecting each added line's number in order. Used to anchor an
    inline PR finding on a real changed line -- ADR-031 forbids anchoring to a
    guessed line, so a diff with no added lines yields an empty list and the
    caller emits no inline finding.
    """
    added: List[int] = []
    new_line = 0
    in_hunk = False
    for raw in file_diff.split("\n"):
        if raw.startswith("@@"):
            try:
                plus = raw.split("+", 1)[1].strip()
                new_line = int(plus.split(",", 1)[0].split()[0])
                in_hunk = True
            except (IndexError, ValueError):
                in_hunk = False
            continue
        if not in_hunk:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            added.append(new_line)
            new_line += 1
        elif raw.startswith("-"):
            # Removed line -- no advance on the post-change side.
            continue
        elif raw.startswith(" "):
            # Context line -- advances the post-change side.
            new_line += 1
    return added


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class ChangeBlock:
    """One contiguous run of +/- lines inside a hunk.

    Attributes:
        removed_base_lines: 1-based BASE line numbers removed in this run,
            in order. Empty for a pure insertion.
        insert_after_base_line: Set only for a pure insertion (no removed
            lines): the BASE line immediately before the insertion point,
            0 when inserted at the top of the file. ``None`` otherwise.
        head_anchor: First added HEAD (RIGHT-side) line in this run, or
            ``None`` for a deletion-only run (ADR-031: never fabricate a line).
    """

    removed_base_lines: Tuple[int, ...]
    insert_after_base_line: Optional[int]
    head_anchor: Optional[int]


@dataclass(frozen=True)
class FileDiffInfo:
    """Per-file diff description built from one base/head fetch.

    Attributes:
        added_lines: Same list :func:`added_line_numbers` returns.
        blocks: Change blocks in file order.
        is_new: True when the file was absent at base and present at head.
        head_text: Decoded head content, or ``None`` when the file is absent
            at head (deleted) or binary. Lets a check re-parse the head
            version (e.g. BREAKING_CHANGES' signature compare) without a
            second fetch.
    """

    added_lines: List[int]
    blocks: List[ChangeBlock]
    is_new: bool
    head_text: Optional[str] = None


def _head_text(head_content: Optional[bytes]) -> Optional[str]:
    """Decoded head text, or ``None`` for an absent or binary file."""
    if head_content is None or _looks_binary(head_content):
        return None
    return _decode(head_content)


def _parse_hunk_header(raw: str) -> Optional[Tuple[int, int, int, int]]:
    """Return (next_base, base_count, next_head, head_count) or None.

    A zero count means the side is empty and its start number names the
    line BEFORE the hunk, so the next line on that side is start + 1.
    """
    match = _HUNK_HEADER_RE.match(raw)
    if not match:
        return None
    base_start = int(match.group(1))
    base_count = int(match.group(2)) if match.group(2) is not None else 1
    head_start = int(match.group(3))
    head_count = int(match.group(4)) if match.group(4) is not None else 1
    next_base = base_start if base_count > 0 else base_start + 1
    next_head = head_start if head_count > 0 else head_start + 1
    return next_base, base_count, next_head, head_count


def change_blocks(file_diff: str) -> List[ChangeBlock]:
    """Split a unified diff into contiguous change runs with base-side lines.

    A run is a maximal sequence of consecutive ``+``/``-`` lines inside a
    hunk; context lines and hunk headers end it. The base counter (from
    ``-a``) advances on ``-`` and context lines, the head counter (from
    ``+c``) on ``+`` and context lines. Hunk line counts bound the body, so
    ``---``/``+++`` file headers between hunks are skipped while a body line
    whose content starts with ``--``/``++`` is still counted.
    ``\\ No newline at end of file`` markers and blank separator lines are
    skipped without ending a run.
    """
    blocks: List[ChangeBlock] = []
    base_line = 0
    head_line = 0
    base_left = 0
    head_left = 0

    removed: List[int] = []
    insert_after: Optional[int] = None
    anchor: Optional[int] = None
    in_run = False

    def _flush() -> None:
        nonlocal removed, insert_after, anchor, in_run
        if in_run:
            blocks.append(ChangeBlock(
                removed_base_lines=tuple(removed),
                insert_after_base_line=None if removed else insert_after,
                head_anchor=anchor,
            ))
        removed = []
        insert_after = None
        anchor = None
        in_run = False

    for raw in file_diff.split("\n"):
        if raw.startswith("@@"):
            _flush()
            parsed = _parse_hunk_header(raw)
            if parsed is None:
                base_left = head_left = 0
            else:
                base_line, base_left, head_line, head_left = parsed
            continue
        if raw.startswith("\\") or raw == "":
            # "\ No newline at end of file", or the blank separator that
            # compute_file_diff emits between body lines (difflib keeps each
            # line's own newline and the join adds another). Neither ends a
            # run nor advances a counter; an empty source line is " ".
            continue
        if base_left <= 0 and head_left <= 0:
            # Outside a hunk body: file headers, git metadata, blank tail.
            _flush()
            continue
        if raw.startswith("-") and base_left > 0:
            if not in_run:
                in_run = True
                insert_after = base_line - 1
            removed.append(base_line)
            base_line += 1
            base_left -= 1
        elif raw.startswith("+") and head_left > 0:
            if not in_run:
                in_run = True
                insert_after = base_line - 1
            if anchor is None:
                anchor = head_line
            head_line += 1
            head_left -= 1
        elif raw.startswith(" "):
            _flush()
            base_line += 1
            head_line += 1
            base_left -= 1
            head_left -= 1
        else:
            # Unparseable body line: end the run, advance nothing.
            _flush()
    _flush()
    return blocks


def _fetch_kwargs_for(
    provider: str,
    repository: str,
    *,
    github_token: Optional[str],
    ado_org: Optional[str],
    ado_project: Optional[str],
    ado_repo: Optional[str],
    ado_access_token: Optional[str],
    gitlab_access_token: Optional[str],
    gitlab_base_url: Optional[str],
) -> Optional[Dict[str, Any]]:
    if provider == "github":
        return {"repository": repository, "github_token": github_token}
    if provider == "azure_devops":
        return {
            "ado_org": ado_org, "ado_project": ado_project,
            "ado_repo": ado_repo, "ado_access_token": ado_access_token,
        }
    if provider == "gitlab":
        return {
            "gitlab_project_path": repository,
            "gitlab_access_token": gitlab_access_token,
            "gitlab_base_url": gitlab_base_url,
        }
    return None


def diff_info_by_file(
    provider: str,
    repository: str,
    base_sha: str,
    head_sha: str,
    changed_files: List[str],
    *,
    github_token: Optional[str] = None,
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    gitlab_access_token: Optional[str] = None,
    gitlab_base_url: Optional[str] = None,
) -> Dict[str, FileDiffInfo]:
    """Map each changed file -> its :class:`FileDiffInfo`.

    Provider-agnostic (github / azure_devops / gitlab): fetches base + head
    bytes via ``shared.source_fetch.fetch_file_at_sha`` and diffs them with
    ``compute_file_diff``. A ``SourceFetchError`` on base means the file is
    new in the PR; on head, that it was deleted.

    Every file that diffed without error is included, even with no added
    lines. Best-effort: a per-file fetch/parse failure is logged and that
    file skipped, never raised. An unknown provider returns ``{}``.
    """
    from shared.source_fetch import SourceFetchError, fetch_file_at_sha

    fetch_kwargs = _fetch_kwargs_for(
        provider, repository,
        github_token=github_token, ado_org=ado_org, ado_project=ado_project,
        ado_repo=ado_repo, ado_access_token=ado_access_token,
        gitlab_access_token=gitlab_access_token,
        gitlab_base_url=gitlab_base_url,
    )
    if fetch_kwargs is None:
        logger.warning("diff_info_by_file: unknown provider %r", provider)
        return {}

    result: Dict[str, FileDiffInfo] = {}
    for path in changed_files or []:
        try:
            try:
                base_bytes = fetch_file_at_sha(provider, path, base_sha, **fetch_kwargs)
            except SourceFetchError:
                base_bytes = None  # file added -- absent at base
            try:
                head_bytes = fetch_file_at_sha(provider, path, head_sha, **fetch_kwargs)
            except SourceFetchError:
                head_bytes = None  # file deleted in head
            file_diff = compute_file_diff(base_bytes, head_bytes, path)
            result[path] = FileDiffInfo(
                added_lines=added_line_numbers(file_diff),
                blocks=change_blocks(file_diff),
                is_new=base_bytes is None and head_bytes is not None,
                head_text=_head_text(head_bytes),
            )
        except Exception as e:  # provider/network/parse -- skip this file
            logger.warning(
                "diff_info_by_file: skipping %s: %s", path, e, exc_info=True)
    return result


def added_lines_by_file(
    provider: str,
    repository: str,
    base_sha: str,
    head_sha: str,
    changed_files: List[str],
    *,
    github_token: Optional[str] = None,
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    gitlab_access_token: Optional[str] = None,
    gitlab_base_url: Optional[str] = None,
) -> Dict[str, List[int]]:
    """Map each changed file -> ALL its added (RIGHT-side) lines in the PR diff.

    Provider-agnostic (github / azure_devops / gitlab): fetches base + head bytes
    via ``shared.source_fetch.fetch_file_at_sha`` and diffs them with
    ``compute_file_diff``, then collects every added line (in order). This lets
    file-level checks (e.g. BREAKING_CHANGES, TEST_COVERAGE) anchor inline
    comments on the changed code -- and, with the full set rather than just the
    first line, anchor *multiple* findings per file on their own distinct lines
    instead of collapsing them all onto line one.

    Best-effort: a per-file fetch/parse failure is logged and skipped, never
    raised. Files with no added lines (pure deletion, binary, or unchanged) are
    omitted from the result (ADR-031: never fabricate a line).
    """
    infos = diff_info_by_file(
        provider, repository, base_sha, head_sha, changed_files,
        github_token=github_token, ado_org=ado_org, ado_project=ado_project,
        ado_repo=ado_repo, ado_access_token=ado_access_token,
        gitlab_access_token=gitlab_access_token,
        gitlab_base_url=gitlab_base_url,
    )
    return {path: info.added_lines for path, info in infos.items() if info.added_lines}

def first_added_lines(
    provider: str,
    repository: str,
    base_sha: str,
    head_sha: str,
    changed_files: List[str],
    **kwargs: Any,
) -> Dict[str, int]:
    """Map each changed file -> its FIRST added (RIGHT-side) line.

    Thin convenience over :func:`added_lines_by_file` for callers that want a
    single anchor per file. Same best-effort contract; same omissions.
    """
    return {
        path: lines[0]
        for path, lines in added_lines_by_file(
            provider, repository, base_sha, head_sha, changed_files, **kwargs
        ).items()
        if lines
    }
