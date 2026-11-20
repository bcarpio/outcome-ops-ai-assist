# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for onenote-integration Lambda (handler, onenote_api, token_manager)."""

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


# Load token_manager and onenote_api FIRST under the names the handler
# imports them as (from token_manager / from onenote_api).
token_module = _load_module(
    "on_int_token_manager", "token_manager.py", "../../onenote-integration",
    also_register_as="token_manager",
)

# Shared Entra ID OAuth helper (ADR-035):
# token_manager.py is now a thin wrapper. Tests patch internals
# (urllib, _get_dynamodb, _get_kms, _encrypt_token, _decrypt_token,
# save_tokens, get_refresh_token, module-level cache globals) on
# the SHARED module, not the wrapper.
import shared.entra_id_oauth as shared_oauth  # noqa: E402
api_module = _load_module(
    "on_int_onenote_api", "onenote_api.py", "../../onenote-integration",
    also_register_as="onenote_api",
)
handler_module = _load_module(
    "on_int_handler", "handler.py", "../../onenote-integration"
)


# ============================================================================
# Helpers
# ============================================================================


def _make_urlopen_response(body_dict_or_text, is_json=True):
    resp = MagicMock()
    if is_json:
        resp.read.return_value = json.dumps(body_dict_or_text).encode("utf-8")
    else:
        resp.read.return_value = body_dict_or_text.encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ============================================================================
# Token Manager Tests
# ============================================================================


class TestONTokenManager:

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
        result = shared_oauth._encrypt_token("token", "test-kms-key")
        assert base64.b64decode(result) == b"encrypted"

    @patch.object(shared_oauth, "_get_kms")
    def test_decrypt_token(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.decrypt.return_value = {"Plaintext": b"decrypted"}
        mock_kms_fn.return_value = mock_kms
        result = shared_oauth._decrypt_token(base64.b64encode(b"cipher").decode())
        assert result == "decrypted"

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "encrypted"
        result = token_module.save_tokens("conn1", "access", "refresh", 3600)
        assert result is True
        # Stored under ONENOTE_TOKEN# prefix (separate from ONEDRIVE_TOKEN#/SHAREPOINT_TOKEN#)
        assert mock_table.put_item.call_args[1]["Item"]["PK"] == "ONENOTE_TOKEN#conn1"

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "PutItem"
        )
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        with patch.object(shared_oauth, "_encrypt_token", return_value="x"):
            assert token_module.save_tokens("c", "a", "r", 60) is False

    @patch.object(shared_oauth, "_decrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "enc"}
        }
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_decrypt.return_value = "decrypted"
        result = token_module.get_refresh_token("c1")
        assert result == "decrypted"

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_missing(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        assert token_module.get_refresh_token("c1") is None

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_delete_tokens(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        assert token_module.delete_tokens("c1") is True
        deleted_pk = mock_table.delete_item.call_args[1]["Key"]["PK"]
        assert deleted_pk == "ONENOTE_TOKEN#c1"

    @patch("urllib.request.urlopen")
    def test_exchange_code_for_tokens_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        })
        access, refresh, expires = token_module.exchange_code_for_tokens(
            "code", "client", "secret", "https://example.com/callback"
        )
        assert access == "at"
        assert refresh == "rt"
        assert expires == 3600

    @patch("urllib.request.urlopen")
    def test_exchange_code_for_tokens_http_error(self, mock_urlopen):
        err = urllib.error.HTTPError(
            "https://example.com", 400, "Bad", {}, None
        )
        err.read = MagicMock(return_value=b"invalid_grant")
        mock_urlopen.side_effect = err
        access, refresh, expires = token_module.exchange_code_for_tokens(
            "code", "client", "secret", "https://example.com/callback"
        )
        assert access is None and refresh is None and expires is None

    @patch("urllib.request.urlopen")
    @patch.object(shared_oauth, "save_tokens")
    @patch.object(shared_oauth, "get_refresh_token", return_value="stored_refresh")
    def test_refresh_access_token_success(self, _gtr, mock_save, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        })
        result = token_module.refresh_access_token("c1", "client", "secret")
        assert result == "new_access"
        mock_save.assert_called_once()
        _kw = mock_save.call_args.kwargs
        assert _kw["connection_id"] == "c1"
        assert _kw["access_token"] == "new_access"
        assert _kw["refresh_token"] == "new_refresh"
        assert _kw["expires_in"] == 3600

    @patch.object(shared_oauth, "get_refresh_token", return_value=None)
    def test_refresh_access_token_no_stored_refresh(self, _gtr):
        assert token_module.refresh_access_token("c1", "client", "secret") is None

    def test_scopes_includes_notes_read_all(self):
        # OneNote requires Notes.Read.All; this is the contract surface that
        # the Azure AD app registration depends on.
        assert "Notes.Read.All" in token_module.ONENOTE_SCOPES
        assert "offline_access" in token_module.ONENOTE_SCOPES


# ============================================================================
# OneNote API Tests
# ============================================================================


class TestOneNoteAPI:

    @patch("urllib.request.urlopen")
    def test_make_request_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"id": "user-1"})
        result = api_module._make_request("token", "/me")
        assert result == {"id": "user-1"}

    @patch("urllib.request.urlopen")
    def test_make_request_401_raises_auth_error(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 401, "U", {}, None)
        err.read = MagicMock(return_value=b"unauth")
        err.fp = MagicMock()
        mock_urlopen.side_effect = err
        with pytest.raises(api_module.OneNoteAuthError):
            api_module._make_request("token", "/me")

    @patch("urllib.request.urlopen")
    def test_make_request_403_raises_auth_error(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 403, "F", {}, None)
        err.read = MagicMock(return_value=b"forbid")
        err.fp = MagicMock()
        mock_urlopen.side_effect = err
        with pytest.raises(api_module.OneNoteAuthError):
            api_module._make_request("token", "/me")

    @patch("urllib.request.urlopen")
    def test_make_request_404_returns_empty_value_list(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 404, "NF", {}, None)
        err.read = MagicMock(return_value=b"")
        err.fp = MagicMock()
        mock_urlopen.side_effect = err
        # JSON path: returns {"value": []}
        assert api_module._make_request("token", "/missing") == {"value": []}

    @patch("urllib.request.urlopen")
    def test_make_request_404_html_returns_empty_string(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 404, "NF", {}, None)
        err.read = MagicMock(return_value=b"")
        err.fp = MagicMock()
        mock_urlopen.side_effect = err
        # HTML path (page content fetch): returns ""
        assert api_module._make_request(
            "token", "/me/onenote/pages/p1/content", accept="text/html"
        ) == ""

    @patch("urllib.request.urlopen")
    def test_make_request_500_raises_api_error(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 500, "S", {}, None)
        err.read = MagicMock(return_value=b"server fail")
        err.fp = MagicMock()
        mock_urlopen.side_effect = err
        with pytest.raises(api_module.OneNoteAPIError):
            api_module._make_request("token", "/me")

    @patch("urllib.request.urlopen")
    def test_paginate_walks_next_links(self, mock_urlopen):
        mock_urlopen.side_effect = [
            _make_urlopen_response({
                "value": [{"id": "nb1"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
            }),
            _make_urlopen_response({"value": [{"id": "nb2"}]}),
        ]
        result = api_module._paginate("token", "/me/onenote/notebooks")
        assert len(result) == 2
        assert {"id": "nb1"} in result
        assert {"id": "nb2"} in result

    @patch("urllib.request.urlopen")
    def test_get_user_profile_returns_id(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "id": "user-123",
            "mail": "u@x.com",
            "displayName": "User",
        })
        result = api_module.get_user_profile("token")
        # `id` is what we use as the connection identity (onenote:{user_id})
        assert result["id"] == "user-123"

    @patch("urllib.request.urlopen")
    def test_list_notebooks(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "value": [
                {"id": "nb1", "displayName": "Personal", "isDefault": True, "isShared": False},
                {"id": "nb2", "displayName": "Team Notes", "isDefault": False, "isShared": True},
            ]
        })
        result = api_module.list_notebooks("token")
        assert len(result) == 2
        assert result[0]["displayName"] == "Personal"
        assert result[1]["isShared"] is True

    @patch("urllib.request.urlopen")
    def test_list_sections(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "value": [{"id": "sec1", "displayName": "Section A"}]
        })
        result = api_module.list_sections("token", "nb1")
        assert len(result) == 1
        assert result[0]["id"] == "sec1"

    @patch("urllib.request.urlopen")
    def test_list_pages(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "value": [
                {"id": "p1", "title": "Page One", "lastModifiedDateTime": "2026-05-10T12:00:00Z"},
            ]
        })
        result = api_module.list_pages("token", "sec1")
        assert len(result) == 1
        assert result[0]["lastModifiedDateTime"] == "2026-05-10T12:00:00Z"

    @patch("urllib.request.urlopen")
    def test_list_pages_with_select(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"value": []})
        api_module.list_pages("token", "sec1", select="id,lastModifiedDateTime")
        called_url = mock_urlopen.call_args[0][0].full_url
        assert "%24select=id%2ClastModifiedDateTime" in called_url \
            or "$select=id,lastModifiedDateTime" in called_url

    @patch("urllib.request.urlopen")
    def test_get_page_content_returns_html(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(
            "<html><body>page content</body></html>", is_json=False
        )
        result = api_module.get_page_content("token", "p1")
        assert "<html>" in result
        assert "page content" in result

    @patch("urllib.request.urlopen")
    def test_get_page_metadata_returns_none_on_404(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 404, "NF", {}, None)
        err.read = MagicMock(return_value=b"")
        err.fp = MagicMock()
        mock_urlopen.side_effect = err
        # _make_request returns {"value": []} on 404; get_page_metadata
        # treats missing id as None
        assert api_module.get_page_metadata("t", "p1") is None

    @patch("urllib.request.urlopen")
    def test_get_notebook_returns_none_on_404(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 404, "NF", {}, None)
        err.read = MagicMock(return_value=b"")
        err.fp = MagicMock()
        mock_urlopen.side_effect = err
        assert api_module.get_notebook("t", "nb1") is None


# ============================================================================
# Integration Handler Tests
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_handler_state(monkeypatch):
    """Reset cached module globals between tests."""
    handler_module._dynamodb = None
    handler_module._ssm = None
    handler_module._sqs = None
    handler_module._oauth_config = None
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    handler_module.ONENOTE_SYNC_QUEUE = "https://sqs.example.com/on-sync"
    handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
    monkeypatch.setenv("AUDIT_TABLE", "test-audit")
    yield


class TestParsePathAndAuth:

    def test_parse_path_basic(self):
        assert handler_module._parse_path("/onenote/authorize") == ("onenote", "authorize")

    def test_parse_path_workspace(self):
        path = "/workspaces/ws1/onenote/connections/conn1/notebooks"
        assert handler_module._parse_path(path) == (
            "workspaces", "ws1", "onenote", "connections", "conn1", "notebooks",
        )

    def test_get_user_from_request(self):
        event = {"headers": {"x-user-email": "u@x.com"}}
        assert handler_module._get_user_from_request(event) == "u@x.com"

    def test_get_user_from_request_missing(self):
        assert handler_module._get_user_from_request({}) is None


class TestAuthorize:

    @patch.object(handler_module, "_load_oauth_config")
    def test_get_authorize_url_success(self, mock_load, oauth_states):
        mock_load.return_value = {
            "client_id": "cid",
            "client_secret": "sec",
            "callback_url": "https://example.com/api/onenote/callback",
        }
        result = handler_module.get_authorize_url("ws1", "admin@x.com")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "auth_url" in body
        assert "client_id=cid" in body["auth_url"]
        # Confirm the OneNote scope (Notes.Read.All) is in the URL
        assert "Notes.Read.All" in body["auth_url"]

        # State is an opaque one-time token bound server-side to the
        # workspace and the admin, not a JSON blob in the URL.
        query = urllib.parse.parse_qs(urllib.parse.urlparse(body["auth_url"]).query)
        state = query["state"][0]
        assert oauth_states.rows[state] == {
            "provider": "onenote",
            "workspace_id": "ws1",
            "user_email": "admin@x.com",
            "extra": {},
        }
        assert "ws1" not in state


class TestCallback:

    @patch.object(handler_module, "log_admin_action")
    @patch.object(handler_module, "save_tokens")
    @patch.object(handler_module, "get_user_profile")
    @patch.object(handler_module, "exchange_code_for_tokens")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "_get_dynamodb")
    def test_handle_callback_success_creates_connection(
        self, mock_db_fn, mock_load, mock_exchange, mock_profile,
        mock_save, mock_audit, oauth_states
    ):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_load.return_value = {
            "client_id": "c", "client_secret": "s", "callback_url": "https://x/cb"
        }
        mock_exchange.return_value = ("at", "rt", 3600)
        mock_profile.return_value = {
            "id": "user-id-1",
            "mail": "u@x.com",
            "displayName": "User",
        }

        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": oauth_states.issue("onenote", "ws1", "admin@x.com"),
            }
        }
        result = handler_module.handle_callback(event, {})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "configure_onenote=" in body["redirect"]

        # Connection record stored with the right shape
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "WS#ws1"
        assert item["SK"].startswith("ONENOTE_CONNECTION#")
        assert item["user_id"] == "user-id-1"
        assert item["selected_notebooks"] == []
        assert item["page_timestamps"] == {}
        assert item["known_page_ids"] == []
        assert item["last_sync_status"] == "needs_configuration"

        # Audit row emitted
        mock_audit.assert_called_once()
        audit_kwargs = mock_audit.call_args[1]
        # Audit names the admin from the stored state; the connected
        # Microsoft account stays in details.
        assert audit_kwargs["user_email"] == "admin@x.com"
        assert audit_kwargs["details"]["user_email_external"] == "u@x.com"
        assert audit_kwargs["details"]["provider"] == "onenote"
        assert audit_kwargs["details"]["user_id"] == "user-id-1"

    @patch.object(handler_module, "_load_oauth_config")
    def test_handle_callback_oauth_denied(self, mock_load):
        event = {"queryStringParameters": {"error": "access_denied"}}
        result = handler_module.handle_callback(event, {})
        body = json.loads(result["body"])
        assert "oauth_denied" in body["redirect"]

    @patch.object(handler_module, "exchange_code_for_tokens")
    def test_handle_callback_rejects_forged_json_state(self, mock_exchange):
        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": json.dumps({"workspace_id": "ws-x"}),
            }
        }
        result = handler_module.handle_callback(event, {})
        assert result["statusCode"] == 400
        assert "error=invalid_state" in json.loads(result["body"])["redirect"]
        mock_exchange.assert_not_called()

    @patch.object(handler_module, "exchange_code_for_tokens")
    def test_handle_callback_rejects_unknown_token(self, mock_exchange):
        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": "not-a-known-token",
            }
        }
        result = handler_module.handle_callback(event, {})
        assert result["statusCode"] == 400
        assert "error=invalid_state" in json.loads(result["body"])["redirect"]
        mock_exchange.assert_not_called()

    @patch.object(handler_module, "log_admin_action")
    @patch.object(handler_module, "save_tokens")
    @patch.object(handler_module, "get_user_profile")
    @patch.object(handler_module, "exchange_code_for_tokens")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "_get_dynamodb")
    def test_handle_callback_state_is_single_use(
        self, mock_db_fn, mock_load, mock_exchange, mock_profile,
        mock_save, mock_audit, oauth_states
    ):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_load.return_value = {
            "client_id": "c", "client_secret": "s", "callback_url": "https://x/cb"
        }
        mock_exchange.return_value = ("at", "rt", 3600)
        mock_profile.return_value = {
            "id": "user-id-1",
            "mail": "u@x.com",
            "displayName": "User",
        }
        token = oauth_states.issue("onenote", "ws1", "admin@x.com")
        event = {"queryStringParameters": {"code": "auth_code", "state": token}}

        first = handler_module.handle_callback(event, {})
        assert first["statusCode"] == 200
        assert "configure_onenote=" in json.loads(first["body"])["redirect"]

        second = handler_module.handle_callback(event, {})
        assert second["statusCode"] == 400
        assert "error=invalid_state" in json.loads(second["body"])["redirect"]
        mock_table.put_item.assert_called_once()


class TestListConnections:

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_success(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "WS#ws1",
                    "SK": "ONENOTE_CONNECTION#c1",
                    "connection_id": "c1",
                    "page_timestamps": {"p1": "2026-05-10T12:00:00Z"},
                    "known_page_ids": ["p1"],
                    "user_id": "user-1",
                }
            ]
        }
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.list_connections("u@x.com", "ws1")
        body = json.loads(result["body"])
        assert len(body["connections"]) == 1
        # Large internal-state fields stripped
        assert "page_timestamps" not in body["connections"][0]
        assert "known_page_ids" not in body["connections"][0]
        # user_id preserved
        assert body["connections"][0]["user_id"] == "user-1"

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_not_member(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.list_connections("u@x.com", "ws1")
        assert result["statusCode"] == 403


class TestGetNotebooks:

    @patch.object(handler_module, "list_notebooks")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "get_connection")
    def test_get_notebooks_marks_selected(
        self, mock_get_conn, mock_load, _refresh, mock_list
    ):
        mock_load.return_value = {"client_id": "c", "client_secret": "s"}
        mock_get_conn.return_value = {
            "connection_id": "c1",
            "selected_notebooks": [{"notebook_id": "nb1", "notebook_name": "Personal"}],
        }
        mock_list.return_value = [
            {"id": "nb1", "displayName": "Personal", "isDefault": True, "isShared": False},
            {"id": "nb2", "displayName": "Team Notes", "isDefault": False, "isShared": True},
        ]

        result = handler_module.get_notebooks("u@x.com", "ws1", "c1")
        body = json.loads(result["body"])
        notebooks = body["notebooks"]

        assert len(notebooks) == 2
        by_id = {nb["notebook_id"]: nb for nb in notebooks}
        assert by_id["nb1"]["selected"] is True
        assert by_id["nb1"]["is_default"] is True
        assert by_id["nb2"]["selected"] is False
        assert by_id["nb2"]["is_shared"] is True

    @patch.object(handler_module, "get_connection", return_value=None)
    def test_get_notebooks_404(self, _gc):
        result = handler_module.get_notebooks("u@x.com", "ws1", "c1")
        assert result["statusCode"] == 404

    @patch.object(handler_module, "refresh_access_token", return_value=None)
    @patch.object(handler_module, "_load_oauth_config", return_value={"client_id": "c", "client_secret": "s"})
    @patch.object(handler_module, "get_connection")
    def test_get_notebooks_refresh_failure_returns_401(self, mock_get_conn, _load, _refresh):
        mock_get_conn.return_value = {"connection_id": "c1", "selected_notebooks": []}
        result = handler_module.get_notebooks("u@x.com", "ws1", "c1")
        assert result["statusCode"] == 401


class TestUpdateNotebooks:

    @patch.object(handler_module, "_queue_sync")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_notebooks_admin_queues_sync(self, mock_db_fn, mock_get_conn, mock_queue):
        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": {"role": "workspace_admin"}},
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_get_conn.return_value = {"connection_id": "c1"}

        body = {"selected_notebooks": [{"notebook_id": "nb1", "notebook_name": "Personal"}]}
        result = handler_module.update_notebooks("u@x.com", "ws1", "c1", body)
        assert result["statusCode"] == 200
        mock_queue.assert_called_once_with("ws1", "c1")

    @patch.object(handler_module, "_get_dynamodb")
    def test_update_notebooks_non_admin_403(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.update_notebooks("u@x.com", "ws1", "c1", {})
        assert result["statusCode"] == 403


class TestDeleteConnection:

    @patch.object(handler_module, "delete_tokens")
    @patch.object(handler_module, "_get_sqs")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_delete_connection_queues_delete_prefix(
        self, mock_db_fn, mock_get_conn, mock_sqs_fn, _del_tok
    ):
        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": {"role": "workspace_admin"}},
            {"Item": {"role": "workspace_admin"}},
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_get_conn.return_value = {"connection_id": "c1"}
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        result = handler_module.delete_connection("u@x.com", "ws1", "c1")
        assert result["statusCode"] == 200

        sent = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert sent == {
            "action": "delete_prefix",
            "workspace_id": "ws1",
            "source": "onenote",
            "prefix": "c1/",
        }


class TestLambdaHandlerRouting:

    def test_404_for_unknown_route(self):
        event = {
            "httpMethod": "GET",
            "path": "/totally/unknown",
            "headers": {"x-user-email": "u@x.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_authorize_requires_workspace_admin(self):
        with patch.object(handler_module, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_table.get_item.return_value = {}
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db

            event = {
                "httpMethod": "GET",
                "path": "/onenote/authorize",
                "headers": {"x-user-email": "u@x.com"},
                "queryStringParameters": {"workspace_id": "ws1"},
            }
            result = handler_module.lambda_handler(event, None)
            assert result["statusCode"] == 403

    def test_callback_routes_without_auth(self):
        with patch.object(handler_module, "handle_callback") as mock_cb:
            mock_cb.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "GET",
                "path": "/onenote/callback",
                "queryStringParameters": {"code": "x", "state": "{}"},
            }
            handler_module.lambda_handler(event, None)
            mock_cb.assert_called_once()



class TestAuthorizeRouteBindsAdmin:
    """GET /onenote/authorize passes the authenticated admin into the
    one-time OAuth state, and refuses non-admins before issuing one."""

    def _event(self):
        return {
            "requestContext": {"http": {"method": "GET", "path": "/onenote/authorize"}},
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

    @patch.object(handler_module, "_get_dynamodb")
    @patch.object(handler_module, "_is_workspace_admin", return_value=True)
    @patch.object(handler_module, "get_authorize_url")
    def test_admin_route_passes_workspace_and_admin(self, mock_url, _admin, _ddb):
        mock_url.return_value = {"statusCode": 200, "body": "{}"}
        handler_module.lambda_handler(self._event(), None)
        mock_url.assert_called_once_with("ws_test", "admin@example.com")

    @patch.object(handler_module, "_get_dynamodb")
    @patch.object(handler_module, "_is_workspace_admin", return_value=False)
    @patch.object(handler_module, "get_authorize_url")
    def test_non_admin_gets_403_and_no_state(self, mock_url, _admin, _ddb, oauth_states):
        result = handler_module.lambda_handler(self._event(), None)
        assert result["statusCode"] == 403
        mock_url.assert_not_called()
        assert oauth_states.rows == {}
