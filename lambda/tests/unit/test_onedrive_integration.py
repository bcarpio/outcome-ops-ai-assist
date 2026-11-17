# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for onedrive-integration Lambda (handler, onedrive_api, token_manager)."""

import base64
import importlib.util
import json
import os
import sys
import urllib.error
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


# Load token_manager and onedrive_api FIRST under the names the handler
# imports them as (from token_manager / from onedrive_api).
token_module = _load_module(
    "od_int_token_manager", "token_manager.py", "../../onedrive-integration",
    also_register_as="token_manager",
)

# Shared Entra ID OAuth helper (ADR-035):
# token_manager.py is now a thin wrapper. Tests patch internals
# (urllib, _get_dynamodb, _get_kms, _encrypt_token, _decrypt_token,
# save_tokens, get_refresh_token, module-level cache globals) on
# the SHARED module, not the wrapper.
import shared.entra_id_oauth as shared_oauth  # noqa: E402
api_module = _load_module(
    "od_int_onedrive_api", "onedrive_api.py", "../../onedrive-integration",
    also_register_as="onedrive_api",
)
handler_module = _load_module(
    "od_int_handler", "handler.py", "../../onedrive-integration"
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


class TestODTokenManager:

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
        # Stored under ONEDRIVE_TOKEN# prefix (separate from SHAREPOINT_TOKEN#)
        assert mock_table.put_item.call_args[1]["Item"]["PK"] == "ONEDRIVE_TOKEN#conn1"

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "PutItem"
        )
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        # _encrypt_token will fail to import KMS - patch to avoid that path
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
        assert deleted_pk == "ONEDRIVE_TOKEN#c1"

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
        # Rotated refresh token gets persisted
        mock_save.assert_called_once()
        _kw = mock_save.call_args.kwargs
        assert _kw["connection_id"] == "c1"
        assert _kw["access_token"] == "new_access"
        assert _kw["refresh_token"] == "new_refresh"
        assert _kw["expires_in"] == 3600

    @patch.object(shared_oauth, "get_refresh_token", return_value=None)
    def test_refresh_access_token_no_stored_refresh(self, _gtr):
        assert token_module.refresh_access_token("c1", "client", "secret") is None


# ============================================================================
# OneDrive API Tests
# ============================================================================


class TestOneDriveAPI:

    @patch("urllib.request.urlopen")
    def test_make_request_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"id": "drv"})
        result = api_module._make_request("token", "/me/drive")
        assert result == {"id": "drv"}

    @patch("urllib.request.urlopen")
    def test_make_request_401_raises_auth_error(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 401, "U", {}, None)
        err.read = MagicMock(return_value=b"unauth")
        mock_urlopen.side_effect = err
        with pytest.raises(api_module.OneDriveAuthError):
            api_module._make_request("token", "/me")

    @patch("urllib.request.urlopen")
    def test_make_request_403_raises_auth_error(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 403, "F", {}, None)
        err.read = MagicMock(return_value=b"forbid")
        mock_urlopen.side_effect = err
        with pytest.raises(api_module.OneDriveAuthError):
            api_module._make_request("token", "/me")

    @patch("urllib.request.urlopen")
    def test_make_request_404_returns_empty(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 404, "NF", {}, None)
        err.read = MagicMock(return_value=b"")
        mock_urlopen.side_effect = err
        result = api_module._make_request("token", "/missing")
        assert result == {"value": []}

    @patch("urllib.request.urlopen")
    def test_make_request_500_raises_api_error(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 500, "S", {}, None)
        err.read = MagicMock(return_value=b"server fail")
        mock_urlopen.side_effect = err
        with pytest.raises(api_module.OneDriveAPIError):
            api_module._make_request("token", "/me")

    @patch("urllib.request.urlopen")
    def test_paginate_walks_next_links(self, mock_urlopen):
        mock_urlopen.side_effect = [
            _make_urlopen_response({
                "value": [{"id": "1"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
            }),
            _make_urlopen_response({"value": [{"id": "2"}]}),
        ]
        result = api_module._paginate("token", "/me/drive/root/children")
        assert len(result) == 2
        assert {"id": "1"} in result
        assert {"id": "2"} in result

    @patch("urllib.request.urlopen")
    def test_get_drive_delta_returns_items_and_link(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "value": [{"id": "f1"}, {"id": "f2", "deleted": {}}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta_link",
        })
        items, delta = api_module.get_drive_delta("token", "drive1")
        assert len(items) == 2
        assert delta == "https://graph.microsoft.com/v1.0/delta_link"

    @patch("urllib.request.urlopen")
    def test_delta_walks_pages_then_emits_delta_link(self, mock_urlopen):
        mock_urlopen.side_effect = [
            _make_urlopen_response({
                "value": [{"id": "1"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
            }),
            _make_urlopen_response({
                "value": [{"id": "2"}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/dl",
            }),
        ]
        items, delta = api_module.get_drive_delta("token", "drv", delta_token=None)
        assert len(items) == 2
        assert delta == "https://graph.microsoft.com/v1.0/dl"

    @patch("urllib.request.urlopen")
    def test_get_item_download_url(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "@microsoft.graph.downloadUrl": "https://download.example.com/file"
        })
        url = api_module.get_item_download_url("token", "drv", "item")
        assert url == "https://download.example.com/file"

    @patch("urllib.request.urlopen")
    def test_get_item_download_url_returns_none_on_error(self, mock_urlopen):
        err = urllib.error.HTTPError("https://x", 500, "S", {}, None)
        err.read = MagicMock(return_value=b"")
        mock_urlopen.side_effect = err
        assert api_module.get_item_download_url("t", "d", "i") is None

    @patch("urllib.request.urlopen")
    def test_get_item_returns_none_on_404(self, mock_urlopen):
        # _make_request returns {"value": []} on 404; get_item should treat
        # as missing
        err = urllib.error.HTTPError("https://x", 404, "NF", {}, None)
        err.read = MagicMock(return_value=b"")
        mock_urlopen.side_effect = err
        assert api_module.get_item("t", "d", "i") is None

    def test_is_folder(self):
        assert api_module.is_folder({"folder": {}}) is True
        assert api_module.is_folder({"file": {}}) is False

    def test_is_file(self):
        assert api_module.is_file({"file": {}}) is True
        assert api_module.is_file({"folder": {}}) is False

    def test_is_deleted(self):
        assert api_module.is_deleted({"deleted": {"state": "deleted"}}) is True
        assert api_module.is_deleted({"@removed": {"reason": "deleted"}}) is True
        assert api_module.is_deleted({"file": {}}) is False

    def test_get_item_path_strips_drive_root_prefix(self):
        item = {
            "name": "report.pdf",
            "parentReference": {"path": "/drive/root:/Documents/Reports"},
        }
        assert api_module.get_item_path(item) == "/Documents/Reports/report.pdf"

    def test_get_item_path_root_level(self):
        item = {
            "name": "topfile.txt",
            "parentReference": {"path": ""},
        }
        assert api_module.get_item_path(item) == "/topfile.txt"


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
    handler_module.ONEDRIVE_SYNC_QUEUE = "https://sqs.example.com/od-sync"
    handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
    monkeypatch.setenv("AUDIT_TABLE", "test-audit")
    yield


class TestParsePathAndAuth:

    def test_parse_path_basic(self):
        assert handler_module._parse_path("/onedrive/authorize") == ("onedrive", "authorize")

    def test_parse_path_workspace(self):
        path = "/workspaces/ws1/onedrive/connections/conn1/folders"
        assert handler_module._parse_path(path) == (
            "workspaces", "ws1", "onedrive", "connections", "conn1", "folders",
        )

    def test_get_user_from_request(self):
        event = {"headers": {"x-user-email": "u@x.com"}}
        assert handler_module._get_user_from_request(event) == "u@x.com"

    def test_get_user_from_request_missing(self):
        assert handler_module._get_user_from_request({}) is None


class TestAuthorize:

    @patch.object(handler_module, "_load_oauth_config")
    def test_get_authorize_url_success(self, mock_load):
        mock_load.return_value = {
            "client_id": "cid",
            "client_secret": "sec",
            "callback_url": "https://example.com/api/onedrive/callback",
        }
        result = handler_module.get_authorize_url("ws1")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "auth_url" in body
        assert "client_id=cid" in body["auth_url"]
        # Confirm the OneDrive scope (Files.Read.All) is in the URL
        assert "Files.Read.All" in body["auth_url"]


class TestCallback:

    @patch.object(handler_module, "log_admin_action")
    @patch.object(handler_module, "save_tokens")
    @patch.object(handler_module, "get_user_drive")
    @patch.object(handler_module, "get_user_profile")
    @patch.object(handler_module, "exchange_code_for_tokens")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "_get_dynamodb")
    def test_handle_callback_success_creates_connection(
        self, mock_db_fn, mock_load, mock_exchange, mock_profile,
        mock_drive, mock_save, mock_audit
    ):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_load.return_value = {
            "client_id": "c", "client_secret": "s", "callback_url": "https://x/cb"
        }
        mock_exchange.return_value = ("at", "rt", 3600)
        mock_profile.return_value = {"mail": "u@x.com", "displayName": "User"}
        mock_drive.return_value = {"id": "drv-1", "name": "OneDrive"}

        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": json.dumps({"workspace_id": "ws1"}),
            }
        }
        result = handler_module.handle_callback(event, {})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "configure_onedrive=" in body["redirect"]

        # Connection record stored with the right shape
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "WS#ws1"
        assert item["SK"].startswith("ONEDRIVE_CONNECTION#")
        assert item["drive_id"] == "drv-1"
        assert item["selected_folders"] == []
        assert item["last_sync_status"] == "needs_configuration"

        # Audit row emitted
        mock_audit.assert_called_once()
        audit_kwargs = mock_audit.call_args[1]
        assert audit_kwargs["details"]["provider"] == "onedrive"
        assert audit_kwargs["details"]["drive_id"] == "drv-1"

    @patch.object(handler_module, "_load_oauth_config")
    def test_handle_callback_oauth_denied(self, mock_load):
        event = {"queryStringParameters": {"error": "access_denied"}}
        result = handler_module.handle_callback(event, {})
        body = json.loads(result["body"])
        assert "oauth_denied" in body["redirect"]


class TestListConnections:

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_success(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "WS#ws1", "SK": "ONEDRIVE_CONNECTION#c1",
                    "connection_id": "c1", "delta_tokens": {"f": "tok"},
                    "drive_id": "drv-1",
                }
            ]
        }
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.list_connections("u@x.com", "ws1")
        body = json.loads(result["body"])
        assert len(body["connections"]) == 1
        # delta_tokens stripped from response (sensitive-ish + noisy)
        assert "delta_tokens" not in body["connections"][0]
        # drive_id preserved
        assert body["connections"][0]["drive_id"] == "drv-1"

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_not_member(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no membership
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.list_connections("u@x.com", "ws1")
        assert result["statusCode"] == 403


class TestGetFolders:

    @patch.object(handler_module, "list_root_children")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "get_connection")
    def test_get_folders_includes_root_synthetic(
        self, mock_get_conn, mock_load, _refresh, mock_list
    ):
        mock_load.return_value = {"client_id": "c", "client_secret": "s"}
        mock_get_conn.return_value = {
            "connection_id": "c1", "selected_folders": [],
        }
        mock_list.return_value = [
            {"id": "fldr_a", "name": "Documents", "folder": {}},
            {"id": "file_b", "name": "x.pdf", "file": {}},  # filtered (not folder)
            {"id": "fldr_c", "name": "Photos", "folder": {}},
        ]

        result = handler_module.get_folders("u@x.com", "ws1", "c1")
        body = json.loads(result["body"])
        folders = body["folders"]

        # Synthetic "root" + 2 real folders (file is filtered)
        assert len(folders) == 3
        assert folders[0]["folder_id"] == "root"
        assert {f["folder_id"] for f in folders} == {"root", "fldr_a", "fldr_c"}

    @patch.object(handler_module, "get_connection", return_value=None)
    def test_get_folders_404(self, _gc):
        result = handler_module.get_folders("u@x.com", "ws1", "c1")
        assert result["statusCode"] == 404


class TestUpdateFolders:

    @patch.object(handler_module, "_queue_sync")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_folders_admin_queues_sync(self, mock_db_fn, mock_get_conn, mock_queue):
        mock_table = MagicMock()
        # Membership lookup: workspace_admin
        mock_table.get_item.side_effect = [
            {"Item": {"role": "workspace_admin"}},
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_get_conn.return_value = {"connection_id": "c1"}

        body = {"selected_folders": [{"folder_id": "root", "folder_name": "(root)"}]}
        result = handler_module.update_folders("u@x.com", "ws1", "c1", body)
        assert result["statusCode"] == 200
        mock_queue.assert_called_once_with("ws1", "c1")

    @patch.object(handler_module, "_get_dynamodb")
    def test_update_folders_non_admin_403(self, mock_db_fn):
        mock_table = MagicMock()
        # Member but not admin and not org_admin
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},  # not org_admin
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.update_folders("u@x.com", "ws1", "c1", {})
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
        # Two membership lookups: _can_access (member yes) + _is_workspace_admin
        mock_table.get_item.side_effect = [
            {"Item": {"role": "workspace_admin"}},  # _can_access
            {"Item": {"role": "workspace_admin"}},  # _is_workspace_admin
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_get_conn.return_value = {"connection_id": "c1"}
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        result = handler_module.delete_connection("u@x.com", "ws1", "c1")
        assert result["statusCode"] == 200

        # Cleanup queued to delete-worker with source="onedrive" + per-connection prefix
        sent = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert sent == {
            "action": "delete_prefix",
            "workspace_id": "ws1",
            "source": "onedrive",
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
        # Mock the membership check to return non-admin
        with patch.object(handler_module, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_table.get_item.return_value = {}  # no admin role, no org_admin
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db

            event = {
                "httpMethod": "GET",
                "path": "/onedrive/authorize",
                "headers": {"x-user-email": "u@x.com"},
                "queryStringParameters": {"workspace_id": "ws1"},
            }
            result = handler_module.lambda_handler(event, None)
            assert result["statusCode"] == 403

    def test_callback_routes_without_auth(self):
        # Callback has no x-user-email header but should still route
        # (delegated to handle_callback)
        with patch.object(handler_module, "handle_callback") as mock_cb:
            mock_cb.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "GET",
                "path": "/onedrive/callback",
                "queryStringParameters": {"code": "x", "state": "{}"},
            }
            handler_module.lambda_handler(event, None)
            mock_cb.assert_called_once()

