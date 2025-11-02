"""Integration tests for ingest-docs Lambda handler flow."""

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_dynamodb, mock_s3

from handler import handler


@mock_s3
@mock_dynamodb
@patch("handler.GITHUB_TOKEN", "test-token")
@patch("handler.github_api_request")
@patch("handler.github_api_raw_content")
def test_handler_ingest_single_adr(mock_github_raw, mock_github_api, s3, dynamodb):
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
        "# ADR-001\nTest ADR",  # First call for ADR
        "# README\nTest README",  # Second call for root README
    ]

    # Mock SSM and Bedrock
    with patch("handler.ssm_client") as mock_ssm, patch("handler.bedrock_client") as mock_bedrock:
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-kb"}},  # KB_BUCKET
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-code-maps"}},  # CODE_MAPS_TABLE
            {"Parameter": {"Value": "test-token"}},  # GITHUB_TOKEN
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
        assert body["documents_ingested"] > 0

        # Verify S3 has documents
        response = s3_client.list_objects_v2(Bucket="dev-outcome-ops-ai-assist-kb")
        assert response["KeyCount"] > 0

        # Verify DynamoDB has items
        table = dynamodb_resource.Table("dev-outcome-ops-ai-assist-code-maps")
        scan_response = table.scan()
        assert scan_response["Count"] > 0


@mock_s3
@mock_dynamodb
@patch("handler.GITHUB_TOKEN", "test-token")
@patch("handler.github_api_request")
def test_handler_github_api_error(mock_github_api):
    """Test handler gracefully handles GitHub API errors."""
    # Arrange
    mock_github_api.side_effect = Exception("GitHub API error")

    with patch("handler.ssm_client") as mock_ssm:
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


@mock_s3
@mock_dynamodb
@patch("handler.GITHUB_TOKEN", "test-token")
def test_handler_with_empty_allowlist():
    """Test handler with invalid/empty allowlist."""
    # Arrange
    with patch("handler.ssm_client") as mock_ssm, patch(
        "builtins.open", create=True
    ) as mock_open:
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-kb"}},
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-code-maps"}},
            {"Parameter": {"Value": "test-token"}},
        ]

        # Simulate invalid YAML
        mock_open.return_value.__enter__.return_value.read.return_value = ""

        # Act - This will raise an error
        with pytest.raises(Exception):
            handler({}, {})


@mock_s3
@mock_dynamodb
@patch("handler.GITHUB_TOKEN", "test-token")
@patch("handler.github_api_request")
@patch("handler.github_api_raw_content")
@patch("handler.bedrock_client")
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

    with patch("handler.ssm_client") as mock_ssm:
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-kb"}},
            {"Parameter": {"Value": "dev-outcome-ops-ai-assist-code-maps"}},
            {"Parameter": {"Value": "test-token"}},
        ]

        # Act
        result = handler({}, {})

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # Should have ingested 4 documents (3 ADRs + 1 README)
        assert body["documents_ingested"] == 4

        # Verify DynamoDB has all items
        table = dynamodb_resource.Table("dev-outcome-ops-ai-assistant-code-maps")
        scan_response = table.scan()
        assert scan_response["Count"] >= 4
