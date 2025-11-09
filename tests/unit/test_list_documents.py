"""
Unit tests for list_documents Lambda handler.
"""

import json
import os
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from pydantic import ValidationError

from src.list_documents.handler import handler
from src.list_documents.schemas import ListDocumentsRequest


@pytest.fixture
def dynamodb_table():
    """Create mocked DynamoDB table for testing."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH },
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalIecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def create_api_gateway_event(body: dict) -> dict:
    """Create a mock API Gateway event."""
    return {
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {"claims": {"email": "test@test.com"}}
        },
    }


def add_document_to_table(
    table,
    user_email: str,
    document_id: str,
    document_type: str,
    title: str,
    created_at: str,
    updated_at: str,
    content: str = "Test content",
    tags: list = None,
):
    """Add a document to the mocked DynamoDB table."""
    table.put_item(
        Item={
            "PK": {"S": f"USER#{user_email}"},
            "SK": {"S": f"DOC# {document_id}"},
            "GSI1PK": {"S": f"USER#{user_email}#DOCS"},
            "GSI1SK": {"S": f"{created_at}#{document_id}"},
            "document_id": {"S": document_id},
            "document_type": {"S": document_type},
            "title": {"S": title},
            "content": {"S": content},
            "created_at": {"S": created_at},
            "updated_at": {"S": updated_at},
            "tags": {"L": [{"S": t} for t in (tags or [])]},
        }
    )


class TestListDocumentsSuccess:
    """Test successful list documents operations."""

    @mock_aws
    def test_list_documents_success():
        """Test successful listing of documents."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH },
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        add_document_to_table(
            table,
            "test@test.com",
            "doc1",
            "adr",
            "Test Document 1",
            "2024-01-01T12:00:00Z",
            "2024-01-01T12:00:00Z",
        )
        add_document_to_table(
            table,
            "test@test.com",
            "doc2",
            "rfc",
            "Test Document 2",
            "2024-01-02T12:00:00Z",
            "2024-01-02T12:00:00Z",
        )

        event = create_api_gateway_event({"limit": 10})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "documents" in body
        assert len(body["documents"]) == 2
        assert body["documents"][0]["document_id"] == "doc2"  # Most recent first
        assert body["documents"][1]["document_id"] == "doc1"

    @mock_aws
    def test_list_documents_with_limit(self):
        """Test listing documents with limit."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Add 3 documents
        for i in range(3):
            add_document_to_table(
                table,
                "test@test.com",
                f"doc{i}",
                "adr",
                f"Test Document {i}",
                f"2024-01-0{i + 1}T12:00:00Z",
                f"2024-01-0{i + 1}T12:00:00Z",
            )

        event = create_api_gateway_event({"limit": 2})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["documents"]) == 2  # Limit applied
        assert body["documents"][0]["document_id"] == "doc2"  # Most recent first

    @mock_aws
    def test_list_documents_empty_result(self):
        """Test listing documents when none exist."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        event = create_api_gateway_event({})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["documents"] == []

    @mock_aws
    def test_list_documents_fewer_than_limit(self):
        """Test listing when fewer documents exist than the limit."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Add 2 documents
        add_document_to_table(
            table,
            "test@test.com",
            "doc1",
            "adr",
            "Test Document 1",
            "2024-01-01T12:00:00Z",
            "2024-01-01T12:00:00Z",
        )
        add_document_to_table(
            table,
            "test@test.com",
            "doc2",
            "adr",
            "Test Document 2",
            "2024-01-02T12:00:00Z",
            "2024-01-02T12:00:00Z",
        )

        event = create_api_gateway_event({"limit": 10})  # Limit higher than count

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["documents"]) == 2  # Only 2 documents exist

    @mock_aws
    def test_list_documents_multiple_types(self):
        """Test listing documents with multiple document types."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Add documents of different types
        add_document_to_table(
            table,
            "test@test.com",
            "doc1",
            "adr",
            "ADR Document",
            "2024-01-01T12:00:00Z",
            "2024-01-01T12:00:00Z",
        )
        add_document_to_table(
            table,
            "test@test.com",
            "doc2",
            "rfc",
            "RFC Document",
            "2024-01-02T12:00:00Z",
            "2024-01-02T12:00:00Z",
        )
        add_document_to_table(
            table,
            "test@test.com",
            "doc3",
            "general",
            "General Document",
            "2024-01-03T12:00:00Z",
            "2024-01-03T12:00:00Z",
        )

        event = create_api_gateway_event({})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["documents"]) == 3
        # Verify all types are present
        doc_types = {doc["document_type"] for doc in body["documents"]}
        assert doc_types == {"adr", "rfc", "general"}


class TestListDocumentsValidation:
    """Test validation error cases."""

    def test_invalid_limit_zero(self):
        """Test that limit of 0 is rejected."""
        # Arrange
        event = create_api_gateway_event({"limit": 0})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()

    def test_invalid_limit_negative(self):
        """Test that negative limit is rejected."""
        # Arrange
        event = create_api_gateway_event({"limit": -5})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()

    def test_invalid_limit_too_large(self):
        """Test that limit over 100 is rejected."""
        # Arrange
        event = create_api_gateway_event({"limit": 101})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(rsponse["body"])
        assert "error" in body
        assert "limit" in body["error"].lower()

    def test_invalid_limit_type(self):
        """Test that non-integer limit is rejected."""
        # Arrange
        event = create_api_gateway_event({"limit": "invalid"})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    def test_invalid_json_body(self):
        """Test that invalid JSON in body is handled."""
        # Arrange
        event = {
            "body": "invalid json",
            "requestContext": {
                "authorizer": {"claims": {"email": "test@test.com"}}
            },
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(rsponse["body"])
        assert "error" in body

    def test_missing_authorizer_context(self):
        """Test that missing authorizer context is handled."""
        # Arrange
        event = {"body": json.dumps({})}

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body


class TestListDocumentsDynamoDBErrors:
    """Test DynamoDB error handling."""

    @mock_aws
    def test_dynamodb_table_not_found(self):
        """Test handling of table not found error."""
        # Arrange - Do not create the table
        event = create_api_gateway_event({})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    @mock_aws
    def test_dynamodb_access_denied(self, monkeypatch):
        """Test handling of DynamoDB access denied error."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH },
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Monkeypatch the query method to raise AccessDeniedException
        def mock_query(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
                "query",
            )

        monkeypatch.setattr(table, "query", mock_query)

        event = create_api_gateway_event({})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(rsponse["body"])
        assert "error" in body

    @mock_aws
    def test_dynamodb_generic_client_error(self, monkeypatch):
        """Test handling of generic DynamoDB client error."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH },
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Monkeypatch the query method to raise a generic ClientError
        def mock_query(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "Internal error"}},
                "query",
            )

        monkeypatch.setattr(table, "query", mock_query)

        event = create_api_gateway_event({})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    @mock_aws
    def test_dynamodb_unexpected_exception(self, monkeypatch):
        """Test handling of unexpected exception during DynamoDB operation."""
        # Arrange
        dynamodb = boto3.resource("dynamodb", region_name="us-west-2")
        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH },
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Monkeypatch the query method to raise an unexpected exception
        def mock_query(*args, **kwargs):
            raise ValueError("Unexpected error")

        monkeypatch.setattr(table, "query", mock_query)

        event = create_api_gateway_event({})

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body


class TestListDocumentsSchemaValidation:
    """Test Pydantic schema validation."""

    def test_schema_validation_valid_request(self):
        """Test that valid request passes schema validation."""
        # Arrange
        data = {"limit": 10}

        # Act
        request = ListDocumentsRequest(**data)

        # Assert
        assert request.limit == 10

    def test_schema_validation_default_limit(self):
        """Test that default limit is applied."""
        # Arrange
        data = {}

        # Act
        request = ListDocumentsRequest(**data)

        # Assert
        assert request.limit == 20  # Default value

    def test_schema_validation_invalid_limit_zero(self):
        """Test that zero limit fails validation."""
        # Arrange
        data = {"limit": 0}

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            ListDocumentsRequest(**data)

        assert "limit" in str(exc_info.value).lower()

    def test_schema_validation_invalid_limit_negative(self):
        """Test that negative limit fails validation."""
        # Arrange
        data = {"limit": -5}

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            ListDocumentsRequest(**data)

        assert "limit" in str(exc_info.value).lower()

    def test_schema_validation_invalid_limit_too_large(self):
        """Test that limit over 100 fails validation."""
        # Arrange
        data = {"limit": 101}

        # Act & Assert
        with pytest.raises(ValidationError) as exc_info:
            ListDocumentsRequest(**data)

        assert "limit" in str(exc_info.value).lower()

    def test_schema_validation_invalid_limit_type(self):
        """Test that non-integer limit fails validation."""
        # Arrange
        data = {"limit": "invalid"}

        # Act & Assert
        with pytest.raises(ValidationError):
            ListDocumentsRequest(**data)
