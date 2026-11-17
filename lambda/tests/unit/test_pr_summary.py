# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for analyze-pr/pr_summary.py (the PR summary builder).

Asserts the correctness properties the standards demand:
- ADR-032: the comment renders NO file list or count of its own (the SCM shows
  those natively); the model is held to prose and instructed not to count; the
  model's prose is passed through verbatim -- there is NO post-hoc scrub of
  model-authored counts (scrubbing is the non-conformant anti-pattern ADR-032
  bans). The only count emitted (the truncation note) is code-computed.
- ADR-025: the diff is passed to the model as primary evidence.
- ADR-035/016: the model is called via the shared client with
  workload="pr-analyzer", tier="basic"; usage is captured.
- ADR-004: a model failure degrades to a clear fallback (non-fatal); the
  checks line still posts.

Mocking discipline (ADR-005): the model call is stubbed at the module-cached
attribute the builder exposes (``pr_summary_mod.invoke``), not at the boto3
factory level.
"""

import importlib.util
import os
import sys

import pytest

from shared.model_client_types import Usage

# Load pr_summary with a unique module name (it lives in analyze-pr/, which is
# not on sys.path during tests; conftest puts lambda/ on the path so its
# ``from shared...`` imports resolve).
_PR_SUMMARY_PATH = os.path.join(
    os.path.dirname(__file__), "../../analyze-pr/pr_summary.py"
)
_spec = importlib.util.spec_from_file_location("pr_summary_mod", _PR_SUMMARY_PATH)
pr_summary_mod = importlib.util.module_from_spec(_spec)
sys.modules["pr_summary_mod"] = pr_summary_mod
_spec.loader.exec_module(pr_summary_mod)

build_pr_summary = pr_summary_mod.build_pr_summary

pytestmark = pytest.mark.unit


class _Meta:
    """Duck-typed stand-in for shared.pr_metadata.PRMetadata."""

    def __init__(self, changed_files, file_change_types,
                 title="Add licensing", author="dev", base_branch="main"):
        self.changed_files = changed_files
        self.file_change_types = file_change_types
        self.title = title
        self.author = author
        self.base_branch = base_branch


class _Resp:
    """Minimal stand-in for shared.model_client.Response."""

    def __init__(self, text, input_tokens=11, output_tokens=22):
        self.text = text
        self.usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def _patch_invoke(monkeypatch, resp_or_exc):
    """Stub the bound ``invoke`` in pr_summary at the module-attribute level."""
    calls = {}

    def _fake_invoke(**kwargs):
        calls.update(kwargs)
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr(pr_summary_mod, "invoke", _fake_invoke)
    return calls


# --------------------------------------------------------------------------- #
# ADR-032: no redundant file list/count; model held to prose; no post-hoc scrub.
# --------------------------------------------------------------------------- #

def test_no_file_table_or_count_rendered(monkeypatch):
    # The SCM already shows the changed files + statuses, so the comment renders
    # neither a table nor a "Files changed (N)" line.
    meta = _Meta(
        changed_files=["a.py", "b/c.py", "d.txt"],
        file_change_types={"a.py": "added", "b/c.py": "modified", "d.txt": "removed"},
    )
    _patch_invoke(monkeypatch, _Resp("Adds a licensing handler and tests."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff --git a/a.py b/a.py\n+x"},
        checks_to_run=["adr_compliance"],
    )
    assert "| File | Change |" not in body  # no table header
    assert "| ---" not in body               # no table separator row
    assert "Files changed (" not in body     # no code-authored count line
    # The individual changed-file paths are NOT echoed back into the comment.
    assert "`a.py`" not in body and "`b/c.py`" not in body


def test_prose_rendered_verbatim_no_scrub(monkeypatch):
    # The model's prose is rendered as-is. Even if the model disobeys and writes
    # a count, the builder does NOT scrub it: post-hoc scrubbing of
    # model-authored counts is the non-conformant anti-pattern ADR-032 bans. The
    # guard against model-authored counts is the system-prompt instruction
    # (asserted separately), not output filtering.
    meta = _Meta(["a.py"], {"a.py": "modified"})
    _patch_invoke(monkeypatch, _Resp("This PR changes 99 files across the service."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"],
    )
    assert "This PR changes 99 files across the service." in body


def test_system_prompt_forbids_counting_and_listing(monkeypatch):
    meta = _Meta(["a.py"], {"a.py": "modified"})
    calls = _patch_invoke(monkeypatch, _Resp("Summary."))
    build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"],
    )
    system = calls["system"].lower()
    assert "do not count files" in system
    assert "concise paragraph" in system


def test_checks_line_rendered(monkeypatch):
    meta = _Meta(["a.py"], {"a.py": "modified"})
    _patch_invoke(monkeypatch, _Resp("Summary."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance", "test_coverage"],
    )
    assert "**Checks queued:** adr_compliance, test_coverage" in body


def test_empty_changed_files_renders_cleanly(monkeypatch):
    _patch_invoke(monkeypatch, _Resp("Nothing much."))
    body, _usage = build_pr_summary(
        metadata=_Meta([], {}), diffs_by_file={}, checks_to_run=[],
    )
    assert "## PR summary" in body
    assert "Nothing much." in body
    assert "Checks queued:** none" in body
    assert "Files changed (" not in body


# --------------------------------------------------------------------------- #
# Truncation note: code-computed (ADR-032), warns the prose may be partial.
# --------------------------------------------------------------------------- #

def test_truncation_note_present_when_truncated(monkeypatch):
    meta = _Meta(["a.py", "big.py"], {"a.py": "modified", "big.py": "modified"})
    _patch_invoke(monkeypatch, _Resp("Summary."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"], truncated_files=["big.py"],
    )
    assert "too large to include" in body
    assert "1 file(s)" in body  # count is code-derived from the omission set


def test_oversized_diff_is_capped_and_signalled(monkeypatch):
    # Six files each with a patch over the per-file byte cap; the running total
    # also trips the global cap, so later files are omitted entirely. The model
    # sees a truncation marker on the capped patch and the note fires.
    files = [f"f{i}.py" for i in range(6)]
    meta = _Meta(files, {f: "modified" for f in files})
    big = "+" + ("x" * 8000)
    diffs = {f: f"diff --git a/{f} b/{f}\n{big}" for f in files}
    calls = _patch_invoke(monkeypatch, _Resp("Summary."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file=diffs, checks_to_run=["adr_compliance"],
    )
    assert "too large to include" in body
    user_text = calls["messages"][0]["content"][0]["text"]
    assert "[diff truncated]" in user_text


def test_no_truncation_note_when_complete(monkeypatch):
    meta = _Meta(["a.py"], {"a.py": "modified"})
    _patch_invoke(monkeypatch, _Resp("Summary."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"],
    )
    assert "too large to include" not in body


# --------------------------------------------------------------------------- #
# ADR-025: diff is the primary evidence passed to the model.
# --------------------------------------------------------------------------- #

def test_diff_passed_as_primary_evidence(monkeypatch):
    meta = _Meta(["a.py"], {"a.py": "modified"})
    calls = _patch_invoke(monkeypatch, _Resp("Summary."))
    build_pr_summary(
        metadata=meta,
        diffs_by_file={"a.py": "diff --git a/a.py b/a.py\n+new_behaviour()"},
        checks_to_run=["adr_compliance"],
    )
    user_text = calls["messages"][0]["content"][0]["text"]
    assert "new_behaviour()" in user_text  # the diff reached the model
    assert "primary evidence" in user_text.lower()


# --------------------------------------------------------------------------- #
# ADR-035 / ADR-016: shared client, workload/tier, usage captured, cap raised.
# --------------------------------------------------------------------------- #

def test_invokes_with_pr_analyzer_basic_and_captures_usage(monkeypatch):
    meta = _Meta(["a.py"], {"a.py": "modified"})
    calls = _patch_invoke(monkeypatch, _Resp("Summary.", input_tokens=7, output_tokens=5))
    _body, usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"],
    )
    assert calls["workload"] == "pr-analyzer"
    # Advanced tier: the summary is a high-visibility, every-PR comment where
    # prose quality matters. The tier string is the only lever; the model id is
    # resolved from tfvars by shared.model_client, not hardcoded here.
    assert calls["tier"] == "advanced"
    # Prose-only output is short; the cap was raised to 1500 for headroom so a
    # large PR no longer blows the budget and collapses the walkthrough.
    assert calls["max_tokens"] == 1500
    assert usage is not None
    assert usage.input_tokens == 7 and usage.output_tokens == 5


# --------------------------------------------------------------------------- #
# ADR-004: degradation / fallbacks (non-fatal); the checks line always posts.
# --------------------------------------------------------------------------- #

def test_model_failure_degrades_to_unavailable(monkeypatch):
    meta = _Meta(["a.py", "b.py"], {"a.py": "added", "b.py": "modified"})
    _patch_invoke(monkeypatch, RuntimeError("bedrock down"))
    body, usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance", "test_coverage"],
    )
    assert usage is None
    assert "Summary unavailable" in body
    # The checks line still posts even though the model raised.
    assert "adr_compliance, test_coverage" in body
    # No file table even in the degraded path.
    assert "| File | Change |" not in body


def test_empty_prose_with_usage_falls_back_to_too_large(monkeypatch):
    # The call succeeded (usage present) but returned no usable prose. The
    # fallback must be the honest "too large" message, NOT the "unavailable"
    # one that implies a failure.
    meta = _Meta(["a.py"], {"a.py": "modified"})
    _patch_invoke(monkeypatch, _Resp("   "))  # whitespace-only
    body, usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"],
    )
    assert usage is not None
    assert "too large to summarize in prose" in body
    assert "Summary unavailable" not in body
