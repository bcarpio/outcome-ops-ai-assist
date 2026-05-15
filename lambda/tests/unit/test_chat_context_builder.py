"""Unit tests for ``chat.context_builder``.

Covers:

- The three block shapes (``build_symbol_facts_block``,
  ``build_symbol_definition_block``,
  ``build_graph_unavailable_block``).
- Empty result rendering -- the audit unlock for "empty graph results".
- Truncation disclosure.
- Staleness rendering across the rule-of-thumb buckets (<60s, <10min,
  <24h, >24h).
- Single-vs-multi-repo SHA suffix semantics.
- Deterministic ordering of rendered sites.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
_CHAT_DIR = _LAMBDA_ROOT / "chat"
for _p in (str(_LAMBDA_ROOT), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from context_builder import (  # noqa: E402  -- path bootstrap above
    GRAPH_GROUNDING_INSTRUCTION,
    SYMBOL_FACTS_HEADER,
    build_graph_unavailable_block,
    build_symbol_definition_block,
    build_symbol_facts_block,
)
from shared.symbol_graph_reader import CallSite, QueryResult  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _site(file_path: str, line: int, repo: str = "owner/repo") -> CallSite:
    return CallSite(
        file_path=file_path,
        line=line,
        repo=repo,
        source_symbol_id=f"python:caller_at::{file_path}",
        language="python",
    )


# ===========================================================================
# build_symbol_facts_block
# ===========================================================================


def test_facts_block_renders_callers_with_definition():
    sites = [
        _site("lambda/github-integration/handler.py", 137, "owner/repo"),
        _site("lambda/jira-integration/handler.py", 114, "owner/repo"),
    ]
    result = QueryResult(
        sites=sites,
        truncated=False,
        graph_sha="a1b2c3def012345",
        staleness_seconds=47,
    )
    definition = _site("lambda/shared/outcomeops_license.py", 42)

    block = build_symbol_facts_block(
        relation="callers",
        target_symbol_id="python:lambda.shared.outcomeops_license::report_repos_snapshot",
        result=result,
        definition_site=definition,
    )

    assert SYMBOL_FACTS_HEADER in block
    assert (
        "Question target: python:lambda.shared.outcomeops_license::report_repos_snapshot"
        in block
    )
    assert "Defined at: lambda/shared/outcomeops_license.py:42" in block
    assert "Direct callers (2 sites" in block
    assert "lambda/github-integration/handler.py:137" in block
    assert "lambda/jira-integration/handler.py:114" in block
    assert "Graph as-of SHA: a1b2c3d" in block
    assert "47 seconds ago" in block


def test_facts_block_renders_zero_result_explicitly():
    """'Empty graph results' -- never omit the empty block. The audit
    unlock depends on the model surfacing 'zero callers' honestly.
    """
    result = QueryResult(
        sites=[],
        truncated=False,
        graph_sha=None,
        staleness_seconds=None,
    )
    block = build_symbol_facts_block(
        relation="callers",
        target_symbol_id="python:does_not::exist",
        result=result,
    )
    assert "Direct callers in this workspace: 0" in block
    # Header still present so the block is parseable by the model.
    assert SYMBOL_FACTS_HEADER in block


def test_facts_block_discloses_truncation():
    sites = [_site(f"file_{i}.py", i + 1) for i in range(3)]
    result = QueryResult(
        sites=sites,
        truncated=True,
        graph_sha="sha1",
        staleness_seconds=10,
    )
    block = build_symbol_facts_block(
        relation="callers",
        target_symbol_id="X",
        result=result,
    )
    assert "(truncated)" in block


def test_facts_block_importers_label_and_singular_count():
    site = _site("apps/app1/index.ts", 12, "owner/web")
    result = QueryResult(
        sites=[site],
        truncated=False,
        graph_sha="sha1",
        staleness_seconds=5,
    )
    block = build_symbol_facts_block(
        relation="importers",
        target_symbol_id="@hyatt-hpe/hpe-utils/http2",
        result=result,
    )
    assert "Importers (1 site in this workspace" in block
    assert "apps/app1/index.ts:12" in block


def test_facts_block_implementers_relation_label():
    site = _site("src/booking_client_impl.ts", 22, "owner/web")
    result = QueryResult(
        sites=[site],
        truncated=False,
        graph_sha="sha1",
        staleness_seconds=5,
    )
    block = build_symbol_facts_block(
        relation="implementers",
        target_symbol_id="IBookingClient",
        result=result,
    )
    assert "Implementers" in block


def test_facts_block_sorts_sites_deterministically():
    """Stable order across runs: (repo, file_path, line)."""
    sites = [
        _site("zfile.py", 1, "owner/b"),
        _site("afile.py", 2, "owner/b"),
        _site("zfile.py", 1, "owner/a"),
    ]
    result = QueryResult(
        sites=sites,
        truncated=False,
        graph_sha=None,
        staleness_seconds=None,
    )
    block = build_symbol_facts_block(
        relation="callers",
        target_symbol_id="X",
        result=result,
    )
    a_zfile_pos = block.find("zfile.py:1  (owner/a)")
    b_afile_pos = block.find("afile.py:2  (owner/b)")
    b_zfile_pos = block.find("zfile.py:1  (owner/b)")
    assert a_zfile_pos < b_afile_pos < b_zfile_pos


# ===========================================================================
# Staleness rendering buckets (symbol-graph reader rule of thumb)
# ===========================================================================


@pytest.mark.parametrize(
    "seconds,expected_phrase",
    [
        (0, "0 seconds ago"),
        (45, "45 seconds ago"),
        (90, "1 minutes ago"),
        (599, "9 minutes ago"),
        (3600, "1 hours ago"),
        (23 * 3600, "23 hours ago"),
        (25 * 3600, "1 days ago (graph may be stale)"),
    ],
)
def test_staleness_phrase_buckets(seconds, expected_phrase):
    result = QueryResult(
        sites=[],
        truncated=False,
        graph_sha="abc1234",
        staleness_seconds=seconds,
    )
    block = build_symbol_facts_block(
        relation="callers",
        target_symbol_id="X",
        result=result,
    )
    assert expected_phrase in block


def test_staleness_missing_graph_sha_shows_freshness_only():
    """Multi-repo answers carry no single SHA -- only freshness."""
    result = QueryResult(
        sites=[],
        truncated=False,
        graph_sha=None,
        staleness_seconds=120,
    )
    block = build_symbol_facts_block(
        relation="callers",
        target_symbol_id="X",
        result=result,
    )
    assert "Graph freshness: oldest contributing repo committed 2 minutes ago" in block
    assert "Graph as-of SHA" not in block


def test_no_staleness_suffix_when_anchors_missing():
    """No SHA and no staleness -> no suffix at all."""
    result = QueryResult(
        sites=[],
        truncated=False,
        graph_sha=None,
        staleness_seconds=None,
    )
    block = build_symbol_facts_block(
        relation="callers",
        target_symbol_id="X",
        result=result,
    )
    assert "Graph as-of SHA" not in block
    assert "Graph freshness" not in block


# ===========================================================================
# build_symbol_definition_block
# ===========================================================================


def test_definition_block_zero_matches_surfaces_not_found():
    block = build_symbol_definition_block(
        symbol_candidate="report_repos_snapshot",
        definitions=[],
    )
    assert "No symbol matching `report_repos_snapshot` found" in block


def test_definition_block_single_match_renders_definition_site():
    d = _site("lambda/shared/outcomeops_license.py", 42)
    block = build_symbol_definition_block(
        symbol_candidate="report_repos_snapshot",
        definitions=[d],
    )
    assert "Defined at: lambda/shared/outcomeops_license.py:42" in block


def test_definition_block_multiple_matches_lists_all():
    defs = [
        _site("repo_a/types.ts", 10, "owner/repo-a"),
        _site("repo_b/types.ts", 15, "owner/repo-b"),
    ]
    block = build_symbol_definition_block(
        symbol_candidate="IBookingClient",
        definitions=defs,
    )
    assert "Multiple definitions matched in this workspace (2)" in block
    assert "repo_a/types.ts:10" in block
    assert "repo_b/types.ts:15" in block


# ===========================================================================
# build_graph_unavailable_block
# ===========================================================================


def test_unavailable_block_mentions_degraded_state():
    block = build_graph_unavailable_block(target_symbol_id="X")
    assert SYMBOL_FACTS_HEADER in block
    assert "unavailable" in block.lower()
    assert "Question target: X" in block


def test_unavailable_block_without_target_still_renders():
    block = build_graph_unavailable_block()
    assert SYMBOL_FACTS_HEADER in block
    assert "unavailable" in block.lower()


# ===========================================================================
# Grounding instruction
# ===========================================================================


def test_grounding_instruction_includes_empty_set_clause():
    """The instruction MUST tell the model not to fabricate when the
    block lists zero callers -- this is the regulated-buyer audit
    unlock per the plan."""
    assert "do not fabricate" in GRAPH_GROUNDING_INSTRUCTION.lower()
    assert "Symbol facts" in GRAPH_GROUNDING_INSTRUCTION
