# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for teams-integration Lambda (handler, teams_api, token_manager)."""

import base64
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

def _load_module(mod_name, filename, directory, also_register_as=None):
    """Load a module from teams-integration by file path."""
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


# Register sibling modules under their import names so handler.py resolves them
token_module = _load_module(
    "teams_int_token_manager", "token_manager.py", "../../teams-integration",
    also_register_as="token_manager",
)

# Shared Entra ID OAuth helper (ADR-035):
# token_manager.py is now a thin wrapper. Tests patch internals
# (urllib, _get_dynamodb, _get_kms, module-level cache globals)
# on the SHARED module, not the wrapper.
import shared.entra_id_oauth as shared_oauth  # noqa: E402
api_module = _load_module(
    "teams_int_teams_api", "teams_api.py", "../../teams-integration",
    also_register_as="teams_api",
)
handler_module = _load_module(
    "teams_int_handler", "handler.py", "../../teams-integration"
)


# ============================================================================
# Helpers
# ============================================================================

def _make_urlopen_response(body_dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ============================================================================
# Token Manager Tests
# ============================================================================

class TestTeamsTokenManagerEncryptDecrypt:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        shared_oauth._dynamodb = None
        shared_oauth._kms = None
        yield

    @patch.object(shared_oauth, "_get_kms")
    def test_encrypt_token(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.encrypt.return_value = {"CiphertextBlob": b"encrypted"}
        mock_kms_fn.return_value = mock_kms

        result = shared_oauth._encrypt_token("my_token", "test-kms-key")
        assert isinstance(result, str)
        assert base64.b64decode(result) == b"encrypted"

    @patch.object(shared_oauth, "_get_kms")
    def test_decrypt_token(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.decrypt.return_value = {"Plaintext": b"decrypted"}
        mock_kms_fn.return_value = mock_kms

        encrypted = base64.b64encode(b"cipher").decode("utf-8")
        result = shared_oauth._decrypt_token(encrypted)
        assert result == "decrypted"


class TestTeamsTokenManagerSaveTokens:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        shared_oauth._dynamodb = None
        shared_oauth._kms = None
        yield

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "encrypted_refresh"

        result = token_module.save_tokens("conn1", "access", "refresh", 3600)
        assert result is True
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "TEAMS_TOKEN#conn1"
        assert item["SK"] == "CREDENTIALS"
        assert item["encrypted_refresh_token"] == "encrypted_refresh"

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_error(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "enc"
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "PutItem"
        )
        result = token_module.save_tokens("conn1", "access", "refresh")
        assert result is False


class TestTeamsTokenManagerGetRefreshToken:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        shared_oauth._dynamodb = None
        shared_oauth._kms = None
        yield

    @patch.object(shared_oauth, "_decrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "enc_val"}
        }
        mock_decrypt.return_value = "plain_token"

        result = token_module.get_refresh_token("conn1")
        assert result == "plain_token"

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_not_found(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {}

        result = token_module.get_refresh_token("conn1")
        assert result is None

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_no_encrypted_field(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {"Item": {}}

        result = token_module.get_refresh_token("conn1")
        assert result is None

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_dynamodb_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "GetItem"
        )
        result = token_module.get_refresh_token("conn1")
        assert result is None


class TestTeamsTokenManagerRefreshAccessToken:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        shared_oauth._dynamodb = None
        shared_oauth._kms = None
        yield

    @patch.object(shared_oauth, "save_tokens")
    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_success(self, mock_get_refresh, mock_urlopen, mock_save):
        mock_get_refresh.return_value = "old_refresh"
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        })
        result = token_module.refresh_access_token("conn1", "cid", "csecret")
        assert result == "new_access"
        mock_save.assert_called_once()

    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_no_stored_token(self, mock_get_refresh):
        mock_get_refresh.return_value = None
        result = token_module.refresh_access_token("conn1", "cid", "csecret")
        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_http_error(self, mock_get_refresh, mock_urlopen):
        mock_get_refresh.return_value = "refresh"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=400, msg="Bad",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'{"error":"invalid_grant"}')),
        )
        result = token_module.refresh_access_token("conn1", "cid", "csecret")
        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_url_error(self, mock_get_refresh, mock_urlopen):
        mock_get_refresh.return_value = "refresh"
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = token_module.refresh_access_token("conn1", "cid", "csecret")
        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_no_access_in_response(self, mock_get_refresh, mock_urlopen):
        mock_get_refresh.return_value = "refresh"
        mock_urlopen.return_value = _make_urlopen_response({"error": "something"})
        result = token_module.refresh_access_token("conn1", "cid", "csecret")
        assert result is None


class TestTeamsTokenManagerDeleteTokens:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        shared_oauth._dynamodb = None
        shared_oauth._kms = None
        yield

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_delete_success(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = token_module.delete_tokens("conn1")
        assert result is True
        mock_table.delete_item.assert_called_once_with(
            Key={"PK": "TEAMS_TOKEN#conn1", "SK": "CREDENTIALS"}
        )

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_delete_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "DeleteItem"
        )
        result = token_module.delete_tokens("conn1")
        assert result is False


class TestTeamsTokenManagerExchangeCode:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        shared_oauth._dynamodb = None
        shared_oauth._kms = None
        yield

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        })
        access, refresh, expires = token_module.exchange_code_for_tokens(
            "code", "cid", "csecret", "https://example.com/cb"
        )
        assert access == "at"
        assert refresh == "rt"
        assert expires == 3600

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=400, msg="Bad",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'error')),
        )
        access, refresh, expires = token_module.exchange_code_for_tokens(
            "code", "cid", "csecret", "https://example.com/cb"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_url_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        access, refresh, expires = token_module.exchange_code_for_tokens(
            "code", "cid", "csecret", "https://example.com/cb"
        )
        assert access is None


# ============================================================================
# Teams API Tests
# ============================================================================

class TestTeamsAPI:

    @patch("teams_int_teams_api.urllib.request.urlopen")
    def test_make_request_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"id": "123"})
        result = api_module._make_request("token", "https://graph.microsoft.com/v1.0/me")
        assert result["id"] == "123"

    @patch("teams_int_teams_api.urllib.request.urlopen")
    def test_make_request_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://graph.microsoft.com", code=401, msg="Unauthorized",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'unauthorized')),
        )
        with pytest.raises(api_module.TeamsAuthError):
            api_module._make_request("token", "https://graph.microsoft.com/v1.0/me")

    @patch("teams_int_teams_api.urllib.request.urlopen")
    def test_make_request_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://graph.microsoft.com", code=500, msg="Server Error",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'error')),
        )
        with pytest.raises(api_module.TeamsAPIError):
            api_module._make_request("token", "https://graph.microsoft.com/v1.0/me")

    @patch("teams_int_teams_api.urllib.request.urlopen")
    def test_make_request_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with pytest.raises(api_module.TeamsAPIError):
            api_module._make_request("token", "https://graph.microsoft.com/v1.0/me")

    @patch.object(api_module, "_make_request")
    def test_get_user_profile(self, mock_req):
        mock_req.return_value = {"displayName": "Test User"}
        result = api_module.get_user_profile("token")
        assert result["displayName"] == "Test User"

    @patch.object(api_module, "_make_request")
    def test_list_joined_teams(self, mock_req):
        mock_req.return_value = {"value": [{"id": "t1"}]}
        result = api_module.list_joined_teams("token")
        assert len(result) == 1

    @patch.object(api_module, "_make_request")
    def test_list_joined_teams_pagination(self, mock_req):
        mock_req.side_effect = [
            {"value": [{"id": "t1"}], "@odata.nextLink": "https://next"},
            {"value": [{"id": "t2"}]},
        ]
        result = api_module.list_joined_teams("token")
        assert len(result) == 2

    @patch.object(api_module, "_make_request")
    def test_list_channels(self, mock_req):
        mock_req.return_value = {"value": [{"id": "c1", "displayName": "General"}]}
        result = api_module.list_channels("token", "team1")
        assert len(result) == 1

    @patch.object(api_module, "_make_request")
    def test_get_channel_messages(self, mock_req):
        mock_req.return_value = {"value": [{"id": "m1"}], "@odata.nextLink": "https://next"}
        messages, next_link = api_module.get_channel_messages("token", "t1", "c1")
        assert len(messages) == 1
        assert next_link == "https://next"

    @patch.object(api_module, "_make_request")
    def test_get_channel_messages_with_skip_token(self, mock_req):
        mock_req.return_value = {"value": [{"id": "m2"}]}
        messages, next_link = api_module.get_channel_messages(
            "token", "t1", "c1", skip_token="https://skip"
        )
        assert len(messages) == 1
        mock_req.assert_called_with("token", "https://skip")

    @patch.object(api_module, "_make_request")
    def test_get_message_replies(self, mock_req):
        mock_req.return_value = {"value": [{"id": "r1"}]}
        result = api_module.get_message_replies("token", "t1", "c1", "m1")
        assert len(result) == 1

    @patch.object(api_module, "_make_request")
    def test_get_messages_delta(self, mock_req):
        mock_req.return_value = {
            "value": [{"id": "m1"}, {"id": "m2", "@removed": {}}],
            "@odata.deltaLink": "https://delta",
        }
        result = api_module.get_messages_delta("token", "t1", "c1")
        assert len(result["messages"]) == 1
        assert len(result["deleted_ids"]) == 1
        assert result["delta_link"] == "https://delta"

    @patch.object(api_module, "_make_request")
    def test_get_messages_delta_next_link(self, mock_req):
        mock_req.return_value = {
            "value": [{"id": "m1"}],
            "@odata.nextLink": "https://next",
        }
        result = api_module.get_messages_delta("token", "t1", "c1")
        assert result["next_link"] == "https://next"
        assert result["delta_link"] is None

    @patch.object(api_module, "_make_request")
    def test_get_channel_files_folder(self, mock_req):
        mock_req.return_value = {"id": "folder1"}
        result = api_module.get_channel_files_folder("token", "t1", "c1")
        assert result["id"] == "folder1"

    @patch.object(api_module, "_make_request")
    def test_get_channel_files_folder_error(self, mock_req):
        mock_req.side_effect = api_module.TeamsAPIError("error")
        result = api_module.get_channel_files_folder("token", "t1", "c1")
        assert result is None

    @patch.object(api_module, "_make_request")
    def test_list_folder_files(self, mock_req):
        mock_req.return_value = {"value": [{"id": "f1", "name": "file.txt"}]}
        result = api_module.list_folder_files("token", "drive1", "folder1")
        assert len(result) == 1

    def test_get_days_back_filter(self):
        result = api_module.get_days_back_filter(30)
        assert isinstance(result, str)

    def test_filter_messages_by_date(self):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        old_iso = "2020-01-01T00:00:00+00:00"
        messages = [
            {"createdDateTime": now_iso},
            {"createdDateTime": old_iso},
            {"createdDateTime": ""},
            {},
        ]
        result = api_module.filter_messages_by_date(messages, 30)
        assert len(result) == 3

    def test_filter_messages_invalid_date(self):
        messages = [{"createdDateTime": "not-a-date"}]
        result = api_module.filter_messages_by_date(messages, 30)
        assert len(result) == 1

    @patch("teams_int_teams_api.urllib.request.urlopen")
    def test_download_file(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = b"file_content"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        result = api_module.download_file("token", "https://download.url")
        assert result == b"file_content"

    @patch("teams_int_teams_api.urllib.request.urlopen")
    def test_download_file_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://download.url", code=404, msg="Not Found",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'')),
        )
        with pytest.raises(api_module.TeamsAPIError):
            api_module.download_file("token", "https://download.url")


# ============================================================================
# Handler Tests
# ============================================================================

class TestTeamsIntegrationHandlerHelpers:

    def test_response(self):
        result = handler_module._response(200, {"key": "val"})
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["key"] == "val"

    def test_error(self):
        result = handler_module._error(400, "bad request")
        assert result["statusCode"] == 400
        assert "bad request" in json.loads(result["body"])["error"]

    def test_redirect(self):
        result = handler_module._redirect("https://example.com")
        assert result["statusCode"] == 302
        assert result["headers"]["Location"] == "https://example.com"

    def test_get_user_from_request(self):
        event = {"headers": {"x-user-email": "user@test.com"}}
        assert handler_module._get_user_from_request(event) == "user@test.com"

    def test_get_user_from_request_no_headers(self):
        assert handler_module._get_user_from_request({}) is None

    def test_parse_path(self):
        assert handler_module._parse_path("/workspaces/ws1/teams/connections") == (
            "workspaces", "ws1", "teams", "connections"
        )

    def test_parse_path_trailing_slash(self):
        assert handler_module._parse_path("/a/b/") == ("a", "b")

    def test_generate_id(self):
        id1 = handler_module.generate_id()
        id2 = handler_module.generate_id()
        assert isinstance(id1, str)
        assert id1 != id2


class TestTeamsIntegrationHandlerRouting:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        handler_module._oauth_config = None
        handler_module._dynamodb = None
        handler_module._ssm = None
        handler_module._sqs = None
        yield

    def test_not_found(self):
        event = {
            "httpMethod": "GET",
            "path": "/unknown/route",
            "headers": {"x-user-email": "user@test.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_auth_required(self):
        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/teams/connections",
            "headers": {},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 401

    @patch.object(handler_module, "list_connections")
    def test_list_connections_route(self, mock_list):
        mock_list.return_value = handler_module._response(200, {"connections": []})
        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/teams/connections",
            "headers": {"x-user-email": "user@test.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_list.assert_called_once_with("user@test.com", "ws1")

    @patch.object(handler_module, "handle_callback")
    def test_callback_route(self, mock_callback):
        mock_callback.return_value = handler_module._response(200, {"redirect": "/ok"})
        event = {
            "httpMethod": "GET",
            "path": "/teams/callback",
            "queryStringParameters": {"code": "abc", "state": "{}"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch.object(handler_module, "delete_connection")
    def test_delete_connection_route(self, mock_delete):
        mock_delete.return_value = handler_module._response(200, {"message": "deleted"})
        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/teams/connections/conn1",
            "headers": {"x-user-email": "user@test.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_delete.assert_called_once_with("user@test.com", "ws1", "conn1")

    @patch.object(handler_module, "save_config")
    def test_save_config_route(self, mock_save):
        mock_save.return_value = handler_module._response(200, {"message": "ok"})
        event = {
            "httpMethod": "PUT",
            "path": "/workspaces/ws1/teams/connections/conn1/config",
            "headers": {"x-user-email": "user@test.com"},
            "body": json.dumps({"days_back": 30}),
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch.object(handler_module, "trigger_sync")
    def test_trigger_sync_route(self, mock_sync):
        mock_sync.return_value = handler_module._response(200, {"message": "ok"})
        event = {
            "httpMethod": "POST",
            "path": "/workspaces/ws1/teams/connections/conn1/sync",
            "headers": {"x-user-email": "user@test.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_invalid_json_body(self):
        event = {
            "httpMethod": "GET",
            "path": "/unknown",
            "headers": {"x-user-email": "user@test.com"},
            "body": "not json{{{",
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 404


class TestTeamsIntegrationListConnections:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        handler_module._oauth_config = None
        handler_module._dynamodb = None
        handler_module._ssm = None
        handler_module._sqs = None
        yield

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_success(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "WS#ws1",
                    "SK": "TEAMS_CONNECTION#conn1",
                    "connection_id": "conn1",
                }
            ]
        }
        result = handler_module.list_connections("user@test.com", "ws1")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["connections"]) == 1

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_not_member(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {}
        result = handler_module.list_connections("user@test.com", "ws1")
        assert result["statusCode"] == 403


class TestTeamsIntegrationCallback:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        handler_module._oauth_config = None
        handler_module._dynamodb = None
        handler_module._ssm = None
        handler_module._sqs = None
        yield

    @patch.object(handler_module, "save_tokens")
    @patch.object(handler_module, "get_user_profile")
    @patch.object(handler_module, "exchange_code_for_tokens")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "_get_dynamodb")
    def test_callback_success(self, mock_db_fn, mock_oauth, mock_exchange,
                               mock_profile, mock_save):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_oauth.return_value = {
            "client_id": "cid", "client_secret": "csec", "callback_url": "https://cb"
        }
        mock_exchange.return_value = ("access", "refresh", 3600)
        mock_profile.return_value = {
            "mail": "user@test.com", "displayName": "Test User"
        }

        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": json.dumps({"workspace_id": "ws1"}),
            }
        }
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "redirect" in body
        assert "ws1" in body["redirect"]

    def test_callback_error_param(self):
        event = {"queryStringParameters": {"error": "access_denied"}}
        result = handler_module.handle_callback(event)
        body = json.loads(result["body"])
        assert "oauth_denied" in body["redirect"]

    def test_callback_missing_code(self):
        event = {"queryStringParameters": {"state": "{}"}}
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, "exchange_code_for_tokens")
    @patch.object(handler_module, "_load_oauth_config")
    def test_callback_token_exchange_failed(self, mock_oauth, mock_exchange):
        mock_oauth.return_value = {
            "client_id": "cid", "client_secret": "csec", "callback_url": "https://cb"
        }
        mock_exchange.return_value = (None, None, None)
        event = {
            "queryStringParameters": {
                "code": "code",
                "state": json.dumps({"workspace_id": "ws1"}),
            }
        }
        result = handler_module.handle_callback(event)
        body = json.loads(result["body"])
        assert "token_exchange_failed" in body["redirect"]

    def test_callback_invalid_state(self):
        event = {
            "queryStringParameters": {
                "code": "code",
                "state": "not-valid-json{{{",
            }
        }
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 400


class TestTeamsIntegrationDeleteConnection:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        handler_module._oauth_config = None
        handler_module._dynamodb = None
        handler_module._ssm = None
        handler_module._sqs = None
        yield

    @patch.object(handler_module, "delete_tokens")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_sqs")
    @patch.object(handler_module, "_get_dynamodb")
    def test_delete_connection_success(self, mock_db_fn, mock_sqs_fn,
                                        mock_get_conn, mock_del_tokens):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        mock_table.get_item.side_effect = [
            {"Item": {"role": "workspace_admin"}},
            {"Item": {"role": "workspace_admin"}},
        ]
        mock_get_conn.return_value = {"connection_id": "conn1"}
        mock_del_tokens.return_value = True

        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
        result = handler_module.delete_connection("admin@test.com", "ws1", "conn1")
        assert result["statusCode"] == 200

    @patch.object(handler_module, "_get_dynamodb")
    def test_delete_connection_not_member(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {}

        result = handler_module.delete_connection("user@test.com", "ws1", "conn1")
        assert result["statusCode"] == 403

