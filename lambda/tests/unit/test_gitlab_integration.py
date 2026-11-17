# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for gitlab-integration Lambda handler.

Mirrors test_azure_devops_integration.py, adapted for GitLab's design
(ADR-050):

- The shared OAuth module ``shared.oauth.gitlab`` owns token exchange +
  the SSM client-secret read + KMS/DDB persistence. ``exchange_code_for_tokens``
  returns just the access-token string (persisting tokens itself), and
  ``get_user_profile`` returns a ``(user_id, username)`` tuple. So the handler
  never calls ``save_tokens`` or reads the client secret directly.
- GitLab is group -> project (two levels), so a repo slug is the full
  ``path_with_namespace`` (>= 2 segments, nested subgroups add more). The DDB
  SK is ``GITLAB_REPO#{slug}``.
- The sync/delete enqueue is a later chunk, so add-repos / delete just touch
  DDB (no queue).

Tests focus on the acceptance criteria the chunk plan calls out:
authorize-URL shape, callback -> connection row, add-repo -> GITLAB_REPO# row,
update/delete slug handling, and the disconnect cascade (connection + tokens +
tied repos).
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


def _load_module(mod_name, filename, directory):
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    mod_dir = os.path.join(os.path.dirname(__file__), directory)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    mod_path = os.path.join(mod_dir, filename)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# Set env vars JUST for the handler's module-load (it reads them at import
# time + caches some into module-level constants). Save originals first and
# restore after the import so we don't leak into other test modules' handlers
# -- notably AUDIT_TABLE, which flips log_admin_action from a no-op to a real
# DDB call in the workspace_management handler (see the ADO test for the burn).
_HANDLER_LOAD_ENV = {
    "GITLAB_CLIENT_ID": "client-uuid",
    "GITLAB_CLIENT_SECRET_SSM_PARAM": "/dev/oo/gitlab/client-secret",
    "GITLAB_REDIRECT_URI": "https://example.com/api/gitlab/callback",
    "GITLAB_BASE_URL": "https://gitlab.com",
    "WORKSPACES_TABLE": "test-workspaces",
    "OAUTH_KMS_KEY_ARN": "arn:aws:kms:us-west-2:123:key/abc",
    "AUDIT_TABLE": "test-audit",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

# Import the shared modules via the ordinary package path so all handlers
# share the SAME module object (double-loading gives distinct identities and
# breaks except-clauses across handlers).
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)
import shared.gitlab_api  # noqa: E402,F401
import shared.oauth.gitlab  # noqa: E402,F401

handler = _load_module(
    "gitlab_int_handler",
    "handler.py",
    "../../gitlab-integration",
)

# Restore env to whatever it was before the handler loaded. The handler
# already cached the values it needs into module-level constants
# (handler.GITLAB_REDIRECT_URI etc.); tests that need a live env value
# (gitlab_oauth.get_client_id reads env at call time) set it explicitly.
for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_handler_clients():
    """Reset the handler's cached boto3 client between tests."""
    handler._dynamodb = None
    yield


@pytest.fixture
def mock_table():
    """A MagicMock that stands in for a DDB Table, returned via the
    handler's ``_get_dynamodb().Table(...)`` chain."""
    table = MagicMock()
    table.get_item.return_value = {}
    table.query.return_value = {"Items": []}
    resource = MagicMock()
    resource.Table.return_value = table
    handler._dynamodb = resource
    return table


def _admin_user(table, email="admin@example.com"):
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

    def test_returns_url(self, mock_table):
        _admin_user(mock_table)
        # get_client_id() reads env live; set it for this call.
        with patch.dict(os.environ, {
            "GITLAB_CLIENT_ID": "client-uuid",
            "GITLAB_BASE_URL": "https://gitlab.com",
        }):
            event = _make_event("GET", "/gitlab/authorize", query={"workspace_id": "w1"})
            result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        url = json.loads(result["body"])["auth_url"]
        assert "gitlab.com/oauth/authorize" in url
        assert "client_id=client-uuid" in url
        assert "redirect_uri=" in url
        assert "scope=" in url
        assert "state=" in url

    def test_returns_401_when_no_user_email(self, mock_table):
        event = _make_event(
            "GET", "/gitlab/authorize",
            user_email=None, query={"workspace_id": "w1"},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 401

    def test_returns_400_when_no_workspace_id(self, mock_table):
        _admin_user(mock_table)
        event = _make_event("GET", "/gitlab/authorize")
        assert handler.lambda_handler(event, None)["statusCode"] == 400

    def test_returns_403_when_not_admin(self, mock_table):
        _member_user(mock_table)
        event = _make_event("GET", "/gitlab/authorize", query={"workspace_id": "w1"})
        assert handler.lambda_handler(event, None)["statusCode"] == 403

    def test_returns_500_when_oauth_app_unconfigured(self, mock_table):
        _admin_user(mock_table)
        # No GITLAB_CLIENT_ID in env -> get_client_id() returns "" -> 500.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITLAB_CLIENT_ID", None)
            event = _make_event(
                "GET", "/gitlab/authorize", query={"workspace_id": "w1"},
            )
            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# Callback endpoint
# ---------------------------------------------------------------------------


class TestCallback:

    def test_invalid_state_redirects_with_error(self, mock_table):
        event = _make_event(
            "GET", "/gitlab/callback",
            query={"code": "auth-code", "state": "not-json"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "gitlab_error=invalid_state" in result["headers"]["Location"]

    def test_missing_code_redirects_with_error(self, mock_table):
        event = _make_event(
            "GET", "/gitlab/callback",
            query={"state": json.dumps({"workspace_id": "w1", "user_email": "u@example.com"})},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "gitlab_error=invalid_callback" in result["headers"]["Location"]

    def test_user_denied_consent_redirects(self, mock_table):
        event = _make_event(
            "GET", "/gitlab/callback", query={"error": "access_denied"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        assert "gitlab_error=oauth_denied" in result["headers"]["Location"]

    @patch.object(handler.gitlab_oauth, "base_url", return_value="https://gitlab.com")
    @patch.object(handler.gitlab_oauth, "get_user_profile")
    @patch.object(handler.gitlab_oauth, "exchange_code_for_tokens")
    @patch.object(handler, "dual_write")
    def test_happy_path_creates_connection_and_redirects(
        self, mock_dual, mock_exchange, mock_profile, mock_base, mock_table,
    ):
        mock_exchange.return_value = "access-x"
        mock_profile.return_value = ("999", "octocat")

        state = json.dumps({"workspace_id": "w1", "user_email": "u@example.com"})
        event = _make_event(
            "GET", "/gitlab/callback",
            query={"code": "auth-code", "state": state},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 302
        location = result["headers"]["Location"]
        assert "/workspaces/w1/settings?tab=integrations" in location
        assert "gitlab_connected=" in location

        # DDB write carried the GITLAB_CONNECTION# row shape.
        assert mock_dual.ddb_put_item.called
        item = mock_dual.ddb_put_item.call_args.args[1]
        assert item["PK"] == "WS#w1"
        assert item["SK"].startswith("GITLAB_CONNECTION#")
        assert item["gitlab_user_id"] == "999"
        assert item["gitlab_username"] == "octocat"

        # The exchange was handed the SAME connection_id used in the SK,
        # so the persisted tokens key the connection.
        sk_conn_id = item["SK"].split("#", 1)[1]
        assert mock_exchange.call_args.kwargs["connection_id"] == sk_conn_id


# ---------------------------------------------------------------------------
# List connections
# ---------------------------------------------------------------------------


class TestListConnections:

    def test_returns_workspace_connections(self, mock_table):
        _member_user(mock_table)
        mock_table.query.return_value = {
            "Items": [{
                "connection_id": "c1",
                "display_name": "octocat",
                "gitlab_username": "octocat",
                "base_url": "https://gitlab.com",
                "created_at": "2026-07-18T00:00:00Z",
            }]
        }
        event = _make_event("GET", "/workspaces/w1/gitlab/connections")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["connections"]) == 1
        assert body["connections"][0]["connection_id"] == "c1"

    def test_403_when_no_membership(self, mock_table):
        _no_membership(mock_table)
        event = _make_event("GET", "/workspaces/w1/gitlab/connections")
        assert handler.lambda_handler(event, None)["statusCode"] == 403


# ---------------------------------------------------------------------------
# List groups + projects (cascade)
# ---------------------------------------------------------------------------


class TestDiscovery:

    @patch.object(handler.gitlab_api, "list_groups")
    @patch.object(handler, "_connection_access_token")
    def test_list_groups(self, mock_token, mock_groups, mock_table):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "member"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi
        mock_token.return_value = "at"
        mock_groups.return_value = [
            {"id": 10, "full_path": "acme", "name": "Acme"},
        ]
        event = _make_event("GET", "/workspaces/w1/gitlab/connections/c1/groups")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["groups"][0]["full_path"] == "acme"

    @patch.object(handler.gitlab_api, "list_group_projects")
    @patch.object(handler, "_connection_access_token")
    def test_list_group_projects_flags_already_added(
        self, mock_token, mock_projects, mock_table,
    ):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "member"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi
        mock_token.return_value = "at"
        mock_projects.return_value = [
            {"id": 1, "slug": "acme/svc", "name": "svc", "default_branch": "main"},
            {"id": 2, "slug": "acme/sub/api", "name": "api", "default_branch": "main"},
        ]
        # svc already enrolled.
        mock_table.query.return_value = {
            "Items": [{"SK": "GITLAB_REPO#acme/svc", "slug": "acme/svc"}]
        }
        event = _make_event(
            "GET", "/workspaces/w1/gitlab/connections/c1/groups/10/projects",
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        by_slug = {p["slug"]: p for p in body["projects"]}
        assert by_slug["acme/svc"]["already_added"] is True
        assert by_slug["acme/sub/api"]["already_added"] is False


# ---------------------------------------------------------------------------
# Add repos
# ---------------------------------------------------------------------------


class TestAddRepos:

    def test_400_when_no_connection_id(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"repos": [{"slug": "acme/svc"}]},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 400

    def test_400_when_empty_repos(self, mock_table):
        _admin_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"connection_id": "c1", "repos": []},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 400

    def test_400_on_invalid_json(self, mock_table):
        _admin_user(mock_table)
        event = _make_event("POST", "/workspaces/w1/gitlab/repos")
        event["body"] = "{this is not valid"
        assert handler.lambda_handler(event, None)["statusCode"] == 400

    def test_404_when_connection_missing(self, mock_table):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {}
        mock_table.get_item.side_effect = gi
        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"connection_id": "missing", "repos": [{"slug": "acme/svc"}]},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 404

    def test_403_when_not_admin(self, mock_table):
        _member_user(mock_table)
        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"connection_id": "c1", "repos": [{"slug": "acme/svc"}]},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 403

    @patch.object(handler.gitlab_api, "get_latest_commit_sha")
    @patch.object(handler.gitlab_api, "get_default_branch")
    @patch.object(handler, "_connection_access_token")
    @patch.object(handler, "dual_write")
    def test_happy_path_writes_repo_row(
        self, mock_dual, mock_token, mock_branch, mock_sha, mock_table,
    ):
        mock_token.return_value = "access-token"
        mock_branch.return_value = "main"
        mock_sha.return_value = "abc123"

        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi

        # Nested-subgroup slug: 3 segments, group is the first two joined.
        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"connection_id": "c1", "repos": [{"slug": "acme/sub/api"}]},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["added"] == 1
        assert body["repos"][0]["slug"] == "acme/sub/api"

        item = mock_dual.ddb_put_item.call_args.args[1]
        assert item["SK"] == "GITLAB_REPO#acme/sub/api"
        assert item["provider"] == "gitlab"
        assert item["group"] == "acme/sub"
        assert item["project"] == "api"
        assert item["default_branch"] == "main"
        assert item["last_commit_sha"] == "abc123"

    def test_skips_repo_missing_slug(self, mock_table):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi
        with patch.object(handler, "_connection_access_token", return_value="at"):
            event = _make_event(
                "POST", "/workspaces/w1/gitlab/repos",
                body={"connection_id": "c1", "repos": [{"name": "no-slug"}]},
            )
            result = handler.lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["added"] == 0
        assert any(s["reason"] == "missing slug" for s in body["skipped"])

    @patch.object(handler.gitlab_api, "get_default_branch")
    @patch.object(handler, "_connection_access_token")
    @patch.object(handler, "dual_write")
    def test_skips_repo_that_already_exists(
        self, mock_dual, mock_token, mock_branch, mock_table,
    ):
        mock_token.return_value = "at"
        mock_branch.return_value = "main"

        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            if Key["SK"].startswith("GITLAB_REPO#"):
                return {"Item": {"slug": "acme/svc"}}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"connection_id": "c1", "repos": [{"slug": "acme/svc"}]},
        )
        result = handler.lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["added"] == 0
        assert any(s["reason"] == "already exists" for s in body["skipped"])
        assert not mock_dual.ddb_put_item.called

    @patch.object(handler.gitlab_api, "get_default_branch")
    @patch.object(handler, "_connection_access_token")
    @patch.object(handler, "dual_write")
    def test_refuses_same_bare_name_from_a_different_instance(
        self, mock_dual, mock_token, mock_branch, mock_table, monkeypatch,
    ):
        """ADR-050 v2.0 collision guard: a project whose bare name is already
        enrolled from a DIFFERENT GitLab instance is refused, because the
        vector / symbol-graph keys have no instance discriminator and would
        silently collide. Two instances configured; the connection targets
        'saas', an existing repo with the same bare name belongs to
        'internal'."""
        monkeypatch.setenv("GITLAB_INSTANCES", json.dumps([
            {"id": "saas", "base_url": "https://gitlab.com",
             "client_id": "s", "client_secret_ssm_param": "/p/s"},
            {"id": "internal", "base_url": "https://gitlab.example.com",
             "client_id": "i", "client_secret_ssm_param": "/p/i"},
        ]))
        mock_token.return_value = "at"
        mock_branch.return_value = "main"

        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1", "instance_id": "saas"}}
            return {}
        mock_table.get_item.side_effect = gi
        # Existing enrolled repo: SAME bare name ('api'), DIFFERENT instance.
        mock_table.query.return_value = {"Items": [
            {"SK": "GITLAB_REPO#othergroup/api", "instance_id": "internal"},
        ]}

        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"connection_id": "c1", "repos": [{"slug": "acme/sub/api"}]},
        )
        result = handler.lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["added"] == 0
        assert any("different GitLab instance" in s["reason"]
                   for s in body["skipped"])
        assert not mock_dual.ddb_put_item.called

    @patch.object(handler.gitlab_api, "get_latest_commit_sha")
    @patch.object(handler.gitlab_api, "get_default_branch")
    @patch.object(handler, "_connection_access_token")
    @patch.object(handler, "dual_write")
    def test_ui_override_branch_wins_over_resolution(
        self, mock_dual, mock_token, mock_branch, mock_sha, mock_table,
    ):
        mock_token.return_value = "at"
        mock_sha.return_value = "abc123"

        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "POST", "/workspaces/w1/gitlab/repos",
            body={"connection_id": "c1", "repos": [
                {"slug": "acme/svc", "default_branch": "develop"}
            ]},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 201
        mock_branch.assert_not_called()  # resolver skipped
        item = mock_dual.ddb_put_item.call_args.args[1]
        assert item["default_branch"] == "develop"


# ---------------------------------------------------------------------------
# Update / delete repo (subgroup slug)
# ---------------------------------------------------------------------------


class TestUpdateRepo:

    @patch.object(handler, "dual_write")
    def test_updates_enabled_flag(self, mock_dual, mock_table):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_REPO#"):
                return {"Item": {
                    "slug": "acme/sub/api", "enabled": True,
                    "include_code_maps": True, "repo_type": "application",
                }}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "PUT", "/workspaces/w1/gitlab/repos/acme/sub/api",
            body={"enabled": False},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        call_kwargs = mock_dual.ddb_update_item.call_args.kwargs
        assert "enabled = :enabled" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":enabled"] is False

    def test_404_on_single_segment_slug(self, mock_table):
        # A one-segment slug can't match the >= 6-part repo route; router miss.
        _admin_user(mock_table)
        event = _make_event(
            "PUT", "/workspaces/w1/gitlab/repos/acme",
            body={"enabled": False},
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 404

    @patch.object(handler, "dual_write")
    def test_accepts_default_branch(self, mock_dual, mock_table):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_REPO#"):
                return {"Item": {
                    "slug": "acme/svc", "enabled": True,
                    "include_code_maps": True, "repo_type": "application",
                }}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event(
            "PUT", "/workspaces/w1/gitlab/repos/acme/svc",
            body={"default_branch": "develop"},
        )
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        call_kwargs = mock_dual.ddb_update_item.call_args.kwargs
        assert "default_branch = :db" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":db"] == "develop"


class TestDeleteRepo:

    @patch.object(handler, "dual_write")
    def test_drops_row(self, mock_dual, mock_table):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_REPO#"):
                return {"Item": {"slug": "acme/svc", "connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi

        event = _make_event("DELETE", "/workspaces/w1/gitlab/repos/acme/svc")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_dual.ddb_delete_item.assert_called_once()


# ---------------------------------------------------------------------------
# Disconnect cascade
# ---------------------------------------------------------------------------


class TestDisconnect:

    def test_404_when_connection_missing(self, mock_table):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {}
        mock_table.get_item.side_effect = gi
        event = _make_event(
            "DELETE", "/workspaces/w1/gitlab/connections/missing",
        )
        assert handler.lambda_handler(event, None)["statusCode"] == 404

    @patch.object(handler.gitlab_oauth, "delete_tokens")
    @patch.object(handler, "dual_write")
    def test_cascade_removes_tied_repos_and_tokens(
        self, mock_dual, mock_delete_tokens, mock_table,
    ):
        def gi(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("GITLAB_CONNECTION#"):
                return {"Item": {"connection_id": "c1"}}
            return {}
        mock_table.get_item.side_effect = gi
        # Two repos tied to c1; one tied to a different connection.
        mock_table.query.return_value = {
            "Items": [
                {"SK": "GITLAB_REPO#acme/a", "slug": "acme/a", "connection_id": "c1"},
                {"SK": "GITLAB_REPO#acme/b", "slug": "acme/b", "connection_id": "c1"},
                {"SK": "GITLAB_REPO#acme/c", "slug": "acme/c", "connection_id": "c2"},
            ]
        }

        event = _make_event("DELETE", "/workspaces/w1/gitlab/connections/c1")
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        # Three DDB deletes: 2 tied repos + 1 connection row.
        assert mock_dual.ddb_delete_item.call_count == 3
        # Token row deleted via the shared module (workspaces_table, connection_id).
        mock_delete_tokens.assert_called_once_with(handler.WORKSPACES_TABLE, "c1")


# ---------------------------------------------------------------------------
# Router 404 catch-all
# ---------------------------------------------------------------------------


class TestRouter404:

    def test_unknown_path_returns_404(self, mock_table):
        event = _make_event("GET", "/gitlab/nonsense")
        assert handler.lambda_handler(event, None)["statusCode"] == 404
