#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for list_recent_docs Lambda handler.
"""

import json
import os
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError


@pytest.fixture(autouse=True)
def setup_environment(monkeypatch):
    """Setup environment variables for tests."""
    monkeypatch.setenv("TABLE_NAME", "test-documents-table")
    monkeypatch.setenv("LOG_LEVEL", "INFO")


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB client."""
    with patch("boto3.resource") as mock_resource:
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        yield mock_table


@pytest.fixture
def handler():
    """Import handler after environment is setup."""
    from list_recent_docs.handler import lambda_handler
    return lambda_handler


def test_handler_success_default_limit(handler, mock_dynamodb):
    """Test successful retrieval with default limit."""
    # Mock DynamoDB response
    mock_items = [
        {
            "document_id": "doc-1",
            "user_id": "user-123",
            "title": "Document 1",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
            "status": "active",
            "version": Decimal("1")
        },
        {
            "document_id": "doc-2",
            "user_id": "user-123",
            "title": "Document 2",
            "created_at": "2024-01-14T10:00:00Z",
            "updated_at": "2024-01-14T10:00:00Z",
            "status": "active",
            "version": Decimal("1")
        }
    ]
    
    mock_dynamodb.query.return_value = {
        "Items": mock_items,
        "Count": 2,
        "ScannedCount": 2
    }
    
    # Create event
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    # Invoke handler
    response = handler(event, {})
    
    # Assertions
    assert response["statusCode"] == 200
    
    body = json.loads(response["body"])
    assert "documents" in body
    assert len(body["documents"]) == 2
    assert body["documents"][0]["document_id"] == "doc-1"
    assert body["documents"][0]["version"] == 1  # Decimal converted to int
    assert body["count"] == 2
    
    # Verify DynamoDB query was called correctly
    mock_dynamodb.query.assert_called_once_with(
        KeyConditionExpression="#user_id = :user_id",
        ExpressionAttributeNames={"#user_id": "user_id"},
        ExpressionAttributeValues={":user_id": "user-123"},
        ScanIndexForward=False,
        Limit=10
    )


def test_handler_success_custom_limit(handler, mock_dynamodb):
    """Test successful retrieval with custom limit."""
    mock_items = [
        {
            "document_id": "doc-1",
            "user_id": "user-123",
            "title": "Document 1",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
            "status": "active",
            "version": Decimal("1")
        }
    ]
    
    mock_dynamodb.query.return_value = {
        "Items": mock_items,
        "Count": 1,
        "ScannedCount": 1
    }
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        },
        "queryStringParameters": {
            "limit": "5"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 200
    
    body = json.loads(response["body"])
    assert len(body["documents"]) == 1
    
    # Verify custom limit was used
    mock_dynamodb.query.assert_called_once_with(
        KeyConditionExpression="#user_id = :user_id",
        ExpressionAttributeNames={"#user_id": "user_id"},
        ExpressionAttributeValues={":user_id": "user-123"},
        ScanIndexForward=False,
        Limit=5
    )


def test_handler_success_no_documents(handler, mock_dynamodb):
    """Test successful response when no documents exist."""
    mock_dynamodb.query.return_value = {
        "Items": [],
        "Count": 0,
        "ScannedCount": 0
    }
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 200
    
    body = json.loads(response["body"])
    assert body["documents"] == []
    assert body["count"] == 0


def test_handler_success_fewer_than_limit(handler, mock_dynamodb):
    """Test successful response when fewer documents than limit."""
    mock_items = [
        {
            "document_id": "doc-1",
            "user_id": "user-123",
            "title": "Document 1",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
            "status": "active",
            "version": Decimal("1")
        }
    ]
    
    mock_dynamodb.query.return_value = {
        "Items": mock_items,
        "Count": 1,
        "ScannedCount": 1
    }
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        },
        "queryStringParameters": {
            "limit": "100"  # Limit higher than available documents
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 200
    
    body = json.loads(response["body"])
    assert len(body["documents"]) == 1
    assert body["count"] == 1


def test_handler_missing_user_id(handler, mock_dynamodb):
    """Test error when userId is missing."""
    event = {
        "pathParameters": {}
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 400
    
    body = json.loads(response["body"])
    assert "error" in body
    assert "userId" in body["error"].lower()
    
    # Verify DynamoDB was not called
    mock_dynamodb.query.assert_not_called()


def test_handler_empty_user_id(handler, mock_dynamodb):
    """Test error when userId is empty."""
    event = {
        "pathParameters": {
            "userId": ""
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 400
    
    body = json.loads(response["body"])
    assert "error" in body
    assert "userId" in body["error"].lower()
    
    mock_dynamodb.query.assert_not_called()


def test_handler_invalid_limit_non_numeric(handler, mock_dynamodb):
    """Test error when limit is not a number."""
    event = {
        "pathParameters": {
            "userId": "user-123"
        },
        "queryStringParameters": {
            "limit": "invalid"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 400
    
    body = json.loads(response["body"])
    assert "error" in body
    assert "limit" in body["error"].lower()
    
    mock_dynamodb.query.assert_not_called()


def test_handler_invalid_limit_too_small(handler, mock_dynamodb):
    """Test error when limit is less than 1."""
    event = {
        "pathParameters": {
            "userId": "user-123"
        },
        "queryStringParameters": {
            "limit": "0"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 400
    
    body = json.loads(response["body"])
    assert "error" in body
    assert "limit" in body["error"].lower()
    
    mock_dynamodb.query.assert_not_called()


def test_handler_invalid_limit_too_large(handler, mock_dynamodb):
    """Test error when limit exceeds maximum."""
    event = {
        "pathParameters": {
            "userId": "user-123"
        },
        "queryStringParameters": {
            "limit": "1001"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 400
    
    body = json.loads(response["body"])
    assert "error" in body
    assert "limit" in body["error"].lower()
    
    mock_dynamodb.query.assert_not_called()


def test_handler_dynamodb_client_error(handler, mock_dynamodb):
    """Test handling of DynamoDB client errors."""
    # Mock DynamoDB to raise ClientError
    error_response = {
        "Error": {
            "Code": "ResourceNotFoundException",
            "Message": "Requested resource not found"
        }
    }
    mock_dynamodb.query.side_effect = ClientError(error_response, "Query")
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 500
    
    body = json.loads(response["body"])
    assert "error" in body
    assert "database" in body["error"].lower() or "failed" in body["error"].lower()


def test_handler_dynamodb_generic_error(handler, mock_dynamodb):
    """Test handling of generic DynamoDB errors."""
    mock_dynamodb.query.side_effect = Exception("Unexpected error")
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 500
    
    body = json.loads(response["body"])
    assert "error" in body


def test_handler_missing_path_parameters(handler, mock_dynamodb):
    """Test error when pathParameters is missing entirely."""
    event = {}
    
    response = handler(event, {})
    
    assert response["statusCode"] == 400
    
    body = json.loads(response["body"])
    assert "error" in body
    
    mock_dynamodb.query.assert_not_called()


def test_handler_cors_headers(handler, mock_dynamodb):
    """Test that CORS headers are included in response."""
    mock_dynamodb.query.return_value = {
        "Items": [],
        "Count": 0,
        "ScannedCount": 0
    }
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    response = handler(event, {})
    
    assert "headers" in response
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert "Content-Type" in response["headers"]
    assert response["headers"]["Content-Type"] == "application/json"


def test_handler_error_response_format(handler, mock_dynamodb):
    """Test that error responses have consistent format."""
    event = {
        "pathParameters": {
            "userId": ""
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 400
    assert "headers" in response
    assert "body" in response
    
    body = json.loads(response["body"])
    assert "error" in body
    assert isinstance(body["error"], str)


def test_handler_success_response_format(handler, mock_dynamodb):
    """Test that success responses have correct format."""
    mock_items = [
        {
            "document_id": "doc-1",
            "user_id": "user-123",
            "title": "Document 1",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
            "status": "active",
            "version": Decimal("1")
        }
    ]
    
    mock_dynamodb.query.return_value = {
        "Items": mock_items,
        "Count": 1,
        "ScannedCount": 1
    }
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 200
    assert "headers" in response
    assert "body" in response
    
    body = json.loads(response["body"])
    assert "documents" in body
    assert "count" in body
    assert isinstance(body["documents"], list)
    assert isinstance(body["count"], int)


def test_handler_decimal_conversion(handler, mock_dynamodb):
    """Test that Decimal values are converted to int/float."""
    mock_items = [
        {
            "document_id": "doc-1",
            "user_id": "user-123",
            "title": "Document 1",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
            "status": "active",
            "version": Decimal("42"),
            "size": Decimal("1024.5")
        }
    ]
    
    mock_dynamodb.query.return_value = {
        "Items": mock_items,
        "Count": 1,
        "ScannedCount": 1
    }
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    response = handler(event, {})
    
    assert response["statusCode"] == 200
    
    body = json.loads(response["body"])
    document = body["documents"][0]
    
    # Verify Decimal values are converted
    assert document["version"] == 42
    assert isinstance(document["version"], int)
    assert document["size"] == 1024.5
    assert isinstance(document["size"], float)


def test_handler_logging_on_error(handler, mock_dynamodb, caplog):
    """Test that errors are logged properly."""
    error_response = {
        "Error": {
            "Code": "ResourceNotFoundException",
            "Message": "Test error"
        }
    }
    mock_dynamodb.query.side_effect = ClientError(error_response, "Query")
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    with caplog.at_level("error"):
        response = handler(event, {})
    
    assert response["statusCode"] == 500
    
    # Verify error was logged
    assert len(caplog.records) > 0
    assert any("record.levelname == "ERROR" for record in caplog.records)


def test_handler_logging_on_success(handler, mock_dynamodb, caplog):
    """Test that successful requests are logged."""
    mock_dynamodb.query.return_value = {
        "Items": [],
        "Count": 0,
        "ScannedCount": 0
    }
    
    event = {
        "pathParameters": {
            "userId": "user-123"
        }
    }
    
    with caplog.at_level("info"):
        response = handler(event, {})
    
    assert response["statusCode"] == 200
    
    # Verify info logs were created
    assert len(caplog.records) > 0
    assert any("record.levelname == "INFO" for record in caplog.records)
