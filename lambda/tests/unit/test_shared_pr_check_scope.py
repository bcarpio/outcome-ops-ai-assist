# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PR-check workspace-scope resolver."""

import logging
from unittest.mock import MagicMock

import pytest

from shared import pr_check_scope


_REPO_SK_PREFIXES = ("GITHUB_REPO#", "GITLAB_REPO#", "AZDO_REPO#")


def _table_stub(github_repo_items=None, all_metadata_items=None):
    """Build a MagicMock table whose paginated scans return the given
    items (single page; no LastEvaluatedKey). Repo-row scans for any
    provider prefix return ``github_repo_items``; tests that care about the
    prefix assert on the recorded scan kwargs."""
    table = MagicMock()

    def scan(**kwargs):
        values = kwargs.get("ExpressionAttributeValues", {}) or {}
        sk = values.get(":sk")

        if sk and str(sk).startswith(_REPO_SK_PREFIXES):
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

    def test_architectural_duplication_uses_read_scope_not_every_workspace(self):
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
        # Read scope: a workspace that never shared with ws-eng is not read.
        assert result == ["ws-eng", "ws-sec"]
        assert "ws-payments" not in result

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
        # ADR_COMPLIANCE uses the read scope: ws-b shared with ws-eng, so its
        # standards apply; ws-consumer (forward grant) is not readable.
        assert set(adr) == {"ws-eng", "ws-global", "ws-b"}
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


# Read scope for retrieval checks: own + is_global + workspaces that shared
# with the repo's workspace (search_allowed_workspaces contains it).
def _sharing_table():
    return _table_stub(
        github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
        all_metadata_items=[
            # Repo workspace granted ws-forward (forward grant, not readable).
            {"workspace_id": "ws-eng", "is_global": False,
             "search_allowed_workspaces": ["ws-forward"]},
            {"workspace_id": "ws-global", "is_global": True},
            # Shared with the repo workspace: readable.
            {"workspace_id": "ws-confluence", "is_global": False,
             "search_allowed_workspaces": ["ws-other", "ws-eng"]},
            # Shared with some OTHER workspace only: not readable.
            {"workspace_id": "ws-elsewhere", "is_global": False,
             "search_allowed_workspaces": ["ws-other"]},
            {"workspace_id": "ws-forward", "is_global": False},
            {"workspace_id": "ws-other", "is_global": False},
        ],
    )


class TestReadScopeChecks:

    def test_read_scope_check_set(self):
        assert pr_check_scope.READ_SCOPE_CHECKS == {
            "ADR_COMPLIANCE",
            "TEST_COVERAGE",
            "LICENSE_COMPLIANCE",
            "ARCHITECTURAL_DUPLICATION",
        }
        assert pr_check_scope.LOCAL_ONLY_CHECKS == {"README_FRESHNESS"}
        assert not hasattr(pr_check_scope, "CROSS_PORTFOLIO_CHECKS")

    @pytest.mark.parametrize("check_type", [
        "ADR_COMPLIANCE",
        "TEST_COVERAGE",
        "LICENSE_COMPLIANCE",
        "ARCHITECTURAL_DUPLICATION",
    ])
    def test_read_scope_includes_own_globals_and_granting(self, check_type):
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type=check_type, repo_name="acme/app", table=_sharing_table(),
        )
        assert result == ["ws-confluence", "ws-eng", "ws-global"]
        # Granted some OTHER workspace: not readable.
        assert "ws-elsewhere" not in result
        # Forward grant from the repo workspace: not readable.
        assert "ws-forward" not in result
        assert "ws-other" not in result

    @pytest.mark.parametrize("check_type", ["README_FRESHNESS", "SOMETHING_NEW"])
    def test_local_only_and_unknown_ignore_grants_and_globals(self, check_type):
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type=check_type, repo_name="acme/app", table=_sharing_table(),
        )
        assert result == ["ws-eng"]

    def test_no_grants_no_globals_is_repo_workspace_only(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
            all_metadata_items=[
                {"workspace_id": "ws-eng", "is_global": False},
                {"workspace_id": "ws-other", "is_global": False},
            ],
        )
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="TEST_COVERAGE", repo_name="acme/app", table=table,
        )
        assert result == ["ws-eng"]

    def test_repo_in_no_workspace_gets_globals_only(self):
        # No repo workspace means nobody's grant can name it; globals remain.
        table = _table_stub(
            github_repo_items=[],
            all_metadata_items=[
                {"workspace_id": "ws-global", "is_global": True},
                {"workspace_id": "ws-b", "search_allowed_workspaces": ["ws-eng"]},
            ],
        )
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="ARCHITECTURAL_DUPLICATION",
            repo_name="acme/orphan",
            table=table,
        )
        assert result == ["ws-global"]

    def test_scope_is_logged_once(self, caplog):
        with caplog.at_level(logging.INFO, logger=pr_check_scope.logger.name):
            result = pr_check_scope.resolve_pr_check_workspace_scope(
                check_type="ADR_COMPLIANCE",
                repo_name="acme/app",
                table=_sharing_table(),
            )
        lines = [
            r.getMessage() for r in caplog.records
            if r.getMessage().startswith("[pr_check_scope] ADR_COMPLIANCE scope")
        ]
        assert lines == [
            f"[pr_check_scope] ADR_COMPLIANCE scope for github:acme/app: {result}"
        ]

    def test_breaking_changes_scope_is_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger=pr_check_scope.logger.name):
            pr_check_scope.resolve_pr_check_workspace_scope(
                check_type="BREAKING_CHANGES",
                repo_name="group/sub/app",
                table=_sharing_table(),
                provider="gitlab",
            )
        assert any(
            r.getMessage().startswith(
                "[pr_check_scope] BREAKING_CHANGES scope for gitlab:group/sub/app:"
            )
            for r in caplog.records
        )


def _repo_scan_sks(table):
    """The :sk values of every repo-row scan the table received."""
    sks = []
    for call in table.scan.call_args_list:
        sk = (call.kwargs.get("ExpressionAttributeValues") or {}).get(":sk")
        if sk and sk != "METADATA":
            sks.append(sk)
    return sks


class TestProviderAwareRepoLookup:

    def test_default_provider_is_github(self):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
        )
        assert pr_check_scope.find_workspaces_for_repo("acme/app", table) == ["ws-eng"]
        assert _repo_scan_sks(table) == ["GITHUB_REPO#acme/app"]

    @pytest.mark.parametrize("provider, slug, expected_sk", [
        ("gitlab", "group/sub/app", "GITLAB_REPO#group/sub/app"),
        ("azure_devops", "org/project/app", "AZDO_REPO#org/project/app"),
    ])
    def test_provider_prefix_and_full_slug(self, provider, slug, expected_sk):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
        )
        result = pr_check_scope.find_workspaces_for_repo(
            slug, table, provider=provider,
        )
        assert result == ["ws-eng"]
        assert _repo_scan_sks(table) == [expected_sk]

    @pytest.mark.parametrize("provider, slug, expected_sk", [
        ("gitlab", "group/sub/app", "GITLAB_REPO#group/sub/app"),
        ("azure_devops", "org/project/app", "AZDO_REPO#org/project/app"),
    ])
    def test_read_scope_threads_provider(self, provider, slug, expected_sk):
        table = _sharing_table()
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="ADR_COMPLIANCE", repo_name=slug, table=table,
            provider=provider,
        )
        assert result == ["ws-confluence", "ws-eng", "ws-global"]
        assert _repo_scan_sks(table) == [expected_sk]

    def test_consumer_scope_threads_provider(self):
        table = _sharing_table()
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="BREAKING_CHANGES", repo_name="org/project/app",
            table=table, provider="azure_devops",
        )
        assert set(result) == {"ws-eng", "ws-global", "ws-forward"}
        assert _repo_scan_sks(table) == ["AZDO_REPO#org/project/app"]

    def test_local_scope_threads_provider(self):
        table = _sharing_table()
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="README_FRESHNESS", repo_name="group/app",
            table=table, provider="gitlab",
        )
        assert result == ["ws-eng"]
        assert _repo_scan_sks(table) == ["GITLAB_REPO#group/app"]

    def test_unknown_provider_returns_empty_without_github_scan(self, caplog):
        table = _table_stub(
            github_repo_items=[{"PK": "WS#ws-eng", "workspace_id": "ws-eng"}],
        )
        with caplog.at_level(logging.WARNING, logger=pr_check_scope.logger.name):
            result = pr_check_scope.find_workspaces_for_repo(
                "acme/app", table, provider="bitbucket",
            )
        assert result == []
        table.scan.assert_not_called()
        assert any("unknown provider" in r.getMessage() for r in caplog.records)

    def test_unknown_provider_read_scope_is_globals_only(self):
        table = _sharing_table()
        result = pr_check_scope.resolve_pr_check_workspace_scope(
            check_type="ADR_COMPLIANCE", repo_name="acme/app", table=table,
            provider="bitbucket",
        )
        assert result == ["ws-global"]
        assert not any(
            sk.startswith("GITHUB_REPO#") for sk in _repo_scan_sks(table)
        )
