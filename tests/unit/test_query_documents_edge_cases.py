"""
Unit tests for edge cases in query_documents Lambda function.

Covers:
- Table has fewer documents than requested limit
- Multiple document types returned (ADRs, READMEs, code maps)
- Documents from multiple repositories
- Correct sorting by timestamp
- Metadata extraction
"""

import json
import pytest
from decimal import Decimal
from unittest.mock import Mock, patch


@pytest.fixture
def lambda_context():
    """Mock Lambda context."""
    context = Mock()
    context.function_name = "query-documents"
    context.request_id = "test-request-id"
    return context


@pytest.fixture
def mock_dynamodb_table():
    """Mock DynamoDB table."""
    with patch("boto3.resource") as mock_resource:
        mock_table = Mock()
        mock_resource.return_value.Table.return_value = mock_table
        yield mock_table


def test_fewer_documents_than_limit(mock_dynamodb_table, lambda_context):
    """
    Test querying when table has fewer documents than requested limit.

    Arrange: Set up DynamoDB to return only 3 documents when 10 are requested
    Act: Call Lambda handler with limit=10
    Assert: Verify only 3 documents are returned without errors
    """
    # Arrange
    from src.lambda_functions.query_documents.index import handler

    mock_dynamodb_table.scan.return_value = {
        "Items": [
            {
                "document_id": "doc1",
                "repository_id": "repo1",
                "document_type": "ADR",
                "file_path": "docs/adrs/adr-001.md",
                "title": "ADR 001",
                "content": "Content 1",
                "timestamp": Decimal("1700000000"),
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            },
            {
                "document_id": "doc2",
                "repository_id": "repo1",
                "document_type": "README",
                "file_path": "README.md",
                "title": "Readme",
                "content": "Content 2",
                "timestamp": Decimal("1700000100"),
                "created_at": "2024-01-01T11:00:00Z",
                "updated_at": "2024-01-01T11:00:00Z"
            },
            {
                "document_id": "doc3",
                "repository_id": "repo1",
                "document_type": "CODE_MAP",
                "file_path": "codemap.md",
                "title": "Code Map",
                "content": "Content 3",
                "timestamp": Decimal("1700000200"),
                "created_at": "2024-01-01T12:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z"
            }
        ]
    }

    event = {
        "queryStringParameters": {
            "limit": "10"
        }
    }

    # Act
    response = handler(event, lambda_context)

    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert len(body["documents"]) == 3
    assert body["total_count"] == 3
    assert body["limit"] == 10


def test_multiple_document_types(mock_dynamodb_table, lambda_context):
    """
    Test querying returns multiple document types (ADRs, READMEs, code maps).

    Arrange: Set up DynamoDB to return mixed document types
    Act: Call Lambda handler
    Assert: Verify all document types are present and correctly formatted
    """
    # Arrange
    from src.lambda_functions.query_documents.index import handler

    mock_dynamodb_table.scan.return_value = {
        "Items": [
            {
                "document_id": "adr-001",
                "repository_id": "repo1",
                "document_type": "ADR",
                "file_path": "docs/adrs/adr-001.md",
                "title": "ADR 001: Architecture Decision",
                "content": "ADR content",
                "timestamp": Decimal("1700000000"),
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            },
            {
                "document_id": "readme-main",
                "repository_id": "repo1",
                "document_type": "README",
                "file_path": "README.md",
                "title": "Project Readme",
                "content": "Readme content",
                "timestamp": Decimal("1700000100"),
                "created_at": "2024-01-01T11:00:00Z",
                "updated_at": "2024-01-01T11:00:00Z"
            },
            {
                "document_id": "codemap-src",
                "repository_id": "repo1",
                "document_type": "CODE_MAP",
                "file_path": "codemap.md",
                "title": "Source Code Map",
                "content": "Code map content",
                "timestamp": Decimal("1700000200"),
                "created_at": "2024-01-01T12:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z"
            },
            {
                "document_id": "adr-002",
                "repository_id": "repo1",
                "document_type": "ADR",
                "file_path": "docs/adrs/adr-002.md",
                "title": "ADR 002: Database Choice",
                "content": "Another ADR content",
                "timestamp": Decimal("1700000300"),
                "created_at": "2024-01-01T13:00:00Z",
                "updated_at": "2024-01-01T13:00:00Z"
            }
        ]
    }

    event = {
        "queryStringParameters": {}
    }

    # Act
    response = handler(event, lambda_context)

    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    documents = body["documents"]
    
    assert len(documents) == 4
    
    # Verify all document types are present
    doc_types = {doc["document_type"] for doc in documents}
    assert "ADR" in doc_types
    assert "README" in doc_types
    assert "CODE_MAP" in doc_types
    
    # Verify ADR count
    adr_count = sum(1 for doc in documents if doc["document_type"] == "ADR")
    assert adr_count == 2
    
    # Verify each document has required fields
    for doc in documents:
        assert "document_id" in doc
        assert "repository_id" in doc
        assert "document_type" in doc
        assert "file_path" in doc
        assert "title" in doc
        assert "content" in doc
        assert "timestamp" in doc


def test_multiple_repositories(mock_dynamodb_table, lambda_context):
    """
    Test querying documents from multiple repositories.

    Arrange: Set up DynamoDB to return documents from different repositories
    Act: Call Lambda handler
    Assert: Verify documents from multiple repos are returned and metadata is correct
    """
    # Arrange
    from src.lambda_functions.query_documents.index import handler

    mock_dynamodb_table.scan.return_value = {
        "Items": [
            {
                "document_id": "doc1",
                "repository_id": "frontend-repo",
                "document_type": "README",
                "file_path": "README.md",
                "title": "Frontend Readme",
                "content": "Frontend content",
                "timestamp": Decimal("1700000100"),
                "created_at": "2024-01-01T11:00:00Z",
                "updated_at": "2024-01-01T11:00:00Z"
            },
            {
                "document_id": "doc2",
                "repository_id": "backend-repo",
                "document_type": "ADR",
                "file_path": "docs/adrs/adr-001.md",
                "title": "Backend ADR",
                "content": "Backend content",
                "timestamp": Decimal("1700000200"),
                "created_at": "2024-01-01T12:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z"
            },
            {
                "document_id": "doc3",
                "repository_id": "infra-repo",
                "document_type": "CODE_MAP",
                "file_path": "codemap.md",
                "title": "Infra Code Map",
                "content": "Infra content",
                "timestamp": Decimal("1700000300"),
                "created_at": "2024-01-01T13:00:00Z",
                "updated_at": "2024-01-01T13:00:00Z"
            },
            {
                "document_id": "doc4",
                "repository_id": "frontend-repo",
                "document_type": "ADR",
                "file_path": "docs/adrs/adr-002.md",
                "title": "Frontend ADR",
                "content": "Frontend ADR content",
                "timestamp": Decimal("1700000000"),
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            }
        ]
    }

    event = {
        "queryStringParameters": {}
    }

    # Act
    response = handler(event, lambda_context)

    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    documents = body["documents"]
    
    assert len(documents) == 4
    
    # Verify multiple repositories are present
    repo_ids = {doc["repository_id"] for doc in documents}
    assert len(repo_ids) == 3
    assert "frontend-repo" in repo_ids
    assert "backend-repo" in repo_ids
    assert "infra-repo" in repo_ids
    
    # Verify each document has correct repository_id
    for doc in documents:
        assert "document_id" in doc
        assert "repository_id" in doc
        assert doc["repository_id"] in repo_ids


def test_timestamp_sorting_newest_first(mock_dynamodb_table, lambda_context):
    """
    Test documents are sorted by timestamp in descending order (newest first).

    Arrange: Set up Documents with different timestamps
    Act: Call Lambda handler
    Assert: Verify documents are sorted by timestamp descending
    """
    # Arrange
    from src.lambda_functions.query_documents.index import handler

    mock_dynamodb_table.scan.return_value = {
        "Items": [
            {
                "document_id": "oldest",
                "repository_id": "repo1",
                "document_type": "ADR",
                "file_path": "doc1.md",
                "title": "Oldest Doc",
                "content": "Content 1",
                "timestamp": Decimal("1700000000"),
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            },
            {
                "document_id": "newest",
                "repository_id": "repo1",
                "document_type": "README",
                "file_path": "doc3.md",
                "title": "Newest Doc",
                "content": "Content 3",
                "timestamp": Decimal("1700000300"),
                "created_at": "2024-01-01T13:00:00Z",
                "updated_at": "2024-01-01T13:00:00Z"
            },
            {
                "document_id": "middle",
                "repository_id": "repo1",
                "document_type": "CODE_MAP",
                "file_path": "doc2.md",
                "title": "Middle Doc",
                "content": "Content 2",
                "timestamp": Decimal("1700000200"),
                "created_at": "2024-01-01T12:00:00Z",
                "updated_at": "2024-01-01T12:00:00Z"
            }
        ]
    }

    event = {
        "queryStringParameters": {}
    }

    # Act
    response = handler(event, lambda_context)

    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    documents = body["documents"]
    
    assert len(documents) == 3
    
    # Verify sorting: newest first
    assert documents[0]["document_id"] == "newest"
    assert documents[1]["document_id"] == "middle"
    assert documents[2]["document_id"] == "oldest"
    
    # Verify timestamps are in descending order
    assert documents[0]["timestamp"] >  documents[1]["timestamp"]
    assert documents[1]["timestamp"] >  documents[2]["timestamp"]


def test_metadata_extraction(mock_dynamodb_table, lambda_context):
    """
    Test that all metadata fields are correctly extracted and returned.

    Arrange: Set up documents with complete metadata
    Act: Call Lambda handler
    Assert: Verify all metadata fields are present and correct
    """
    # Arrange
    from src.lambda_functions.query_documents.index import handler

    mock_dynamodb_table.scan.return_value = {
        "Items": [
            {
                "document_id": "doc-123",
                "repository_id": "repo-456",
                "document_type": "ADR",
                "file_path": "docs/adrs/adr-001-database-choice.md",
                "title": "ADR 001: Database Choice",
                "content": "# ADR 001: Database Choice\n\n## Context\nWe need to choose a database.",
                "timestamp": Decimal("1705000000"),
                "created_at": "2024-01-15T10:30:00Z",
                "updated_at": "2024-01-20T14:45:00Z"
            }
        ]
    }

    event = {
        "queryStringParameters": {}
    }

    # Act
    response = handler(event, lambda_context)

    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    documents = body["documents"]
    
    assert len(documents) == 1
    doc = documents[0]
    
    # Verify all metadata fields are present
    assert doc["document_id"] == "doc-123"
    assert doc["repository_id"] == "repo-456"
    assert doc["document_type"] == "ADR"
    assert doc["file_path"] == "docs/adrs/adr-001-database-choice.md"
    assert doc["title"] == "ADR 001: Database Choice"
    assert "# ADR 001: Database Choice" in doc["content"]
    assert doc["timestamp"] == 1705000000
    assert doc["created_at"] == "2024-01-15T10:30:00Z"
    assert doc["updated_at"] == "2024-01-20T14:45:00Z"
    
    # Verify no extra fields are present
    expected_fields = {
        "document_id", "repository_id", "document_type", "file_path",
        "title", "content", "timestamp", "created_at", "updated_at"
    }
    assert set(doc.keys()) == expected_fields


def test_empty_table(mock_dynamodb_table, lambda_context):
    """
    Test querying when table is empty.

    Arrange: Set up DynamoDB to return no items
    Act: Call Lambda handler
    Assert: Verify empty list is returned without errors
    """
    # Arrange
    from src.lambda_functions.query_documents.index import handler

    mock_dynamodb_table.scan.return_value = {
        "Items": []
    }

    event = {
        "queryStringParameters": {}
    }

    # Act
    response = handler(event, lambda_context)

    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["documents"] == []
    assert body["total_count"] == 0
    assert body["limit"] == 100


def test_timestamp_sorting_with_identical_timestamps(mock_dynamodb_table, lambda_context):
    """
    Test sorting behavior when multiple documents have identical timestamps.

    Arrange: Set up documents with same timestamps
    Act: Call Lambda handler
    Assert: Verify documents are returned consistently
    """
    # Arrange
    from src.lambda_functions.query_documents.index import handler

    mock_dynamodb_table.scan.return_value = {
        "Items": [
            {
                "document_id": "doc1",
                "repository_id": "repo1",
                "document_type": "ADR",
                "file_path": "doc1.md",
                "title": "Doc 1",
                "content": "Content 1",
                "timestamp": Decimal("1700000000"),
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            },
            {
                "document_id": "doc2",
                "repository_id": "repo1",
                "document_type": "README",
                "file_path": "doc2.md",
                "title": "Doc 2",
                "content": "Content 2",
                "timestamp": Decimal("1700000000"),
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            },
            {
                "document_id": "doc3",
                "repository_id": "repo1",
                "document_type": "CODE_MAP",
                "file_path": "doc3.md",
                "title": "Doc 3",
                "content": "Content 3",
                "timestamp": Decimal("1700000000"),
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T10:00:00Z"
            }
        ]
    }

    event = {
        "queryStringParameters": {}
    }

    # Act
    response = handler(event, lambda_context)

    # Assert
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    documents = body["documents"]
    
    assert len(documents) == 3
    
    # Verify all documents have the same timestamp
    for doc in documents:
        assert doc["timestamp"] == 1700000000
    
    # Verify all documents are present
    doc_ids = {doc["document_id"] for doc in documents}
    assert doc_ids == {"doc1", "doc2", "doc3"}
