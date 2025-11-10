"""
Pydantic schemas for list-recent-docs Lambda handler.

Defines request validation and response structures for listing recently ingested documents.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ListRecentDocsRequest(BaseModel):
    """
    Request schema for listing recent documents.
    
    Attributes:
        limit: Maximum number of documents to return (1-100, default: 10)
    """
    limit: Optional[int] = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of documents to return (1-100)"
    )

    class Config:
        """Pydantic configuration."""
        extra = "forbid"  # Disallow extra fields in request


class DocumentMetadata(BaseModel):
    """
    Metadata for a single document.
    
    Attributes:
        pk: Partition key (repo#<repo-name>)
        sk: Sort key (doc-type#<doc-id>)
        doc_type: Type of document (e.g., 'adr', 'runbook')
        file_path: Path to the document file
        ingested_at: ISO 8601 timestamp of when document was ingested
        repo: Repository name
    """
    pk: str = Field(..., description="Partition key (repo#<repo-name>)")
    sk: str = Field(..., description="Sort key (doc-type#<doc-id>)")
    doc_type: str = Field(..., description="Type of document")
    file_path: str = Field(..., description="Path to the document file")
    ingested_at: str = Field(..., description="ISO 8601 timestamp of ingestion")
    repo: str = Field(..., description="Repository name")

    class Config:
        """Pydantic configuration."""
        extra = "forbid"


class ListRecentDocsResponse(BaseModel):
    """
    Response schema for listing recent documents.
    
    Attributes:
        documents: List of document metadata
        total_returned: Number of documents returned in this response
        limit: The limit that was applied to the query
    """
    documents: List[DocumentMetadata] = Field(
        default_factory=list,
        description="List of document metadata"
    )
    total_returned: int = Field(
        ...,
        ge=0,
        description="Number of documents returned"
    )
    limit: int = Field(
        ...,
        ge=1,
        le=100,
        description="The limit applied to the query"
    )

    class Config:
        """Pydantic configuration."""
        extra = "forbid"
