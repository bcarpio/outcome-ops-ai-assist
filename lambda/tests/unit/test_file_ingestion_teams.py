"""Unit tests for file-ingestion Lambda -- Teams file handler.

Covers the chunk 03 etag safety net + the base process_teams_file flow.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading (mirrors test_file_ingestion_onedrive.py)
# ============================================================================

mock_dynamodb_resource = MagicMock()
mock_s3_client = MagicMock()
mock_ssm_client = MagicMock()


def _load_handler_module():
    if "file_ingestion_handler" in sys.modules:
        del sys.modules["file_ingestion_handler"]

    fi_dir = os.path.join(os.path.dirname(__file__), "../../file-ingestion")
    if fi_dir not in sys.path:
        sys.path.insert(0, fi_dir)

    handler_path = os.path.join(fi_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "file_ingestion_handler", handler_path,
    )
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["file_ingestion_handler"] = handler_module
    spec.loader.exec_module(handler_module)

    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.s3_client = mock_s3_client
    handler_module.ssm_client = mock_ssm_client
    return handler_module


handler_module = _load_handler_module()
process_teams_file = handler_module.process_teams_file
_teams_file_unchanged = handler_module._teams_file_unchanged


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_dynamodb_resource.reset_mock()
    mock_s3_client.reset_mock()
    mock_s3_client.put_object.side_effect = None
    mock_s3_client.head_object.side_effect = None
    handler_module.KB_BUCKET = "test-kb-bucket"
    handler_module._account_id = "123456789012"
    yield


def _make_urlopen_response(content_bytes):
    resp = MagicMock()
    resp.read.return_value = content_bytes
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _not_found_error():
    return ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "HeadObject",
    )


def _server_error():
    return ClientError(
        {"Error": {"Code": "500", "Message": "InternalError"}},
        "HeadObject",
    )


def _sample_file_info(**overrides):
    base = {
        "item_id": "01ABC",
        "channel_name": "General",
        "filename": "notes.md",
        "download_url": "https://download.example/file",
        "mime_type": "text/markdown",
        "size": 42,
        "etag": '"etag-current"',
    }
    base.update(overrides)
    return base


# ============================================================================
# _teams_file_unchanged (chunk 03)
# ============================================================================


class TestTeamsFileUnchanged:
    """Fail-safe contract: only return True on a clean match. Any error
    path returns False so the caller takes the download+put flow."""

    def test_returns_true_when_etag_matches(self):
        mock_s3_client.head_object.return_value = {
            "Metadata": {"teams_etag": '"same"'},
        }
        assert _teams_file_unchanged("workspaces/ws/teams/g/files/x", '"same"') is True

    def test_returns_false_when_etag_mismatches(self):
        mock_s3_client.head_object.return_value = {
            "Metadata": {"teams_etag": '"old"'},
        }
        assert _teams_file_unchanged("k", '"new"') is False

    def test_returns_false_when_no_stored_etag(self):
        mock_s3_client.head_object.return_value = {"Metadata": {}}
        assert _teams_file_unchanged("k", '"any"') is False

    def test_returns_false_on_404(self):
        mock_s3_client.head_object.side_effect = _not_found_error()
        assert _teams_file_unchanged("k", '"e"') is False

    def test_returns_false_on_5xx_but_logs(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        mock_s3_client.head_object.side_effect = _server_error()
        assert _teams_file_unchanged("k", '"e"') is False
        assert any("head_object failed" in rec.message for rec in caplog.records)


# ============================================================================
# process_teams_file: etag safety net + download+put flow
# ============================================================================


class TestProcessTeamsFileEtagSafetyNet:

    @patch("urllib.request.urlopen")
    def test_matching_etag_short_circuits(self, mock_urlopen, caplog):
        """Head_object matches -> no download, no put; returns the s3
        key so the caller sees a normal success path."""
        import logging
        caplog.set_level(logging.INFO)
        mock_s3_client.head_object.return_value = {
            "Metadata": {"teams_etag": '"etag-current"'},
        }
        s3_key = process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(),
            sync_id="s",
        )
        assert s3_key == "workspaces/ws/teams/General/files/notes.md"
        mock_urlopen.assert_not_called()
        mock_s3_client.put_object.assert_not_called()
        assert any("[teams-file-skip]" in rec.message for rec in caplog.records)

    @patch("urllib.request.urlopen")
    def test_mismatched_etag_falls_through_to_put(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(b"body-bytes")
        mock_s3_client.head_object.return_value = {
            "Metadata": {"teams_etag": '"etag-old"'},
        }
        s3_key = process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(),
            sync_id="s",
        )
        assert s3_key == "workspaces/ws/teams/General/files/notes.md"
        mock_s3_client.put_object.assert_called_once()
        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Body"] == b"body-bytes"
        assert call_kwargs["Metadata"]["teams_etag"] == '"etag-current"'

    @patch("urllib.request.urlopen")
    def test_404_first_upload_puts(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(b"body")
        mock_s3_client.head_object.side_effect = _not_found_error()
        s3_key = process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(),
            sync_id="s",
        )
        assert s3_key is not None
        mock_s3_client.put_object.assert_called_once()
        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Metadata"]["teams_etag"] == '"etag-current"'

    @patch("urllib.request.urlopen")
    def test_5xx_falls_through_to_put(self, mock_urlopen):
        """Fail-safe: a broken head_object never silently drops a real
        change. Handler still puts."""
        mock_urlopen.return_value = _make_urlopen_response(b"body")
        mock_s3_client.head_object.side_effect = _server_error()
        s3_key = process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(),
            sync_id="s",
        )
        assert s3_key is not None
        mock_s3_client.put_object.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_missing_etag_in_body_skips_check_and_puts(self, mock_urlopen):
        """When teams-sync didn't send an etag (edge case), don't call
        head_object -- just put."""
        mock_urlopen.return_value = _make_urlopen_response(b"body")
        s3_key = process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(etag=""),
            sync_id="s",
        )
        assert s3_key is not None
        mock_s3_client.head_object.assert_not_called()
        mock_s3_client.put_object.assert_called_once()
        # Metadata omits teams_etag when we didn't have one.
        assert "teams_etag" not in mock_s3_client.put_object.call_args[1]["Metadata"]


class TestProcessTeamsFileBaseFlow:

    @patch("urllib.request.urlopen")
    def test_uploads_with_expected_metadata(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(b"body")
        mock_s3_client.head_object.side_effect = _not_found_error()
        process_teams_file(
            workspace_id="ws-test", connection_id="conn-abc",
            file_info=_sample_file_info(
                channel_name="Design & Docs",
                filename="RFC-01.md",
            ),
            sync_id="sync-01",
        )
        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-kb-bucket"
        assert call_kwargs["Body"] == b"body"
        assert call_kwargs["ContentType"] == "text/markdown"
        metadata = call_kwargs["Metadata"]
        assert metadata["source"] == "teams_file"
        assert metadata["workspace_id"] == "ws-test"
        assert metadata["connection_id"] == "conn-abc"
        assert metadata["teams_channel"] == "Design & Docs"
        assert metadata["teams_item_id"] == "01ABC"
        assert metadata["teams_etag"] == '"etag-current"'
        assert metadata["sync_id"] == "sync-01"

    def test_returns_none_when_download_url_missing(self):
        result = process_teams_file(
            workspace_id="ws", connection_id="c",
            file_info=_sample_file_info(download_url=""),
            sync_id="s",
        )
        assert result is None
        mock_s3_client.head_object.assert_not_called()
        mock_s3_client.put_object.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_put_error_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(b"body")
        mock_s3_client.head_object.side_effect = _not_found_error()
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "PutObject",
        )
        result = process_teams_file(
            workspace_id="ws", connection_id="c",
            file_info=_sample_file_info(),
            sync_id="s",
        )
        assert result is None


class TestTwoLayerDefensePayoff:
    """Chunk 04 integration: exercises chunks 02 and 03 together across
    the SQS boundary. Proves the pattern the whole plan was designed to
    close -- even when Graph's delta returns an unchanged item on a
    subsequent cycle (metadata-only save, permission edit, etc.), the
    etag safety net catches it and short-circuits before the S3 put and
    downstream process-documents re-embedding fire."""

    @patch("urllib.request.urlopen")
    def test_second_cycle_with_same_etag_skips_upload(self, mock_urlopen, caplog):
        """Cycle 1: 404 -> put with teams_etag stored.
        Cycle 2: head_object returns same etag -> short-circuit."""
        import logging
        caplog.set_level(logging.INFO)
        mock_urlopen.return_value = _make_urlopen_response(b"body-v1")

        # Cycle 1: first upload.
        mock_s3_client.head_object.side_effect = _not_found_error()
        s3_key_1 = process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(etag='"cycle-etag"'),
            sync_id="cycle-1",
        )
        assert s3_key_1 is not None
        assert mock_s3_client.put_object.call_count == 1
        stored_etag = mock_s3_client.put_object.call_args[1]["Metadata"]["teams_etag"]
        assert stored_etag == '"cycle-etag"'

        # Cycle 2: same file, same etag. head_object now returns a match.
        mock_s3_client.reset_mock()
        mock_urlopen.reset_mock()
        mock_s3_client.head_object.side_effect = None
        mock_s3_client.head_object.return_value = {
            "Metadata": {"teams_etag": stored_etag},
        }
        s3_key_2 = process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(etag='"cycle-etag"'),
            sync_id="cycle-2",
        )
        assert s3_key_2 == s3_key_1
        mock_urlopen.assert_not_called()
        mock_s3_client.put_object.assert_not_called()
        assert any("[teams-file-skip]" in rec.message for rec in caplog.records)

    @patch("urllib.request.urlopen")
    def test_second_cycle_with_new_etag_re_uploads(self, mock_urlopen):
        """The safety net does NOT short-circuit a real change -- if
        Graph reports a new etag, the file gets downloaded and the S3
        metadata is updated with the new etag."""
        mock_urlopen.return_value = _make_urlopen_response(b"body-v2")

        # Old etag stored from a prior cycle.
        mock_s3_client.head_object.return_value = {
            "Metadata": {"teams_etag": '"cycle-etag-v1"'},
        }
        process_teams_file(
            workspace_id="ws", connection_id="c1",
            file_info=_sample_file_info(etag='"cycle-etag-v2"'),
            sync_id="cycle-2",
        )

        mock_s3_client.put_object.assert_called_once()
        put_kwargs = mock_s3_client.put_object.call_args[1]
        assert put_kwargs["Body"] == b"body-v2"
        assert put_kwargs["Metadata"]["teams_etag"] == '"cycle-etag-v2"'
