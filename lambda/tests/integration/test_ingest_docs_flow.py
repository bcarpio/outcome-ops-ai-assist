"""Integration tests for ingest-docs Lambda handler flow."""

import json
import sys
import os
import importlib.util
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# Import the ingest-docs handler explicitly to avoid conflicts with other handler modules
_handler_path = os.path.join(os.path.dirname(__file__), '..', '..', 'ingest-docs', 'handler.py')
_spec = importlib.util.spec_from_file_location("ingest_docs_handler", _handler_path)
ingest_docs_handler = importlib.util.module_from_spec(_spec)
sys.modules["ingest_docs_handler"] = ingest_docs_handler
_spec.loader.exec_module(ingest_docs_handler)
handler = ingest_docs_handler.handler


@pytest.fixture(autouse=True)
def reset_config():
    """Ensure module-level config reloads each test."""
    ingest_docs_handler.KB_BUCKET = None
    ingest_docs_handler.CODE_MAPS_TABLE = None
    ingest_docs_handler.GITHUB_TOKEN = None


@mock_aws
@patch("ingest_docs_handler.GITHUB_TOKEN", "test-token")
@patch("ingest_docs_handler.github_api_request")
@patch("ingest_docs_handler.github_api_raw_content")
def test_handler_ingest_single_adr(mock_github_raw, mock_github_api):
    """Test handler ingesting a single ADR."""
    # Arrange - Set up AWS resources
    s3_client = boto3.client("s3", region_name="us-west-2")
    s3_client.create_bucket(
        Bucket="dev-outcome-ops-ai-assist-kb",
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )

    dynamodb_resource = boto3.resource("dynamodb", region_name="us-west-2")
    dynamodb_resource.create_table(
        TableName="dev-outcome-ops-ai-assist-code-maps",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Mock GitHub API responses
    mock_github_api.return_value = [
        {"name": "ADR-001-test.md", "path": "docs/adr/ADR-001-test.md", "type": "file"}
    ]
    mock_github_raw.side_effect = [
        "# ADR-001\nTest ADR",       # docs/adr/ADR-001-test.md
        "# README\nTest README",     # README.md
        "# Docs README\nMore docs"   # docs/README.md
    ]

    # Mock SSM and Bedrock
    with patch("ingest_docs_handler.ssm_client") as mock_ssm, patch("ingest_docs_handler.bedrock_client") as mock_bedrock:
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-kb"}},  # KB_BUCKET
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-code-maps"}},  # CODE_MAPS_TABLE
            {"Parameter": {"Value": "test-token"}},  # GITHUB_TOKEN
            {"Parameter": {"Value": json.dumps({"repos": [{"name": "test-repo", "project": "test-owner"}]})}},  # repos-allowlist
        ]

        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({"embedding": [0.1] * 1024}).encode())
        }

        # Act
        result = handler({}, {})

        # Assert - Verify response
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["message"] == "Document ingestion completed"
        assert body["total_docs_ingested"] == 3

        # Verify S3 has documents
        response = s3_client.list_objects_v2(Bucket="dev-outcome-ops-ai-assist-kb")
        assert response["KeyCount"] > 0

        # Verify DynamoDB has items
        table = dynamodb_resource.Table("dev-outcome-ops-ai-assist-code-maps")
        scan_response = table.scan()
        assert scan_response["Count"] == 3
        assert scan_response["Count"] > 0


@mock_aws
@patch("ingest_docs_handler.GITHUB_TOKEN", "test-token")
@patch("ingest_docs_handler.github_api_request")
def test_handler_github_api_error(mock_github_api):
    """Test handler gracefully handles GitHub API errors."""
    # Arrange
    mock_github_api.side_effect = Exception("GitHub API error")

    with patch("ingest_docs_handler.ssm_client") as mock_ssm:
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-kb"}},
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-code-maps"}},
            {"Parameter": {"Value": "test-token"}},
        ]

        # Act
        result = handler({}, {})

        # Assert - Handler should return error response
        assert result["statusCode"] == 500
        assert "error" in result["body"].lower()


@mock_aws
@patch("ingest_docs_handler.GITHUB_TOKEN", "test-token")
def test_handler_with_empty_allowlist():
    """Handler should return 500 when allowlist JSON is invalid."""
    with patch("ingest_docs_handler.ssm_client") as mock_ssm:
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-kb"}},
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-code-maps"}},
            {"Parameter": {"Value": "test-token"}},
            {"Parameter": {"Value": ""}},  # Invalid JSON
        ]

        result = handler({}, {})

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "expecting value" in body["error"].lower()


@mock_aws
@patch("ingest_docs_handler.GITHUB_TOKEN", "test-token")
@patch("ingest_docs_handler.github_api_request")
@patch("ingest_docs_handler.github_api_raw_content")
@patch("ingest_docs_handler.bedrock_client")
def test_handler_multiple_documents(mock_bedrock, mock_github_raw, mock_github_api):
    """Test handler ingesting multiple documents."""
    # Arrange - Set up AWS resources
    s3_client = boto3.client("s3", region_name="us-west-2")
    s3_client.create_bucket(
        Bucket="dev-outcome-ops-ai-assist-kb",
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )

    dynamodb_resource = boto3.resource("dynamodb", region_name="us-west-2")
    dynamodb_resource.create_table(
        TableName="dev-outcome-ops-ai-assist-code-maps",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Mock GitHub API to return multiple ADRs
    mock_github_api.return_value = [
        {"name": "ADR-001.md", "path": "docs/adr/ADR-001.md", "type": "file"},
        {"name": "ADR-002.md", "path": "docs/adr/ADR-002.md", "type": "file"},
        {"name": "ADR-003.md", "path": "docs/adr/ADR-003.md", "type": "file"},
    ]

    # Mock GitHub raw content for ADRs and READMEs
    mock_github_raw.side_effect = [
        "# ADR-001\nContent 1",  # ADR-001
        "# ADR-002\nContent 2",  # ADR-002
        "# ADR-003\nContent 3",  # ADR-003
        "# README\nRoot readme",  # Root README
    ]

    mock_bedrock.invoke_model.return_value = {
        "body": MagicMock(read=lambda: json.dumps({"embedding": [0.1] * 1024}).encode())
    }

    with patch("ingest_docs_handler.ssm_client") as mock_ssm:
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-kb"}},
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-code-maps"}},
            {"Parameter": {"Value": "test-token"}},
            {"Parameter": {"Value": json.dumps({"repos": [{"name": "test-repo", "project": "test-owner"}]})}},  # repos-allowlist
        ]

        # Act
        result = handler({}, {})

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["total_docs_ingested"] == 4  # 3 ADRs + 1 README

        # Verify DynamoDB has all items
        table = dynamodb_resource.Table("dev-outcome-ops-ai-assist-code-maps")
        scan_response = table.scan()
        assert scan_response["Count"] >= 4
