"""Unit tests for file-ingestion Lambda -- Salesforce branches (chunk 05)."""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


os.environ.setdefault("SALESFORCE_CLIENT_ID", "test-client-id")

mock_dynamodb_resource = MagicMock()
mock_s3_client = MagicMock()
mock_ssm_client = MagicMock()


def _load_handler_module():
    if "file_ingestion_handler" in sys.modules:
        del sys.modules["file_ingestion_handler"]
    fi_dir = os.path.join(os.path.dirname(__file__), "../../file-ingestion")
    if fi_dir not in sys.path:
        sys.path.insert(0, fi_dir)
    lambda_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    handler_path = os.path.join(fi_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("file_ingestion_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["file_ingestion_handler"] = handler_module
    spec.loader.exec_module(handler_module)
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.s3_client = mock_s3_client
    handler_module.ssm_client = mock_ssm_client
    return handler_module


handler_module = _load_handler_module()
process_salesforce_record = handler_module.process_salesforce_record
process_salesforce_attachment = handler_module.process_salesforce_attachment
_build_repo_identifier = handler_module._build_repo_identifier


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_dynamodb_resource.reset_mock()
    mock_s3_client.reset_mock()
    handler_module.KB_BUCKET = "test-kb-bucket"
    handler_module._account_id = "123456789012"
    yield


class TestProcessRecord:
    @patch.object(handler_module.dual_write, "s3_put_object")
    def test_uploads_markdown_and_returns_key(self, mock_put):
        key = process_salesforce_record(
            workspace_id="ws1",
            connection_id="conn-1",
            file_info={
                "record_id": "001XXX",
                "object_type": "Account",
                "title": "Acme Corp",
                "markdown": "---\nobject: Account\n---\n\n# Acme Corp\n",
            },
            instance_url="https://acme.my.salesforce.com",
        )
        assert key == "workspaces/ws1/salesforce/conn-1/account/001XXX.md"
        mock_put.assert_called_once()
        kwargs = mock_put.call_args.kwargs
        assert kwargs["Bucket"] == "test-kb-bucket"
        assert kwargs["ContentType"] == "text/markdown"
        assert kwargs["Body"] == b"---\nobject: Account\n---\n\n# Acme Corp\n"
        assert kwargs["Metadata"]["source"] == "salesforce"
        assert kwargs["Metadata"]["salesforce_record_id"] == "001XXX"
        assert kwargs["Metadata"]["salesforce_object_type"] == "Account"

    def test_missing_record_id_returns_none(self):
        key = process_salesforce_record(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"object_type": "Account", "markdown": "x"},
        )
        assert key is None

    def test_missing_markdown_returns_none(self):
        key = process_salesforce_record(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"record_id": "001", "object_type": "Account"},
        )
        assert key is None

    def test_object_type_hint_used_when_file_info_missing(self):
        with patch.object(handler_module.dual_write, "s3_put_object") as mock_put:
            key = process_salesforce_record(
                workspace_id="ws1", connection_id="conn-1",
                file_info={"record_id": "003", "markdown": "x"},
                object_type_hint="Contact",
            )
        assert key == "workspaces/ws1/salesforce/conn-1/contact/003.md"


class TestRepoIdentifier:
    def test_salesforce_account_uses_instance_url(self):
        ident = _build_repo_identifier(
            source="salesforce_account",
            workspace_id="ws1",
            body={"instance_url": "https://acme.my.salesforce.com"},
            file_info={"record_id": "001XXX"},
        )
        assert ident == "ws1:salesforce_account:https___acme.my.salesforce.com"

    def test_salesforce_contact_same_ident_shape(self):
        ident = _build_repo_identifier(
            source="salesforce_contact",
            workspace_id="ws1",
            body={"instance_url": "https://acme.my.salesforce.com"},
            file_info={"record_id": "003"},
        )
        assert ident.endswith(":https___acme.my.salesforce.com")

    def test_salesforce_missing_instance_url_empty(self):
        ident = _build_repo_identifier(
            source="salesforce_opportunity",
            workspace_id="ws1",
            body={},
            file_info={},
        )
        assert ident == "ws1:salesforce_opportunity:"

    def test_salesforce_attachment_shares_org_identity(self):
        # Attachments count against the same license entry as parent records
        # (per ADR-019, one Salesforce org == one licensable repo).
        ident = _build_repo_identifier(
            source="salesforce_attachment",
            workspace_id="ws1",
            body={"instance_url": "https://acme.my.salesforce.com"},
            file_info={"content_version_id": "068AAA"},
        )
        assert ident == "ws1:salesforce_attachment:https___acme.my.salesforce.com"


class TestProcessAttachment:
    """Chunk 01 -- file-ingestion side of the ContentVersion port."""

    @patch.object(handler_module.dual_write, "s3_put_object")
    @patch("shared.salesforce_api.download_content_version",
           return_value=b"%PDF-1.4\nfake proposal bytes")
    @patch("shared.oauth.salesforce.refresh_access_token",
           return_value=("access-tok", "https://acme.my.salesforce.com"))
    def test_downloads_and_uploads_binary(
        self, mock_refresh, mock_download, mock_put,
    ):
        key = process_salesforce_attachment(
            workspace_id="ws1",
            connection_id="conn-1",
            file_info={
                "content_version_id": "068AAA",
                "content_document_id": "069AAA",
                "parent_record_id": "006OOO",
                "file_name": "SDS_VT_Industries_SDLC_Modernization_Proposal.pdf",
            },
            sync_id="sync-abc",
            instance_url="https://acme.my.salesforce.com",
        )
        assert key == (
            "workspaces/ws1/salesforce/conn-1/attachments/006OOO/"
            "SDS_VT_Industries_SDLC_Modernization_Proposal.pdf"
        )
        mock_refresh.assert_called_once_with("ws1", "conn-1")
        mock_download.assert_called_once_with(
            "https://acme.my.salesforce.com", "access-tok", "068AAA",
        )
        kwargs = mock_put.call_args.kwargs
        assert kwargs["Body"] == b"%PDF-1.4\nfake proposal bytes"
        assert kwargs["Metadata"]["source"] == "salesforce_attachment"
        assert kwargs["Metadata"]["salesforce_content_version_id"] == "068AAA"
        assert kwargs["Metadata"]["salesforce_content_document_id"] == "069AAA"
        assert kwargs["Metadata"]["salesforce_parent_record_id"] == "006OOO"
        assert kwargs["Metadata"]["sync_id"] == "sync-abc"

    def test_missing_version_id_returns_none(self):
        key = process_salesforce_attachment(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"parent_record_id": "006", "file_name": "x.pdf"},
        )
        assert key is None

    def test_missing_parent_id_returns_none(self):
        key = process_salesforce_attachment(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"content_version_id": "068", "file_name": "x.pdf"},
        )
        assert key is None

    def test_missing_file_name_returns_none(self):
        key = process_salesforce_attachment(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"content_version_id": "068", "parent_record_id": "006"},
        )
        assert key is None

    @patch("shared.oauth.salesforce.refresh_access_token",
           side_effect=ValueError("expired"))
    def test_refresh_failure_returns_none(self, mock_refresh):
        key = process_salesforce_attachment(
            workspace_id="ws1", connection_id="conn-1",
            file_info={
                "content_version_id": "068",
                "parent_record_id": "006",
                "file_name": "x.pdf",
            },
            instance_url="https://acme.my.salesforce.com",
        )
        assert key is None

    @patch("shared.salesforce_api.download_content_version",
           side_effect=RuntimeError("network"))
    @patch("shared.oauth.salesforce.refresh_access_token",
           return_value=("tok", "https://acme.my.salesforce.com"))
    def test_download_failure_returns_none(self, mock_refresh, mock_download):
        key = process_salesforce_attachment(
            workspace_id="ws1", connection_id="conn-1",
            file_info={
                "content_version_id": "068",
                "parent_record_id": "006",
                "file_name": "x.pdf",
            },
            instance_url="https://acme.my.salesforce.com",
        )
        assert key is None
