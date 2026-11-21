# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the analyze-pr summary-comment wiring helpers.

Covers the logic-bearing helpers added for the PR summary feature:
- ``_render_summary_footer`` — token footer, empty on model failure.
- ``_fetch_diffs_via_sha`` — ADO/GitLab bounded diff fetch: the per-file cap,
  the added/removed base/head skip, and non-fatal fetch errors all surface as
  ``truncated_files`` (ADR-032) rather than dropping a file silently.

The full per-provider call sites (metadata build + upsert with
COMMENT_ROLE_SUMMARY) are exercised in the live parity step.

Mocking (ADR-005): patch at the module attribute the helper looks up
(``shared.source_fetch.fetch_file_at_sha`` / ``shared.pr_diff.compute_file_diff``),
never the boto3 factory.
"""

import importlib.util
import os
import sys

import pytest

from shared.source_fetch import SourceFetchError

# Load the analyze-pr handler under a DISTINCT module name so this file does
# not clobber sys.modules["analyze_pr_handler"], which test_analyze_pr.py loads
# and relies on for its @patch("analyze_pr_handler....") targets.
_HANDLER_PATH = os.path.join(os.path.dirname(__file__), "../../analyze-pr/handler.py")
_spec = importlib.util.spec_from_file_location("analyze_pr_handler_wiring", _HANDLER_PATH)
_handler_module = importlib.util.module_from_spec(_spec)
sys.modules["analyze_pr_handler_wiring"] = _handler_module
_spec.loader.exec_module(_handler_module)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# _render_summary_footer
# --------------------------------------------------------------------------- #

class _Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


def test_footer_empty_when_usage_none():
    assert _handler_module._render_summary_footer(None, "advanced") == ""


def test_footer_reports_tokens_and_tier():
    # The tier is passed in (from pr_summary.SUMMARY_MODEL_TIER), never hardcoded,
    # so a tfvar model swap stays accurately labelled.
    footer = _handler_module._render_summary_footer(_Usage(13, 27), "advanced")
    assert "Model: Advanced Model" in footer
    assert "Tokens: 13 input + 27 output = 40 total" in footer
    assert "<details>" in footer


def test_footer_reports_cost_from_tier_pricing(monkeypatch):
    # Same tfvars-driven pricing the PR checks use; 1M in + 1M out at
    # $3 / $15 is $18.
    monkeypatch.setitem(_handler_module._TIER_DISPLAY, "advanced",
                        ("Advanced Model", {"input": 3.0, "output": 15.0}))
    footer = _handler_module._render_summary_footer(
        _Usage(1_000_000, 1_000_000), "advanced")
    assert "Cost: $18.0000" in footer
    assert "Tokens: 1,000,000 input + 1,000,000 output = 2,000,000 total" in footer


def test_footer_basic_tier_named_and_priced():
    footer = _handler_module._render_summary_footer(_Usage(10, 10), "basic")
    assert "Model: Basic Model" in footer
    assert "Cost: $" in footer


def test_footer_unknown_tier_has_no_cost_line():
    footer = _handler_module._render_summary_footer(_Usage(10, 10), "mystery")
    assert "Model: mystery" in footer
    assert "Cost:" not in footer


# --------------------------------------------------------------------------- #
# _fetch_diffs_via_sha
# --------------------------------------------------------------------------- #

def test_fetch_diffs_caps_at_max_files(monkeypatch):
    calls = {"fetch": 0}

    def _fake_fetch(provider, path, sha, **kw):
        calls["fetch"] += 1
        return b"content"

    monkeypatch.setattr("shared.source_fetch.fetch_file_at_sha", _fake_fetch)
    monkeypatch.setattr("shared.pr_diff.compute_file_diff",
                        lambda b, h, p: f"diff:{p}")

    diffs, truncated = _handler_module._fetch_diffs_via_sha(
        provider="gitlab",
        changed_files=["a.py", "b.py", "c.py"],
        change_types={"a.py": "modified", "b.py": "modified", "c.py": "modified"},
        base_sha="base", head_sha="head", max_files=2,
    )
    assert set(diffs) == {"a.py", "b.py"}
    assert truncated == ["c.py"]  # beyond the cap -> reported, not dropped


def test_fetch_diffs_added_file_skips_base(monkeypatch):
    fetched_shas = []

    def _fake_fetch(provider, path, sha, **kw):
        fetched_shas.append(sha)
        return b"content"

    monkeypatch.setattr("shared.source_fetch.fetch_file_at_sha", _fake_fetch)
    monkeypatch.setattr("shared.pr_diff.compute_file_diff",
                        lambda b, h, p: "diff")

    _handler_module._fetch_diffs_via_sha(
        provider="azure_devops",
        changed_files=["new.py"],
        change_types={"new.py": "added"},
        base_sha="base", head_sha="head", max_files=10,
    )
    # An added file has no base side -> only the head sha is fetched.
    assert fetched_shas == ["head"]


def test_fetch_diffs_removed_file_skips_head(monkeypatch):
    fetched_shas = []
    monkeypatch.setattr(
        "shared.source_fetch.fetch_file_at_sha",
        lambda provider, path, sha, **kw: fetched_shas.append(sha) or b"x",
    )
    monkeypatch.setattr("shared.pr_diff.compute_file_diff", lambda b, h, p: "diff")

    _handler_module._fetch_diffs_via_sha(
        provider="azure_devops",
        changed_files=["gone.py"],
        change_types={"gone.py": "removed"},
        base_sha="base", head_sha="head", max_files=10,
    )
    assert fetched_shas == ["base"]


def test_fetch_diffs_fetch_error_truncates_not_raises(monkeypatch):
    def _boom(provider, path, sha, **kw):
        raise SourceFetchError("upstream 500")

    monkeypatch.setattr("shared.source_fetch.fetch_file_at_sha", _boom)
    monkeypatch.setattr("shared.pr_diff.compute_file_diff", lambda b, h, p: "diff")

    diffs, truncated = _handler_module._fetch_diffs_via_sha(
        provider="gitlab",
        changed_files=["a.py"],
        change_types={"a.py": "modified"},
        base_sha="base", head_sha="head", max_files=10,
    )
    assert diffs == {}
    assert truncated == ["a.py"]  # fetch failure surfaced, not swallowed silently


def test_fetch_diffs_empty_diff_not_included(monkeypatch):
    monkeypatch.setattr(
        "shared.source_fetch.fetch_file_at_sha",
        lambda provider, path, sha, **kw: b"same",
    )
    # compute_file_diff returns "" for an unchanged file.
    monkeypatch.setattr("shared.pr_diff.compute_file_diff", lambda b, h, p: "")

    diffs, truncated = _handler_module._fetch_diffs_via_sha(
        provider="gitlab",
        changed_files=["a.py"],
        change_types={"a.py": "modified"},
        base_sha="base", head_sha="head", max_files=10,
    )
    assert diffs == {}
    assert truncated == []


def test_summary_role_constant():
    assert _handler_module.COMMENT_ROLE_SUMMARY == "summary"


# --------------------------------------------------------------------------- #
# _store_deferred_ado_summary (ADR-057: ADO defers the summary to the fan-in)
# --------------------------------------------------------------------------- #

def test_store_deferred_ado_summary_writes_body_and_expected_set(monkeypatch):
    captured = {}

    class _Table:
        def put_item(self, Item=None):
            captured["item"] = Item

    class _DDB:
        def Table(self, _name):
            return _Table()

    monkeypatch.setattr(_handler_module, "dynamodb", _DDB())
    _handler_module._store_deferred_ado_summary(
        pk="PR#azure_devops#bcarpio/OutcomeOps/repo#4",
        body="## PR Summary\n\nprose\n\n**Checks queued:** ADR_COMPLIANCE",
        expected_checks=["ADR_COMPLIANCE", "LICENSE_COMPLIANCE"],
    )
    item = captured["item"]
    assert item["PK"] == "PR#azure_devops#bcarpio/OutcomeOps/repo#4"
    assert item["SK"] == _handler_module.DEFERRED_SUMMARY_SK
    assert item["summaryBody"].startswith("## PR Summary")
    # Stored as a DynamoDB String Set the fan-in matches completion against.
    assert item["expectedChecks"] == {"ADR_COMPLIANCE", "LICENSE_COMPLIANCE"}
