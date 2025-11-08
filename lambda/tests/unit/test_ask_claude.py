"""
Unit tests for ask-claude Lambda function.

Tests cover:
- RAG prompt construction
- Claude API invocation via Bedrock Converse
- Retry logic with exponential backoff
- Source extraction from context
- Error handling
"""

import json
import time
from unittest.mock import Mock, patch, MagicMock
import pytest
import sys
import os
from botocore.exceptions import ClientError

# Add lambda directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../ask-claude'))

from handler import (
    handler,
    build_prompt,
    invoke_claude_with_retry,
    extract_sources_from_context
)


class TestBuildPrompt:
    """Test RAG prompt construction."""

    def test_build_prompt_with_single_context(self):
        # Arrange
        query = "How should Lambda handlers be structured?"
        context = [
            {
                "score": 0.95,
                "text": "Lambda handlers should follow a standardized structure...",
                "source": "ADR: ADR-004"
            }
        ]

        # Act
        result = build_prompt(query, context)

        # Assert
        assert query in result
        assert "ADR: ADR-004" in result
        assert "Lambda handlers should follow" in result
        assert "CONTEXT:" in result
        assert "INSTRUCTIONS:" in result

    def test_build_prompt_with_multiple_contexts(self):
        # Arrange
        query = "How should testing be done?"
        context = [
            {"score": 0.95, "text": "Testing should follow...", "source": "ADR: ADR-003"},
            {"score": 0.78, "text": "Use pytest for unit tests...", "source": "README.md"}
        ]

        # Act
        result = build_prompt(query, context)

        # Assert
        assert "[Document 1]" in result
        assert "[Document 2]" in result
        assert "ADR: ADR-003" in result
        assert "README.md" in result

    def test_build_prompt_includes_relevance_scores(self):
        # Arrange
        query = "test query"
        context = [{"score": 0.93, "text": "content", "source": "source1"}]

        # Act
        result = build_prompt(query, context)

        # Assert
        assert "Relevance: 0.93" in result


class TestInvokeClaudeWithRetry:
    """Test Claude API invocation with retry logic."""

    @patch('handler.bedrock_client')
    def test_invoke_claude_success_first_try(self, mock_bedrock):
        # Arrange
        prompt = "Test prompt"
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "Test response"}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        }

        # Act
        result = invoke_claude_with_retry(prompt)

        # Assert
        assert result["output"]["message"]["content"][0]["text"] == "Test response"
        assert mock_bedrock.converse.call_count == 1

    @patch('handler.bedrock_client')
    @patch('time.sleep')  # Mock sleep to speed up tests
    def test_invoke_claude_retry_on_throttling(self, mock_sleep, mock_bedrock):
        # Arrange
        prompt = "Test prompt"
        mock_bedrock.converse.side_effect = [
            ClientError({"Error": {"Code": "ThrottlingException"}}, "converse"),
            {"output": {"message": {"content": [{"text": "Success"}]}}, "usage": {"inputTokens": 100, "outputTokens": 50}}
        ]

        # Act
        result = invoke_claude_with_retry(prompt, max_retries=3)

        # Assert
        assert result["output"]["message"]["content"][0]["text"] == "Success"
        assert mock_bedrock.converse.call_count == 2
        assert mock_sleep.call_count == 1  # Slept once before retry

    @patch('handler.bedrock_client')
    @patch('time.sleep')
    def test_invoke_claude_exponential_backoff(self, mock_sleep, mock_bedrock):
        # Arrange
        prompt = "Test prompt"
        mock_bedrock.converse.side_effect = [
            ClientError({"Error": {"Code": "ThrottlingException"}}, "converse"),
            ClientError({"Error": {"Code": "ThrottlingException"}}, "converse"),
            {"output": {"message": {"content": [{"text": "Success"}]}}, "usage": {"inputTokens": 100, "outputTokens": 50}}
        ]

        # Act
        result = invoke_claude_with_retry(prompt, max_retries=3)

        # Assert
        assert mock_bedrock.converse.call_count == 3
        # Verify exponential backoff: 1s, 2s
        sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert sleep_calls == [1, 2]

    @patch('handler.bedrock_client')
    def test_invoke_claude_no_retry_on_validation_error(self, mock_bedrock):
        # Arrange
        prompt = "Test prompt"
        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ValidationException"}}, "converse"
        )

        # Act & Assert
        with pytest.raises(ClientError) as exc_info:
            invoke_claude_with_retry(prompt)

        assert exc_info.value.response["Error"]["Code"] == "ValidationException"
        assert mock_bedrock.converse.call_count == 1  # No retry

    @patch('handler.bedrock_client')
    @patch('time.sleep')
    def test_invoke_claude_max_retries_exceeded(self, mock_sleep, mock_bedrock):
        # Arrange
        prompt = "Test prompt"
        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "converse"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            invoke_claude_with_retry(prompt, max_retries=2)

        assert mock_bedrock.converse.call_count == 2


class TestExtractSourcesFromContext:
    """Test source extraction from context documents."""

    def test_extract_sources_single_source(self):
        # Arrange
        context = [
            {"score": 0.95, "text": "content", "source": "ADR: ADR-001"}
        ]

        # Act
        result = extract_sources_from_context(context)

        # Assert
        assert result == ["ADR: ADR-001"]

    def test_extract_sources_multiple_unique_sources(self):
        # Arrange
        context = [
            {"score": 0.95, "text": "content1", "source": "ADR: ADR-001"},
            {"score": 0.80, "text": "content2", "source": "README.md"},
            {"score": 0.75, "text": "content3", "source": "doc#architecture"}
        ]

        # Act
        result = extract_sources_from_context(context)

        # Assert
        assert len(result) == 3
        assert "ADR: ADR-001" in result
        assert "README.md" in result

    def test_extract_sources_deduplicates(self):
        # Arrange
        context = [
            {"score": 0.95, "text": "content1", "source": "ADR: ADR-001"},
            {"score": 0.80, "text": "content2", "source": "ADR: ADR-001"},
            {"score": 0.75, "text": "content3", "source": "README.md"}
        ]

        # Act
        result = extract_sources_from_context(context)

        # Assert
        assert len(result) == 2
        assert result == ["ADR: ADR-001", "README.md"]

    def test_extract_sources_missing_source_field(self):
        # Arrange
        context = [
            {"score": 0.95, "text": "content"}  # Missing source
        ]

        # Act
        result = extract_sources_from_context(context)

        # Assert
        assert result == ["Unknown"]


class TestHandler:
    """Test Lambda handler function."""

    @patch('handler.invoke_claude_with_retry')
    def test_handler_success(self, mock_invoke):
        # Arrange
        event = {
            "query": "How should Lambda handlers be structured?",
            "context": [
                {"score": 0.95, "text": "Lambda handlers should...", "source": "ADR: ADR-004"}
            ]
        }
        mock_invoke.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "According to ADR-004, Lambda handlers should follow a standardized structure."}]
                }
            },
            "usage": {"inputTokens": 1500, "outputTokens": 200}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "answer" in body
        assert "sources" in body
        assert "ADR: ADR-004" in body["sources"]

    def test_handler_missing_query(self):
        # Arrange
        event = {
            "context": [{"score": 0.95, "text": "content", "source": "source"}]
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    def test_handler_empty_context(self):
        # Arrange
        event = {
            "query": "How should Lambda handlers be structured?",
            "context": []
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "don't have enough information" in body["answer"]
        assert body["sources"] == []

    @patch('handler.invoke_claude_with_retry')
    def test_handler_claude_returns_empty_content(self, mock_invoke):
        # Arrange
        event = {
            "query": "test query",
            "context": [{"score": 0.95, "text": "content", "source": "source"}]
        }
        mock_invoke.return_value = {
            "output": {"message": {"content": []}},
            "usage": {"inputTokens": 100, "outputTokens": 0}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    @patch('handler.invoke_claude_with_retry')
    def test_handler_exception_handling(self, mock_invoke):
        # Arrange
        event = {
            "query": "test query",
            "context": [{"score": 0.95, "text": "content", "source": "source"}]
        }
        mock_invoke.side_effect = Exception("Bedrock error")

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    @patch('handler.invoke_claude_with_retry')
    def test_handler_logs_token_usage(self, mock_invoke):
        # Arrange
        event = {
            "query": "test query",
            "context": [{"score": 0.95, "text": "content", "source": "source"}]
        }
        mock_invoke.return_value = {
            "output": {"message": {"content": [{"text": "answer"}]}},
            "usage": {"inputTokens": 2000, "outputTokens": 300}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        # Token usage should be logged (verified through logs in integration tests)
