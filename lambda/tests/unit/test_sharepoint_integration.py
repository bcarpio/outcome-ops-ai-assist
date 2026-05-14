"""Unit tests for sharepoint-integration Lambda (handler, sharepoint_api, token_manager)."""

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


# Load token_manager and sharepoint_api first, registering them under the
# names the handler will import (from token_manager / from sharepoint_api).
token_module = _load_module(
    "sp_int_token_manager", "token_manager.py", "../../sharepoint-integration",
    also_register_as="token_manager",
)
api_module = _load_module(
    "sp_int_sharepoint_api", "sharepoint_api.py", "../../sharepoint-integration",
    also_register_as="sharepoint_api",
)
handler_module = _load_module(
    "sp_int_handler", "handler.py", "../../sharepoint-integration"
)


# ============================================================================
# Module-level autouse fixture
# ============================================================================

@pytest.fixture(autouse=True)
def _pin_report_repos_snapshot_to_none():
    """Pin `report_repos_snapshot` to None so the license-snapshot helper
    short-circuits during tests that don't explicitly exercise it.

    Other test modules (test_*_sync etc.) inject a MagicMock
    outcomeops_license into sys.modules at import time, which would
    otherwise drive the helper into a real DDB scan against a generic
    MagicMock (infinite-loop risk). Tests that DO exercise the snapshot
    path use @patch.object on this same attribute, which still wins over
    the None default.
    """
    saved = handler_module.report_repos_snapshot
    handler_module.report_repos_snapshot = None
    yield
    handler_module.report_repos_snapshot = saved


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

class TestSPTokenManager:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        token_module._dynamodb = None
        token_module._kms = None
        yield

    @patch.object(token_module, "_get_kms")
    def test_encrypt_token(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.encrypt.return_value = {"CiphertextBlob": b"encrypted"}
        mock_kms_fn.return_value = mock_kms
        result = token_module._encrypt_token("token")
        assert base64.b64decode(result) == b"encrypted"

    @patch.object(token_module, "_get_kms")
    def test_decrypt_token(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.decrypt.return_value = {"Plaintext": b"decrypted"}
        mock_kms_fn.return_value = mock_kms
        result = token_module._decrypt_token(base64.b64encode(b"cipher").decode())
        assert result == "decrypted"

    @patch.object(token_module, "_encrypt_token")
    @patch.object(token_module, "_get_dynamodb")
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "encrypted"
        result = token_module.save_tokens("conn1", "access", "refresh", 3600)
        assert result is True
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "SHAREPOINT_TOKEN#conn1"

    @patch.object(token_module, "_encrypt_token")
    @patch.object(token_module, "_get_dynamodb")
    def test_save_tokens_error(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "enc"
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "PutItem"
        )
        assert token_module.save_tokens("conn1", "a", "r") is False

    @patch.object(token_module, "_decrypt_token")
    @patch.object(token_module, "_get_dynamodb")
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {"Item": {"encrypted_refresh_token": "enc"}}
        mock_decrypt.return_value = "plain"
        assert token_module.get_refresh_token("conn1") == "plain"

    @patch.object(token_module, "_get_dynamodb")
    def test_get_refresh_token_not_found(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {}
        assert token_module.get_refresh_token("conn1") is None

    @patch.object(token_module, "_get_dynamodb")
    def test_get_refresh_token_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "GetItem"
        )
        assert token_module.get_refresh_token("conn1") is None

    @patch.object(token_module, "save_tokens")
    @patch("sp_int_token_manager.urllib.request.urlopen")
    @patch.object(token_module, "get_refresh_token")
    def test_refresh_success(self, mock_get, mock_urlopen, mock_save):
        mock_get.return_value = "old_refresh"
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600,
        })
        result = token_module.refresh_access_token("conn1", "cid", "csec")
        assert result == "new_at"
        mock_save.assert_called_once()

    @patch.object(token_module, "get_refresh_token")
    def test_refresh_no_token(self, mock_get):
        mock_get.return_value = None
        assert token_module.refresh_access_token("conn1", "cid", "csec") is None

    @patch("sp_int_token_manager.urllib.request.urlopen")
    @patch.object(token_module, "get_refresh_token")
    def test_refresh_http_error(self, mock_get, mock_urlopen):
        mock_get.return_value = "refresh"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=400, msg="Bad",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'invalid_grant')),
        )
        assert token_module.refresh_access_token("conn1", "cid", "csec") is None

    @patch.object(token_module, "_get_dynamodb")
    def test_delete_tokens_success(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        assert token_module.delete_tokens("conn1") is True
        mock_table.delete_item.assert_called_once_with(
            Key={"PK": "SHAREPOINT_TOKEN#conn1", "SK": "CREDENTIALS"}
        )

    @patch.object(token_module, "_get_dynamodb")
    def test_delete_tokens_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "DeleteItem"
        )
        assert token_module.delete_tokens("conn1") is False

    @patch("sp_int_token_manager.urllib.request.urlopen")
    def test_exchange_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        })
        a, r, e = token_module.exchange_code_for_tokens("code", "cid", "csec", "https://cb")
        assert a == "at"
        assert r == "rt"
        assert e == 3600

    @patch("sp_int_token_manager.urllib.request.urlopen")
    def test_exchange_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=400, msg="Bad",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'err')),
        )
        a, r, e = token_module.exchange_code_for_tokens("code", "cid", "csec", "https://cb")
        assert a is None


# ============================================================================
# SharePoint API Tests
# ============================================================================

class TestSharePointAPI:

    @patch("sp_int_sharepoint_api.urllib.request.urlopen")
    def test_make_request_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"id": "123"})
        result = api_module._make_request("token", "https://graph.microsoft.com/v1.0/me")
        assert result["id"] == "123"

    @patch("sp_int_sharepoint_api.urllib.request.urlopen")
    def test_make_request_auth_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://graph.microsoft.com", code=401, msg="Unauthorized",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'unauthorized')),
        )
        with pytest.raises(api_module.SharePointAuthError):
            api_module._make_request("token", "https://graph.microsoft.com/v1.0/me")

    @patch("sp_int_sharepoint_api.urllib.request.urlopen")
    def test_make_request_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://graph.microsoft.com", code=500, msg="Server Error",
            hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'error')),
        )
        with pytest.raises(api_module.SharePointAPIError):
            api_module._make_request("token", "https://graph.microsoft.com/v1.0/me")

    @patch.object(api_module, "_make_request")
    def test_get_user_profile(self, mock_req):
        mock_req.return_value = {"displayName": "User"}
        assert api_module.get_user_profile("token")["displayName"] == "User"

    @patch.object(api_module, "_make_request")
    def test_get_user_sites(self, mock_req):
        mock_req.return_value = {
            "value": [
                {"name": "MySite", "displayName": "My Site", "webUrl": "https://site"},
                {"name": "contentTypeHub", "displayName": "Hub"},
            ]
        }
        result = api_module.get_user_sites("token")
        assert len(result) == 1
        assert result[0]["name"] == "MySite"

    @patch.object(api_module, "_make_request")
    def test_get_user_sites_pagination(self, mock_req):
        mock_req.side_effect = [
            {"value": [{"name": "s1", "displayName": "S1"}], "@odata.nextLink": "https://next"},
            {"value": [{"name": "s2", "displayName": "S2"}]},
        ]
        result = api_module.get_user_sites("token")
        assert len(result) == 2

    def test_get_site_display_name_uses_display(self):
        site = {"displayName": "Custom Name", "name": "site1"}
        assert api_module._get_site_display_name(site) == "Custom Name"

    def test_get_site_display_name_falls_back_to_name(self):
        site = {"displayName": "Team Site", "name": "mysite"}
        assert api_module._get_site_display_name(site) == "mysite"

    def test_get_site_display_name_from_url(self):
        site = {"displayName": "Team Site", "name": "", "webUrl": "https://org.sharepoint.com/sites/MySite"}
        assert api_module._get_site_display_name(site) == "MySite"

    def test_get_site_display_name_unknown(self):
        site = {"displayName": "", "name": "", "webUrl": "https://org.sharepoint.com"}
        result = api_module._get_site_display_name(site)
        assert result == "Unknown Site"

    @patch.object(api_module, "_make_request")
    def test_list_site_drives(self, mock_req):
        mock_req.return_value = {"value": [{"id": "d1", "name": "Documents"}]}
        result = api_module.list_site_drives("token", "site1")
        assert len(result) == 1

    @patch.object(api_module, "_make_request")
    def test_get_drive_delta(self, mock_req):
        mock_req.return_value = {
            "value": [{"id": "item1"}],
            "@odata.deltaLink": "https://delta",
        }
        items, delta = api_module.get_drive_delta("token", "drive1")
        assert len(items) == 1
        assert delta == "https://delta"

    @patch.object(api_module, "_make_request")
    def test_get_drive_delta_with_token(self, mock_req):
        mock_req.return_value = {"value": [], "@odata.deltaLink": "https://new-delta"}
        items, delta = api_module.get_drive_delta("token", "drive1", delta_token="https://old-delta")
        mock_req.assert_called_with("token", "https://old-delta")

    @patch.object(api_module, "_make_request")
    def test_list_site_pages(self, mock_req):
        mock_req.return_value = {"value": [{"id": "p1", "title": "Page"}]}
        result = api_module.list_site_pages("token", "site1")
        assert len(result) == 1

    @patch.object(api_module, "_make_request")
    def test_list_site_pages_error(self, mock_req):
        mock_req.side_effect = api_module.SharePointAPIError("err")
        result = api_module.list_site_pages("token", "site1")
        assert result == []

    @patch.object(api_module, "_make_request")
    def test_get_page_content(self, mock_req):
        mock_req.return_value = {"title": "Page", "canvasLayout": {}}
        result = api_module.get_page_content("token", "site1", "page1")
        assert result["title"] == "Page"

    @patch.object(api_module, "_make_request")
    def test_get_page_content_error(self, mock_req):
        mock_req.side_effect = api_module.SharePointAPIError("err")
        result = api_module.get_page_content("token", "site1", "page1")
        assert result is None

    def test_extract_page_text(self):
        page = {
            "title": "Test Page",
            "canvasLayout": {
                "horizontalSections": [
                    {
                        "columns": [
                            {
                                "webparts": [
                                    {"innerHtml": "<p>Hello</p> <b>World</b>"}
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        text = api_module.extract_page_text(page)
        assert "Test Page" in text
        assert "Hello" in text
        assert "World" in text

    def test_extract_page_text_empty(self):
        page = {"title": "Empty", "canvasLayout": {}}
        text = api_module.extract_page_text(page)
        assert "Empty" in text

    @patch("sp_int_sharepoint_api.urllib.request.urlopen")
    def test_download_file(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = b"content"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        result = api_module.download_file("https://download.url")
        assert result == b"content"


# ============================================================================
# Handler Tests
# ============================================================================

class TestSPHandlerHelpers:

    def test_response(self):
        result = handler_module._response(200, {"key": "val"})
        assert result["statusCode"] == 200

    def test_error(self):
        result = handler_module._error(400, "bad")
        assert result["statusCode"] == 400

    def test_redirect(self):
        result = handler_module._redirect("https://example.com")
        assert result["statusCode"] == 302

    def test_parse_path(self):
        assert handler_module._parse_path("/sharepoint/authorize") == ("sharepoint", "authorize")

    def test_get_user_from_request(self):
        assert handler_module._get_user_from_request({"headers": {"x-user-email": "u@t.com"}}) == "u@t.com"

    def test_generate_id(self):
        assert isinstance(handler_module.generate_id(), str)


class TestSPHandlerRouting:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        handler_module._oauth_config = None
        handler_module._dynamodb = None
        handler_module._ssm = None
        handler_module._sqs = None
        yield

    def test_not_found(self):
        event = {"httpMethod": "GET", "path": "/unknown", "headers": {"x-user-email": "u@t.com"}}
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_auth_required(self):
        event = {"httpMethod": "GET", "path": "/workspaces/ws1/sharepoint/connections", "headers": {}}
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 401

    @patch.object(handler_module, "list_connections")
    def test_list_connections_route(self, mock_list):
        mock_list.return_value = handler_module._response(200, {"connections": []})
        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/sharepoint/connections",
            "headers": {"x-user-email": "u@t.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch.object(handler_module, "handle_callback")
    def test_callback_route(self, mock_cb):
        mock_cb.return_value = handler_module._response(200, {"redirect": "/"})
        event = {"httpMethod": "GET", "path": "/sharepoint/callback", "queryStringParameters": {"code": "c", "state": "{}"}}
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch.object(handler_module, "delete_connection")
    def test_delete_route(self, mock_del):
        mock_del.return_value = handler_module._response(200, {"message": "ok"})
        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/sharepoint/connections/conn1",
            "headers": {"x-user-email": "u@t.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200

    @patch.object(handler_module, "trigger_sync")
    def test_sync_route(self, mock_sync):
        mock_sync.return_value = handler_module._response(200, {"message": "ok"})
        event = {
            "httpMethod": "POST",
            "path": "/workspaces/ws1/sharepoint/connections/conn1/sync",
            "headers": {"x-user-email": "u@t.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200


class TestSPHandlerCallback:

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
    def test_callback_success(self, mock_db_fn, mock_oauth, mock_exchange, mock_profile, mock_save):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_oauth.return_value = {"client_id": "cid", "client_secret": "csec", "callback_url": "https://cb"}
        mock_exchange.return_value = ("access", "refresh", 3600)
        mock_profile.return_value = {"mail": "u@t.com", "displayName": "User"}

        event = {"queryStringParameters": {"code": "c", "state": json.dumps({"workspace_id": "ws1"})}}
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 200

    def test_callback_error(self):
        event = {"queryStringParameters": {"error": "denied"}}
        result = handler_module.handle_callback(event)
        body = json.loads(result["body"])
        assert "oauth_denied" in body["redirect"]

    def test_callback_missing_code(self):
        event = {"queryStringParameters": {"state": "{}"}}
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 400

    def test_callback_invalid_state(self):
        event = {"queryStringParameters": {"code": "c", "state": "bad{{"}}
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 400


class TestSPHandlerListConnections:

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
        mock_table.query.return_value = {"Items": [{"PK": "WS#ws1", "SK": "SHAREPOINT_CONNECTION#c1", "connection_id": "c1"}]}
        result = handler_module.list_connections("u@t.com", "ws1")
        assert result["statusCode"] == 200

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_not_member(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {}
        result = handler_module.list_connections("u@t.com", "ws1")
        assert result["statusCode"] == 403


# ============================================================================
# License snapshot wiring (chunk 03 of license-layer-v2 migration)
# ============================================================================

class TestSPLicenseSnapshotWiring:
    """The connect (callback), scope-update (update_sites), and disconnect
    (delete_connection) paths must fire report_repos_snapshot with the full
    active-repo set so the license server's view tracks DDB truth.
    """

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
    @patch.object(handler_module, "report_repos_snapshot")
    @patch.object(handler_module, "get_active_repos_for_customer")
    @patch.object(handler_module, "_get_dynamodb")
    def test_callback_publishes_snapshot(
        self, mock_db_fn, mock_get_active, mock_snapshot, mock_oauth,
        mock_exchange, mock_profile, mock_save,
    ):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_oauth.return_value = {"client_id": "cid", "client_secret": "csec", "callback_url": "https://cb"}
        mock_exchange.return_value = ("access", "refresh", 3600)
        mock_profile.return_value = {"mail": "u@t.com", "displayName": "User"}
        mock_get_active.return_value = []

        event = {"queryStringParameters": {"code": "c", "state": json.dumps({"workspace_id": "ws1"})}}
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 200
        mock_snapshot.assert_called_once_with(active_repos=[])

    @patch.object(handler_module, "save_tokens")
    @patch.object(handler_module, "get_user_profile")
    @patch.object(handler_module, "exchange_code_for_tokens")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "report_repos_snapshot")
    @patch.object(handler_module, "get_active_repos_for_customer",
                  side_effect=RuntimeError("ddb boom"))
    @patch.object(handler_module, "_get_dynamodb")
    def test_callback_swallows_snapshot_failure(
        self, mock_db_fn, mock_get_active, mock_snapshot, mock_oauth,
        mock_exchange, mock_profile, mock_save,
    ):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_oauth.return_value = {"client_id": "cid", "client_secret": "csec", "callback_url": "https://cb"}
        mock_exchange.return_value = ("access", "refresh", 3600)
        mock_profile.return_value = {"mail": "u@t.com", "displayName": "User"}

        event = {"queryStringParameters": {"code": "c", "state": json.dumps({"workspace_id": "ws1"})}}
        result = handler_module.handle_callback(event)
        assert result["statusCode"] == 200
        mock_snapshot.assert_not_called()

    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "report_repos_snapshot")
    @patch.object(handler_module, "get_active_repos_for_customer")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_sites_publishes_snapshot(
        self, mock_db_fn, mock_get_active, mock_snapshot, mock_get_conn,
    ):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn1"}
        mock_get_active.return_value = ["sharepoint:site-abc"]
        handler_module.SHAREPOINT_SYNC_QUEUE = None

        body = {"selected_sites": [{"site_id": "site-abc", "name": "Site"}]}
        result = handler_module.update_sites("admin@example.com", "ws1", "conn1", body)
        assert result["statusCode"] == 200
        mock_snapshot.assert_called_once_with(active_repos=["sharepoint:site-abc"])

    @patch.object(handler_module, "delete_tokens")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "report_repos_snapshot")
    @patch.object(handler_module, "get_active_repos_for_customer")
    @patch.object(handler_module, "_get_dynamodb")
    def test_delete_connection_publishes_snapshot(
        self, mock_db_fn, mock_get_active, mock_snapshot, mock_get_conn,
        mock_del_tokens,
    ):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn1", "workspace_id": "ws1"}
        mock_get_active.return_value = []
        handler_module.DELETE_QUEUE = None

        result = handler_module.delete_connection("admin@example.com", "ws1", "conn1")
        assert result["statusCode"] == 200
        mock_snapshot.assert_called_once_with(active_repos=[])
