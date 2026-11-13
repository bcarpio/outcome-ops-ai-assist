"""Unit tests for analyze-pr/pr_summary.py (the PR summary builder).

Asserts the correctness properties the standards demand:
- ADR-032: the per-file table + count are code-supplied, never model arithmetic;
  a hallucinated path from the model never reaches the table; truncation is
  signalled.
- ADR-025: the diff is passed to the model as primary evidence.
- ADR-035/016: the model is called via the shared client with
  workload="pr-analyzer", tier="basic"; usage is captured.
- ADR-004: a model failure degrades to the deterministic summary (non-fatal).

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
# ADR-032: table + count are code-supplied, not model arithmetic.
# --------------------------------------------------------------------------- #

def test_table_enumerates_exactly_changed_files(monkeypatch):
    meta = _Meta(
        changed_files=["a.py", "b/c.py", "d.txt"],
        file_change_types={"a.py": "added", "b/c.py": "modified", "d.txt": "removed"},
    )
    # The model lies about the count (prose) and invents a file in its FILE
    # NOTES. The count header and the table rows are code-supplied, so neither
    # the fabricated count nor the invented path may reach the structured output.
    _patch_invoke(monkeypatch, _Resp(
        "This PR changes 99 files across the service.\n"
        "FILE NOTES:\n- evil.py: injected\n- a.py: adds handler"
    ))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff --git a/a.py b/a.py\n+x"},
        checks_to_run=["adr_compliance"],
    )
    # Exactly the three input files appear as table rows.
    assert "| `a.py` |" in body and "| `b/c.py` |" in body and "| `d.txt` |" in body
    # The invented file is never a table row (a hallucinated path cannot leak
    # into the structured table, even if prose mentions files).
    assert "| `evil.py` |" not in body
    # Count header is len(changed_files) == 3, not the model's "99".
    assert "Files changed (3)" in body
    # Statuses come from file_change_types, not the model.
    assert "| `a.py` | added |" in body
    assert "| `d.txt` | removed |" in body
    # The in-range note for a real file is kept; the hallucinated note is dropped.
    assert "adds handler" in body
    assert "injected" not in body


def test_empty_changed_files_renders_cleanly(monkeypatch):
    _patch_invoke(monkeypatch, _Resp("Nothing much."))
    body, _usage = build_pr_summary(
        metadata=_Meta([], {}), diffs_by_file={}, checks_to_run=[],
    )
    assert "Files changed (0)" in body
    assert "No files changed" in body
    assert "Checks queued:** none" in body


def test_truncation_note_present_when_truncated(monkeypatch):
    meta = _Meta(["a.py", "big.py"], {"a.py": "modified", "big.py": "modified"})
    _patch_invoke(monkeypatch, _Resp("Summary."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"], truncated_files=["big.py"],
    )
    assert "truncated or omitted" in body
    # The truncated file still appears as a row (never silently dropped).
    assert "`big.py`" in body
    assert "diff omitted" in body


def test_oversized_diff_is_capped_and_signalled(monkeypatch):
    # Six files each with a patch over the per-file byte cap; the running total
    # also trips the global cap after a few, so later files are omitted
    # entirely. Either way every file stays in the table and the truncation
    # note fires (ADR-032 -- never a silently-partial walkthrough).
    files = [f"f{i}.py" for i in range(6)]
    meta = _Meta(files, {f: "modified" for f in files})
    big = "+" + ("x" * 8000)
    diffs = {f: f"diff --git a/{f} b/{f}\n{big}" for f in files}
    calls = _patch_invoke(monkeypatch, _Resp("Summary."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file=diffs, checks_to_run=["adr_compliance"],
    )
    assert "truncated or omitted" in body
    for f in files:  # all six remain as rows
        assert f"| `{f}` |" in body
    # The model saw a truncation marker on the per-file-capped patch.
    user_text = calls["messages"][0]["content"][0]["text"]
    assert "[diff truncated]" in user_text


def test_no_truncation_note_when_complete(monkeypatch):
    meta = _Meta(["a.py"], {"a.py": "modified"})
    _patch_invoke(monkeypatch, _Resp("Summary."))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"],
    )
    assert "truncated or omitted" not in body


def test_pipe_in_path_and_note_is_escaped(monkeypatch):
    meta = _Meta(["weird|name.py"], {"weird|name.py": "added"})
    _patch_invoke(monkeypatch, _Resp(
        "s\nFILE NOTES:\n- weird|name.py: has | pipe"
    ))
    body, _usage = build_pr_summary(
        metadata=meta, diffs_by_file={}, checks_to_run=["adr_compliance"],
    )
    # Pipes inside cells are escaped so they can't break the table.
    assert "weird\\|name.py" in body
    assert "has \\| pipe" in body


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
# ADR-035 / ADR-016: shared client, workload/tier, usage captured.
# --------------------------------------------------------------------------- #

def test_invokes_with_pr_analyzer_basic_and_captures_usage(monkeypatch):
    meta = _Meta(["a.py"], {"a.py": "modified"})
    calls = _patch_invoke(monkeypatch, _Resp("Summary.", input_tokens=7, output_tokens=5))
    _body, usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance"],
    )
    assert calls["workload"] == "pr-analyzer"
    assert calls["tier"] == "basic"
    assert usage is not None
    assert usage.input_tokens == 7 and usage.output_tokens == 5


# --------------------------------------------------------------------------- #
# ADR-004: a model failure degrades to the deterministic summary (non-fatal).
# --------------------------------------------------------------------------- #

def test_model_failure_degrades_but_table_survives(monkeypatch):
    meta = _Meta(["a.py", "b.py"], {"a.py": "added", "b.py": "modified"})
    _patch_invoke(monkeypatch, RuntimeError("bedrock down"))
    body, usage = build_pr_summary(
        metadata=meta, diffs_by_file={"a.py": "diff\n+1"},
        checks_to_run=["adr_compliance", "test_coverage"],
    )
    # Deterministic parts are present even though the model raised.
    assert usage is None
    assert "Files changed (2)" in body
    assert "`a.py`" in body and "`b.py`" in body
    assert "adr_compliance, test_coverage" in body
    assert "Summary unavailable" in body
