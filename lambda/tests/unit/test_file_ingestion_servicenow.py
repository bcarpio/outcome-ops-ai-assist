# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for file-ingestion Lambda - ServiceNow branches."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


os.environ.setdefault("SERVICENOW_INSTANCE_URL", "https://acme.service-now.com")
os.environ.setdefault("SERVICENOW_CLIENT_ID", "test-client-id")

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
process_servicenow_article = handler_module.process_servicenow_article
process_servicenow_attachment = handler_module.process_servicenow_attachment

# Force-load the real shared modules so the process functions' deferred
# imports resolve to the same module objects the tests patch.
# ``from shared.oauth import servicenow`` inside the process functions
# does a package attribute lookup on ``shared.oauth``, not a
# ``sys.modules["shared.oauth.servicenow"]`` lookup, so ``patch.dict``
# on sys.modules would silently not intercept.
from shared.oauth import servicenow as sn_oauth  # noqa: E402
from shared import servicenow_api as sn_api  # noqa: E402


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_dynamodb_resource.reset_mock()
    mock_s3_client.reset_mock()
    handler_module.KB_BUCKET = "test-kb-bucket"
    handler_module._account_id = "123456789012"
    yield


class TestProcessArticle:
    @patch.object(handler_module.dual_write, "s3_put_object")
    @patch.object(sn_api, "get_article", return_value={
        "sys_id": "art-1",
        "number": "KB0001",
        "short_description": "Reset your password",
        "text": "<p>Follow these steps to reset your password.</p>",
    })
    @patch.object(sn_oauth, "refresh_access_token", return_value="access-tok")
    def test_uploads_wrapped_html_and_returns_key(
        self, mock_refresh, mock_get, mock_put
    ):
        key = process_servicenow_article(
            workspace_id="ws1",
            connection_id="conn-1",
            file_info={"article_sys_id": "art-1"},
            instance_url="https://acme.service-now.com",
        )
        assert key == "workspaces/ws1/servicenow/conn-1/art-1.html"
        mock_put.assert_called_once()
        put_kwargs = mock_put.call_args.kwargs
        assert put_kwargs["Bucket"] == "test-kb-bucket"
        assert put_kwargs["ContentType"] == "text/html"
        body = put_kwargs["Body"].decode("utf-8")
        assert "Reset your password" in body
        assert "Follow these steps" in body
        assert put_kwargs["Metadata"]["source"] == "servicenow"
        assert put_kwargs["Metadata"]["servicenow_article_sys_id"] == "art-1"

    def test_missing_article_sys_id_returns_none(self):
        key = process_servicenow_article(
            workspace_id="ws1", connection_id="conn-1",
            file_info={}, instance_url="https://acme.service-now.com",
        )
        assert key is None

    @patch.object(handler_module.dual_write, "s3_put_object")
    @patch.object(sn_api, "get_article", return_value={
        "sys_id": "art-1", "number": "KB0001",
        "short_description": "hi", "text": "<p>x</p>",
    })
    @patch.object(sn_oauth, "refresh_access_token", return_value="tok")
    def test_missing_instance_url_falls_back_to_env(
        self, mock_refresh, mock_get, mock_put
    ):
        # instance_url="" now falls back to SERVICENOW_INSTANCE_URL env var
        # (deploy-scoped). Upload succeeds.
        key = process_servicenow_article(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"article_sys_id": "art-1"},
            instance_url="",
        )
        assert key == "workspaces/ws1/servicenow/conn-1/art-1.html"

    def test_missing_instance_url_and_env_unconfigured_returns_none(self):
        with patch.object(
            sn_oauth,
            "get_instance_url",
            side_effect=ValueError("SERVICENOW_INSTANCE_URL not configured"),
        ):
            key = process_servicenow_article(
                workspace_id="ws1", connection_id="conn-1",
                file_info={"article_sys_id": "art-1"},
                instance_url="",
            )
        assert key is None

    @patch.object(sn_oauth, "refresh_access_token", side_effect=ValueError("expired"))
    def test_token_refresh_failure_returns_none(self, mock_refresh):
        key = process_servicenow_article(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"article_sys_id": "art-1"},
            instance_url="https://acme.service-now.com",
        )
        assert key is None

    @patch.object(sn_api, "get_article", return_value=None)
    @patch.object(sn_oauth, "refresh_access_token", return_value="tok")
    def test_vanished_article_returns_none(self, mock_refresh, mock_get):
        key = process_servicenow_article(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"article_sys_id": "art-1"},
            instance_url="https://acme.service-now.com",
        )
        assert key is None


class TestProcessAttachment:
    @patch.object(handler_module.dual_write, "s3_put_object")
    @patch.object(sn_api, "get_attachment_bytes", return_value=b"PDF-bytes")
    @patch.object(sn_oauth, "refresh_access_token", return_value="tok")
    def test_uploads_binary_with_content_type(
        self, mock_refresh, mock_get, mock_put
    ):
        key = process_servicenow_attachment(
            workspace_id="ws1",
            connection_id="conn-1",
            file_info={
                "attachment_sys_id": "att-1",
                "article_sys_id": "art-1",
                "file_name": "guide.pdf",
                "content_type": "application/pdf",
            },
            instance_url="https://acme.service-now.com",
        )
        assert key == "workspaces/ws1/servicenow/conn-1/art-1/attachments/guide.pdf"
        put_kwargs = mock_put.call_args.kwargs
        assert put_kwargs["Body"] == b"PDF-bytes"
        assert put_kwargs["ContentType"] == "application/pdf"
        assert put_kwargs["Metadata"]["source"] == "servicenow_attachment"

    def test_missing_required_fields_returns_none(self):
        # Missing article_sys_id
        key = process_servicenow_attachment(
            workspace_id="ws1", connection_id="conn-1",
            file_info={"attachment_sys_id": "a", "file_name": "x.pdf"},
            instance_url="https://acme.service-now.com",
        )
        assert key is None

