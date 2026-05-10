"""Unit tests for teams-sync Lambda (handler, teams_api, token_manager)."""

import importlib.util
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading -- pre-mock module-level boto3 clients
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


# Load teams_api and token_manager first, registering under import names
teams_api_module = _load_module(
    "teams_sync_teams_api", "teams_api.py", "../../teams-sync",
    also_register_as="teams_api",
)
token_module = _load_module(
    "teams_sync_token_manager", "token_manager.py", "../../teams-sync",
    also_register_as="token_manager",
)

# Load handler (has module-level boto3 clients)
handler_module = _load_module(
    "teams_sync_handler", "handler.py", "../../teams-sync"
)
# Replace module-level clients
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
    handler_module.TEAMS_SYNC_QUEUE = "https://sqs.example.com/teams-sync"

    # ADR-019: stub the license layer for default-pass behavior in
    # existing tests. License-specific tests override these per-test.
    monkeypatch.setattr(
        handler_module,
        "validate_license",
        lambda *a, **kw: {"sub": "test-customer", "license": {"tier": "team"}},
    )

    def _allow(*a, **kw):
        result = MagicMock()
        result.allowed = True
        result.reason = None
        result.current = 0
        result.limit = 100
        return result

    monkeypatch.setattr(handler_module, "check_repo_limit", _allow)
    monkeypatch.setattr(handler_module, "track_repo_active", lambda *a, **kw: True)
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
        "SK": "TEAMS_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "config": {
            "days_back": 30,
            "include_channels": [
                {
                    "team_id": "team1",
                    "channel_id": "chan1",
                    "channel_name": "General",
                }
            ],
            "sync_files": True,
        },
        "delta_links": {},
    }


# ============================================================================
# teams_api tests (sync copy)
# ============================================================================

class TestTeamsSyncAPI:

    @patch.object(teams_api_module, "_make_request")
    def test_get_channel_messages(self, mock_req):
        mock_req.return_value = {"value": [{"id": "m1"}]}
        msgs, next_link = teams_api_module.get_channel_messages("tok", "t1", "c1")
        assert len(msgs) == 1
        assert next_link is None

    @patch.object(teams_api_module, "_make_request")
    def test_get_message_replies(self, mock_req):
        mock_req.return_value = {"value": [{"id": "r1"}]}
        result = teams_api_module.get_message_replies("tok", "t1", "c1", "m1")
        assert len(result) == 1

    @patch.object(teams_api_module, "_make_request")
    def test_get_messages_delta(self, mock_req):
        mock_req.return_value = {
            "value": [{"id": "m1"}],
            "@odata.deltaLink": "https://delta",
        }
        result = teams_api_module.get_messages_delta("tok", "t1", "c1")
        assert len(result["messages"]) == 1
        assert result["delta_link"] == "https://delta"

    @patch.object(teams_api_module, "_make_request")
    def test_get_channel_files_folder(self, mock_req):
        mock_req.return_value = {"id": "folder1"}
        result = teams_api_module.get_channel_files_folder("tok", "t1", "c1")
        assert result["id"] == "folder1"

    @patch.object(teams_api_module, "_make_request")
    def test_get_channel_files_folder_error(self, mock_req):
        mock_req.side_effect = teams_api_module.TeamsAPIError("err")
        result = teams_api_module.get_channel_files_folder("tok", "t1", "c1")
        assert result is None

    @patch.object(teams_api_module, "_make_request")
    def test_list_folder_files(self, mock_req):
        mock_req.return_value = {"value": [{"id": "f1"}]}
        result = teams_api_module.list_folder_files("tok", "d1", "f1")
        assert len(result) == 1

    def test_filter_messages_by_date_includes_recent(self):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        msgs = [{"createdDateTime": now_iso}]
        result = teams_api_module.filter_messages_by_date(msgs, 30)
        assert len(result) == 1

    def test_filter_messages_by_date_excludes_old(self):
        msgs = [{"createdDateTime": "2020-01-01T00:00:00+00:00"}]
        result = teams_api_module.filter_messages_by_date(msgs, 30)
        assert len(result) == 0


# ============================================================================
# Handler helper tests
# ============================================================================

class TestTeamsSyncHandlerHelpers:

    def test_sanitize_filename(self):
        assert handler_module.sanitize_filename("test:file/name.txt") == "test_file_name.txt"

    def test_sanitize_filename_strips_dots(self):
        assert handler_module.sanitize_filename(".hidden.") == "hidden"

    def test_sanitize_filename_truncates(self):
        long_name = "a" * 300
        result = handler_module.sanitize_filename(long_name)
        assert len(result) <= 200

    def test_build_message_html_basic(self):
        msg = {
            "from": {"user": {"displayName": "Sender"}},
            "createdDateTime": "2024-01-01T00:00:00Z",
            "body": {"content": "<p>Hello</p>", "contentType": "html"},
            "subject": "Test Subject",
        }
        html = handler_module.build_message_html(msg)
        assert "Sender" in html
        assert "Test Subject" in html
        assert "<p>Hello</p>" in html

    def test_build_message_html_text_content(self):
        msg = {
            "from": {"user": {"displayName": "Sender"}},
            "createdDateTime": "2024-01-01T00:00:00Z",
            "body": {"content": "Plain text", "contentType": "text"},
        }
        html = handler_module.build_message_html(msg)
        assert "<pre>" in html

    def test_build_message_html_with_replies(self):
        msg = {
            "from": {"user": {"displayName": "Sender"}},
            "createdDateTime": "2024-01-01T00:00:00Z",
            "body": {"content": "Main", "contentType": "html"},
        }
        replies = [
            {
                "from": {"user": {"displayName": "Replier"}},
                "createdDateTime": "2024-01-01T01:00:00Z",
                "body": {"content": "Reply text", "contentType": "text"},
            }
        ]
        html = handler_module.build_message_html(msg, replies)
        assert "Replies" in html
        assert "Replier" in html

    def test_build_message_html_no_sender(self):
        msg = {
            "from": None,
            "createdDateTime": "2024-01-01T00:00:00Z",
            "body": {"content": "Content", "contentType": "html"},
        }
        html = handler_module.build_message_html(msg)
        assert "Unknown" in html


class TestTeamsSyncGetConnection:

    def test_get_connection_success(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": {"connection_id": "conn1"}
        }
        result = handler_module.get_connection("ws1", "conn1")
        assert result["connection_id"] == "conn1"

    def test_get_connection_not_found(self, mock_table):
        mock_table.get_item.return_value = {}
        result = handler_module.get_connection("ws1", "conn1")
        assert result is None

    def test_get_connection_error(self, mock_table):
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "GetItem"
        )
        result = handler_module.get_connection("ws1", "conn1")
        assert result is None


class TestTeamsSyncUpdateStatus:

    def test_update_status_syncing(self, mock_table):
        handler_module.update_connection_status("ws1", "conn1", "syncing")
        mock_table.update_item.assert_called_once()

    def test_update_status_completed_with_counts(self, mock_table):
        handler_module.update_connection_status(
            "ws1", "conn1", "completed",
            message_count=10, file_count=5,
            delta_links={"key": "link"},
        )
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":msg_count" in call_kwargs["ExpressionAttributeValues"]
        assert ":file_count" in call_kwargs["ExpressionAttributeValues"]
        assert ":delta" in call_kwargs["ExpressionAttributeValues"]

    def test_update_status_error_with_message(self, mock_table):
        handler_module.update_connection_status(
            "ws1", "conn1", "error", error="Something failed"
        )
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":error" in call_kwargs["ExpressionAttributeValues"]

    def test_update_status_dynamodb_error(self, mock_table):
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "UpdateItem"
        )
        # Should not raise
        handler_module.update_connection_status("ws1", "conn1", "error")


class TestTeamsSyncQueueIngestion:

    def test_queue_message_ingestion(self):
        msg = {"id": "m1", "subject": "Test", "createdDateTime": "2024-01-01T00:00:00Z"}
        handler_module.queue_message_ingestion(
            "ws1", "conn1", msg, "General", "<html></html>", "sync1", 0
        )
        mock_sqs_client.send_message.assert_called_once()

    def test_queue_message_ingestion_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "SendMessage"
        )
        msg = {"id": "m1"}
        # Should not raise
        handler_module.queue_message_ingestion(
            "ws1", "conn1", msg, "General", "<html></html>", "sync1", 0
        )

    def test_queue_file_ingestion(self):
        file_item = {
            "id": "f1",
            "name": "doc.pdf",
            "@microsoft.graph.downloadUrl": "https://download.url",
            "file": {"mimeType": "application/pdf"},
            "size": 1000,
        }
        handler_module.queue_file_ingestion(
            "ws1", "conn1", file_item, "General", "sync1"
        )
        mock_sqs_client.send_message.assert_called_once()

    def test_queue_file_ingestion_no_download_url(self):
        file_item = {"id": "f1", "name": "doc.pdf"}
        handler_module.queue_file_ingestion(
            "ws1", "conn1", file_item, "General", "sync1"
        )
        mock_sqs_client.send_message.assert_not_called()


class TestTeamsSyncProcessRequest:

    @patch.object(handler_module, "_sync_channel_files")
    @patch.object(handler_module, "_sync_channel_full")
    @patch.object(handler_module, "refresh_access_token")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "update_connection_status")
    @patch.object(handler_module, "get_connection")
    def test_process_sync_success(self, mock_get_conn, mock_update,
                                   mock_oauth, mock_refresh,
                                   mock_full_sync, mock_file_sync,
                                   sample_connection):
        mock_get_conn.return_value = sample_connection
        mock_oauth.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_full_sync.return_value = (5, "delta_link")
        mock_file_sync.return_value = 3

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })
        assert result["status"] == "success"
        assert result["messages_queued"] == 5
        assert result["files_queued"] == 3

    @patch.object(handler_module, "get_connection")
    def test_process_sync_connection_not_found(self, mock_get_conn):
        mock_get_conn.return_value = None
        result = handler_module.process_sync_request({
            "workspace_id": "ws1", "connection_id": "conn1"
        })
        assert result["status"] == "error"

    @patch.object(handler_module, "update_connection_status")
    @patch.object(handler_module, "get_connection")
    def test_process_sync_no_channels(self, mock_get_conn, mock_update):
        mock_get_conn.return_value = {
            "config": {"include_channels": [], "days_back": 30},
            "delta_links": {},
        }
        result = handler_module.process_sync_request({
            "workspace_id": "ws1", "connection_id": "conn1"
        })
        assert result["status"] == "skipped"

    @patch.object(handler_module, "refresh_access_token")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "update_connection_status")
    @patch.object(handler_module, "get_connection")
    def test_process_sync_auth_failed(self, mock_get_conn, mock_update,
                                       mock_oauth, mock_refresh,
                                       sample_connection):
        mock_get_conn.return_value = sample_connection
        mock_oauth.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = None

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "error"
        assert "Authentication" in result["error"]


class TestTeamsSyncLambdaHandler:

    @patch.object(handler_module, "process_sync_request")
    def test_handler_success(self, mock_process):
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})}
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_handler_no_records(self):
        result = handler_module.lambda_handler({}, None)
        assert result["statusCode"] == 400

    @patch.object(handler_module, "process_sync_request")
    def test_handler_invalid_json(self, mock_process):
        event = {"Records": [{"body": "not-json{{{"}]}
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    @patch.object(handler_module, "process_sync_request")
    def test_handler_multiple_records(self, mock_process):
        mock_process.side_effect = [
            {"status": "success"},
            {"status": "error", "error": "failed"},
        ]
        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})},
                {"body": json.dumps({"workspace_id": "ws2", "connection_id": "c2"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        body = json.loads(result["body"])
        assert len(body["results"]) == 2


# ============================================================================
# License layer enforcement (ADR-019)
# ============================================================================


class TestTeamsSyncLicenseEnforcement:

    def test_lambda_handler_license_valid(self, monkeypatch):
        """Happy path: validate_license returns claims, sync proceeds."""
        mock_validate = MagicMock(return_value={"sub": "cust-123"})
        monkeypatch.setattr(handler_module, "validate_license", mock_validate)
        with patch.object(handler_module, "process_sync_request") as mock_proc:
            mock_proc.return_value = {"status": "success"}
            event = {
                "Records": [
                    {"body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})}
                ]
            }
            result = handler_module.lambda_handler(event, None)
            assert result["statusCode"] == 200
            mock_validate.assert_called_once()

    def test_lambda_handler_license_import_error(self, monkeypatch):
        """Layer-not-attached: emits LICENSE_FAIL_OPEN audit row + raises."""
        monkeypatch.setattr(handler_module, "validate_license", None)
        mock_log = MagicMock()
        monkeypatch.setattr(handler_module, "log_license_fail_open", mock_log)

        event = {"Records": [{"body": json.dumps({"workspace_id": "ws1"})}]}
        with pytest.raises(ImportError):
            handler_module.lambda_handler(event, None)
        mock_log.assert_called_once()
        assert "teams-sync" in mock_log.call_args[0]

    def test_lambda_handler_license_invalid(self, monkeypatch):
        """validate_license raising any non-ImportError surfaces as raise."""
        def _raise(*a, **kw):
            raise RuntimeError("license expired")
        monkeypatch.setattr(handler_module, "validate_license", _raise)

        event = {"Records": [{"body": json.dumps({"workspace_id": "ws1"})}]}
        with pytest.raises(RuntimeError, match="license expired"):
            handler_module.lambda_handler(event, None)

    def test_check_repo_limit_blocked_skips_source(self, monkeypatch, sample_connection):
        """When check_repo_limit returns allowed=False, the channel is skipped."""
        blocked = MagicMock()
        blocked.allowed = False
        blocked.reason = "repo_limit_reached"
        blocked.current = 5
        blocked.limit = 5
        mock_check = MagicMock(return_value=blocked)
        monkeypatch.setattr(handler_module, "check_repo_limit", mock_check)
        mock_track = MagicMock(return_value=True)
        monkeypatch.setattr(handler_module, "track_repo_active", mock_track)

        with patch.object(handler_module, "_load_oauth_config") as mock_oauth, \
             patch.object(handler_module, "refresh_access_token") as mock_refresh, \
             patch.object(handler_module, "get_connection") as mock_get_conn, \
             patch.object(handler_module, "update_connection_status"), \
             patch.object(handler_module, "_sync_channel_full") as mock_full, \
             patch.object(handler_module, "_sync_channel_files") as mock_files:
            mock_get_conn.return_value = sample_connection
            mock_oauth.return_value = {"client_id": "c", "client_secret": "s"}
            mock_refresh.return_value = "tok"

            handler_module.process_sync_request({
                "workspace_id": "ws_test", "connection_id": "conn_test"
            })

            mock_check.assert_called_once_with(repo="teams:team1/chan1")
            # Blocked: per-channel sync work and tracking are all skipped.
            mock_full.assert_not_called()
            mock_files.assert_not_called()
            mock_track.assert_not_called()

    def test_track_repo_active_called_on_successful_sync(
        self, monkeypatch, sample_connection
    ):
        """After a successful per-channel sync, track_repo_active is called."""
        mock_track = MagicMock(return_value=True)
        monkeypatch.setattr(handler_module, "track_repo_active", mock_track)

        with patch.object(handler_module, "_load_oauth_config") as mock_oauth, \
             patch.object(handler_module, "refresh_access_token") as mock_refresh, \
             patch.object(handler_module, "get_connection") as mock_get_conn, \
             patch.object(handler_module, "update_connection_status"), \
             patch.object(handler_module, "_sync_channel_full") as mock_full, \
             patch.object(handler_module, "_sync_channel_files") as mock_files:
            mock_get_conn.return_value = sample_connection
            mock_oauth.return_value = {"client_id": "c", "client_secret": "s"}
            mock_refresh.return_value = "tok"
            mock_full.return_value = (5, "delta")
            mock_files.return_value = 2

            handler_module.process_sync_request({
                "workspace_id": "ws_test", "connection_id": "conn_test"
            })

            mock_track.assert_called_once_with(repo="teams:team1/chan1")
