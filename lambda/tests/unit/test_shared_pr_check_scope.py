"""Unit tests for the PR-check workspace-scope resolver (chunk 04)."""

from unittest.mock import MagicMock

import pytest

from shared import pr_check_scope


def _table_stub(github_repo_items=None, all_metadata_items=None):
    """Build a MagicMock table whose paginated scans return the given
    items (single page; no LastEvaluatedKey)."""
    table = MagicMock()

    def scan(**kwargs):
        expr = kwargs.get("FilterExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {}) or {}
        sk = values.get(":sk")

        if sk and str(sk).startswith("GITHUB_REPO#"):
            return {"Items": github_repo_items or []}
        if sk == "METADATA":
            # Two variants: is_global=true and all metadata.
            if ":ig" in values:
                return {
                    "Items": [
                        i for i in (all_metadata_items or [])
                        if i.get("is_global") is True
                    ],
                }
            return {"Items": all_metadata_items or []}
        return {"Items": []}

    table.scan.side_effect = scan
    return table


class TestResolvePrCheckWorkspaceScope:

    def test_standards_check_returns_repo_workspaces_plus_globals(self):
        table = _table_stub(
            github_repo_items=[
                {"PK": "WS#ws-eng", "workspace_id": "ws-eng"},
            ],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False},
                {"workspace_id": "ws-sec", "is_global": True},
                {"workspace_id": "ws-ea", "is_global": True},
                {"workspace_id": "ws-other", "is_global": False},
            ],
        )
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="ADR_COMPLIANCE",
            repo_name="acme/app",
            table=table,
        )
        # Repo workspace + globals; not the other unrelated workspace.
        assert set(result) == {"ws-eng", "ws-sec", "ws-ea"}
        assert "ws-other" not in result

    def test_readme_freshness_stays_local(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False},
                {"workspace_id": "ws-sec", "is_global": True},
            ],
        )
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="README_FRESHNESS",
            repo_name="acme/app",
            table=table,
        )
        # Just the repo's workspace -- README about this repo, not org
        # standards.
        assert result == ["ws-eng"]

    def test_architectural_duplication_walks_every_workspace(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False},
                {"workspace_id": "ws-payments", "is_global": False},
                {"workspace_id": "ws-sec", "is_global": True},
            ],
        )
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="ARCHITECTURAL_DUPLICATION",
            repo_name="acme/app",
            table=table,
        )
        # Cross-portfolio: every workspace.
        assert set(result) == {"ws-eng", "ws-payments", "ws-sec"}

    def test_repo_in_multiple_workspaces_returns_all_of_them(self):
        table = _table_stub(
            github_repo_items=[
                {"PK": "WS#ws-eng", "workspace_id": "ws-eng"},
                {"PK": "WS#ws-shared", "workspace_id": "ws-shared"},
            ],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False},
                {"workspace_id": "ws-shared", "is_global": False},
                {"workspace_id": "ws-sec", "is_global": True},
            ],
        )
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="LICENSE_COMPLIANCE",
            repo_name="acme/app",
            table=table,
        )
        # Both owning workspaces + the global.
        assert set(result) == {"ws-eng", "ws-shared", "ws-sec"}

    def test_unknown_check_type_falls_back_to_local(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-sec", "is_global": True},
            ],
        )
        # Unknown check_type: safe default is LOCAL_ONLY -- broadening
        # on an unrecognized type would leak content across scopes.
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="SOMETHING_NEW",
            repo_name="acme/app",
            table=table,
        )
        assert result == ["ws-eng"]

    def test_empty_check_type_returns_empty(self):
        table = _table_stub()
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="",
            repo_name="acme/app",
            table=table,
        )
        assert result == []

    def test_repo_not_in_any_workspace_returns_globals_for_standards(self):
        # Rare (misconfigured deploy?) but reader shouldn't crash.
        table = _table_stub(
            github_repo_items=[],
            all_metadata_items=[
                {"workspace_id": "ws-sec", "is_global": True},
            ],
        )
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="ADR_COMPLIANCE",
            repo_name="acme/orphan",
            table=table,
        )
        # No repo workspaces + globals still resolvable.
        assert result == ["ws-sec"]


# ADR-056 §1.3 + §6: cross-workspace grant reverse-lookup and the resolution
# scope (own workspace(s) + is_global + granting workspaces).
class TestCrossWorkspaceScope:

    def test_grant_reverse_lookup_returns_granting_workspace(self):
        # ws-b grants ws-eng; ws-c grants someone unrelated; ws-eng (self) and
        # a non-granting workspace are excluded.
        table = _table_stub(all_metadata_items=[
            {"workspace_id": "ws-b", "search_allowed_workspaces": ["ws-eng"]},
            {"workspace_id": "ws-c", "search_allowed_workspaces": ["ws-xyz"]},
            {"workspace_id": "ws-eng", "search_allowed_workspaces": ["ws-eng"]},
            {"workspace_id": "ws-none"},
        ])
        result = pr_check_scope.list_workspaces_granting_access(["ws-eng"], table)
        assert result == ["ws-b"]

    def test_grant_reverse_lookup_empty_input(self):
        table = _table_stub(all_metadata_items=[
            {"workspace_id": "ws-b", "search_allowed_workspaces": ["ws-eng"]},
        ])
        assert pr_check_scope.list_workspaces_granting_access([], table) == []

    def test_resolution_scope_unions_own_global_and_grants(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False},
                {"workspace_id": "ws-global", "is_global": True},
                {"workspace_id": "ws-b", "search_allowed_workspaces": ["ws-eng"]},
                {"workspace_id": "ws-unrelated", "is_global": False},
            ],
        )
        result = pr_check_scope.resolve_repo_definition_scope("acme/app", table)
        assert set(result) == {"ws-eng", "ws-global", "ws-b"}
        assert "ws-unrelated" not in result

    def test_breaking_changes_scope_is_forward_consumer_scope(self):
        # Producer repo in ws-eng, which GRANTED ws-consumer (its consumers may
        # live there). BREAKING_CHANGES searches ws-consumer (forward); a sibling
        # standards check does not. A workspace that granted ws-eng (reverse) is
        # NOT a consumer location and must not appear.
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False,
                 "search_allowed_workspaces": ["ws-consumer"]},
                {"workspace_id": "ws-global", "is_global": True},
                {"workspace_id": "ws-consumer", "is_global": False},
                {"workspace_id": "ws-b", "search_allowed_workspaces": ["ws-eng"]},
            ],
        )
        bc = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="BREAKING_CHANGES", repo_name="acme/app", table=table,
        )
        adr = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="ADR_COMPLIANCE", repo_name="acme/app", table=table,
        )
        assert set(bc) == {"ws-eng", "ws-global", "ws-consumer"}
        assert "ws-b" not in bc  # reverse granter is not a consumer location
        assert set(adr) == {"ws-eng", "ws-global"}
        assert "ws-consumer" not in adr

    def test_consumer_query_scope_forward_grant(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-eng",
                 "search_allowed_workspaces": ["ws-consumer"]},
                {"workspace_id": "ws-consumer"},
                {"workspace_id": "ws-b",
                 "search_allowed_workspaces": ["ws-eng"]},  # reverse, ignored
            ],
        )
        result = pr_check_scope.resolve_consumer_query_scope("acme/app", table)
        assert set(result) == {"ws-eng", "ws-consumer"}
        assert "ws-b" not in result

    def test_consumer_query_scope_global_producer_is_everywhere(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-glib", "workspace_id": "ws-glib"}],
            all_metadata_items=[
                {"workspace_id": "ws-glib", "is_global": True},
                {"workspace_id": "ws-a"},
                {"workspace_id": "ws-b"},
            ],
        )
        # A global producer is readable by all, so its consumers may be anywhere.
        result = pr_check_scope.resolve_consumer_query_scope("acme/glib", table)
        assert set(result) == {"ws-glib", "ws-a", "ws-b"}

    def test_breaking_changes_without_grants_matches_standards(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False},
                {"workspace_id": "ws-global", "is_global": True},
            ],
        )
        bc = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="BREAKING_CHANGES", repo_name="acme/app", table=table,
        )
        assert set(bc) == {"ws-eng", "ws-global"}
