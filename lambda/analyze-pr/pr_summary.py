# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""pr_summary — build the PR/MR summary comment body.

Internal builder for ``analyze-pr``. Given provider-agnostic PR metadata and
per-file unified diffs, it produces the markdown body for the summary comment
(a plain-English "what changed and why" walkthrough plus the list of checks
that will run) and returns the model token usage so the caller can render a
cost footer.

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
- **ADR-032** — the model is kept from authoring aggregates: the system prompt
  forbids it from counting files or stating totals, and the comment renders no
  file list or count of its own. The SCM (GitHub / GitLab / ADO) already
  renders the authoritative changed-file list and per-file statuses natively,
  so duplicating them here would be redundant and would reintroduce exactly the
  model-authored-count risk the ADR bans. The only count this module emits (the
  truncation note's "N files") is computed in code, never by the model.
- **ADR-035 / ADR-016** — the model is called through ``shared.model_client``
  with ``workload="pr-analyzer"`` and ``tier="advanced"`` so Bedrock spend is
  tagged to the right application-inference profile and the model id comes from
  the deployment's tfvars (swappable to e.g. Opus), never hardcoded here.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

from shared.model_client import invoke
from shared.model_client_types import Usage

logger = logging.getLogger()

# ADR-016 cost-attribution: tag this spend to the pr-analyzer workload. The
# summary runs at the ADVANCED tier -- it is a high-visibility, every-PR comment
# where prose quality carries the feature -- and the advanced tier resolves to
# whatever model the deployment maps it to via tfvars
# (``bedrock_advanced_model_id`` / the pr-analyzer advanced app-inference
# profile), swappable to e.g. Opus per customer. No model id is hardcoded here;
# the tier string is the only lever and ``shared.model_client`` does the lookup.
_WORKLOAD = "pr-analyzer"
_TIER = "advanced"

# Public so the comment's usage footer can report the tier without duplicating
# the literal. Kept in lock-step with ``_TIER``.
SUMMARY_MODEL_TIER = _TIER

# Diff size caps. A summary is a cheap, high-visibility artifact; it must not
# balloon model cost on a large PR. Oversized patches are truncated and the
# omission is surfaced per ADR-032.
_MAX_PATCH_BYTES_PER_FILE = int(os.environ.get("PR_SUMMARY_MAX_PATCH_BYTES_PER_FILE", "6000"))
_MAX_TOTAL_PATCH_BYTES = int(os.environ.get("PR_SUMMARY_MAX_TOTAL_PATCH_BYTES", "24000"))
# Output cap. The model writes prose only (a 2-4 sentence walkthrough, no
# per-file notes), so the response is short by construction; 1500 leaves ample
# headroom for a complex change without letting a runaway response balloon cost
# or (as an earlier per-file-notes design did on large PRs) blow the budget and
# collapse the prose entirely. Tunable per-customer via the env var.
_MAX_OUTPUT_TOKENS = int(os.environ.get("PR_SUMMARY_MAX_OUTPUT_TOKENS", "1500"))


def build_pr_summary(
    *,
    metadata,
    diffs_by_file: Dict[str, str],
    checks_to_run: List[str],
    truncated_files: Optional[List[str]] = None,
) -> Tuple[str, Optional[Usage]]:
    """Build the markdown summary body and return (body, usage).

    The body is a prose walkthrough of the diff plus the queued-checks line. It
    deliberately renders NO file list or count: the SCM already shows the
    changed files and their statuses, so repeating them adds nothing and would
    reintroduce the model-authored-aggregate risk ADR-032 bans. The model is
    held to prose only and told not to count.

    Args:
        metadata: ``shared.pr_metadata.PRMetadata`` — provides ``changed_files``,
            ``file_change_types`` (path -> added|modified|removed|renamed),
            ``title``, ``author``, ``base_branch``.
        diffs_by_file: path -> unified-diff text. May be partial or empty; any
            file missing here (or capped) is counted as truncated.
        checks_to_run: human-readable check names already computed upstream.
        truncated_files: files whose diff the caller could not fetch/include.
            Merged with diffs this builder caps.

    Returns:
        ``(markdown_body, usage)``. ``usage`` is ``None`` when the model call
        failed (the body still carries the checks line and a clear fallback).
    """
    changed_files: List[str] = list(getattr(metadata, "changed_files", []) or [])
    change_types: Dict[str, str] = dict(getattr(metadata, "file_change_types", {}) or {})

    # Cap the diffs we feed the model, tracking what we omit so the reviewer
    # knows the prose may not cover every file. Start from any caller-supplied
    # truncation set so both sources are surfaced together.
    omitted = set(truncated_files or [])
    capped_diffs, capped_omitted = _cap_diffs(diffs_by_file)
    omitted.update(capped_omitted)
    # A changed file with no diff available at all is also an omission.
    for path in changed_files:
        if path not in diffs_by_file and path not in capped_diffs:
            omitted.add(path)

    prose, usage = _invoke_model(
        metadata=metadata,
        changed_files=changed_files,
        change_types=change_types,
        capped_diffs=capped_diffs,
        omitted=sorted(omitted),
    )

    parts = [
        "## PR summary",
        "",
        _resolve_prose(prose, usage),
        "",
        _render_checks_line(checks_to_run),
    ]
    trunc_note = _render_truncation_note(sorted(omitted))
    if trunc_note:
        parts += ["", trunc_note]
    return "\n".join(parts), usage


def _resolve_prose(prose: str, usage: Optional[Usage]) -> str:
    """Pick the prose block, with a clear fallback when the model gave nothing.

    Three cases: usable prose (render it), a failed model call (``usage`` is
    ``None`` — the call was logged with ``exc_info`` upstream), or a call that
    succeeded but returned nothing usable (rare). Each gets an honest message
    rather than a blank or a misleading "unavailable" on a large-but-fine PR.
    """
    text = (prose or "").strip()
    if text:
        return text
    if usage is None:
        return "_Summary unavailable for this revision._"
    return (
        "_This revision is too large to summarize in prose; "
        "review the changed files directly._"
    )


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
) -> Tuple[str, Optional[Usage]]:
    """Call the model for the prose walkthrough. Degrades on error.

    On any model failure this logs with ``exc_info=True`` and returns empty
    prose + ``None`` usage, so the comment still posts with the checks line and
    a fallback (ADR-004 error handling).
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
            "[pr-summary] model invocation failed; emitting summary without prose",
            exc_info=True,
        )
        return "", None
    return (resp.text or "").strip(), resp.usage


def _build_system_prompt() -> str:
    """System prompt: ADR-025 diff-is-truth discipline, prose only (ADR-032)."""
    return (
        "You summarize a pull request for a human reviewer. Rules:\n"
        "- Describe ONLY what the diff shows. Do not invent behaviour that is "
        "not present in the diff.\n"
        "- The diff is the source of truth. The PR title and description are "
        "claims to check against it. If the description disagrees with the "
        "diff, say so explicitly; do not average or smooth over the "
        "disagreement.\n"
        "- Do NOT count files, enumerate file names, or state totals. The "
        "reviewer already sees the changed-file list in the PR, and any counts "
        "are computed separately.\n"
        "- Write ONE concise paragraph (2-4 sentences): what changed and why. "
        "No headings, no bullet list, no file-by-file breakdown."
    )


def _build_user_prompt(
    metadata,
    changed_files: List[str],
    change_types: Dict[str, str],
    capped_diffs: Dict[str, str],
    omitted: List[str],
) -> str:
    """Assemble the user message. Diff is primary evidence; title/author secondary.

    The changed-file list is passed as grounding CONTEXT for the prose; it is
    not rendered back into the comment (the SCM shows it natively).
    """
    title = getattr(metadata, "title", "") or ""
    author = getattr(metadata, "author", "") or ""
    base_branch = getattr(metadata, "base_branch", "") or ""

    lines: List[str] = []
    lines.append("PR metadata (secondary context — a claim to verify against the diff):")
    lines.append(f"- title: {title}")
    lines.append(f"- author: {author}")
    lines.append(f"- target branch: {base_branch}")
    lines.append("")
    lines.append("Changed files (context only — do not list these back):")
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


def _render_checks_line(checks_to_run: List[str]) -> str:
    """Render the queued-checks line from the upstream-computed list.

    This is the summary's distinct value: the SCM shows the files, but only
    OutcomeOps knows which checks are about to run on this PR.
    """
    if not checks_to_run:
        return "**Checks queued:** none"
    return "**Checks queued:** " + ", ".join(checks_to_run)


def _render_truncation_note(omitted: List[str]) -> str:
    """Code-computed note (ADR-032) when some diffs were too large to summarize.

    The count is derived in code from the omission set, never authored by the
    model. It warns the reviewer that the prose may not cover every change.
    """
    if not omitted:
        return ""
    return (
        "> Note: diffs for "
        f"{len(omitted)} file(s) were too large to include, so the summary "
        "above may not cover them."
    )
