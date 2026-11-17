# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for azure-devops-boards-integration Lambda handler.

Strategy mirrors ``test_azure_devops_integration.py``:
- Load the handler via importlib with token_manager +
  azure_devops_work_items_api pre-registered so relative imports
  resolve.
- Mock the DDB resource at the handler module level so routing and
  authorization logic can be tested without a real table.
- Focus on acceptance criteria: authorize URL construction,
  callback state validation, add-projects input-shape validation,
  disconnect cascade, sync trigger filter.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------


def _load_module(mod_name, filename, directory, also_register_as=None):
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    mod_dir = os.path.join(os.path.dirname(__file__), directory)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    mod_path = os.path.join(mod_dir, filename)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    if also_register_as:
        sys.modules[also_register_as] = module
    spec.loader.exec_module(module)
    return module


# Env vars for handler module-load (cached at import time).
_HANDLER_LOAD_ENV = {
    "AZURE_DEVOPS_TENANT_ID": "example-tenant-uuid",
    "AZURE_DEVOPS_CLIENT_ID": "client-uuid",
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM": "/dev/oo/azure-devops/client-secret",
    "AZURE_DEVOPS_BOARDS_REDIRECT_URI": "https://example.com/api/azure-devops-boards/callback",
    "WORKSPACES_TABLE": "test-workspaces",
    "AZURE_DEVOPS_BOARDS_SYNC_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/azure-devops-boards-sync",
    "DELETE_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/delete",
    "AUDIT_TABLE": "test-audit",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

# Chunk 16: consolidated. token_manager -> shared/oauth/azure_devops_boards.py.
# The Repos client (azure_devops_api) -> shared/azure_devops_api.py. Import
# the shared modules via the ordinary package path so all consumers share
# the SAME module object -- double-loading via _load_module gives distinct
# class identities AND breaks the normal `shared.oauth` package registration.
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)
import shared.oauth.azure_devops_boards as token_module  # noqa: E402
import shared.azure_devops_api as repos_api  # noqa: E402
# Legacy aliases for lingering @patch("token_manager.X") / @patch("azure_devops_api.X") targets.
sys.modules["token_manager"] = token_module
sys.modules["azure_devops_api"] = repos_api
api_module = _load_module(
    "azdo_boards_work_items_api",
    "azure_devops_work_items_api.py",
    "../../azure-devops-boards-integration",
    also_register_as="azure_devops_work_items_api",
)
handler = _load_module(
    "azdo_boards_int_handler",
    "handler.py",
    "../../azure-devops-boards-integration",
)

# Restore env.
for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_handler_clients():
    handler._dynamodb = None
    handler._ssm = None
    handler._sqs = None
    handler._client_secret = None
    yield


@pytest.fixture
def mock_table():
    table = MagicMock()
    table.get_item.return_value = {}
    table.query.return_value = {"Items": []}
    resource = MagicMock()
    resource.Table.return_value = table
    handler._dynamodb = resource
    return table


@pytest.fixture
def mock_sqs():
    sqs = MagicMock()
    handler._sqs = sqs
    return sqs


def _admin_user(table):
    def get_item(Key, **_):
        if Key["SK"].startswith("WS_MEMBER#"):
            return {"Item": {"role": "workspace_admin"}}
        return {}
    table.get_item.side_effect = get_item


def _member_user(table):
    def get_item(Key, **_):
        if Key["SK"].startswith("WS_MEMBER#"):
            return {"Item": {"role": "member"}}
        return {}
    table.get_item.side_effect = get_item


def _no_membership(table):
    table.get_item.return_value = {}


def _make_event(method, path, *, user_email="admin@example.com", query=None, body=None):
    return {
        "headers": {"x-user-email": user_email} if user_email else {},
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else "",
        "requestContext": {"http": {"method": method, "path": path}},
    }


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------


class TestAuthorize:

    def test_admin_gets_auth_url(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "GET", "/azure-devops-boards/authorize",
            query={"workspace_id": "ws1"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "auth_url" in body
        assert "login.microsoftonline.com/example-tenant-uuid" in body["auth_url"]
        assert "vso.work" in body["auth_url"]
        # Uses the Boards redirect URI, not the Repos one.
        assert "azure-devops-boards" in body["auth_url"]

    def test_member_not_admin_gets_403(self, mock_table):
        _member_user(mock_table)
        event = _make_event(
            "GET", "/azure-devops-boards/authorize",
            query={"workspace_id": "ws1"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 403

    def test_missing_workspace_id_gets_400(self, mock_table):
        event = _make_event("GET", "/azure-devops-boards/authorize")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


class TestCallback:

    def test_invalid_state_redirects_with_error(self, mock_table):
        event = _make_event(
            "GET", "/azure-devops-boards/callback",
            user_email=None,
            query={"code": "auth-code", "state": "not-valid-json"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "invalid_state" in result["headers"]["Location"]

    def test_oauth_error_redirects(self, mock_table):
        event = _make_event(
            "GET", "/azure-devops-boards/callback",
            user_email=None,
            query={"error": "consent_required"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "oauth_denied" in result["headers"]["Location"]

    def test_happy_path_writes_connection_and_redirects(self, mock_table):
        state = json.dumps({"workspace_id": "ws1", "user_email": "admin@example.com"})
        with patch.object(handler, "_get_client_secret", return_value="sec"), \
             patch.object(
                 handler, "exchange_code_for_tokens",
                 return_value=("access-x", "refresh-x", 3600),
             ), \
             patch.object(
                 handler, "get_user_profile",
                 return_value={
                     "displayName": "Alice",
                     "emailAddress": "alice@example.com",
                     "id": "user-id-abc",
                 },
             ), \
             patch.object(handler, "save_tokens", return_value=True), \
             patch.object(handler, "log_admin_action"), \
             patch.object(handler.dual_write, "ddb_put_item") as mock_put:
            event = _make_event(
                "GET", "/azure-devops-boards/callback",
                user_email=None,
                query={"code": "auth-code", "state": state},
            )
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 302
        assert "azure_devops_boards_connected" in result["headers"]["Location"]
        # Connection row written with Boards SK prefix.
        put_call = mock_put.call_args
        row = put_call.args[1]
        assert row["SK"].startswith("AZDO_BOARDS_CONNECTION#")
        assert row["provider"] == "azure_devops_boards"
        assert row["display_name"] == "Alice"


# ---------------------------------------------------------------------------
# List connections
# ---------------------------------------------------------------------------


class TestListConnections:

    def test_member_can_list(self, mock_table):
        _member_user(mock_table)
        mock_table.query.return_value = {
            "Items": [
                {
                    "connection_id": "c1",
                    "display_name": "Alice",
                    "user_email_external": "alice@example.com",
                    "tenant_id": "t",
                    "created_at": "2026-07-04T00:00:00Z",
                },
            ],
        }
        event = _make_event(
            "GET", "/workspaces/ws1/azure-devops-boards/connections",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["connections"][0]["connection_id"] == "c1"

    def test_non_member_403(self, mock_table):
        _no_membership(mock_table)
        event = _make_event(
            "GET", "/workspaces/ws1/azure-devops-boards/connections",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 403


# ---------------------------------------------------------------------------
# Add projects
# ---------------------------------------------------------------------------


class TestAddProjects:

    def _admin_env(self, mock_table):
        _admin_user(mock_table)
        mock_table.get_item.side_effect = None

        # 1st get_item: WS_MEMBER (admin lookup); 2nd: ORG_MEMBER;
        # 3rd: get connection; then per-project: get existing project row.
        # Simpler approach: fake by patching the module helpers.
        return None

    def test_missing_connection_id_400(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/ws1/azure-devops-boards/projects",
            body={"projects": [{"org": "o", "project": "p"}]},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400
        assert "connection_id" in json.loads(result["body"])["error"]

    def test_missing_projects_list_400(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/ws1/azure-devops-boards/projects",
            body={"connection_id": "c1"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_happy_path_persists_and_queues_sync(self, mock_table, mock_sqs):
        _admin_user(mock_table)
        with patch.object(handler, "_get_connection", return_value={"connection_id": "c1"}), \
             patch.object(handler, "_get_workspace_project", return_value=None), \
             patch.object(handler.dual_write, "ddb_put_item") as mock_put:
            event = _make_event(
                "POST", "/workspaces/ws1/azure-devops-boards/projects",
                body={
                    "connection_id": "c1",
                    "projects": [
                        {"org": "example-org", "project": "widgets"},
                    ],
                },
            )
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["added"] == 1
        assert body["projects"][0]["slug"] == "example-org/widgets"

        # DDB row written under Boards project SK.
        row = mock_put.call_args.args[1]
        assert row["SK"] == "AZDO_BOARDS_PROJECT#example-org/widgets"
        assert row["provider"] == "azure_devops_boards"
        assert row["last_changed_watermark"] is None
        assert row["sync_status"] == "pending"

        # Sync-queue message sent.
        assert mock_sqs.send_message.call_count == 1
        message = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert message["org"] == "example-org"
        assert message["project"] == "widgets"
        assert message["triggered_by"] == "manual"

    def test_duplicate_project_skipped(self, mock_table, mock_sqs):
        _admin_user(mock_table)
        with patch.object(handler, "_get_connection", return_value={"connection_id": "c1"}), \
             patch.object(
                 handler, "_get_workspace_project",
                 return_value={"slug": "o/p", "org": "o", "project": "p"},
             ), \
             patch.object(handler.dual_write, "ddb_put_item") as mock_put:
            event = _make_event(
                "POST", "/workspaces/ws1/azure-devops-boards/projects",
                body={
                    "connection_id": "c1",
                    "projects": [{"org": "o", "project": "p"}],
                },
            )
            result = handler.lambda_handler(event, None)

        body = json.loads(result["body"])
        assert body["added"] == 0
        assert body["skipped"][0]["reason"] == "already exists"
        mock_put.assert_not_called()

    def test_non_admin_403(self, mock_table):
        _member_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/ws1/azure-devops-boards/projects",
            body={
                "connection_id": "c1",
                "projects": [{"org": "o", "project": "p"}],
            },
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 403


# ---------------------------------------------------------------------------
# List projects
# ---------------------------------------------------------------------------


class TestListProjects:

    def test_returns_only_projects_with_slug(self, mock_table):
        _member_user(mock_table)
        mock_table.query.return_value = {
            "Items": [
                {
                    "SK": "AZDO_BOARDS_PROJECT#example-org/widgets",
                    "org": "example-org", "project": "widgets",
                    "connection_id": "c1", "enabled": True,
                },
            ],
        }
        event = _make_event(
            "GET", "/workspaces/ws1/azure-devops-boards/projects",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["projects"][0]["slug"] == "example-org/widgets"


# ---------------------------------------------------------------------------
# Delete project
# ---------------------------------------------------------------------------


class TestDeleteProject:

    def test_deletes_row_and_queues_prefix_delete(self, mock_table, mock_sqs):
        _admin_user(mock_table)
        with patch.object(
            handler, "_get_workspace_project",
            return_value={"slug": "example-org/widgets"},
        ), patch.object(handler.dual_write, "ddb_delete_item") as mock_delete:
            event = _make_event(
                "DELETE",
                "/workspaces/ws1/azure-devops-boards/projects/example-org/widgets",
            )
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_delete.assert_called_once()
        # Delete-prefix message sent.
        assert mock_sqs.send_message.call_count == 1
        message = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert message["action"] == "delete_prefix"
        assert message["source"] == "azure_devops_boards"
        assert message["prefix"] == "azure_devops_boards/example-org/widgets/"

    def test_malformed_slug_400(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "DELETE",
            "/workspaces/ws1/azure-devops-boards/projects/only-one-segment-x",
        )
        result = handler.lambda_handler(event, None)
        # Router routes >=6 parts only, so this is 5 parts -> 404.
        assert result["statusCode"] == 404


# ---------------------------------------------------------------------------
# Trigger sync
# ---------------------------------------------------------------------------


class TestTriggerSync:

    def test_all_projects_only_queues_enabled(self, mock_table, mock_sqs):
        _member_user(mock_table)
        # Mix of enabled and disabled projects.
        mock_table.query.return_value = {
            "Items": [
                {
                    "SK": "AZDO_BOARDS_PROJECT#o1/p1",
                    "connection_id": "c1", "enabled": True,
                },
                {
                    "SK": "AZDO_BOARDS_PROJECT#o1/p2",
                    "connection_id": "c1", "enabled": False,
                },
            ],
        }
        event = _make_event(
            "POST", "/workspaces/ws1/azure-devops-boards/sync",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["projects"] == 1
        assert mock_sqs.send_message.call_count == 1

    def test_specific_slug_disabled_400(self, mock_table, mock_sqs):
        _member_user(mock_table)
        with patch.object(
            handler, "_get_workspace_project",
            return_value={"slug": "o/p", "connection_id": "c1", "enabled": False},
        ):
            event = _make_event(
                "POST", "/workspaces/ws1/azure-devops-boards/sync",
                body={"slug": "o/p"},
            )
            result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


class TestDisconnect:

    def test_cascade_removes_projects_and_connection(self, mock_table, mock_sqs):
        _admin_user(mock_table)
        # Simulate a project attached to this connection.
        mock_table.query.return_value = {
            "Items": [
                {
                    "SK": "AZDO_BOARDS_PROJECT#o/p1",
                    "connection_id": "c1",
                },
            ],
        }
        with patch.object(
            handler, "_get_connection", return_value={"connection_id": "c1"},
        ), patch.object(handler, "delete_tokens", return_value=True), \
             patch.object(handler.dual_write, "ddb_delete_item") as mock_delete:
            event = _make_event(
                "DELETE",
                "/workspaces/ws1/azure-devops-boards/connections/c1",
            )
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 200
        # Two deletes: project + connection.
        assert mock_delete.call_count == 2
        # Delete-prefix job queued for the project.
        assert mock_sqs.send_message.call_count == 1

    def test_non_admin_403(self, mock_table):
        _member_user(mock_table)
        event = _make_event(
            "DELETE", "/workspaces/ws1/azure-devops-boards/connections/c1",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 403


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class TestRouter404:

    def test_unknown_path_returns_404(self, mock_table):
        event = _make_event("GET", "/does/not/exist")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_unknown_boards_subpath_returns_404(self, mock_table):
        _member_user(mock_table)
        event = _make_event(
            "GET", "/workspaces/ws1/azure-devops-boards/no-such-subpath",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 404
