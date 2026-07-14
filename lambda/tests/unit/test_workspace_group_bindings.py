"""Tests for the Azure AD group->workspace backend (chunk 11a).

Covers create/delete/list group bindings + the on-login sync_memberships
reconciliation in workspace-management/handler.py.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _load_handler_module():
    # Load under a UNIQUE module name so we never delete/clobber the
    # "workspace_management_handler" entry that test_workspace_management.py
    # captures -- reloading a shared module name mid-suite invalidates the
    # other file's captured functions + patches. We patch.object(H, ...) on
    # this private instance, so string-target patching is not needed.
    handler_path = os.path.join(
        os.path.dirname(__file__), "../../workspace-management/handler.py")
    name = "wm_handler_group_bindings"
    spec = importlib.util.spec_from_file_location(name, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load_handler_module()
create_group_binding = H.create_group_binding
delete_group_binding = H.delete_group_binding
list_group_bindings = H.list_group_bindings
bindable_groups = H.bindable_groups
sync_memberships = H.sync_memberships
GroupBindingRequest = H.GroupBindingRequest
SyncMembershipsRequest = H.SyncMembershipsRequest

_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)
from shared import graph_client  # noqa: E402

ADMIN = "admin@outcomeops.onmicrosoft.com"
USER = "alice@outcomeops.onmicrosoft.com"


def _binding(gid, ws, role="member"):
    return {"group_id": gid, "workspace_id": ws, "role": role}


def _member(ws, role="member", source=None):
    m = {"PK": f"USER#{USER}", "SK": f"WS_MEMBER#{ws}",
         "workspace_id": ws, "role": role, "user_email": USER}
    if source is not None:
        m["source"] = source
    return m


class TestGroupBindingCRUD:
    @patch.object(H, "log_admin_action")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_is_workspace_admin", return_value=True)
    @patch.object(H, "_get_workspaces_table",
           return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_create_binding_admin(self, ddb, _t, _adm, put, _audit):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        table.get_item.return_value = {"Item": {"PK": "WS#ws1"}}

        res = create_group_binding(
            ADMIN, "ws1", GroupBindingRequest(group_id="g1", role="workspace_admin"))

        assert res["statusCode"] == 201
        item = put.call_args.args[1]
        assert item["PK"] == "GROUP#g1"
        assert item["SK"] == "WORKSPACE#ws1"
        assert item["GSI1PK"] == "WS#ws1" and item["GSI1SK"] == "GROUP#g1"
        assert item["role"] == "workspace_admin"

    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_is_workspace_admin", return_value=False)
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_create_binding_non_admin_403(self, ddb, _t, _adm, put):
        ddb.return_value.Table.return_value = MagicMock()
        res = create_group_binding(USER, "ws1", GroupBindingRequest(group_id="g1"))
        assert res["statusCode"] == 403
        put.assert_not_called()

    @patch.object(H, "log_admin_action")
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H, "_is_workspace_admin", return_value=True)
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_delete_binding_admin(self, ddb, _t, _adm, dele, _audit):
        ddb.return_value.Table.return_value = MagicMock()
        res = delete_group_binding(ADMIN, "ws1", "g1")
        assert res["statusCode"] == 200
        assert dele.call_args.args[1] == {"PK": "GROUP#g1", "SK": "WORKSPACE#ws1"}

    @patch.object(H, "_is_workspace_admin", return_value=True)
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_list_bindings(self, ddb, _t, _adm):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        table.query.return_value = {"Items": [
            {"group_id": "g1", "workspace_id": "ws1", "role": "member",
             "created_at": "t0"}]}
        res = list_group_bindings(ADMIN, "ws1")
        assert res["statusCode"] == 200

    @patch.object(H, "log_admin_action")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_is_workspace_admin", return_value=True)
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_create_binding_stores_group_name(self, ddb, _t, _adm, put, _audit):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        table.get_item.return_value = {"Item": {"PK": "WS#ws1"}}
        res = create_group_binding(
            ADMIN, "ws1",
            GroupBindingRequest(group_id="00000000-0000-0000-0000-000000000001",
                                role="member", group_name="outcomeops-eng"))
        assert res["statusCode"] == 201
        assert put.call_args.args[1]["group_name"] == "outcomeops-eng"


class TestBindableGroups:
    @patch.object(graph_client, "search_groups",
                  return_value=[{"id": "1", "display_name": "outcomeops-eng"}])
    @patch.object(H, "_is_workspace_admin", return_value=True)
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_admin_gets_graph_results(self, ddb, _t, _adm, search):
        ddb.return_value.Table.return_value = MagicMock()
        res = bindable_groups(ADMIN, "ws1", "eng")
        assert res["statusCode"] == 200
        search.assert_called_once_with("eng")

    @patch.object(graph_client, "search_groups")
    @patch.object(H, "_is_workspace_admin", return_value=False)
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_non_admin_403(self, ddb, _t, _adm, search):
        ddb.return_value.Table.return_value = MagicMock()
        res = bindable_groups(USER, "ws1", "eng")
        assert res["statusCode"] == 403
        search.assert_not_called()


class TestSyncMemberships:
    @patch.object(H, "log_admin_action")
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_new_group_grant_written(self, ddb, _t, put, dele, _audit):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        # query order: user's existing members first, then bindings for g1
        table.query.side_effect = [
            {"Items": []},                                   # existing members
            {"Items": [_binding("g1", "ws1", "member")]},   # bindings for g1
        ]
        res = sync_memberships(USER, SyncMembershipsRequest(groups=["g1"]))
        assert res["statusCode"] == 200
        item = put.call_args.args[1]
        assert item["SK"] == "WS_MEMBER#ws1"
        assert item["source"] == "group:g1"
        assert item["user_email"] == USER
        dele.assert_not_called()

    @patch.object(H, "log_admin_action")
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_group_removed_revokes(self, ddb, _t, put, dele, _audit):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        # no groups -> no binding queries; existing has a group-owned row
        table.query.side_effect = [
            {"Items": [_member("ws1", source="group:g1")]},  # existing members
        ]
        res = sync_memberships(USER, SyncMembershipsRequest(groups=[]))
        assert res["statusCode"] == 200
        put.assert_not_called()
        assert dele.call_args.args[1] == {"PK": f"USER#{USER}", "SK": "WS_MEMBER#ws1"}

    @patch.object(H, "log_admin_action")
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_manual_row_untouched(self, ddb, _t, put, dele, _audit):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        table.query.side_effect = [
            {"Items": [_member("ws1", role="workspace_admin", source="manual")]},
        ]
        res = sync_memberships(USER, SyncMembershipsRequest(groups=[]))
        assert res["statusCode"] == 200
        put.assert_not_called()
        dele.assert_not_called()

    @patch.object(graph_client, "member_of", return_value=[])
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_overflow_without_graph_skips(self, ddb, _t, put, dele, memberof):
        # >200 overage + Graph off (member_of returns []) -> skip, no revocation
        table = MagicMock()
        table.query.side_effect = [{"Items": []}]  # existing members
        ddb.return_value.Table.return_value = table
        res = sync_memberships(
            USER, SyncMembershipsRequest(overflow=True, user_oid="oid-1"))
        assert res["statusCode"] == 200
        assert res["body"] and "skipped" in res["body"]
        put.assert_not_called()
        dele.assert_not_called()

    @patch.object(H, "log_admin_action")
    @patch.object(graph_client, "member_of", return_value=["g1"])
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_overflow_with_graph_resolves(self, ddb, _t, put, dele, memberof, _a):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        table.query.side_effect = [
            {"Items": []},                                   # existing members
            {"Items": [_binding("g1", "ws1", "member")]},   # bindings for g1
        ]
        res = sync_memberships(
            USER, SyncMembershipsRequest(overflow=True, user_oid="oid-1"))
        assert res["statusCode"] == 200
        memberof.assert_called_once_with("oid-1")
        assert put.call_args.args[1]["source"] == "group:g1"


class TestSyncMembershipsGraphCentric:
    """resolve_via_graph: ALB+Azure group-centric resolution (ADR-048 v1.3)."""

    def setup_method(self):
        H._bound_groups_cache.update({"exp": 0.0, "ids": []})

    @patch.object(H, "log_admin_action")
    @patch.object(graph_client, "groups_containing_user", return_value=["g1"])
    @patch.object(graph_client, "enabled", return_value=True)
    @patch.object(H, "_bound_group_ids", return_value=["g1", "g2"])
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_resolve_via_graph_provisions(
            self, ddb, _t, put, dele, bound, _en, gcu, _a):
        table = MagicMock()
        ddb.return_value.Table.return_value = table
        # query order: existing members first, then bindings for g1
        table.query.side_effect = [
            {"Items": []},                                   # existing members
            {"Items": [_binding("g1", "ws1", "member")]},   # bindings for g1
        ]
        res = sync_memberships(
            USER, SyncMembershipsRequest(resolve_via_graph=True, user_oid="oid-1"))
        assert res["statusCode"] == 200
        # candidate = bound groups (no existing group rows to union in here)
        gcu.assert_called_once_with("oid-1", ["g1", "g2"])
        assert put.call_args.args[1]["SK"] == "WS_MEMBER#ws1"
        assert put.call_args.args[1]["source"] == "group:g1"
        dele.assert_not_called()

    @patch.object(graph_client, "enabled", return_value=False)
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_resolve_via_graph_disabled_skips(self, ddb, _t, put, dele, _en):
        table = MagicMock()
        table.query.side_effect = [{"Items": []}]  # existing members
        ddb.return_value.Table.return_value = table
        res = sync_memberships(
            USER, SyncMembershipsRequest(resolve_via_graph=True, user_oid="oid-1"))
        assert res["statusCode"] == 200
        assert "skipped" in res["body"]
        put.assert_not_called()
        dele.assert_not_called()

    @patch.object(graph_client, "groups_containing_user",
                  side_effect=RuntimeError("graph down"))
    @patch.object(graph_client, "enabled", return_value=True)
    @patch.object(H, "_bound_group_ids", return_value=["g1"])
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_resolve_via_graph_error_skips_no_revoke(
            self, ddb, _t, put, dele, bound, _en, gcu):
        # Graph lookup fails -> skip; must NOT revoke even though a group row exists.
        table = MagicMock()
        table.query.side_effect = [
            {"Items": [_member("ws1", source="group:g1")]},  # existing members
        ]
        ddb.return_value.Table.return_value = table
        res = sync_memberships(
            USER, SyncMembershipsRequest(resolve_via_graph=True, user_oid="oid-1"))
        assert res["statusCode"] == 200
        assert "skipped" in res["body"]
        put.assert_not_called()
        dele.assert_not_called()

    @patch.object(H, "log_admin_action")
    @patch.object(graph_client, "groups_containing_user", return_value=[])
    @patch.object(graph_client, "enabled", return_value=True)
    @patch.object(H, "_bound_group_ids", return_value=["g1"])
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_resolve_via_graph_empty_is_authoritative_revokes(
            self, ddb, _t, put, dele, bound, _en, gcu, _a):
        # Graph CONFIRMS the user is in none of the candidate groups -> revoke.
        table = MagicMock()
        table.query.side_effect = [
            {"Items": [_member("ws1", source="group:g1")]},  # existing members
        ]
        ddb.return_value.Table.return_value = table
        res = sync_memberships(
            USER, SyncMembershipsRequest(resolve_via_graph=True, user_oid="oid-1"))
        assert res["statusCode"] == 200
        put.assert_not_called()
        assert dele.call_args.args[1] == {"PK": f"USER#{USER}", "SK": "WS_MEMBER#ws1"}

    @patch.object(H, "log_admin_action")
    @patch.object(graph_client, "groups_containing_user", return_value=["g1"])
    @patch.object(graph_client, "enabled", return_value=True)
    @patch.object(H, "_bound_group_ids", return_value=[])  # STALE/empty scan
    @patch.object(H.dual_write, "ddb_delete_item")
    @patch.object(H.dual_write, "ddb_put_item")
    @patch.object(H, "_get_workspaces_table", return_value="t")
    @patch.object(H, "_get_dynamodb")
    def test_resolve_via_graph_stale_scan_no_false_revoke(
            self, ddb, _t, put, dele, bound, _en, gcu, _a):
        # SAFETY: bound-groups scan is stale/empty, but the user already has a
        # group:g1 row. The existing group id must be unioned into the candidate
        # so membership is re-verified -> user still in g1, binding still there ->
        # keep. A stale scan must NEVER falsely revoke a real membership.
        table = MagicMock()
        table.query.side_effect = [
            {"Items": [_member("ws1", source="group:g1")]},  # existing members
            {"Items": [_binding("g1", "ws1", "member")]},    # bindings for g1
        ]
        ddb.return_value.Table.return_value = table
        res = sync_memberships(
            USER, SyncMembershipsRequest(resolve_via_graph=True, user_oid="oid-1"))
        assert res["statusCode"] == 200
        # candidate came from the EXISTING row (g1), not the empty scan
        gcu.assert_called_once_with("oid-1", ["g1"])
        dele.assert_not_called()            # no false revoke
        put.assert_not_called()             # already a member, nothing to add

    @patch.object(H, "_get_workspaces_table", return_value="t")
    def test_bound_group_ids_scan_paginates_and_caches(self, _t):
        H._bound_groups_cache.update({"exp": 0.0, "ids": []})
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [{"group_id": "g1"}, {"group_id": "g2"}],
             "LastEvaluatedKey": {"PK": "x"}},
            {"Items": [{"group_id": "g2"}, {"group_id": "g3"}]},
        ]
        out = H._bound_group_ids(table)
        assert out == ["g1", "g2", "g3"]         # distinct + sorted
        assert table.scan.call_count == 2        # paginated
        # second call served from the TTL cache -> no further scans
        assert H._bound_group_ids(table) == ["g1", "g2", "g3"]
        assert table.scan.call_count == 2
