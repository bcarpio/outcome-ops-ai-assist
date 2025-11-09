"""
Unit tests for list-recent-docs Lambda handler.

Tests cover happy path scenarios including:
- Valid requests with different limit values
- Default limit behavior
- Empty table scenario
"""
import json
import os
from datetime import datetime, timezone
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from lambda.handlers.list_recent_docs import handler


@pytest.fixture
def lambda_context():
    """Create a mock Lambda context object."""
    context = Mock()
    context.function_name = "test-list-recent-docs"
    context.request_id = "test-request-id"
    context.log_group_name = "/aws/lambda/test-list-recent-docs"
    context.log_stream_name = "test-log-stream"
    return context


@mock_aws
def test_handler_success_with_default_limit(lambda_context):
    """Test handler returns recent documents with default limit (10)."""
    # Arrange - Set up DynamoDB table with test data
    table_name = "test-documents-table"
    os.environ["TABLE_NAME"] = table_name
    
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"}
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"}
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "updated_at_index",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5
                }
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    )
    
    # Add 15 test documents to verify limit works
    for i in range(15):
        timestamp = datetime(now(tz=timezone.utc).replace(microsecond=0).isoformat()
        dynamodb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"repo#test-repo"},
                "SK": {"S": f"adr#ADR-{15 - i}"},
                "updated_at": {"S": timestamp},
                "title": {"S": f"Test Document {i + 1}"},
                "content": {"S": f"This is test document {i + 1}"},
                "file_path": {"S": f"docs/test-{i + 1}.md"}
            }
        )
    
    # Act - Invoke handler without limit (should use default of 10)
    event = {}
    response = handler(event, lambda_context)
    
    # Assert - Verify response
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert "documents" in body
    assert "count" in body
    assert body["count"] == 10  # Default limit
    assert len(body["documents"]) == 10
    
    # Verify documents are sorted by updated_at (most recent first)
    documents = body["documents"]
    for i in range(len(documents) - 1):
        assert documents[i]["updated_at"] >= documents[i + 1]["updated_at"]
    
    # Verify document structure
    first_doc = documents[0]
    assert "id" in first_doc
    assert "title" in first_doc
    assert "file_path" in first_doc
    assert "updated_at" in first_doc
    assert "content" not in first_doc  # Content should not be included


@mock_aws
def test_handler_success_with_custom_limit_5(lambda_context):
    """Test handler returns correct number of documents with custom limit of 5."""
    # Arrange
    table_name = "test-documents-table"
    os.environ["TABLE_NAME"] = table_name
    
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH'},
            {"AttributeName": "SK", "KeyType": "RANGE"}
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"}
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "updated_at_index",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5
                }
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    )
    
    # Add 10 test documents
    for i in range(10):
        timestamp = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
        dynamodb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"repo#test-repo"},
                "SK": {"S": f"adr#ADR-{10 - i}"},
                "updated_at": {"S": timestamp},
                "title": {"S": f"Test Document {i + 1}"},
                "content": {"S": f"This is test document {i + 1}"},
                "file_path": {"S": f"docs/test-{i + 1}.md"}
            }
        )
    
    # Act - Invoke handler with limit of 5
    event = {
        "queryStringParameters": {
            "limit": "5"
        }
    }
    response = handler(event, lambda_context)
    
    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 5
    assert len(body["documents"]) == 5


@mock_aws
def test_handler_success_with_custom_limit_20(lambda_context):
    """Test handler returns correct number of documents with custom limit of 20."""
    # Arrange
    table_name = "test-documents-table"
    os.environ["TABLE_NAME"] = table_name
    
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH'},
            {"AttributeName": "SK", "KeyType": "RANGE"}
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"}
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "updated_at_index",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5
                }
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    )
    
    # Add 25 test documents to verify limit works
    for i in range(25):
        timestamp = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
        dynamodb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"repo#test-repo"},
                "SK": {"S": f"adr#ADR-{25 - i}"},
                "updated_at": {"S": timestamp},
                "title": {"S": f"Test Document {i + 1}"},
                "content": {"S": f"This is test document {i + 1}"},
                "file_path": {"S": f"docs/test-{i + 1}.md"}
            }
        )
    
    # Act - Invoke handler with limit of 20
    event = {
        "queryStringParameters": {
            "limit": "20"
        }
    }
    response = handler(event, lambda_context)
    
    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 20
    assert len(body["documents"]) == 20


@mock_aws
def test_handler_success_with_custom_limit_1(lambda_context):
    """Test handler returns correct number of documents with custom limit of 1."""
    # Arrange
    table_name = "test-documents-table"
    os.environ["TABLE_NAME"] = table_name
    
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH'},
            {"AttributeName": "SK", "KeyType": "RANGE"}
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"}
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "updated_at_index",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5
                }
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    )
    
    # Add 5 test documents
    for i in range(5):
        timestamp = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
        dynamodb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"repo#test-repo"},
                "SK": {"S": f"adr#ADR-{5 - i}"},
                "updated_at": {"S": timestamp},
                "title": {"S": f"Test Document {i + 1}"},
                "content": {"S": f"This is test document {i + 1}"},
                "file_path": {"S": f"docs/test-{i + 1}.md"}
            }
        )
    
    # Act - Invoke handler with limit of 1
    event = {
        "queryStringParameters": {
            "limit": "1"
        }
    }
    response = handler(event, lambda_context)
    
    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 1
    assert len(body["documents"]) == 1


@mock_aws
def test_handler_success_with_empty_table(lambda_context):
    """Test handler returns empty list when table has no documents."""
    # Arrange - Create empty table
    table_name = "test-documents-table"
    os.environ["TABLE_NAME"] = table_name
    
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"}
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"}
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "updated_at_index",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5
                }
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    )
    
    # Do not add any items - table is empty
    
    # Act - Invoke handler
    event = {}
    response = handler(event, lambda_context)
    
    # Assert - Verify empty response
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert "documents" in body
    assert "count" in body
    assert body["count"] == 0
    assert body["documents"] == []


@mock_aws
def test_handler_success_with_fewer_docs_than_limit(lambda_context):
    """Test handler returns all documents when table has fewer than requested limit."""
    # Arrange
    table_name = "test-documents-table"
    os.environ["TABLE_NAME"] = table_name
    
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH'},
            {"AttributeName": "SK", "KeyType": "RANGE"}
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"}
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "updated_at_index",
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"}
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5
                }
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    )
    
    # Add only 3 documents
    for i in range(3):
        timestamp = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
        dynamodb.put_item(
            TableName=table_name,
            Item={
                "PK": {"S": f"repo#test-repo"},
                "SK": {"S": f"adr#ADR-{3 - i}"},
                "updated_at": {"S": timestamp},
                "title": {"S": f"Test Document {i + 1}"},
                "content": {"S": f"This is test document {i + 1}"},
                "file_path": {"S": f"docs/test-{i + 1}.md"}
            }
        )
    
    # Act - Invoke handler with limit of 10 (but only 3 docs exist)
    event = {
        "queryStringParameters": {
            "limit": "10"
        }
    }
    response = handler(event, lambda_context)
    
    # Assert - Should return all 3 documents, not 10
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 3
    assert len(body["documents"]) == 3
