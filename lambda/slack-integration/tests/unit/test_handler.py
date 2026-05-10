"""Unit tests for slack-integration handler.py.

Covers helpers, auth, OAuth state single-use, all 8 routes, audit logs,
SLACK_ENABLED gate, sync engine, multi-team guard, security headers
regression, and user-cache resolution.
"""

import json
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import jwt
import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Helpers and JSON encoder
# ============================================================================


class TestDecimalEncoder:
    def test_encodes_integer_decimal(self):
        from handler import DecimalEncoder
        assert json.dumps({"n": Decimal("10")}, cls=DecimalEncoder) == '{"n": 10}'

    def test_encodes_float_decimal(self):
        from handler import DecimalEncoder
        assert json.dumps({"n": Decimal("10.5")}, cls=DecimalEncoder) == '{"n": 10.5}'


class TestResponseHelpers:
    def test_response_status_code(self):
        from handler import _response
        assert _response(204, {})["statusCode"] == 204

    def test_response_body_is_json(self):
        from handler import _response
        assert json.loads(_response(200, {"k": "v"})["body"]) == {"k": "v"}

    def test_error_returns_error_field(self):
        from handler import _error
        body = json.loads(_error(400, "bad")["body"])
        assert body["error"] == "bad"

    def test_redirect_sets_location_and_302(self):
        from handler import _redirect
        result = _redirect("https://example.com/x")
        assert result["statusCode"] == 302
        assert result["headers"]["Location"] == "https://example.com/x"


class TestAuthContext:
    def test_valid_bearer_returns_payload(self, mock_jwt_secret, jwt_secret):
        from handler import get_auth_context
        token = jwt.encode({"sub": "u1", "org_id": "o1", "exp": int(time.time()) + 3600},
                           jwt_secret, algorithm="HS256")
        assert get_auth_context({"headers": {"Authorization": f"Bearer {token}"}})["sub"] == "u1"

    def test_missing_auth_returns_none(self, mock_jwt_secret):
        from handler import get_auth_context
        assert get_auth_context({"headers": {}}) is None

    def test_invalid_jwt_returns_none(self, mock_jwt_secret):
        from handler import get_auth_context
        assert get_auth_context({"headers": {"Authorization": "Bearer junk"}}) is None

    def test_expired_jwt_returns_none(self, mock_jwt_secret, jwt_secret):
        from handler import get_auth_context
        token = jwt.encode({"sub": "u1", "exp": int(time.time()) - 100},
                           jwt_secret, algorithm="HS256")
        assert get_auth_context({"headers": {"Authorization": f"Bearer {token}"}}) is None


class TestRequireAuth:
    def test_valid_token_with_membership_passes(
        self, mock_dynamodb, mock_jwt_secret, admin_token, org_membership_admin
    ):
        from handler import require_auth
        mock_dynamodb.get_item.return_value = {"Item": org_membership_admin}
        ctx, err = require_auth({"headers": {"Authorization": f"Bearer {admin_token}"}})
        assert err is None
        assert ctx["org_role"] == "org_admin"

    def test_removed_user_rejected(self, mock_dynamodb, mock_jwt_secret, admin_token):
        from handler import require_auth
        mock_dynamodb.get_item.return_value = {}
        ctx, err = require_auth({"headers": {"Authorization": f"Bearer {admin_token}"}})
        assert ctx is None
        assert err["statusCode"] == 401

    def test_no_token_returns_401(self, mock_jwt_secret):
        from handler import require_auth
        ctx, err = require_auth({"headers": {}})
        assert err["statusCode"] == 401


class TestCheckWorkspaceAccess:
    def test_member_role_grants_member_access(self, mock_dynamodb):
        from handler import check_workspace_access
        mock_dynamodb.get_item.return_value = {"Item": {"role": "member"}}
        assert check_workspace_access({"sub": "u"}, "ws") is None

    def test_member_role_denied_when_admin_required(self, mock_dynamodb):
        from handler import check_workspace_access
        mock_dynamodb.get_item.return_value = {"Item": {"role": "member"}}
        result = check_workspace_access({"sub": "u"}, "ws", "workspace_admin")
        assert result["statusCode"] == 403

    def test_non_member_returns_403(self, mock_dynamodb):
        from handler import check_workspace_access
        mock_dynamodb.get_item.return_value = {}
        assert check_workspace_access({"sub": "u"}, "ws")["statusCode"] == 403


# ============================================================================
# OAuth state
# ============================================================================


class TestOAuthStateSingleUse:
    def test_claim_writes_dynamodb_row_with_condition(self, mock_dynamodb):
        from handler import _claim_oauth_state
        result = _claim_oauth_state("state-abc", {"workspace_id": "ws"})
        assert result is True
        call = mock_dynamodb.put_item.call_args.kwargs
        assert call["Item"]["PK"] == "SLACK_STATE#state-abc"
        assert call["Item"]["entity_type"] == "slack_oauth_state"
        assert call["ConditionExpression"] == "attribute_not_exists(PK)"

    def test_claim_returns_false_on_conditional_check_failure(self, mock_dynamodb):
        from handler import _claim_oauth_state
        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        assert _claim_oauth_state("state-abc", {}) is False

    def test_consume_returns_payload(self, mock_dynamodb):
        from handler import _consume_oauth_state
        mock_dynamodb.delete_item.return_value = {
            "Attributes": {"state_payload": {"workspace_id": "ws_1"},
                           "ttl": int(time.time()) + 600}
        }
        assert _consume_oauth_state("state-abc") == {"workspace_id": "ws_1"}

    def test_consume_returns_none_on_replay(self, mock_dynamodb):
        from handler import _consume_oauth_state
        mock_dynamodb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
            "DeleteItem",
        )
        assert _consume_oauth_state("state-abc") is None

    def test_consume_returns_none_on_expired_ttl(self, mock_dynamodb):
        from handler import _consume_oauth_state
        mock_dynamodb.delete_item.return_value = {
            "Attributes": {"state_payload": {"workspace_id": "ws_1"},
                           "ttl": int(time.time()) - 100}
        }
        assert _consume_oauth_state("state-abc") is None


# ============================================================================
# User cache + mention substitution
# ============================================================================


class TestUserCache:
    def test_resolve_user_hits_cache(self, mock_dynamodb):
        from handler import _resolve_user
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "display_name": "Alice",
                "ttl": int(time.time()) + 3600,
            }
        }
        with patch("handler.users_info") as mock_uinfo:
            name = _resolve_user("T01", "U01ABCDEF", "tok")
        assert name == "Alice"
        mock_uinfo.assert_not_called()

    def test_resolve_user_falls_back_to_users_info_on_miss(self, mock_dynamodb):
        from handler import _resolve_user
        mock_dynamodb.get_item.return_value = {}
        with patch("handler.users_info") as mock_uinfo:
            mock_uinfo.return_value = {
                "ok": True,
                "user": {"id": "U01ABCDEF", "profile": {"display_name": "Bob"}},
            }
            name = _resolve_user("T01", "U01ABCDEF", "tok")
        assert name == "Bob"
        mock_uinfo.assert_called_once()
        # Cache write happened too
        assert mock_dynamodb.put_item.called

    def test_resolve_user_returns_id_when_users_info_fails(self, mock_dynamodb):
        from handler import _resolve_user
        mock_dynamodb.get_item.return_value = {}
        with patch("handler.users_info", side_effect=ValueError("rate_limited")):
            name = _resolve_user("T01", "U01ABCDEF", "tok")
        assert name == "U01ABCDEF"

    def test_resolve_user_skips_stale_cache_entry(self, mock_dynamodb):
        from handler import _resolve_user
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "display_name": "Stale Name",
                "ttl": int(time.time()) - 100,  # expired
            }
        }
        with patch("handler.users_info") as mock_uinfo:
            mock_uinfo.return_value = {
                "ok": True,
                "user": {"id": "U01", "profile": {"display_name": "Fresh Name"}},
            }
            name = _resolve_user("T01", "U01", "tok")
        assert name == "Fresh Name"

    def test_substitute_mentions_replaces_user_id_tokens(self, mock_dynamodb):
        from handler import _substitute_mentions
        mock_dynamodb.get_item.return_value = {
            "Item": {"display_name": "Alice", "ttl": int(time.time()) + 3600}
        }
        out = _substitute_mentions("hey <@U01ABCDEF> can you review?", "T01", "tok")
        assert out == "hey @Alice can you review?"

    def test_substitute_mentions_handles_alt_pipe_form(self, mock_dynamodb):
        """Slack sometimes returns <@USER_ID|fallback_name>."""
        from handler import _substitute_mentions
        mock_dynamodb.get_item.return_value = {
            "Item": {"display_name": "Alice", "ttl": int(time.time()) + 3600}
        }
        out = _substitute_mentions("hey <@U01|alice> can you?", "T01", "tok")
        assert out == "hey @Alice can you?"


# ============================================================================
# Routes
# ============================================================================


class TestHandleAuthorize:
    def test_returns_auth_url_with_required_params(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, mock_token_manager,
        admin_token, org_membership_admin, workspace_membership_admin
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
        ]
        event = api_gateway_event(
            "GET", "/slack/authorize",
            headers={"Authorization": f"Bearer {admin_token}"},
            query_params={"workspace_id": "ws_test123"},
        )
        response = handler(event, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "auth_url" in body
        assert "client_id=test-client-id" in body["auth_url"]
        assert "state=" in body["auth_url"]
        # Bot scopes are URL-encoded with comma-encoded as %2C
        assert "channels%3Ahistory" in body["auth_url"]
        assert "users%3Aread" in body["auth_url"]

    def test_missing_workspace_id_returns_400(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, admin_token,
        org_membership_admin
    ):
        from handler import handler
        mock_dynamodb.get_item.return_value = {"Item": org_membership_admin}
        event = api_gateway_event(
            "GET", "/slack/authorize",
            headers={"Authorization": f"Bearer {admin_token}"},
            query_params={},
        )
        assert handler(event, None)["statusCode"] == 400

    def test_member_cannot_authorize(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, member_token,
        workspace_membership_member
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {"Item": workspace_membership_member},
        ]
        event = api_gateway_event(
            "GET", "/slack/authorize",
            headers={"Authorization": f"Bearer {member_token}"},
            query_params={"workspace_id": "ws_test123"},
        )
        assert handler(event, None)["statusCode"] == 403

    def test_unauthenticated_returns_401(self, api_gateway_event, mock_jwt_secret):
        from handler import handler
        event = api_gateway_event("GET", "/slack/authorize", query_params={"workspace_id": "ws"})
        assert handler(event, None)["statusCode"] == 401

    def test_writes_audit_log(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, mock_token_manager,
        admin_token, org_membership_admin, workspace_membership_admin
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
        ]
        event = api_gateway_event(
            "GET", "/slack/authorize",
            headers={"Authorization": f"Bearer {admin_token}"},
            query_params={"workspace_id": "ws_test123"},
        )
        handler(event, None)
        actions = [
            c.kwargs["Item"].get("action")
            for c in mock_dynamodb.put_item.call_args_list
            if "action" in c.kwargs.get("Item", {})
        ]
        assert "slack.connect.start" in actions


class TestHandleCallback:
    def _state_in_db(self, mock_dynamodb, payload, ttl_offset=600):
        mock_dynamodb.delete_item.return_value = {
            "Attributes": {"state_payload": payload,
                           "ttl": int(time.time()) + ttl_offset}
        }

    def test_callback_redirects_to_configure_on_success(
        self, api_gateway_event, mock_dynamodb, mock_token_manager, mock_slack_api
    ):
        from handler import handler
        self._state_in_db(mock_dynamodb, {
            "org_id": "org_1", "user_id": "user_1", "workspace_id": "ws_1"
        })
        mock_dynamodb.query.return_value = {"Items": []}
        event = api_gateway_event(
            "GET", "/slack/callback",
            query_params={"code": "abc", "state": "s"},
        )
        response = handler(event, None)
        assert response["statusCode"] == 302
        location = response["headers"]["Location"]
        assert "/slack/configure" in location
        assert "workspace_id=ws_1" in location
        assert "connection_id=" in location

    def test_callback_provider_error_redirects(self, api_gateway_event):
        from handler import handler
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"error": "access_denied"})
        response = handler(event, None)
        assert response["statusCode"] == 302
        assert "slack_error=access_denied" in response["headers"]["Location"]

    def test_callback_missing_code_or_state_redirects(self, api_gateway_event):
        from handler import handler
        event = api_gateway_event("GET", "/slack/callback", query_params={})
        response = handler(event, None)
        assert response["statusCode"] == 302
        assert "missing_params" in response["headers"]["Location"]

    def test_callback_replayed_state_redirects_invalid_state(
        self, api_gateway_event, mock_dynamodb
    ):
        from handler import handler
        mock_dynamodb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "missing"}},
            "DeleteItem",
        )
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"code": "abc", "state": "replayed"})
        response = handler(event, None)
        assert response["statusCode"] == 302
        assert "invalid_state" in response["headers"]["Location"]

    def test_callback_no_bot_token_redirects_with_error(
        self, api_gateway_event, mock_dynamodb, mock_token_manager, mock_slack_api
    ):
        from handler import handler
        self._state_in_db(mock_dynamodb, {
            "org_id": "o", "user_id": "u", "workspace_id": "w"
        })
        mock_slack_api["oauth_v2_access"].return_value = {
            "ok": True,
            # no access_token
            "team": {"id": "T1", "name": "Test"},
        }
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"code": "abc", "state": "s"})
        response = handler(event, None)
        assert response["statusCode"] == 302
        assert "no_bot_token" in response["headers"]["Location"]

    def test_callback_creates_connection_when_none_exists(
        self, api_gateway_event, mock_dynamodb, mock_token_manager, mock_slack_api
    ):
        from handler import handler
        self._state_in_db(mock_dynamodb, {
            "org_id": "org_1", "user_id": "user_1", "workspace_id": "ws_1"
        })
        mock_dynamodb.query.return_value = {"Items": []}
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"code": "abc", "state": "s"})
        handler(event, None)
        connection_puts = [
            c for c in mock_dynamodb.put_item.call_args_list
            if c.kwargs["Item"].get("entity_type") == "slack_connection"
        ]
        assert len(connection_puts) == 1
        item = connection_puts[0].kwargs["Item"]
        assert item["team_id"] == "T01TEST"
        assert item["last_sync_status"] == "needs_configuration"

    def test_callback_refuses_different_team(
        self, api_gateway_event, mock_dynamodb, mock_token_manager, mock_slack_api,
        configured_connection
    ):
        """Per open-questions.md #4 — switching teams requires a clean disconnect."""
        from handler import handler
        self._state_in_db(mock_dynamodb, {
            "org_id": "org_xyz789", "user_id": "user_abc123", "workspace_id": "ws_test123"
        })
        mock_dynamodb.query.return_value = {"Items": [configured_connection]}
        # New OAuth says different team
        mock_slack_api["oauth_v2_access"].return_value = {
            "ok": True,
            "access_token": "xoxb-new",
            "team": {"id": "T_DIFFERENT", "name": "Other Workspace"},
            "bot_user_id": "U02",
        }
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"code": "abc", "state": "s"})
        response = handler(event, None)
        assert response["statusCode"] == 302
        assert "different_team_already_connected" in response["headers"]["Location"]
        mock_token_manager["save_tokens"].assert_not_called()

    def test_callback_seeds_user_cache(
        self, api_gateway_event, mock_dynamodb, mock_token_manager, mock_slack_api
    ):
        """After token storage, _populate_user_cache iterates users.list and
        writes one cache row per user."""
        from handler import handler
        self._state_in_db(mock_dynamodb, {
            "org_id": "o", "user_id": "u", "workspace_id": "w"
        })
        mock_dynamodb.query.return_value = {"Items": []}
        mock_slack_api["iterate_users"].return_value = iter([
            {"id": "U01", "name": "alice", "profile": {"display_name": "Alice"}},
            {"id": "U02", "name": "bob", "profile": {"display_name": "Bob"}},
        ])
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"code": "abc", "state": "s"})
        handler(event, None)
        cache_puts = [
            c for c in mock_dynamodb.put_item.call_args_list
            if c.kwargs["Item"].get("entity_type") == "slack_user_cache"
        ]
        assert len(cache_puts) == 2
        assert {p.kwargs["Item"]["user_id"] for p in cache_puts} == {"U01", "U02"}

    def test_callback_user_cache_seed_failure_is_non_fatal(
        self, api_gateway_event, mock_dynamodb, mock_token_manager, mock_slack_api
    ):
        from handler import handler
        self._state_in_db(mock_dynamodb, {
            "org_id": "o", "user_id": "u", "workspace_id": "w"
        })
        mock_dynamodb.query.return_value = {"Items": []}
        mock_slack_api["iterate_users"].side_effect = ValueError("Rate limited")
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"code": "abc", "state": "s"})
        # Must not raise; callback still completes successfully
        response = handler(event, None)
        assert response["statusCode"] == 302
        assert "/slack/configure" in response["headers"]["Location"]


class TestHandleListConnections:
    def test_returns_connections(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret,
        admin_token, org_membership_admin, workspace_membership_admin,
        configured_connection
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
        ]
        mock_dynamodb.query.return_value = {"Items": [configured_connection]}
        event = api_gateway_event(
            "GET", "/workspace/ws_test123/slack/connections",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123"},
        )
        response = handler(event, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["connections"]) == 1
        assert body["connections"][0]["team_name"] == "Test Workspace"

    def test_response_excludes_encrypted_fields(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret,
        admin_token, org_membership_admin, workspace_membership_admin,
        configured_connection
    ):
        from handler import handler
        connection_with_token = dict(configured_connection)
        connection_with_token["encrypted_bot_token"] = "SHOULD_NOT_LEAK"
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
        ]
        mock_dynamodb.query.return_value = {"Items": [connection_with_token]}
        event = api_gateway_event(
            "GET", "/workspace/ws_test123/slack/connections",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123"},
        )
        response = handler(event, None)
        assert "SHOULD_NOT_LEAK" not in response["body"]


class TestHandleListChannels:
    def test_returns_member_channels(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, mock_token_manager,
        mock_slack_api, admin_token, org_membership_admin,
        workspace_membership_admin, unconfigured_connection
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
            {"Item": unconfigured_connection},
        ]
        mock_slack_api["iterate_member_channels"].return_value = iter([
            {"id": "C0123", "name": "engineering", "is_private": False, "num_members": 12},
            {"id": "C0456", "name": "leadership", "is_private": True, "num_members": 4},
        ])
        event = api_gateway_event(
            "GET", "/workspace/ws_test123/slack/connections/slack_abc123/channels",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        response = handler(event, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert {c["channel_id"] for c in body["channels"]} == {"C0123", "C0456"}

    def test_returns_previously_saved_selection_under_selected_channels_key(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, mock_token_manager,
        mock_slack_api, admin_token, org_membership_admin,
        workspace_membership_admin, configured_connection
    ):
        """When the user reopens Configure, the picker must restore the
        channels they previously saved. Field name is `selected_channels`
        (matches api.ts contract and the Clio `selected_resource_types`
        convention). Regression for the bug where the field was named
        `selected_channel_ids` and the UI silently fell back to an
        empty Set."""
        from handler import handler
        # configured_connection has include_channels=["C0123456"]
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
            {"Item": configured_connection},
        ]
        mock_slack_api["iterate_member_channels"].return_value = iter([
            {"id": "C0123456", "name": "engineering", "is_private": False, "num_members": 12},
            {"id": "C0NEW", "name": "newly-invited", "is_private": False, "num_members": 4},
        ])
        event = api_gateway_event(
            "GET", "/workspace/ws_test123/slack/connections/slack_abc123/channels",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        response = handler(event, None)
        body = json.loads(response["body"])
        assert "selected_channels" in body, (
            "Response field MUST be `selected_channels` — UI reads this exact key. "
            "If renamed, the picker silently forgets prior selections."
        )
        assert "selected_channel_ids" not in body
        assert body["selected_channels"] == ["C0123456"]

    def test_member_cannot_list_channels(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, member_token,
        workspace_membership_member
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {"Item": workspace_membership_member},
        ]
        event = api_gateway_event(
            "GET", "/workspace/ws_test123/slack/connections/slack_abc123/channels",
            headers={"Authorization": f"Bearer {member_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        assert handler(event, None)["statusCode"] == 403


class TestHandleSaveConfig:
    def test_saves_and_queues_sync(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, mock_sqs_client,
        admin_token, org_membership_admin, workspace_membership_admin,
        configured_connection
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
            {"Item": configured_connection},
        ]
        event = api_gateway_event(
            "PUT", "/workspace/ws_test123/slack/connections/slack_abc123/config",
            body={"include_channels": ["C0123", "C0456"]},
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        response = handler(event, None)
        assert response["statusCode"] == 200
        assert mock_sqs_client.send_message.called

    def test_empty_channels_returns_400(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, admin_token,
        org_membership_admin, workspace_membership_admin
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
        ]
        event = api_gateway_event(
            "PUT", "/workspace/ws_test123/slack/connections/slack_abc123/config",
            body={"include_channels": []},
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        assert handler(event, None)["statusCode"] == 400


class TestHandleDisconnect:
    def test_disconnect_removes_and_queues_delete(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, mock_token_manager,
        mock_sqs_client, admin_token, org_membership_admin,
        workspace_membership_admin, configured_connection
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
            {"Item": configured_connection},
        ]
        event = api_gateway_event(
            "DELETE", "/workspace/ws_test123/slack/connections/slack_abc123",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        response = handler(event, None)
        assert response["statusCode"] == 200
        assert mock_token_manager["delete_tokens"].called
        assert mock_dynamodb.delete_item.called
        assert mock_sqs_client.send_message.called


class TestHandleManualSync:
    def test_queues_sync(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, mock_sqs_client,
        admin_token, org_membership_admin, workspace_membership_admin,
        configured_connection
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
            {"Item": configured_connection},
        ]
        event = api_gateway_event(
            "POST", "/workspace/ws_test123/slack/connections/slack_abc123/sync",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        response = handler(event, None)
        assert response["statusCode"] == 200
        assert mock_sqs_client.send_message.called

    def test_no_channels_returns_400(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, admin_token,
        org_membership_admin, workspace_membership_admin, unconfigured_connection
    ):
        from handler import handler
        mock_dynamodb.get_item.side_effect = [
            {"Item": org_membership_admin},
            {"Item": workspace_membership_admin},
            {"Item": unconfigured_connection},
        ]
        event = api_gateway_event(
            "POST", "/workspace/ws_test123/slack/connections/slack_abc123/sync",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "ws_test123", "connectionId": "slack_abc123"},
        )
        assert handler(event, None)["statusCode"] == 400


# ============================================================================
# SQS sync engine
# ============================================================================


class TestSyncConnection:
    def test_full_sync_enqueues_messages_and_files(
        self, sqs_event, mock_dynamodb, mock_token_manager, mock_slack_api,
        mock_sqs_client
    ):
        from handler import handler
        mock_slack_api["iterate_member_channels"].return_value = iter([
            {"id": "C0123", "name": "engineering", "is_member": True}
        ])
        mock_slack_api["iterate_channel_messages"].return_value = iter([
            {
                "ts": "1714530000.000100",
                "user": "U01",
                "text": "hello world",
                "files": [{"id": "F01", "name": "spec.pdf",
                           "url_private_download": "https://files.slack.com/x"}],
            },
        ])
        # User cache hit so resolve doesn't call users.info
        mock_dynamodb.get_item.return_value = {
            "Item": {"display_name": "Alice", "ttl": int(time.time()) + 3600}
        }
        event = sqs_event([{
            "connection_id": "c1", "workspace_id": "w1", "org_id": "o1",
            "team_id": "T01TEST", "include_channels": ["C0123"], "cursors": {},
        }])
        response = handler(event, None)
        assert response["statusCode"] == 200
        # 1 slack_message + 1 slack_file = 2 SQS sends to FILE_INGESTION_QUEUE
        ingestion_calls = [
            c for c in mock_sqs_client.send_message.call_args_list
            if "slack_" in c.kwargs.get("MessageBody", "")
        ]
        assert len(ingestion_calls) == 2
        sources = [json.loads(c.kwargs["MessageBody"])["source"] for c in ingestion_calls]
        assert "slack_message" in sources
        assert "slack_file" in sources

    def test_delta_sync_passes_oldest_cursor(
        self, sqs_event, mock_dynamodb, mock_token_manager, mock_slack_api,
        mock_sqs_client
    ):
        from handler import handler
        mock_slack_api["iterate_member_channels"].return_value = iter([])
        mock_slack_api["iterate_channel_messages"].return_value = iter([])
        event = sqs_event([{
            "connection_id": "c1", "workspace_id": "w1", "org_id": "o1",
            "team_id": "T01TEST", "include_channels": ["C0123"],
            "cursors": {"C0123": "1714000000.000000"},
        }])
        handler(event, None)
        mock_slack_api["iterate_channel_messages"].assert_called_with(
            "xoxb-test-bot-token", "C0123", oldest="1714000000.000000"
        )

    def test_delta_sync_increments_file_count_atomically(
        self, sqs_event, mock_dynamodb, mock_token_manager, mock_slack_api,
        mock_sqs_client
    ):
        """Delta sync ADDs to file_count instead of overwriting. Mirror of
        the Clio + Dropbox fix."""
        from handler import handler
        mock_slack_api["iterate_member_channels"].return_value = iter([])
        mock_slack_api["iterate_channel_messages"].return_value = iter([
            {"ts": "1714530000.000100", "user": "U01", "text": "x"},
        ])
        mock_dynamodb.get_item.return_value = {
            "Item": {"display_name": "Alice", "ttl": int(time.time()) + 3600}
        }
        event = sqs_event([{
            "connection_id": "c1", "workspace_id": "w1", "org_id": "o1",
            "team_id": "T01TEST", "include_channels": ["C0123"],
            "cursors": {"C0123": "100"},
        }])
        handler(event, None)
        adjust_calls = [
            c for c in mock_dynamodb.update_item.call_args_list
            if "ADD file_count" in c.kwargs.get("UpdateExpression", "")
        ]
        assert len(adjust_calls) == 1
        assert adjust_calls[0].kwargs["ExpressionAttributeValues"][":delta"] == 1

    def test_full_sync_sets_file_count_via_set_not_add(
        self, sqs_event, mock_dynamodb, mock_token_manager, mock_slack_api,
        mock_sqs_client
    ):
        from handler import handler
        mock_slack_api["iterate_member_channels"].return_value = iter([])
        mock_slack_api["iterate_channel_messages"].return_value = iter([
            {"ts": "1.0", "user": "U01", "text": "a"},
            {"ts": "2.0", "user": "U01", "text": "b"},
        ])
        mock_dynamodb.get_item.return_value = {
            "Item": {"display_name": "Alice", "ttl": int(time.time()) + 3600}
        }
        event = sqs_event([{
            "connection_id": "c1", "workspace_id": "w1", "org_id": "o1",
            "team_id": "T01TEST", "include_channels": ["C0123"], "cursors": {},
        }])
        handler(event, None)
        adjust_calls = [
            c for c in mock_dynamodb.update_item.call_args_list
            if "ADD file_count" in c.kwargs.get("UpdateExpression", "")
        ]
        assert adjust_calls == []
        success_update = next(
            c for c in mock_dynamodb.update_item.call_args_list
            if "last_sync_status = :status" in c.kwargs.get("UpdateExpression", "")
            and "file_count = :files" in c.kwargs.get("UpdateExpression", "")
        )
        assert success_update.kwargs["ExpressionAttributeValues"][":files"] == 2


# ============================================================================
# Feature gate
# ============================================================================


class TestSlackEnabledGate:
    def test_disabled_returns_404_for_authorize(self, api_gateway_event, monkeypatch):
        from handler import handler
        monkeypatch.setenv("SLACK_ENABLED", "false")
        event = api_gateway_event("GET", "/slack/authorize", query_params={"workspace_id": "ws"})
        assert handler(event, None)["statusCode"] == 404

    def test_disabled_returns_404_for_callback(self, api_gateway_event, monkeypatch):
        from handler import handler
        monkeypatch.setenv("SLACK_ENABLED", "false")
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"code": "x", "state": "y"})
        assert handler(event, None)["statusCode"] == 404

    def test_disabled_returns_404_for_workspace_routes(
        self, api_gateway_event, monkeypatch, admin_token
    ):
        from handler import handler
        monkeypatch.setenv("SLACK_ENABLED", "false")
        event = api_gateway_event(
            "GET", "/workspace/w/slack/connections",
            headers={"Authorization": f"Bearer {admin_token}"},
            path_params={"id": "w"},
        )
        assert handler(event, None)["statusCode"] == 404

    def test_disabled_drops_sqs_message_silently(self, sqs_event, monkeypatch):
        from handler import handler
        monkeypatch.setenv("SLACK_ENABLED", "false")
        event = sqs_event([{"connection_id": "c"}])
        response = handler(event, None)
        assert response["statusCode"] == 200


# ============================================================================
# Routing / 404
# ============================================================================


class TestRouting:
    def test_unknown_route_returns_404(
        self, api_gateway_event, mock_dynamodb, mock_jwt_secret, admin_token,
        org_membership_admin
    ):
        from handler import handler
        mock_dynamodb.get_item.return_value = {"Item": org_membership_admin}
        event = api_gateway_event(
            "GET", "/slack/unknown",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert handler(event, None)["statusCode"] == 404

    def test_unauthenticated_workspace_route_returns_401(self, api_gateway_event):
        from handler import handler
        event = api_gateway_event(
            "GET", "/workspace/w/slack/connections", path_params={"id": "w"},
        )
        assert handler(event, None)["statusCode"] == 401


# ============================================================================
# Security headers regression
# ============================================================================


class TestSecurityHeadersRegression:
    REQUIRED_HEADERS = {
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
    }

    def _assert_headers(self, response):
        for header, expected in self.REQUIRED_HEADERS.items():
            assert response["headers"].get(header) == expected, (
                f"{header} missing or wrong on status {response['statusCode']}"
            )

    def test_response_helper_includes_all_five(self):
        from handler import _response
        self._assert_headers(_response(200, {"ok": True}))

    def test_error_helper_includes_all_five(self):
        from handler import _error
        self._assert_headers(_error(400, "bad"))

    def test_redirect_helper_includes_all_five(self):
        from handler import _redirect
        self._assert_headers(_redirect("https://example.com"))

    def test_401_unauthenticated_response_has_headers(self, api_gateway_event):
        from handler import handler
        event = api_gateway_event("GET", "/workspace/w/slack/connections",
                                  path_params={"id": "w"})
        response = handler(event, None)
        assert response["statusCode"] == 401
        self._assert_headers(response)

    def test_disabled_404_response_has_headers(self, api_gateway_event, monkeypatch):
        from handler import handler
        monkeypatch.setenv("SLACK_ENABLED", "false")
        event = api_gateway_event("GET", "/slack/authorize",
                                  query_params={"workspace_id": "w"})
        response = handler(event, None)
        assert response["statusCode"] == 404
        self._assert_headers(response)

    def test_callback_redirect_has_headers(self, api_gateway_event):
        from handler import handler
        event = api_gateway_event("GET", "/slack/callback",
                                  query_params={"error": "denied"})
        response = handler(event, None)
        assert response["statusCode"] == 302
        self._assert_headers(response)
