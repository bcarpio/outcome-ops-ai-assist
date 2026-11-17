# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sharepoint-sync Lambda (handler, sharepoint_api, token_manager)."""

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

mock_dynamodb_resource = MagicMock()
mock_sqs_client = MagicMock()
mock_ssm_client = MagicMock()


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


sp_api_module = _load_module(
    "sp_sync_sharepoint_api", "sharepoint_api.py", "../../sharepoint-sync",
    also_register_as="sharepoint_api",
)
sp_token_module = _load_module(
    "sp_sync_token_manager", "token_manager.py", "../../sharepoint-sync",
    also_register_as="token_manager",
)
handler_module = _load_module(
    "sp_sync_handler", "handler.py", "../../sharepoint-sync"
)
handler_module.dynamodb = mock_dynamodb_resource
handler_module.sqs_client = mock_sqs_client
handler_module.ssm_client = mock_ssm_client


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks(monkeypatch):
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_ssm_client.reset_mock()
    handler_module._oauth_config = None
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
    handler_module.SHAREPOINT_SYNC_QUEUE = "https://sqs.example.com/sp-sync"
    yield


@pytest.fixture
def mock_table():
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_connection():
    return {
        "PK": "WS#ws_test",
        "SK": "SHAREPOINT_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "selected_sites": [
            {
                "site_id": "site1",
                "site_name": "TestSite",
                "drives": [{"id": "drive1", "name": "Documents"}],
            }
        ],
        "include_pages": True,
        "delta_tokens": {},
        "last_sync_at": None,
    }


# ============================================================================
# SharePoint Sync API tests
# ============================================================================

class TestSPSyncAPI:

    @patch.object(sp_api_module, "_make_request")
    def test_get_drive_delta(self, mock_req):
        mock_req.return_value = {
            "value": [{"id": "item1"}],
            "@odata.deltaLink": "https://delta",
        }
        items, delta = sp_api_module.get_drive_delta("tok", "drive1")
        assert len(items) == 1
        assert delta == "https://delta"

    @patch.object(sp_api_module, "_make_request")
    def test_get_drive_delta_pagination(self, mock_req):
        mock_req.side_effect = [
            {"value": [{"id": "i1"}], "@odata.nextLink": "https://next"},
            {"value": [{"id": "i2"}], "@odata.deltaLink": "https://delta"},
        ]
        items, delta = sp_api_module.get_drive_delta("tok", "drive1")
        assert len(items) == 2
        assert delta == "https://delta"

    @patch.object(sp_api_module, "_make_request")
    def test_list_site_pages(self, mock_req):
        mock_req.return_value = {"value": [{"id": "p1"}]}
        result = sp_api_module.list_site_pages("tok", "site1")
        assert len(result) == 1

    @patch.object(sp_api_module, "_make_request")
    def test_list_site_pages_error(self, mock_req):
        mock_req.side_effect = sp_api_module.SharePointAPIError("err")
        result = sp_api_module.list_site_pages("tok", "site1")
        assert result == []

    def test_extract_page_text(self):
        page = {
            "title": "Page Title",
            "canvasLayout": {
                "horizontalSections": [
                    {"columns": [{"webparts": [{"innerHtml": "<p>Content</p>"}]}]}
                ]
            }
        }
        text = sp_api_module.extract_page_text(page)
        assert "Page Title" in text
        assert "Content" in text

    def test_extract_page_text_empty(self):
        text = sp_api_module.extract_page_text({"title": "Empty"})
        assert "Empty" in text

    @patch.object(sp_api_module, "_make_request")
    def test_get_item_download_url(self, mock_req):
        mock_req.return_value = {"@microsoft.graph.downloadUrl": "https://dl"}
        result = sp_api_module.get_item_download_url("tok", "d1", "i1")
        assert result == "https://dl"

    @patch.object(sp_api_module, "_make_request")
    def test_get_item_download_url_error(self, mock_req):
        mock_req.side_effect = sp_api_module.SharePointAPIError("err")
        result = sp_api_module.get_item_download_url("tok", "d1", "i1")
        assert result is None


# ============================================================================
# Handler tests
# ============================================================================

class TestSPSyncHelpers:

    def test_sanitize_filename(self):
        assert handler_module.sanitize_filename("test:file.txt") == "test_file.txt"

    def test_sanitize_filename_truncates(self):
        assert len(handler_module.sanitize_filename("a" * 300)) <= 200


class TestSPSyncGetConnection:

    def test_success(self, mock_table):
        mock_table.get_item.return_value = {"Item": {"connection_id": "c1"}}
        result = handler_module.get_connection("ws1", "c1")
        assert result["connection_id"] == "c1"

    def test_not_found(self, mock_table):
        mock_table.get_item.return_value = {}
        assert handler_module.get_connection("ws1", "c1") is None

    def test_error(self, mock_table):
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "GetItem"
        )
        assert handler_module.get_connection("ws1", "c1") is None


class TestSPSyncUpdateStatus:

    def test_update_syncing(self, mock_table):
        handler_module.update_connection_status("ws1", "c1", "syncing")
        mock_table.update_item.assert_called_once()

    def test_update_completed(self, mock_table):
        handler_module.update_connection_status(
            "ws1", "c1", "completed", file_count=5, page_count=3,
            delta_tokens={"d1": "link"},
        )
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":file_count" in call_kwargs["ExpressionAttributeValues"]
        assert ":page_count" in call_kwargs["ExpressionAttributeValues"]

    def test_update_with_error(self, mock_table):
        handler_module.update_connection_status("ws1", "c1", "error", error="failed")
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":error" in call_kwargs["ExpressionAttributeValues"]

    def test_update_dynamodb_error(self, mock_table):
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "UpdateItem"
        )
        handler_module.update_connection_status("ws1", "c1", "error")


class TestSPSyncQueueIngestion:

    def test_queue_file_ingestion(self):
        item = {"id": "i1", "name": "file.txt", "parentReference": {"path": "/root"}}
        handler_module.queue_file_ingestion(
            "ws1", "c1", item, "Site", "Docs", "https://dl", "sync1", 0, 10
        )
        mock_sqs_client.send_message.assert_called_once()

    def test_queue_page_ingestion(self):
        page = {"id": "p1", "title": "Page", "webUrl": "https://page"}
        handler_module.queue_page_ingestion(
            "ws1", "c1", page, "Site", "Page text", "sync1", 0, 10
        )
        mock_sqs_client.send_message.assert_called_once()


class TestSPSyncProcessRequest:

    @patch.object(handler_module, "list_site_pages")
    @patch.object(handler_module, "get_drive_delta")
    @patch.object(handler_module, "refresh_access_token")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "update_connection_status")
    @patch.object(handler_module, "get_connection")
    def test_process_success(self, mock_get_conn, mock_update,
                              mock_oauth, mock_refresh, mock_delta,
                              mock_pages, sample_connection):
        mock_get_conn.return_value = sample_connection
        mock_oauth.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_delta.return_value = (
            [{"id": "i1", "file": {"mimeType": "application/pdf"}, "@microsoft.graph.downloadUrl": "https://dl"}],
            "https://delta",
        )
        mock_pages.return_value = []

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "success"

    @patch.object(handler_module, "get_connection")
    def test_process_connection_not_found(self, mock_get_conn):
        mock_get_conn.return_value = None
        result = handler_module.process_sync_request({
            "workspace_id": "ws1", "connection_id": "c1"
        })
        assert result["status"] == "error"

    @patch.object(handler_module, "update_connection_status")
    @patch.object(handler_module, "get_connection")
    def test_process_no_sites(self, mock_get_conn, mock_update):
        mock_get_conn.return_value = {
            "selected_sites": [],
            "include_pages": True,
            "delta_tokens": {},
        }
        result = handler_module.process_sync_request({
            "workspace_id": "ws1", "connection_id": "c1"
        })
        assert result["status"] == "skipped"

    @patch.object(handler_module, "refresh_access_token")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "update_connection_status")
    @patch.object(handler_module, "get_connection")
    def test_process_auth_failed(self, mock_get_conn, mock_update,
                                  mock_oauth, mock_refresh, sample_connection):
        mock_get_conn.return_value = sample_connection
        mock_oauth.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = None
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "error"


class TestSPSyncLambdaHandler:
    """SQS partial-batch-failure contract."""

    @patch.object(handler_module, "process_sync_request")
    def test_handler_success(self, mock_process):
        mock_process.return_value = {"status": "success"}
        event = {"Records": [{"messageId": "m1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})}]}
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    def test_handler_no_records(self):
        result = handler_module.lambda_handler({}, None)
        assert result["statusCode"] == 400

    @patch.object(handler_module, "process_sync_request")
    def test_handler_invalid_json(self, mock_process):
        event = {"Records": [{"messageId": "bad", "body": "bad{{{"}]}
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}

    @patch.object(handler_module, "process_sync_request")
    def test_handler_multiple_records(self, mock_process):
        """Only the failed record is redelivered; success is ack'd."""
        mock_process.side_effect = [
            {"status": "success"},
            {"status": "error", "error": "fail"},
        ]
        event = {
            "Records": [
                {"messageId": "m1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})},
                {"messageId": "m2", "body": json.dumps({"workspace_id": "ws2", "connection_id": "c2"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "m2"}]}

