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
    def mock_aws_env(self, monpatch):
        """
        Set up mock AWS environment variables.
        """
        monpatch.setenv("AWS_REGION", "us-east-1")
        monpatch.setenv("DYNAMODB_TABLE_NAME", "test-table")
        monpatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monpatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monpatch.setenv("AWS_SECURITY_TOKEN", "test")

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
                            {"AttributeName": "SK", "KeyType": "HASH'},
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
            },
            {
                "PK": "DOC#002",
                "SK": "META",
                "document_id": "002",
                "file_path": "docs/architecture/adr-002.md",
                "repository_name": "outcome-ops-ai-assist",
                "document_type": "adr",
                "source": "github",
                "created_at": "2024-01-14T10:00:00Z",
                "updated_at": "2024-01-14T10:00:00Z"
            },
            {
                "PK": "DOC#003",
                "SK": "META",
                "document_id": "003",
                "file_path": "README.md",
                "repository_name": "outcome-ops-ai-assist",
                "document_type": "readme",
                "source": "github",
                "created_at": "2024-01-13T10:00:00Z",
                "updated_at": "2024-01-13T10:00:00Z"
            },
            {
                "PK": "DOC#004",
                "SK": "META",
                "document_id": "004",
                "file_path": "docs/guides/getting-started.md",
                "repository_name": "outcome-ops-ai-assist",
                "document_type": "guide",
                "source": "github",
                "created_at": "2024-01-12T10:00:00Z",
                "updated_at": "2024-01-12T10:00:00Z"
            },
            {
                "PK": "DOC#005",
                "SK": "META",
                "document_id": "005",
                "file_path": "docs/api/reference.md",
                "repository_name": "outcome-ops-ai-assist",
                "document_type": "api",
                "source": "github",
                "created_at": "2024-01-11T10:00:00Z",
                "updated_at": "2024-01-11T10:00:00Z"
            },
            {
                "PK": "DOC#006",
                "SK": "META",
                "document_id": "006",
                "file_path": "docs/deployment/aws.md",
                "repository_name": "outcome-ops-ai-assist",
                "document_type": "deployment",
                "source": "github",
                "created_at": "2024-01-10T10:00:00Z",
                "updated_at": "2024-01-10T10:00:00Z"
            }
        ]

    def test_valid_request_with_limit_5(self, dynamodb_table, sample_documents):
        """
        Test successful request with limit=5.
        
        Verifies that the handler returns a 200 status code and at most 5 documents
        sorted by created_at in descending order.
        """
        with mock_aws():
            # Populate table with sample data
            for doc in sample_documents:
                dynamodb_table.put_item(Item=doc)
            
            # Create test event
            event = {
                "queryStringParameters": {
                    "limit": "5"
                }
            }
            
            # Invoke handler
            response = handler(event, {})
            
            # Assertions
            assert response["statusCode"] == 200
            
            body = json.loads(response["body"])
            assert "documents" in body
            assert isinstance(body["documents"], list)
            assert len(body["documents"]) <= 5
            assert len(body["documents"]) == 5  # We have 6 docs, so should get 5
            
            # Verify documents are sorted by created_at descending
            documents = body["documents"]
            for i in range(len(documents) - 1):
                assert documents[i]["created_at"] >= documents[i + 1]["created_at"]
            
            # Verify document structure
            for doc in documents:
                assert "document_id" in doc
                assert "file_path" in doc
                assert "repository_name" in doc
                assert "document_type" in doc
                assert "source" in doc
                assert "created_at" in doc
                assert "updated_at" in doc
                # PK and SK should not be in response
                assert "PK" not in doc
                assert "SK" not in doc
            
            # Verify Cors headers
            assert "headers" in response
            assert "Access-Control-Allow-Origin" in response["headers"]
            assert "Access-Control-Allow-Headers" in response["headers"]

    def test_valid_request_with_default_limit(self, dynamodb_table, sample_documents):
        """
        Test successful request with default limit (no limit provided).
        
        Verifies that the handler returns a 200 status code and uses the default
        limit of 10 documents.
        """
        with mock_aws():
            # Populate table with sample data
            for doc in sample_documents:
                dynamodb_table.put_item(Item=doc)
            
            # Create test event without limit parameter
            event = {
                "queryStringParameters": None
            }
            
            # Invoke handler
            response = handler(event, {})
            
            # Assertions
            assert response["statusCode"] == 200
            
            body = json.loads(response["body"])
            assert "documents" in body
            assert isinstance(body["documents"], list)
            # We have 6 docs, so should get all 6 with default limit of 10
            assert len(body["documents"]) == 6
            assert len(body["documents"]) <= 10
            
            # Verify documents are sorted by created_at descending
            documents = body["documents"]
            for i in range(len(documents) - 1):
                assert documents[i]["created_at"] >= documents[i + 1]["created_at"]
            
            # Verify document structure
            for doc in documents:
                assert "document_id" in doc
                assert "file_path" in doc
                assert "repository_name" in doc
                assert "document_type" in doc
                assert "source" in doc
                assert "created_at" in doc
                assert "updated_at" in doc
                # PK and SK should not be in response
                assert "PK" not in doc
                assert "SK" not in doc

    def test_empty_table_returns_empty_array(self, dynamodb_table):
        """
        Test successful request with empty table.
        
        Verifies that the handler returns a 200 status code and an empty array
        when the table contains no documents.
        """
        with mock_aws():
            # Do not populate table - leave it empty
            
            # Create test event
            event = {
                "queryStringParameters": None
            }
            
            # Invoke handler
            response = handler(event, {})
            
            # Assertions
            assert response["statusCode"] == 200
            
            body = json.loads(response["body"])
            assert "documents" in body
            assert isinstance(body["documents"], list)
            assert len(body["documents"]) == 0
            
            # Verify CORS headers
            assert "headers" in response
            assert "Access-Control-Allow-Origin" in response["headers"]
            assert "Access-Control-Allow-Headers" in response["headers"]
