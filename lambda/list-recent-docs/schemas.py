"""
Pydantic schemas for list-recent-docs Lambda function.

Defines request/response models for querying recently ingested documents.
"""

from typing import List, Optional
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ListRecentDocsRequest(BaseModel):
    """
    Request schema for listing recent documents.
    
    Attributes:
        limit: Number of documents to return (1-100, default 10)
    """

    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of documents to return (must be between 1 and 100)",
    )

    class Config:
        """Pydantic configuration."""

        json_schema_extra = {
            "example": {"limit": 20},
        }


class DocumentMetadata(BaseModel):
    """
    Metadata for a single document in the knowledge base.
    
    Attributes:
        pk: Partition key (repo#{repository_name})
        sk: Sort key ({doc_type}#{identifier})
        doc_type: Document type (adr, readme, doc, code_map)
        file_path: Path to the document in the repository
        ingested_at: Timestamp when the document was ingested
        repo: Repository name
    """

    pk: str = Field(..., description="Partition key (repo#{repository_name})")
    sk: str = Field(..., description="Sort key ({doc_type}#{identifier})")
    doc_type: str = Field(..., description="Document type (adr, readme, doc, code_map)")
    file_path: str = Field(..., description="Path to the document in the repository")
    ingested_at: str = Field(..., description="ISO 8601 timestamp of ingestion")
    repo: str = Field(..., description="Repository name")

    @field_validator("ingested_at")
    def validate_timestamp(\
        cls, v: str
    ) -> str:  # pylint: disable=no-self-argument
        """Validate that ingested_at is a valid ISO 8601 timestamp."""
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            return v
        except ValueError as e:
            raise ValueError(
                f"ingested_at must be a valid ISO 8601 timestamp: {e}"
            ) from e

    class Config:
        """Pydantic configuration."""

        json_schema_extra = {
            "example": {
                "pk": "repo#outcome-ops-ai-assist",
                "sk": "adr#ADR-001-create-adrs",
                "doc_type": "adr",
                "file_path": "docs/adr/ADR-001-create-adrs.md",
                "ingested_at": "2025-01-08T12:00:00Z",
                "repo": "outcome-ops-ai-assist",
            },
        }


class ListRecentDocsResponse(BaseModel):
    """
    Response schema for listing recent documents.
    
    Attributes:
        documents: List of document metadata
        count: Number of documents returned
    """

    documents: List[DocumentMetadata] = Field(
        ..., description="List of document metadata, ordered by ingestion time (most recent first)"
    )
    count: int = Field(..., ge=0, description="Number of documents returned")

    class Config:
        """Pydantic configuration."""

        json_schema_extra = {
            "example": {
                "documents": [
                    {
                        "pk": "repo#outcome-ops-ai-assist",
                        "sk": "adr#ADR-001-create-adrs",
                        "doc_type": "adr",
                        "file_path": "docs/adr/ADR-001-create-adrs.md",
                        "ingested_at": "2025-01-08T12:00:00Z",
                        "repo": "outcome-ops-ai-assist",
                    },
                    {
                        "pk": "repo#outcome-ops-ai-assist",
                        "sk": "readme#root",
                        "doc_type": "readme",
                        "file_path": "README.md",
                        "ingested_at": "2025-01-08T11:30:00Z",
                        "repo": "outcome-ops-ai-assist",
                    },
                ],
                "count": 2,
            },
        }
