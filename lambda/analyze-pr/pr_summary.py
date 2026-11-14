"""pr_summary — build the PR/MR summary comment body.

Internal builder for ``analyze-pr``. Given provider-agnostic PR metadata and
per-file unified diffs, it produces the markdown body for the summary comment
(a plain-English "what changed and why", a per-file changes table, and the
list of checks that will run) and returns the model token usage so the caller
can render a cost footer.

This module is an internal helper, NOT an API Gateway handler: no CORS, no
JWT, no ``_response`` envelope. It is invoked in-process by
``analyze-pr/handler.py`` and returns ``(markdown_body, usage_or_None)``.

Standards applied (retrieved at plan time via OutcomeOps MCP):

- **ADR-004** — structured logging with a module-name prefix, env read at
  module level, try/except with ``exc_info=True`` on the model call, type
  hints and docstrings.
- **ADR-025** — summarize ONLY what the diff shows; never invent behaviour not
  in the diff; if the PR title/description disagrees with the diff, say so
  rather than smoothing it over. The diff is the source of truth; the
  description is a claim to check against it.
- **ADR-032** — the per-file table and the file count are built in code from
  ``metadata.changed_files`` + ``metadata.file_change_types``; the model never
  decides which files are listed and never computes "N files changed". When a
  diff is capped or omitted, a visible truncation signal is emitted so a
  reviewer is never shown a clipped walkthrough as if it were whole.
- **ADR-035 / ADR-016** — the model is called through ``shared.model_client``
  with ``workload="pr-analyzer"`` and ``tier="basic"`` (the Haiku-class cheap
  tier) so Bedrock spend is tagged to the right application-inference profile.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

from shared.model_client import invoke
from shared.model_client_types import Usage

logger = logging.getLogger()

# ADR-016 cost-attribution: tag this spend to the pr-analyzer workload at the
# cheap (Haiku-class) tier. A summary is light synthesis, not deep reasoning.
_WORKLOAD = "pr-analyzer"
_TIER = "basic"

# Diff size caps. A summary is a cheap, high-visibility artifact; it must not
# balloon model cost on a large PR. Oversized patches are truncated and the
# omission is surfaced per ADR-032.
_MAX_PATCH_BYTES_PER_FILE = int(os.environ.get("PR_SUMMARY_MAX_PATCH_BYTES_PER_FILE", "6000"))
_MAX_TOTAL_PATCH_BYTES = int(os.environ.get("PR_SUMMARY_MAX_TOTAL_PATCH_BYTES", "24000"))
_MAX_OUTPUT_TOKENS = int(os.environ.get("PR_SUMMARY_MAX_OUTPUT_TOKENS", "700"))

_FILE_NOTES_MARKER = "FILE NOTES:"


def build_pr_summary(
    *,
    metadata,
    diffs_by_file: Dict[str, str],
    checks_to_run: List[str],
    truncated_files: Optional[List[str]] = None,
) -> Tuple[str, Optional[Usage]]:
    """Build the markdown summary body and return (body, usage).

    Args:
        metadata: ``shared.pr_metadata.PRMetadata`` — provides ``changed_files``,
            ``file_change_types`` (path -> added|modified|removed|renamed),
            ``title``, ``author``, ``base_branch``.
        diffs_by_file: path -> unified-diff text. May be partial or empty; any
            file missing here (or capped) is reported as truncated.
        checks_to_run: human-readable check names already computed upstream.
        truncated_files: files whose diff the caller could not fetch/include.
            Merged with diffs this builder caps.

    Returns:
        ``(markdown_body, usage)``. ``usage`` is ``None`` when the model call
        failed (the body still contains the deterministic table + checks line).
        The per-file table and count are always code-supplied; only the prose
        and the optional per-file notes come from the model.
    """
    changed_files: List[str] = list(getattr(metadata, "changed_files", []) or [])
    change_types: Dict[str, str] = dict(getattr(metadata, "file_change_types", {}) or {})

    # Cap the diffs we feed the model, tracking what we omit. Start from any
    # caller-supplied truncation set so both sources are surfaced together.
    omitted = set(truncated_files or [])
    capped_diffs, capped_omitted = _cap_diffs(diffs_by_file)
    omitted.update(capped_omitted)
    # A changed file with no diff available at all is also an omission.
    for path in changed_files:
        if path not in diffs_by_file and path not in capped_diffs:
            omitted.add(path)

    prose, notes_by_file, usage = _invoke_model(
        metadata=metadata,
        changed_files=changed_files,
        change_types=change_types,
        capped_diffs=capped_diffs,
        omitted=sorted(omitted),
    )

    table = _render_file_table(changed_files, change_types, notes_by_file, omitted)
    checks_line = _render_checks_line(checks_to_run)
    trunc_note = _render_truncation_note(sorted(omitted))

    parts = [
        "## PR summary",
        "",
        prose.strip() or "_Summary unavailable for this revision._",
        "",
        table,
        "",
        checks_line,
    ]
    if trunc_note:
        parts += ["", trunc_note]
    body = "\n".join(parts)
    return body, usage


def _cap_diffs(diffs_by_file: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Return (included_diffs, omitted_paths) honoring per-file + total caps.

    Files are included in sorted order for determinism. A file whose patch
    exceeds the per-file cap is truncated (kept, marked). Once the running
    total exceeds the global cap, remaining files are omitted entirely.
    """
    included: Dict[str, str] = {}
    omitted: List[str] = []
    running = 0
    for path in sorted(diffs_by_file):
        patch = diffs_by_file[path] or ""
        encoded = patch.encode("utf-8", "replace")
        if running >= _MAX_TOTAL_PATCH_BYTES:
            omitted.append(path)
            continue
        if len(encoded) > _MAX_PATCH_BYTES_PER_FILE:
            patch = encoded[:_MAX_PATCH_BYTES_PER_FILE].decode("utf-8", "ignore")
            patch += "\n... [diff truncated] ..."
            omitted.append(path)
        included[path] = patch
        running += len(patch.encode("utf-8", "replace"))
    return included, omitted


def _invoke_model(
    *,
    metadata,
    changed_files: List[str],
    change_types: Dict[str, str],
    capped_diffs: Dict[str, str],
    omitted: List[str],
) -> Tuple[str, Dict[str, str], Optional[Usage]]:
    """Call the model for prose + optional per-file notes. Degrades on error.

    On any model failure this logs with ``exc_info=True`` and returns empty
    prose + empty notes + ``None`` usage, so the deterministic table still
    renders (ADR-004 error handling, ADR-032 code-supplied structure).
    """
    system = _build_system_prompt()
    user_text = _build_user_prompt(metadata, changed_files, change_types, capped_diffs, omitted)
    try:
        resp = invoke(
            workload=_WORKLOAD,
            tier=_TIER,
            system=system,
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
    except Exception:
        logger.error(
            "[pr-summary] model invocation failed; emitting deterministic "
            "summary without prose",
            exc_info=True,
        )
        return "", {}, None
    prose, notes = _parse_model_output(resp.text or "", changed_files)
    return prose, notes, resp.usage


def _build_system_prompt() -> str:
    """System prompt enforcing the ADR-025 diff-is-truth discipline."""
    return (
        "You summarize a pull request for a human reviewer. Rules:\n"
        "- Describe ONLY what the diff shows. Do not invent behaviour that is "
        "not present in the diff.\n"
        "- The diff is the source of truth. The PR title and description are "
        "claims to check against it. If the description disagrees with the "
        "diff, say so explicitly; do not average or smooth over the "
        "disagreement.\n"
        "- Do not count files or state totals; that is computed separately.\n"
        "- Be concise and concrete.\n\n"
        "Output format:\n"
        "1. A 2-4 sentence paragraph: what changed and why.\n"
        f"2. Then a line '{_FILE_NOTES_MARKER}' followed by at most one bullet "
        "per file as '- <path>: <short note>'. Omit this section if you have "
        "nothing file-specific to add."
    )


def _build_user_prompt(
    metadata,
    changed_files: List[str],
    change_types: Dict[str, str],
    capped_diffs: Dict[str, str],
    omitted: List[str],
) -> str:
    """Assemble the user message. Diff is primary evidence; title/author secondary."""
    title = getattr(metadata, "title", "") or ""
    author = getattr(metadata, "author", "") or ""
    base_branch = getattr(metadata, "base_branch", "") or ""

    lines: List[str] = []
    lines.append("PR metadata (secondary context — a claim to verify against the diff):")
    lines.append(f"- title: {title}")
    lines.append(f"- author: {author}")
    lines.append(f"- target branch: {base_branch}")
    lines.append("")
    lines.append("Changed files (authoritative list; statuses are code-supplied):")
    for path in changed_files:
        lines.append(f"- {path} ({change_types.get(path, 'modified')})")
    if omitted:
        lines.append("")
        lines.append(
            "NOTE: diffs for these files were omitted or truncated due to "
            "size; do not infer their contents: " + ", ".join(omitted)
        )
    lines.append("")
    lines.append("Diffs (primary evidence):")
    if capped_diffs:
        for path in sorted(capped_diffs):
            lines.append(f"\n--- {path} ---")
            lines.append(capped_diffs[path])
    else:
        lines.append("(no diff text available)")
    return "\n".join(lines)


def _parse_model_output(text: str, changed_files: List[str]) -> Tuple[str, Dict[str, str]]:
    """Split model output into prose and a path->note map.

    The note map is keyed ONLY by paths that are in ``changed_files``; any
    other path the model emits is ignored, so a hallucinated path cannot
    appear in the table (ADR-032).
    """
    if _FILE_NOTES_MARKER in text:
        prose_part, notes_part = text.split(_FILE_NOTES_MARKER, 1)
    else:
        prose_part, notes_part = text, ""
    valid = set(changed_files)
    notes: Dict[str, str] = {}
    for raw in notes_part.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if ":" not in line:
            continue
        path, note = line.split(":", 1)
        path = path.strip()
        note = note.strip()
        if path in valid and note:
            notes[path] = note
    return prose_part.strip(), notes


def _render_file_table(
    changed_files: List[str],
    change_types: Dict[str, str],
    notes_by_file: Dict[str, str],
    omitted,
) -> str:
    """Render the per-file table. Rows == changed_files; count is len() (ADR-032)."""
    count = len(changed_files)
    header = f"**Files changed ({count}):**"
    if count == 0:
        return header + "\n\n_No files changed._"
    omitted_set = set(omitted)
    rows = ["", "| File | Change | Note |", "| --- | --- | --- |"]
    for path in changed_files:
        status = change_types.get(path, "modified")
        note = notes_by_file.get(path, "")
        if path in omitted_set and not note:
            note = "_diff omitted/truncated_"
        # Escape pipes so a path or note can't break the table.
        safe_path = path.replace("|", "\\|")
        safe_note = note.replace("|", "\\|")
        rows.append(f"| `{safe_path}` | {status} | {safe_note} |")
    return header + "\n" + "\n".join(rows)


def _render_checks_line(checks_to_run: List[str]) -> str:
    """Render the queued-checks line from the upstream-computed list."""
    if not checks_to_run:
        return "**Checks queued:** none"
    return "**Checks queued:** " + ", ".join(checks_to_run)


def _render_truncation_note(omitted: List[str]) -> str:
    """Load-bearing truncation signal (ADR-032) when any diff was clipped."""
    if not omitted:
        return ""
    return (
        "> Note: the diffs for "
        f"{len(omitted)} file(s) were truncated or omitted from this summary "
        "due to size, so the walkthrough above may be partial. The file list "
        "and count are complete."
    )
