"""Unit tests for azure-devops-integration Lambda handler.

The handler ties OAuth + REST client + DDB together. The strategy here
mirrors the sharepoint/github integration tests:

- Load the handler module via importlib, with token_manager + azure_devops_api
  registered under their unprefixed names so the handler's
  ``from token_manager import ...`` resolves.
- Mock the DDB resource at the handler module level so the routing +
  authorization logic can be tested without a real (or moto) table.
- Mock token_manager + azure_devops_api functions where their network
  calls would otherwise execute.

The tests focus on what the chunk plan calls out as acceptance criteria:
authorize-URL shape, callback state validation, license-snapshot
emission, add-repos input-shape validation, disconnect cascade
behavior, and sync filter (``enabled=true`` and
``include_code_maps=true``).
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


# Set env vars JUST for the handler's module-load (it reads them at
# import time + caches them). Save originals first and restore after
# the import so we don't leak into other test modules' handlers --
# pytest collects test_azure_devops_* before test_workspace_management,
# and that handler reads ``AUDIT_TABLE`` at its own module load. Leaking
# a non-empty value flips ``log_admin_action`` from a no-op to a real
# DDB call; the tests don't mock that path, and the moto-backed table
# doesn't exist, so they fail. Burned 2026-06-05.
_HANDLER_LOAD_ENV = {
    "AZURE_DEVOPS_TENANT_ID": "contoso-tenant-uuid",
    "AZURE_DEVOPS_CLIENT_ID": "client-uuid",
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM": "/dev/oo/azure-devops/client-secret",
    "AZURE_DEVOPS_REDIRECT_URI": "https://example.com/api/azure-devops/callback",
    "AZURE_DEVOPS_INTEGRATION_ENABLED": "true",
    "WORKSPACES_TABLE": "test-workspaces",
    "AZURE_DEVOPS_SYNC_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/azure-devops-sync",
    "DELETE_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/delete",
    "AUDIT_TABLE": "test-audit",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

# Load token_manager + azure_devops_api so the handler's imports resolve.
token_module = _load_module(
    "azdo_int_token_manager",
    "token_manager.py",
    "../../azure-devops-integration",
    also_register_as="token_manager",
)
api_module = _load_module(
    "azdo_int_api",
    "azure_devops_api.py",
    "../../azure-devops-integration",
    also_register_as="azure_devops_api",
)
handler = _load_module(
    "azdo_int_handler",
    "handler.py",
    "../../azure-devops-integration",
)

# Restore env to whatever it was before the handler loaded. The handler
# already cached the values into module-level constants
# (handler.AZURE_DEVOPS_TENANT_ID etc.); tests that need to flip them
# use ``patch.object(handler, "...")`` rather than mutating os.environ
# again.
for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_snapshot_helper():
    """Pin ``report_repos_snapshot`` so the license-snapshot helper
    short-circuits unless a test explicitly exercises it."""
    saved = handler.report_repos_snapshot
    handler.report_repos_snapshot = None
    yield
    handler.report_repos_snapshot = saved


@pytest.fixture(autouse=True)
def _reset_handler_clients():
    """Reset the handler's cached boto3/ssm clients between tests so a
    test that patches ``_dynamodb`` doesn't leak into the next."""
    handler._dynamodb = None
    handler._ssm = None
    handler._sqs = None
    handler._client_secret = None
    yield


@pytest.fixture
def mock_table():
    """A MagicMock that stands in for a DDB Table. Returned via the
    handler's ``_get_dynamodb().Table(...)`` chain."""
    table = MagicMock()
    table.get_item.return_value = {}
    table.query.return_value = {"Items": []}
    resource = MagicMock()
    resource.Table.return_value = table
    handler._dynamodb = resource
    return table


def _admin_user(table, email="admin@example.com"):
    """Configure the mock table so ``email`` is a workspace_admin
    in any workspace queried."""
    def get_item(Key, **_):
        if Key["SK"].startswith("WS_MEMBER#"):
            return {"Item": {"role": "workspace_admin"}}
        if Key["SK"] == "ORG_MEMBER":
            return {}
        return {}
    table.get_item.side_effect = get_item


def _member_user(table, email="member@example.com"):
    def get_item(Key, **_):
        if Key["SK"].startswith("WS_MEMBER#"):
            return {"Item": {"role": "member"}}
        if Key["SK"] == "ORG_MEMBER":
            return {}
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
# Authorize endpoint
# ---------------------------------------------------------------------------


class TestAuthorize:

    def test_returns_url_with_single_tenant(self, mock_table):
        _admin_user(mock_table)
        event = _make_event("GET", "/azure-devops/authorize", query={"workspace_id": "w1"})
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        url = body["auth_url"]
        # Per-tenant authorize URL; NOT /common/ -- Entra ID rejects
        # /common/ for single-tenant ADO app registrations.
        assert "login.microsoftonline.com/contoso-tenant-uuid/oauth2" in url
        assert "/common/" not in url
        assert "client_id=client-uuid" in url
        # Scope is URL-encoded; spaces become +
        assert "scope=" in url
        assert "redirect_uri=https" in url

    def test_returns_401_when_no_user_email(self, mock_table):
        event = _make_event(
            "GET", "/azure-devops/authorize",
            user_email=None, query={"workspace_id": "w1"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_returns_400_when_no_workspace_id(self, mock_table):
        _admin_user(mock_table)
        event = _make_event("GET", "/azure-devops/authorize")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_returns_403_when_not_admin(self, mock_table):
        _member_user(mock_table)
        event = _make_event(
            "GET", "/azure-devops/authorize", query={"workspace_id": "w1"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 403

    def test_returns_404_when_integration_disabled(self, mock_table):
        _admin_user(mock_table)
        with patch.object(handler, "AZURE_DEVOPS_INTEGRATION_ENABLED", False):
            event = _make_event(
                "GET", "/azure-devops/authorize", query={"workspace_id": "w1"},
            )
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 404

    def test_returns_500_when_oauth_app_unconfigured(self, mock_table):
        _admin_user(mock_table)
        # Simulate tfvars empty for tenant_id; integration would never
        # actually authorize successfully in this state.
        with patch.object(handler, "AZURE_DEVOPS_TENANT_ID", ""):
            event = _make_event(
                "GET", "/azure-devops/authorize", query={"workspace_id": "w1"},
            )
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# Callback endpoint
# ---------------------------------------------------------------------------


class TestCallback:

    def test_invalid_state_redirects_with_error(self, mock_table):
        event = _make_event(
            "GET", "/azure-devops/callback",
            query={"code": "auth-code", "state": "not-json"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "azure_devops_error=invalid_state" in result["headers"]["Location"]

    def test_missing_code_redirects_with_error(self, mock_table):
        event = _make_event(
            "GET", "/azure-devops/callback",
            query={"state": json.dumps({"workspace_id": "w1", "user_email": "u@example.com"})},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "azure_devops_error=invalid_callback" in result["headers"]["Location"]

    def test_user_denied_consent_redirects(self, mock_table):
        event = _make_event(
            "GET", "/azure-devops/callback",
            query={"error": "access_denied"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "azure_devops_error=oauth_denied" in result["headers"]["Location"]

    @patch.object(handler, "exchange_code_for_tokens")
    @patch.object(handler, "get_user_profile")
    @patch.object(handler, "_get_client_secret")
    @patch.object(handler, "dual_write")
    @patch.object(handler, "save_tokens")
    def test_happy_path_creates_connection_and_redirects(
        self, mock_save, mock_dual, mock_secret, mock_profile, mock_exchange, mock_table,
    ):
        mock_secret.return_value = "secret-value"
        mock_exchange.return_value = ("access-x", "refresh-x", 3600)
        mock_profile.return_value = {
            "emailAddress": "external@contoso.com",
            "displayName": "Jane Doe",
            "id": "member-uuid",
        }
        mock_save.return_value = True

        state = json.dumps({"workspace_id": "w1", "user_email": "u@example.com"})
        event = _make_event(
            "GET", "/azure-devops/callback",
            query={"code": "auth-code", "state": state},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        location = result["headers"]["Location"]
        assert "/workspaces/w1/settings?tab=integrations" in location
        assert "azure_devops_connected=" in location

        # DDB write was called with the AZDO_CONNECTION# row shape.
        assert mock_dual.ddb_put_item.called
        item = mock_dual.ddb_put_item.call_args.args[1]
        assert item["PK"] == "WS#w1"
        assert item["SK"].startswith("AZDO_CONNECTION#")
        assert item["tenant_id"] == "contoso-tenant-uuid"
        assert item["member_id"] == "member-uuid"

        # Tokens were saved keyed by the same connection_id.
        sk_conn_id = item["SK"].split("#", 1)[1]
        save_call = mock_save.call_args
        assert save_call.args[0] == sk_conn_id


# ---------------------------------------------------------------------------
# List connections
# ---------------------------------------------------------------------------


class TestListConnections:

    def test_returns_workspace_connections(self, mock_table):
        _member_user(mock_table)
        mock_table.query.return_value = {
            "Items": [{
                "connection_id": "c1",
                "display_name": "Jane",
                "user_email_external": "jane@contoso.com",
                "tenant_id": "contoso-tenant-uuid",
                "created_at": "2026-06-05T00:00:00Z",
            }]
        }
        event = _make_event("GET", "/workspaces/w1/azure-devops/connections")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["connections"]) == 1
        assert body["connections"][0]["connection_id"] == "c1"

    def test_403_when_no_membership(self, mock_table):
        _no_membership(mock_table)
        event = _make_event("GET", "/workspaces/w1/azure-devops/connections")
        assert handler.lambda_handler(event, None)["statusCode"] == 403


# ---------------------------------------------------------------------------
# Add repos
# ---------------------------------------------------------------------------


class TestAddRepos:

    def test_400_when_no_connection_id(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/w1/azure-devops/repos",
            body={"repos": [{"org": "o", "project": "p", "repo": "r"}]},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 400

    def test_400_when_empty_repos(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/w1/azure-devops/repos",
            body={"connection_id": "c1", "repos": []},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 400

    def test_400_on_invalid_json(self, mock_table):
        _admin_user(mock_table)
        event = _make_event("POST", "/workspaces/w1/azure-devops/repos")
        event["body"] = "{this is not valid"
        assert handler.lambda_handler(event, None)["statusCode"] == 400

    def test_404_when_connection_missing(self, mock_table):
        _admin_user(mock_table)
        # _get_connection -> get_item returns no Item for AZDO_CONNECTION#
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {}
        mock_table.get_item.side_effect = gi
        event = _make_event(
            "POST", "/workspaces/w1/azure-devops/repos",
            body={"connection_id": "missing", "repos": [
                {"org": "o", "project": "p", "repo": "r"}
            ]},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 404

    def test_403_when_not_admin(self, mock_table):
        _member_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/w1/azure-devops/repos",
            body={"connection_id": "c1", "repos": [
                {"org": "o", "project": "p", "repo": "r"}
            ]},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 403

    @patch.object(handler, "_connection_access_token")
    @patch.object(handler, "get_default_branch")
    @patch.object(handler, "get_latest_commit_sha")
    @patch.object(handler, "_get_sqs")
    @patch.object(handler, "dual_write")
    def test_happy_path_writes_repo_row_and_queues_sync(
        self, mock_dual, mock_sqs_fn, mock_sha, mock_branch, mock_token, mock_table,
    ):
        mock_token.return_value = "access-token"
        mock_branch.return_value = "main"
        mock_sha.return_value = "abc123"
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        # Connection exists; no AZDO_REPO# row yet for this slug.
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("AZDO_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "POST", "/workspaces/w1/azure-devops/repos",
            body={
                "connection_id": "c1",
                "repos": [
                    {"org": "example-org", "project": "example-project", "repo": "example-repo"}
                ],
            },
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["added"] == 1
        assert body["repos"][0]["slug"] == "example-org/example-project/example-repo"

        # DDB row written under AZDO_REPO#{org}/{project}/{repo}.
        item = mock_dual.ddb_put_item.call_args.args[1]
        assert item["SK"] == "AZDO_REPO#example-org/example-project/example-repo"
        assert item["org"] == "example-org"
        assert item["provider"] == "azure_devops"
        assert item["default_branch"] == "main"
        assert item["last_commit_sha"] == "abc123"

        # Sync was queued (include_code_maps defaults to True).
        assert mock_sqs.send_message.called
        msg = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert msg["slug"] == "example-org/example-project/example-repo"

    @patch.object(handler, "_connection_access_token")
    @patch.object(handler, "get_default_branch")
    @patch.object(handler, "_get_sqs")
    @patch.object(handler, "dual_write")
    def test_skips_repo_that_already_exists(
        self, mock_dual, mock_sqs_fn, mock_branch, mock_token, mock_table,
    ):
        mock_token.return_value = "access-token"
        mock_branch.return_value = "main"
        # Connection exists; existing AZDO_REPO# row exists too.
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("AZDO_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            if Key["SK"].startswith("AZDO_REPO#"):
                return {"Item": {"slug": "example-org/example-project/example-repo"}}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "POST", "/workspaces/w1/azure-devops/repos",
            body={
                "connection_id": "c1",
                "repos": [
                    {"org": "example-org", "project": "example-project", "repo": "example-repo"}
                ],
            },
        )
        result = handler.lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["added"] == 0
        assert any(s["reason"] == "already exists" for s in body["skipped"])
        # No DDB write for skipped repo.
        assert not mock_dual.ddb_put_item.called


# ---------------------------------------------------------------------------
# Update / delete repo (URL-encoded slug)
# ---------------------------------------------------------------------------


class TestUpdateRepo:

    @patch.object(handler, "dual_write")
    def test_updates_enabled_flag(self, mock_dual, mock_table):
        _admin_user(mock_table)
        # _get_workspace_repo returns existing row; mock get_item for both
        # the WS_MEMBER# admin check and the AZDO_REPO# lookup.
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("AZDO_REPO#"):
                return {"Item": {
                    "slug": "o/p/r", "enabled": True, "include_code_maps": True,
                    "repo_type": "application",
                }}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "PUT", "/workspaces/w1/azure-devops/repos/o/p/r",
            body={"enabled": False},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        # The UpdateExpression carries "enabled = :enabled" set to False.
        call_kwargs = mock_dual.ddb_update_item.call_args.kwargs
        assert "enabled = :enabled" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":enabled"] is False

    def test_400_on_malformed_slug(self, mock_table):
        _admin_user(mock_table)
        # Two-segment slug -- router won't match, returns 404. The router
        # only catches len(parts) >= 7 (i.e. 3-segment slug under
        # /workspaces/{id}/azure-devops/repos/). Two-segment is a router
        # miss, NOT a 400; the 400 path is for routes that DO match the
        # /repos/{slug} shape but carry a malformed slug. Confirm
        # the rejection happens cleanly.
        event = _make_event(
            "PUT", "/workspaces/w1/azure-devops/repos/o/p",
            body={"enabled": False},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 404


class TestDeleteRepo:

    @patch.object(handler, "_get_sqs")
    @patch.object(handler, "dual_write")
    def test_queues_delete_and_drops_row_and_snapshots(
        self, mock_dual, mock_sqs_fn, mock_table,
    ):
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("AZDO_REPO#"):
                return {"Item": {"slug": "o/p/r", "connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi

        # Wire up a real snapshot stub so we can assert it was invoked.
        snapshot_calls = []
        handler.report_repos_snapshot = lambda **kw: snapshot_calls.append(kw)
        try:
            handler.get_active_repos_for_customer = lambda _r, _t: ["o/p/r"]
            event = _make_event("DELETE", "/workspaces/w1/azure-devops/repos/o/p/r")
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200
            # Delete enqueued.
            assert mock_sqs.send_message.called
            # DDB row dropped.
            mock_dual.ddb_delete_item.assert_called_once()
            # License snapshot was invoked after row drop.
            assert len(snapshot_calls) == 1
        finally:
            handler.report_repos_snapshot = None


# ---------------------------------------------------------------------------
# Trigger sync filter
# ---------------------------------------------------------------------------


class TestTriggerSync:

    @patch.object(handler, "_get_sqs")
    def test_skips_disabled_repos(self, mock_sqs_fn, mock_table):
        _member_user(mock_table)
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        # Three repos: enabled+codemaps, disabled, codemaps=false.
        # Only the first should enqueue.
        mock_table.query.return_value = {
            "Items": [
                {"SK": "AZDO_REPO#a/b/c1", "slug": "a/b/c1", "connection_id": "x",
                 "enabled": True, "include_code_maps": True},
                {"SK": "AZDO_REPO#a/b/c2", "slug": "a/b/c2", "connection_id": "x",
                 "enabled": False, "include_code_maps": True},
                {"SK": "AZDO_REPO#a/b/c3", "slug": "a/b/c3", "connection_id": "x",
                 "enabled": True, "include_code_maps": False},
            ]
        }
        event = _make_event("POST", "/workspaces/w1/azure-devops/sync", body={})
        result = handler.lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["repos"] == 1
        assert mock_sqs.send_message.call_count == 1
        msg = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert msg["slug"] == "a/b/c1"

    @patch.object(handler, "_get_sqs")
    def test_message_body_carries_org_project_repo(self, mock_sqs_fn, mock_table):
        """Regression guard for the 2026-06-05 prd bug: the integration
        Lambda's _queue_sync_job was enqueuing only {workspace_id, slug,
        connection_id, triggered_by} but azure-devops-sync's
        process_sync_message reads message['org'|'project'|'repo']
        directly and KeyError'd on the first real sync. Pin the contract."""
        _member_user(mock_table)
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs
        mock_table.query.return_value = {
            "Items": [{
                "SK": "AZDO_REPO#example-org/example-project/example-repo",
                "slug": "example-org/example-project/example-repo",
                "connection_id": "x",
                "enabled": True,
                "include_code_maps": True,
            }],
        }
        event = _make_event("POST", "/workspaces/w1/azure-devops/sync", body={})
        handler.lambda_handler(event, None)
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        # The sync worker requires these three. Without them it
        # KeyErrors on the first message and the repo stays "pending"
        # forever.
        assert body["org"] == "example-org"
        assert body["project"] == "example-project"
        assert body["repo"] == "example-repo"
        assert body["slug"] == "example-org/example-project/example-repo"
        assert body["connection_id"] == "x"

    @patch.object(handler, "_get_sqs")
    def test_single_repo_path(self, mock_sqs_fn, mock_table):
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "member"}}
            if Key["SK"].startswith("AZDO_REPO#"):
                return {"Item": {"slug": "a/b/c", "connection_id": "x", "enabled": True}}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "POST", "/workspaces/w1/azure-devops/sync", body={"slug": "a/b/c"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["repos"] == 1


# ---------------------------------------------------------------------------
# Disconnect cascade
# ---------------------------------------------------------------------------


class TestDisconnect:

    def test_404_when_connection_missing(self, mock_table):
        _admin_user(mock_table)
        # WS_MEMBER admin yes; AZDO_CONNECTION# absent.
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {}
        mock_table.get_item.side_effect = gi
        event = _make_event(
            "DELETE", "/workspaces/w1/azure-devops/connections/missing",
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 404

    @patch.object(handler, "_get_sqs")
    @patch.object(handler, "dual_write")
    @patch.object(handler, "delete_tokens")
    def test_cascade_removes_tied_repos_and_tokens(
        self, mock_delete_tokens, mock_dual, mock_sqs_fn, mock_table,
    ):
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("AZDO_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi
        # Two repos tied to c1; one tied to a different connection.
        mock_table.query.return_value = {
            "Items": [
                {"SK": "AZDO_REPO#a/b/c1", "slug": "a/b/c1", "connection_id": "c1"},
                {"SK": "AZDO_REPO#a/b/c2", "slug": "a/b/c2", "connection_id": "c1"},
                {"SK": "AZDO_REPO#a/b/c3", "slug": "a/b/c3", "connection_id": "c2"},
            ]
        }
        mock_delete_tokens.return_value = True

        event = _make_event(
            "DELETE", "/workspaces/w1/azure-devops/connections/c1",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        # Two repo delete-jobs enqueued for the cascade (c1's repos only).
        assert mock_sqs.send_message.call_count == 2
        # Three DDB deletes: 2 repos + 1 connection row.
        assert mock_dual.ddb_delete_item.call_count == 3
        # Token row deleted.
        mock_delete_tokens.assert_called_once_with("c1")


# ---------------------------------------------------------------------------
# Router 404 catch-all
# ---------------------------------------------------------------------------


class TestRouter404:

    def test_unknown_path_returns_404(self, mock_table):
        event = _make_event("GET", "/azure-devops/nonsense")
        assert handler.lambda_handler(event, None)["statusCode"] == 404
