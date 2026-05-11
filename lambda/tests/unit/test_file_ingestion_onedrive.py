"""Unit tests for file-ingestion Lambda - OneDrive file handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

mock_dynamodb_resource = MagicMock()
mock_s3_client = MagicMock()
mock_ssm_client = MagicMock()


def _load_handler_module():
    if 'file_ingestion_handler' in sys.modules:
        del sys.modules['file_ingestion_handler']

    fi_dir = os.path.join(os.path.dirname(__file__), '../../file-ingestion')
    if fi_dir not in sys.path:
        sys.path.insert(0, fi_dir)

    handler_path = os.path.join(fi_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("file_ingestion_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['file_ingestion_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.s3_client = mock_s3_client
    handler_module.ssm_client = mock_ssm_client

    return handler_module


handler_module = _load_handler_module()
process_onedrive_file = handler_module.process_onedrive_file


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_dynamodb_resource.reset_mock()
    mock_s3_client.reset_mock()
    mock_s3_client.put_object.side_effect = None
    handler_module.KB_BUCKET = "test-kb-bucket"
    handler_module._account_id = "123456789012"
    yield


@pytest.fixture(autouse=True)
def stub_outcomeops_license():
    """ADR-019: the handler imports validate_license / check_repo_limit
    INSIDE lambda_handler, so the layer module must exist in sys.modules
    before each invocation. Mirrors the stub in
    test_file_ingestion_outlook.py."""
    stub = MagicMock()
    stub.validate_license.return_value = {"sub": "test-customer"}
    allowed = MagicMock()
    allowed.allowed = True
    allowed.reason = None
    allowed.current = 0
    allowed.limit = 100
    stub.check_repo_limit.return_value = allowed
    stub.track_repo_active.return_value = True
    saved = sys.modules.get("outcomeops_license")
    sys.modules["outcomeops_license"] = stub
    yield stub
    if saved is not None:
        sys.modules["outcomeops_license"] = saved
    else:
        sys.modules.pop("outcomeops_license", None)


def _make_urlopen_response(content_bytes):
    resp = MagicMock()
    resp.read.return_value = content_bytes
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ============================================================================
# process_onedrive_file
# ============================================================================


class TestProcessOneDriveFile:

    @patch("urllib.request.urlopen")
    def test_uploads_to_per_connection_path(self, mock_urlopen):
        """OneDrive files land under workspaces/{ws}/onedrive/{conn_id}/...
        so multiple connections in the same workspace don't collide."""
        mock_urlopen.return_value = _make_urlopen_response(b"file bytes")

        file_info = {
            "item_id": "01ABC",
            "drive_id": "drv_xyz",
            "drive_name": "OneDrive",
            "folder_path": "/Documents/Reports",
            "filename": "Q1_Report.pdf",
            "download_url": "https://download.example.com/file",
            "mime_type": "application/pdf",
        }

        s3_key = process_onedrive_file(
            workspace_id="ws_test",
            connection_id="conn_abc",
            file_info=file_info,
            sync_id="sync_01",
        )

        assert s3_key.startswith("workspaces/ws_test/onedrive/conn_abc/")
        # Folder structure preserved
        assert "Documents" in s3_key
        assert "Reports" in s3_key
        assert s3_key.endswith("Q1_Report.pdf")

        # put_object called with correct metadata
        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-kb-bucket"
        assert call_kwargs["Body"] == b"file bytes"
        assert call_kwargs["ContentType"] == "application/pdf"
        metadata = call_kwargs["Metadata"]
        assert metadata["source"] == "onedrive"
        assert metadata["workspace_id"] == "ws_test"
        assert metadata["connection_id"] == "conn_abc"
        assert metadata["onedrive_drive_id"] == "drv_xyz"
        assert metadata["onedrive_item_id"] == "01ABC"
        assert metadata["sync_id"] == "sync_01"

    @patch("urllib.request.urlopen")
    def test_root_level_file_lands_directly_under_connection(self, mock_urlopen):
        """Files at the drive root (folder_path == '') land directly under
        the connection prefix - no synthetic 'root' segment."""
        mock_urlopen.return_value = _make_urlopen_response(b"x")

        file_info = {
            "item_id": "i", "drive_id": "d", "drive_name": "OneDrive",
            "folder_path": "", "filename": "top.txt",
            "download_url": "https://x", "mime_type": "text/plain",
        }

        s3_key = process_onedrive_file(
            workspace_id="ws", connection_id="c1",
            file_info=file_info, sync_id="s",
        )
        assert s3_key == "workspaces/ws/onedrive/c1/top.txt"

    @patch("urllib.request.urlopen")
    def test_sanitizes_unsafe_chars_per_segment(self, mock_urlopen):
        """Each path segment gets sanitized independently so the hierarchy
        is preserved while OS-unsafe chars are scrubbed."""
        mock_urlopen.return_value = _make_urlopen_response(b"x")

        file_info = {
            "item_id": "i", "drive_id": "d", "drive_name": "OneDrive",
            "folder_path": "/My/<weird:folder>/sub",
            "filename": 'q/uestion?.pdf',
            "download_url": "https://x", "mime_type": "application/pdf",
        }

        s3_key = process_onedrive_file(
            workspace_id="ws", connection_id="c1",
            file_info=file_info, sync_id=None,
        )
        # Slashes within filename get sanitized; folder slashes preserved
        # as hierarchy. Unsafe chars (< : > ? /) replaced with _.
        assert "<" not in s3_key
        assert ":" not in s3_key.replace("workspaces:", "")  # legitimate first colon only if any
        assert "?" not in s3_key

    def test_missing_download_url_returns_none(self):
        result = process_onedrive_file(
            workspace_id="ws", connection_id="c1",
            file_info={"filename": "x.pdf", "download_url": ""},
            sync_id=None,
        )
        assert result is None
        mock_s3_client.put_object.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_download_failure_returns_none(self, mock_urlopen):
        import urllib.error
        err = urllib.error.HTTPError("https://x", 500, "S", {}, None)
        err.read = MagicMock(return_value=b"")
        mock_urlopen.side_effect = err

        result = process_onedrive_file(
            workspace_id="ws", connection_id="c1",
            file_info={
                "filename": "x.pdf",
                "download_url": "https://x",
                "drive_id": "d",
            },
            sync_id=None,
        )
        assert result is None

    @patch("urllib.request.urlopen")
    def test_s3_error_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(b"x")
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "PutObject"
        )

        result = process_onedrive_file(
            workspace_id="ws", connection_id="c1",
            file_info={
                "filename": "x.pdf",
                "download_url": "https://x",
                "drive_id": "d",
            },
            sync_id=None,
        )
        assert result is None


# ============================================================================
# lambda_handler dispatch - onedrive source
# ============================================================================


class TestLambdaHandlerDispatchOneDrive:

    @patch.object(handler_module, "process_onedrive_file")
    def test_onedrive_source_routes_to_process_onedrive_file(self, mock_process):
        mock_process.return_value = "workspaces/ws/onedrive/c/x.pdf"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "onedrive",
                    "workspace_id": "ws",
                    "connection_id": "c",
                    "file": {
                        "drive_id": "drv",
                        "filename": "x.pdf",
                        "download_url": "https://x",
                    },
                    "sync_id": "s",
                })
            }]
        }
        handler_module.lambda_handler(event, None)
        mock_process.assert_called_once()
        kwargs = mock_process.call_args[1]
        assert kwargs["workspace_id"] == "ws"
        assert kwargs["connection_id"] == "c"
