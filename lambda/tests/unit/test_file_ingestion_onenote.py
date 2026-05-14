"""Unit tests for file-ingestion Lambda - OneNote page handler."""

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
process_onenote_page = handler_module.process_onenote_page


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
    before each invocation."""
    stub = MagicMock()
    stub.validate_license.return_value = {"sub": "test-customer"}
    allowed = MagicMock()
    allowed.allowed = True
    allowed.reason = None
    allowed.current = 0
    allowed.limit = 100
    stub.check_repo_limit.return_value = allowed
    saved = sys.modules.get("outcomeops_license")
    sys.modules["outcomeops_license"] = stub
    yield stub
    if saved is not None:
        sys.modules["outcomeops_license"] = saved
    else:
        sys.modules.pop("outcomeops_license", None)


# ============================================================================
# process_onenote_page
# ============================================================================


class TestProcessOneNotePage:

    def test_uploads_html_with_correct_key_shape(self):
        """OneNote pages land at
        workspaces/{ws}/onenote/{conn}/{notebook}/{section}/{page_id}.html
        — page_id as the filename stem is what makes delete-worker's orphan
        check a direct string match (no HeadObject)."""
        file_info = {
            "page_id": "page-1",
            "title": "Meeting Notes",
            "notebook_id": "nb_a",
            "notebook_name": "Personal",
            "section_id": "sec_1",
            "section_name": "Q2",
            "last_modified": "2026-05-10T12:00:00Z",
            "content_html": "<html><body>actual content here</body></html>",
        }

        s3_key = process_onenote_page(
            workspace_id="ws_test",
            connection_id="conn_abc",
            file_info=file_info,
            sync_id="sync_01",
        )

        assert s3_key == "workspaces/ws_test/onenote/conn_abc/nb_a/sec_1/page-1.html"

        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-kb-bucket"
        # Body is the HTML, encoded as UTF-8
        assert b"actual content here" in call_kwargs["Body"]
        assert call_kwargs["ContentType"] == "text/html"

        metadata = call_kwargs["Metadata"]
        assert metadata["source"] == "onenote"
        assert metadata["workspace_id"] == "ws_test"
        assert metadata["connection_id"] == "conn_abc"
        assert metadata["onenote_page_id"] == "page-1"
        assert metadata["onenote_notebook_id"] == "nb_a"
        assert metadata["onenote_section_id"] == "sec_1"
        assert metadata["onenote_notebook_name"] == "Personal"
        assert metadata["onenote_section_name"] == "Q2"
        assert metadata["onenote_title"] == "Meeting Notes"
        assert metadata["onenote_last_modified"] == "2026-05-10T12:00:00Z"
        assert metadata["sync_id"] == "sync_01"

    def test_sanitizes_unsafe_chars_in_id_segments(self):
        """Slashes inside Graph IDs MUST be sanitized — otherwise they'd
        introduce extra hierarchy into the S3 key and break orphan detection
        (which expects exactly one segment per id)."""
        file_info = {
            "page_id": "page/with/slashes",
            "title": "T",
            "notebook_id": "nb/x",
            "notebook_name": "N",
            "section_id": "sec/1",
            "section_name": "S",
            "last_modified": "",
            "content_html": "<html><body>xyz</body></html>",
        }

        s3_key = process_onenote_page(
            workspace_id="ws", connection_id="c1",
            file_info=file_info, sync_id=None,
        )

        # Exactly 6 slashes: workspaces/ws/onenote/c1/{nb}/{sec}/{page}.html
        segments = s3_key.split("/")
        assert len(segments) == 7
        assert segments[0] == "workspaces"
        assert segments[1] == "ws"
        assert segments[2] == "onenote"
        assert segments[3] == "c1"
        # Each id segment is one piece — internal "/" got sanitized to "_"
        assert "/" not in segments[4]
        assert "/" not in segments[5]
        assert segments[4] == "nb_x"
        assert segments[5] == "sec_1"
        assert segments[6] == "page_with_slashes.html"

    def test_page_id_in_filename_uses_sanitized_form(self):
        """The page_id in the filename stem is the EXACT form that
        onenote-sync stores in known_page_ids (sanitize_filename(page_id)),
        so delete-worker's orphan match works without further normalization."""
        file_info = {
            "page_id": "weird id  with spaces",
            "title": "T",
            "notebook_id": "nb",
            "notebook_name": "N",
            "section_id": "sec",
            "section_name": "S",
            "last_modified": "",
            "content_html": "<html>x</html>",
        }

        s3_key = process_onenote_page(
            workspace_id="ws", connection_id="c1",
            file_info=file_info, sync_id=None,
        )

        # filename stem == sanitize_filename(page_id) == "weird_id_with_spaces"
        filename = s3_key.split("/")[-1]
        assert filename == "weird_id_with_spaces.html"

    def test_missing_page_id_returns_none(self):
        result = process_onenote_page(
            workspace_id="ws", connection_id="c1",
            file_info={"page_id": "", "content_html": "<html>x</html>"},
            sync_id=None,
        )
        assert result is None
        mock_s3_client.put_object.assert_not_called()

    def test_empty_content_returns_none(self):
        """Empty/tiny HTML body is a no-op — would produce no useful
        embeddings."""
        result = process_onenote_page(
            workspace_id="ws", connection_id="c1",
            file_info={"page_id": "p1", "content_html": ""},
            sync_id=None,
        )
        assert result is None
        mock_s3_client.put_object.assert_not_called()

    def test_default_notebook_section_when_missing(self):
        """If notebook_id / section_id are blank, the key still has well-
        formed segments rather than collapsing to weird shapes."""
        file_info = {
            "page_id": "p1",
            "title": "T",
            "notebook_id": "",
            "notebook_name": "",
            "section_id": "",
            "section_name": "",
            "last_modified": "",
            "content_html": "<html><body>content here</body></html>",
        }

        s3_key = process_onenote_page(
            workspace_id="ws", connection_id="c1",
            file_info=file_info, sync_id=None,
        )

        # workspaces/ws/onenote/c1/_default/_default/p1.html
        assert s3_key == "workspaces/ws/onenote/c1/_default/_default/p1.html"

    def test_s3_error_returns_none(self):
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "PutObject"
        )

        result = process_onenote_page(
            workspace_id="ws", connection_id="c1",
            file_info={
                "page_id": "p1",
                "notebook_id": "nb",
                "section_id": "sec",
                "content_html": "<html><body>xyz</body></html>",
            },
            sync_id=None,
        )
        assert result is None


# ============================================================================
# lambda_handler dispatch - onenote source
# ============================================================================


class TestLambdaHandlerDispatchOneNote:

    @patch.object(handler_module, "process_onenote_page")
    def test_onenote_source_routes_to_process_onenote_page(self, mock_process):
        mock_process.return_value = "workspaces/ws/onenote/c/nb/sec/p1.html"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "onenote",
                    "workspace_id": "ws",
                    "connection_id": "c",
                    "file": {
                        "page_id": "p1",
                        "notebook_id": "nb",
                        "section_id": "sec",
                        "content_html": "<html>x</html>",
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
        # file_info passes through intact
        assert kwargs["file_info"]["page_id"] == "p1"
        assert kwargs["sync_id"] == "s"
