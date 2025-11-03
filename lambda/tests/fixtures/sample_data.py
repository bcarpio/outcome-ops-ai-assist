"""
Test fixtures and sample data for Lambda function tests.
"""

import pytest


@pytest.fixture
def sample_query():
    """Sample query for testing vector search and RAG pipeline."""
    return "How should Lambda handlers be structured?"


@pytest.fixture
def sample_embedding():
    """Sample 1024-dimensional embedding vector for testing."""
    # Simplified embedding (in reality, this would be from Bedrock Titan v2)
    return [0.1] * 1024


@pytest.fixture
def sample_documents():
    """Sample documents with embeddings from DynamoDB."""
    return [
        {
            "pk": "repo#outcome-ops-ai-assist",
            "sk": "adr#ADR-004-lambda-handler-standards",
            "type": "adr",
            "content": "# ADR-004: Lambda Handler Standards\n\nLambda handlers should follow this structure...",
            "repo": "outcome-ops-ai-assist",
            "file_path": "docs/adr/ADR-004-lambda-handler-standards.md",
            "embedding": [0.9] * 1024  # High similarity
        },
        {
            "pk": "repo#outcome-ops-ai-assist",
            "sk": "doc#architecture",
            "type": "doc",
            "content": "# Architecture Overview\n\nThe system uses Lambda functions for compute...",
            "repo": "outcome-ops-ai-assist",
            "file_path": "docs/architecture.md",
            "embedding": [0.5] * 1024  # Medium similarity
        },
        {
            "pk": "repo#outcome-ops-ai-assist",
            "sk": "readme#root",
            "type": "readme",
            "content": "# OutcomeOps AI Assist\n\nAn AI-powered engineering assistant...",
            "repo": "outcome-ops-ai-assist",
            "file_path": "README.md",
            "embedding": [0.2] * 1024  # Low similarity
        }
    ]


@pytest.fixture
def sample_dynamodb_items():
    """Sample DynamoDB Items structure."""
    return {
        "Items": [
            {
                "PK": {"S": "repo#outcome-ops-ai-assist"},
                "SK": {"S": "adr#ADR-004-lambda-handler-standards"},
                "type": {"S": "adr"},
                "content": {"S": "# ADR-004: Lambda Handler Standards"},
                "repo": {"S": "outcome-ops-ai-assist"},
                "file_path": {"S": "docs/adr/ADR-004-lambda-handler-standards.md"},
                "embedding": {"L": [{"N": str(0.9)} for _ in range(1024)]}
            }
        ]
    }


@pytest.fixture
def sample_vector_search_results():
    """Sample vector search results."""
    return [
        {
            "score": 0.95,
            "text": "# ADR-004: Lambda Handler Standards\n\nLambda handlers should follow this structure...",
            "source": "ADR: ADR-004-lambda-handler-standards",
            "type": "adr",
            "repo": "outcome-ops-ai-assist",
            "file_path": "docs/adr/ADR-004-lambda-handler-standards.md"
        },
        {
            "score": 0.78,
            "text": "# Architecture Overview\n\nThe system uses Lambda functions...",
            "source": "doc#architecture",
            "type": "doc",
            "repo": "outcome-ops-ai-assist",
            "file_path": "docs/architecture.md"
        }
    ]


@pytest.fixture
def sample_claude_response():
    """Sample response from Claude via Bedrock Converse API."""
    return {
        "output": {
            "message": {
                "content": [
                    {
                        "text": "According to ADR-004, Lambda handlers should follow a standardized structure with logging, AWS client initialization, and error handling."
                    }
                ]
            }
        },
        "usage": {
            "inputTokens": 1500,
            "outputTokens": 200
        }
    }


@pytest.fixture
def sample_bedrock_embedding_response():
    """Sample response from Bedrock Titan Embeddings v2."""
    return {
        "embedding": [0.1] * 1024,
        "inputTextTokenCount": 50
    }
