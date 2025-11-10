"""
Unit tests for list_recent_docs schemas.

Covers:
- Pydantic validation for valid/invalid limit values
- Default values
- Edge cases (0, 101, missing limit)
"""

import pytest
from pydantic import ValidationError

from lambda.list_recent_docs.schemas import ListRecentDocsRequest, ListRecentDocsResponse, DocumentItem


class TestListRecentDocsRequest:
    """Test ListRecentDocsRequest schema validation."""

    def test_valid_request_with_limit(self):
        """Test valid request with explicit limit."""
        request = ListRecentDocsRequest(limit=50)
        assert request.limit == 50

    def test_default_limit(self):
        """Test that limit defaults to 10 when not provided."""
        request = ListRecentDocsRequest()
        assert request.limit == 10

    def test_valid_minimum_limit(self):
        """Test valid minimum limit value (1)."""
        request = ListRecentDocsRequest(limit=1)
        assert request.limit == 1

    def test_valid_maximum_limit(self):
        """Test valid maximum limit value (100)."""
        request = ListRecentDocsRequest(limit=100)
        assert request.limit == 100

    def test_invalid_limit_zero(self):
        """Test that limit of 0 is invalid."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsRequest(limit=0)
        
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("limit",)
        assert "greater than" in errors[0]["msg"].lower()

    def test_invalid_limit_negative(self):
        """Test that negative limit is invalid."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsRequest(limit=-1)
        
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("limit",)
        assert "greater than" in errors[0]["msg"].lower()

    def test_invalid_limit_exceeds_maximum(self):
        """Test that limit of 101 exceeds maximum."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsRequest(limit=101)
        
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("limit",)
        assert "less than or equal" in errors[0]["msg"].lower()

    def test_invalid_limit_type_string(self):
        """Test that limit must be an integer, not string."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsRequest(limit="10")  # type: ignore
        
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("limit",)

    def test_invalid_limit_type_float(self):
        """Test that limit must be an integer, not float."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsRequest(limit=10.5)  # type: ignore
        
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("limit",)

    def test_invalid_limit_type_none(self):
        """Test that limit cannot be None explicitly."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsRequest(limit=None)  # type: ignore
        
        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("limit",)


class TestDocumentItem:
    """Test DocumentItem schema validation."""

    def test_valid_document_item(self):
        """Test valid document item creation."""
        doc = DocumentItem(
            document_id="doc-123",
            title="Test Document",
            created_at="2024-01-01T12:00:00Z",
            created_by="user123"
        )
        assert doc.document_id == "doc-123"
        assert doc.title == "Test Document"
        assert doc.created_at == "2024-01-01T12:00:00Z"
        assert doc.created_by == "user123"

    def test_missing_required_field_document_id(self):
        """Test that document_id is required."""
        with pytest.raises(ValidationError) as exc_info:
            DocumentItem(
                title="Test Document",
                created_at="2024-01-01T12:00:00Z",
                created_by="user123"
            )
        
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("document_id",) for error in errors)

    def test_missing_required_field_title(self):
        """Test that title is required."""
        with pytest.raises(ValidationError) as exc_info:
            DocumentItem(
                document_id="doc-123",
                created_at="2024-01-01T12:00:00Z",
                created_by="user123"
            )
        
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("title",) for error in errors)

    def test_missing_required_field_created_at(self):
        """Test that created_at is required."""
        with pytest.raises(ValidationError) as exc_info:
            DocumentItem(
                document_id="doc-123",
                title="Test Document",
                created_by="user123"
            )
        
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("created_at",) for error in errors)

    def test_missing_required_field_created_by(self):
        """Test that created_by is required."""
        with pytest.raises(ValidationError) as exc_info:
            DocumentItem(
                document_id="doc-123",
                title="Test Document",
                created_at="2024-01-01T12:00:00Z"
            )
        
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("created_by",) for error in errors)

    def test_empty_string_fields(self):
        """Test that empty strings are invalid for required fields."""
        with pytest.raises(ValidationError) as exc_info:
            DocumentItem(
                document_id="",
                title="Test Document",
                created_at="2024-01-01T12:00:00Z",
                created_by="user123"
            )
        
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("document_id",) for error in errors)


class TestListRecentDocsResponse:
    """Test ListRecentDocsResponse schema validation."""

    def test_valid_response_with_documents(self):
        """Test valid response with documents."""
        documents = [
            DocumentItem(
                document_id="doc-1",
                title="Document 1",
                created_at="2024-01-01T12:00:00Z",
                created_by="user1"
            ),
            DocumentItem(
                document_id="doc-2",
                title="Document 2",
                created_at="2024-01-02T12:00:00Z",
                created_by="user2"
            )
        ]
        response = ListRecentDocsResponse(documents=documents)
        assert len(response.documents) == 2
        assert response.documents[0].document_id == "doc-1"
        assert response.documents[1].document_id == "doc-2"

    def test_valid_response_empty_list(self):
        """Test valid response with empty document list."""
        response = ListRecentDocsResponse(documents=[])
        assert len(response.documents) == 0
        assert response.documents == []

    def test_missing_required_field_documents(self):
        """Test that documents field is required."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsResponse()  # type: ignore
        
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("documents",) for error in errors)

    def test_invalid_documents_type(self):
        """Test that documents must be a list."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsResponse(documents="not-a-list")  # type: ignore
        
        errors = exc_info.value.errors()
        assert any(error["loc"] == ("documents",) for error in errors)

    def test_invalid_document_item_in_list(self):
        """Test that invalid document items in list are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ListRecentDocsResponse(
                documents=[
                    {"document_id": "doc-1", "title": "Valid"},  # Missing required fields
                ]
            )
        
        errors = exc_info.value.errors()
        assert len(errors) > 0
        # Error should be in the documents list
        assert any("documents" in str(error["loc"]) for error in errors)
