"""
Unit tests for vector-query Lambda function.

Tests cover:
- Query embedding generation
- Cosine similarity calculation
- Document scanning from DynamoDB
- Result ranking and filtering
- Error handling
"""

import json
from unittest.mock import Mock, patch, MagicMock
import pytest
import sys
import os
import importlib.util

# Load the vector-query handler module
handler_path = os.path.join(os.path.dirname(__file__), '../../vector-query/handler.py')
spec = importlib.util.spec_from_file_location("vector_query_handler", handler_path)
handler = importlib.util.module_from_spec(spec)
sys.modules['vector_query_handler'] = handler
spec.loader.exec_module(handler)


class TestGenerateEmbedding:
    """Test embedding generation with Bedrock Titan v2."""

    @patch('vector_query_handler.bedrock_client')
    def test_generate_embedding_success(self, mock_bedrock):
        # Arrange
        mock_response = Mock()
        mock_response.__getitem__ = Mock(return_value=Mock(read=Mock(return_value=json.dumps({"embedding": [0.1] * 1024}).encode())))
        mock_bedrock.invoke_model.return_value = mock_response

        # Act
        result = handler.generate_embedding("test query")

        # Assert
        assert len(result) == 1024
        assert all(isinstance(x, float) for x in result)
        mock_bedrock.invoke_model.assert_called_once()

    @patch('vector_query_handler.bedrock_client')
    def test_generate_embedding_empty_response(self, mock_bedrock):
        # Arrange
        mock_response = Mock()
        mock_response.__getitem__ = Mock(return_value=Mock(read=Mock(return_value=json.dumps({"embedding": []}).encode())))
        mock_bedrock.invoke_model.return_value = mock_response

        # Act
        result = handler.generate_embedding("test query")

        # Assert
        assert result == []


class TestCosineSimilarity:
    """Test cosine similarity calculation."""

    def test_cosine_similarity_identical_vectors(self):
        # Arrange
        vec = [1.0, 2.0, 3.0]

        # Act
        result = handler.cosine_similarity(vec, vec)

        # Assert
        assert pytest.approx(result, rel=1e-5) == 1.0

    def test_cosine_similarity_orthogonal_vectors(self):
        # Arrange
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]

        # Act
        result = handler.cosine_similarity(vec1, vec2)

        # Assert
        assert pytest.approx(result, rel=1e-5) == 0.0

    def test_cosine_similarity_different_lengths(self):
        # Arrange
        vec1 = [1.0, 2.0]
        vec2 = [1.0, 2.0, 3.0]

        # Act
        result = handler.cosine_similarity(vec1, vec2)

        # Assert
        assert result == 0.0

    def test_cosine_similarity_empty_vectors(self):
        # Act
        result = handler.cosine_similarity([], [])

        # Assert
        assert result == 0.0

    def test_cosine_similarity_zero_magnitude(self):
        # Arrange
        vec1 = [0.0, 0.0, 0.0]
        vec2 = [1.0, 2.0, 3.0]

        # Act
        result = handler.cosine_similarity(vec1, vec2)

        # Assert
        assert result == 0.0


class TestFormatSource:
    """Test source formatting for different document types."""

    def test_format_source_adr(self):
        # Arrange
        doc = {"type": "adr", "sk": "adr#ADR-001", "repo": "test"}

        # Act
        result = handler.format_source(doc)

        # Assert
        assert result == "ADR: ADR-001"

    def test_format_source_readme_root(self):
        # Arrange
        doc = {"type": "readme", "sk": "readme#root", "repo": "test"}

        # Act
        result = handler.format_source(doc)

        # Assert
        assert result == "README.md - test"

    def test_format_source_doc(self):
        # Arrange
        doc = {"type": "doc", "sk": "doc#architecture", "repo": "test"}

        # Act
        result = handler.format_source(doc)

        # Assert
        assert result == "architecture.md - test"


class TestHandler:
    """Test Lambda handler function."""

    @patch.object(handler, 'CODE_MAPS_TABLE', 'test-table')
    @patch.object(handler, 'scan_documents')
    @patch.object(handler, 'generate_embedding')
    def test_handler_success(self, mock_generate, mock_scan):
        # Arrange
        event = {"query": "test query", "topK": 3}
        mock_generate.return_value = [0.5] * 1024
        mock_scan.return_value = [
            {"pk": "1", "sk": "adr#1", "type": "adr", "content": "Test content", "repo": "test", "file_path": "1.md", "embedding": [0.5] * 1024}
        ]

        # Act
        response = handler.handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body) == 1
        assert "score" in body[0]

    @patch.object(handler, 'CODE_MAPS_TABLE', 'test-table')
    def test_handler_missing_query(self):
        # Arrange
        event = {"topK": 3}  # Missing query

        # Act
        response = handler.handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
