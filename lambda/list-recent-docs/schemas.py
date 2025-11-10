"""
Pydantic schemas for list-recent-docs Lambda handler.

Defines request validation and response structures for listing recent documents.
"""

from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field, field_validator


class ListRecentDocsRequest(BaseModel):
    """
    Request schema for listing recent documents.

    Attributes:
        limit: Number of documents to return (1-100, default: 10)
    """

    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of documents to return (min: 1, max: 100)",
    )

    class Config:
        """Pydantic configuration."""

        json_schema_extra = {
            "example": {"limit": 20},
        }


class DocumentMetadata(BaseModel):
    """
    Metadata for a single document.

    Attributes:
        document_id: Unique identifier for the document
        title: Document title
        uploaded_at: Timestamp when document was uploaded
        uploaded_by: User ID who uploaded the document
        file_size: Size of the document in bytes
        file_type: MIME type of the document
        status: Processing status of the document
    """

    document_id: str = Field(..., description="Unique document identifier")
    title: str = Field(..., description="Document title")
    uploaded_at: str = Field(..., description="ISO 8601 timestamp of upload")
    uploaded_by: str = Field(..., description="User ID who uploaded the document")
    file_size: int = Field(..., description="File size in bytes")
    file_type: str = Field(..., description="MIME type of the document")
    status: str = Field(..., description="Processing status (e.g., 'processing', 'completed', 'failed')")

    class Config:
        """Pydantic configuration."""

        json_schema_extra = {
            "example": {
                "document_id": "doc-1234567890",
                "title": "Quarterly Report Q3 2024.pdf",
                "uploaded_at": "2024-01-15T10:30:00Z",
                "uploaded_by": "user-789012345",
                "file_size": 2560000,
                "file_type": "application/pdf",
                "status": "completed",
            }
        }


class ListRecentDocsResponse(BaseModel):
    """
    Response schema for listing recent documents.

    Attributes:
        documents: List of document metadata
        count: Number of documents returned
    """

    documents: List[DocumentMetadata] = Field(
        default_factory=list, description="List of document metadata"
    )
    count: int = Field(..., description="Number of documents returned")

    class Config:
        """Pydantic configuration."""

        json_schema_extra = {
            "example": {
                "documents": [
                    {
                        "document_id": "doc-1234567890",
                        "title": "Quarterly Report Q3 2024.pdf",
                        "uploaded_at": "2024-01-15T10:30:00Z",
                        "uploaded_by": "user-789012345",
                        "file_size": 2560000,
                        "file_type": "application/pdf",
                        "status": "completed",
                    },
                    {
                        "document_id": "doc-0987654321",
                        "title": "Meeting Notes Jan 2024.docx",
                        "uploaded_at": "2024-01-14T14:15:00Z",
                        "uploaded_by": "user-123456789",
                        "file_size": 51200,
                        "file_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "status": "completed",
                    },
                ],
                "count": 2,
            }
        }


class ErrorResponse(BaseModel):
    """
    Error response schema.

    Attributes:
        error: Error message
        details: Optional additional error details
    """

    error: str = Field(..., description="Error message")
    details: Optional[str] = Field(
        default=None, description="Additional error details"
    )

    class Config:
        """Pydantic configuration."""

        json_schema_extra = {
            "example": {
                "error": "Validation error",
                "details": "limit must be between 1 and 100",
            }
        }
