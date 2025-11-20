"""
Unit tests for list_recent_docs Lambda handler.

Tests cover happy path scenarios for successful request handling.
"""

import json
import pytest
from datetime import datetime, timezone
from moto import mock_aws
import boto3

from lambda.list_recent_docs.handler import handler


@pytest.mark.unit
class TestListRecentDocsHappyPath:
    """
    Test class for happy path scenarios of list_recent_docs Lambda.
    """

    @pytest.fixture(autouse=True)
    def mock_aws_env(self, monkeypatch):
        """
        Set up mock AWS environment variables.
        """
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("DYNAMODB_TABLE_NAME", "test-table")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setenv("AWS_SECURITY_TOKEN", "test")

    @pytest.fixture
    def dynamodb_table(self):
        """
        Create a mock DynamoDB table for testing.
        """
        with mock_aws():
            dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
            
            # Create table
            table = dynamodb.create_table(
                TableName="test-table",
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"}
                ],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"}
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "GSI1",
                        "KeySchema": [
                            {"AttributeName": "SK", "KeyType": "HASH"},
                            {"AttributeName": "created_at", "KeyType": "RANGE"}
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                        "ProvisionedThroughput": {
                            "ReadCapacityUnits": 5,
                            "WriteCapacityUnits": 5
                        }
                    }
                ],
                ProvisionedThroughput={
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5
                }
            )
            
            yield table

    @pytest.fixture
    def sample_documents(self):
        """
        Create sample documents for testing.
        """
        return [
            {
                "PK": "DOC#001",
                "SK": "META",
                "document_id": "001",
                "file_path": "docs/architecture/adr-001.md",
                "repository_name": "outcome-ops-ai-assist",
                "document_type": "adr",
                "source": "github",
                "created_at": "2024-01-15T10:00:00Z",
                "updated_at": "2024-01-15T10:00:00Z"
            }
        ]