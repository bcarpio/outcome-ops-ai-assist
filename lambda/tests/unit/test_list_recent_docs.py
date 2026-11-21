# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the list-recent-docs Lambda (KB ingestion verification).

Tests cover:
- Request validation (Pydantic: limit bounds, defaults, workspace scope)
- SSM config loading and per-container caching
- Delegation to shared.retrieval.list_recent_documents
- Error mapping (S3 Vectors ClientError -> 502, unexpected -> 500)
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

with mock_aws():
    handler_path = os.path.join(os.path.dirname(__file__), "../../list-recent-docs/handler.py")
    spec = importlib.util.spec_from_file_location("list_recent_docs_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["list_recent_docs_handler"] = handler_module
    spec.loader.exec_module(handler_module)

handler = handler_module.handler
load_vector_config = handler_module.load_vector_config


@pytest.fixture(autouse=True)
def _reset_config():
    """Each test starts with a cold container (no cached vector config)."""
    handler_module._vector_config = None
    yield
    handler_module._vector_config = None


@pytest.fixture
def ssm():
    client = MagicMock()
    client.get_parameter.side_effect = lambda Name: {
        "Parameter": {"Value": "bucket-x" if Name.endswith("bucket-name") else "index-y"}
    }
    with patch.object(handler_module, "ssm_client", client):
        yield client


@pytest.fixture
def list_docs():
    result = {
        "documents": [
            {
                "key": "k1",
                "workspace_id": "ws-1",
                "repo": "org/app",
                "doc_type": "adr",
                "file_path": "workspaces/ws-1/github/org/app/docs/adr/ADR-001.md",
                "ingested_at": "2026-11-18T13:00:00+00:00",
            }
        ],
        "scanned": 42,
        "truncated": False,
    }
    with patch.object(
        handler_module.shared_retrieval, "list_recent_documents", return_value=result
    ) as mock:
        yield mock


class TestRequestValidation:
    def test_defaults_limit_to_10(self, ssm, list_docs):
        # Act
        response = handler({}, None)

        # Assert
        assert response["statusCode"] == 200
        assert list_docs.call_args.kwargs["limit"] == 10
        assert list_docs.call_args.kwargs["workspace_id"] is None

    def test_none_event_treated_as_empty(self, ssm, list_docs):
        assert handler(None, None)["statusCode"] == 200

    @pytest.mark.parametrize("limit", [0, 101, -5, "ten"])
    def test_rejects_out_of_range_limit(self, ssm, list_docs, limit):
        # Act
        response = handler({"limit": limit}, None)

        # Assert
        assert response["statusCode"] == 400
        assert response["body"]["error"] == "Invalid request"
        assert tuple(response["body"]["details"][0]["loc"]) == ("limit",)
        list_docs.assert_not_called()

    @pytest.mark.parametrize("limit", [1, 100])
    def test_accepts_boundary_limits(self, ssm, list_docs, limit):
        assert handler({"limit": limit}, None)["statusCode"] == 200
        assert list_docs.call_args.kwargs["limit"] == limit

    def test_rejects_empty_workspace_id(self, ssm, list_docs):
        assert handler({"workspace_id": ""}, None)["statusCode"] == 400

    def test_passes_workspace_scope(self, ssm, list_docs):
        handler({"workspace_id": "ws-1"}, None)
        assert list_docs.call_args.kwargs["workspace_id"] == "ws-1"


class TestSuccess:
    def test_returns_documents_and_scan_stats(self, ssm, list_docs):
        # Act
        response = handler({"limit": 5}, None)

        # Assert
        body = response["body"]
        assert body["count"] == 1
        assert body["scanned"] == 42
        assert body["truncated"] is False
        assert body["documents"][0]["doc_type"] == "adr"
        kwargs = list_docs.call_args.kwargs
        assert kwargs["vector_bucket"] == "bucket-x"
        assert kwargs["vector_index"] == "index-y"
        assert kwargs["max_scan"] == handler_module.MAX_SCAN


class TestConfig:
    def test_loads_from_env_scoped_ssm_paths(self, ssm):
        config = load_vector_config()

        assert config == {"bucket": "bucket-x", "index": "index-y"}
        names = [c.kwargs["Name"] for c in ssm.get_parameter.call_args_list]
        prefix = f"/{handler_module.ENVIRONMENT}/{handler_module.APP_NAME}/s3vectors"
        assert names == [f"{prefix}/bucket-name", f"{prefix}/index-name"]

    def test_caches_per_container(self, ssm):
        load_vector_config()
        load_vector_config()

        assert ssm.get_parameter.call_count == 2  # bucket + index, once


class TestErrors:
    def test_ssm_failure_returns_502(self, list_docs):
        failing = MagicMock()
        failing.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "missing"}}, "GetParameter"
        )
        with patch.object(handler_module, "ssm_client", failing):
            response = handler({}, None)

        assert response["statusCode"] == 502
        assert "ParameterNotFound" in response["body"]["error"]
        list_docs.assert_not_called()

    def test_s3vectors_client_error_returns_502(self, ssm):
        err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListVectors")
        with patch.object(handler_module.shared_retrieval, "list_recent_documents", side_effect=err):
            response = handler({}, None)

        assert response["statusCode"] == 502
        assert response["body"]["error"] == "Failed to list documents (AccessDeniedException)"

    def test_unexpected_error_returns_500_without_leaking_detail(self, ssm):
        with patch.object(
            handler_module.shared_retrieval, "list_recent_documents", side_effect=RuntimeError("boom")
        ):
            response = handler({}, None)

        assert response == {"statusCode": 500, "body": {"error": "Internal error"}}
