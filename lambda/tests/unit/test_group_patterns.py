"""Unit tests for shared.group_patterns (Azure AD naming-convention matching)."""

from __future__ import annotations

import os
import sys

_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.group_patterns import (  # noqa: E402
    filter_groups,
    filter_names,
    graph_prefix_filters,
    looks_like_object_id,
    matches,
)

GUID = "d3f5d7a8-92f6-44c3-b220-7f29445e4677"
PATTERNS = ["outcomeops-*", "*-outcomeops-*"]


class TestLooksLikeObjectId:
    def test_guid_is_object_id(self):
        assert looks_like_object_id(GUID)
        assert looks_like_object_id(GUID.upper())

    def test_name_is_not_object_id(self):
        assert not looks_like_object_id("outcomeops-engineering")
        assert not looks_like_object_id("")
        assert not looks_like_object_id("not-a-guid")


class TestMatches:
    def test_prefix_match_case_insensitive(self):
        assert matches("OutcomeOps-Engineering", PATTERNS)
        assert matches("outcomeops-sales", PATTERNS)

    def test_region_division_first(self):
        assert matches("emea-outcomeops-eng", PATTERNS)
        assert matches("FIN-OutcomeOps-Users", PATTERNS)

    def test_non_matching_name_rejected(self):
        assert not matches("aws-engineering", PATTERNS)
        assert not matches("randomgroup", PATTERNS)

    def test_empty_patterns_allow_all(self):
        # v1.0 back-compat: no configured patterns -> no restriction.
        assert matches("anything", [])
        assert matches("aws-foo", [])

    def test_empty_name_never_matches_with_patterns(self):
        assert not matches("", PATTERNS)


class TestFilterNames:
    def test_keeps_only_matching_names(self):
        claim = ["outcomeops-eng", "aws-eng", "emea-outcomeops-data", "random"]
        assert filter_names(claim, PATTERNS) == ["outcomeops-eng", "emea-outcomeops-data"]

    def test_guid_dropped_under_name_patterns(self):
        # a GUID can't match 'outcomeops-*'; it's resolved via Graph, not here.
        assert filter_names([GUID, "outcomeops-eng"], PATTERNS) == ["outcomeops-eng"]

    def test_guid_kept_when_no_restriction(self):
        assert filter_names([GUID], []) == [GUID]

    def test_guid_kept_with_star_allow_all(self):
        assert filter_names([GUID], ["*"]) == [GUID]

    def test_skips_falsy(self):
        assert filter_names(["", None, "outcomeops-x"], PATTERNS) == ["outcomeops-x"]


class TestFilterGroups:
    def test_filters_dicts_by_display_name(self):
        groups = [
            {"id": "1", "display_name": "outcomeops-eng"},
            {"id": "2", "display_name": "aws-eng"},
            {"id": "3", "display_name": "sec-outcomeops-ops"},
        ]
        kept = filter_groups(groups, PATTERNS)
        assert [g["id"] for g in kept] == ["1", "3"]

    def test_custom_name_key(self):
        groups = [{"id": "1", "displayName": "outcomeops-eng"}]
        assert filter_groups(groups, PATTERNS, name_key="displayName") == groups


class TestGraphPrefixFilters:
    def test_clean_prefix_becomes_startswith_fragment(self):
        assert graph_prefix_filters(["outcomeops-*"]) == ["outcomeops-"]

    def test_embedded_star_is_not_a_prefix(self):
        # '*-outcomeops-*' can't be a startswith -> None (fetch + local filter)
        assert graph_prefix_filters(["*-outcomeops-*"]) is None

    def test_mixed_returns_only_clean_prefixes(self):
        assert graph_prefix_filters(PATTERNS) == ["outcomeops-"]

    def test_empty_patterns_returns_empty_list(self):
        assert graph_prefix_filters([]) == []
