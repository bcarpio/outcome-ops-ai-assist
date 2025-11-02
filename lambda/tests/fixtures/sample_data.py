"""Sample test data and fixtures for ingest-docs tests."""

import pytest


@pytest.fixture
def sample_adr_content():
    """Sample ADR file content."""
    return """# ADR-001: Test Infrastructure Pattern

## Status: Accepted

## Context
Testing is important for code quality.

## Decision
We will use pytest for all tests.

## Consequences
- Better code quality
- More confidence in deployments
"""


@pytest.fixture
def sample_readme_content():
    """Sample README file content."""
    return """# Project Documentation

This is a test README for the knowledge base.

## Features
- Fast ingestion
- Embedding generation
- S3 storage

## Setup
Follow the documentation to set up.
"""


@pytest.fixture
def github_api_response_list_files():
    """Mock GitHub API response for listing files."""
    return [
        {
            "name": "ADR-001-test.md",
            "path": "docs/adr/ADR-001-test.md",
            "type": "file",
            "size": 1024,
        },
        {
            "name": "ADR-002-test.md",
            "path": "docs/adr/ADR-002-test.md",
            "type": "file",
            "size": 2048,
        },
    ]


@pytest.fixture
def sample_embedding():
    """Sample 1024-dimensional embedding vector."""
    return [0.123] * 1024


@pytest.fixture
def dynamodb_adr_item():
    """Sample DynamoDB item for an ADR."""
    return {
        "PK": {"S": "repo#outcome-ops-ai-assist"},
        "SK": {"S": "adr#ADR-001"},
        "type": {"S": "adr"},
        "content": {"S": "# ADR-001\nTest content"},
        "file_path": {"S": "docs/adr/ADR-001.md"},
        "content_hash": {"S": "abc123def456"},
        "timestamp": {"S": "2025-11-02T12:00:00"},
        "repo": {"S": "outcome-ops-ai-assist"},
    }


@pytest.fixture
def dynamodb_readme_item():
    """Sample DynamoDB item for a README."""
    return {
        "PK": {"S": "repo#outcome-ops-ai-assist"},
        "SK": {"S": "readme#root"},
        "type": {"S": "readme"},
        "content": {"S": "# README\nTest content"},
        "file_path": {"S": "README.md"},
        "content_hash": {"S": "xyz789abc123"},
        "timestamp": {"S": "2025-11-02T12:00:00"},
        "repo": {"S": "outcome-ops-ai-assist"},
    }


@pytest.fixture
def allowlist_config():
    """Sample allowlist configuration."""
    return {
        "repos": [
            {
                "name": "outcome-ops-ai-assist",
                "project": "bcarpio/outcome-ops-ai-assist",
                "type": "standards",
            }
        ]
    }
