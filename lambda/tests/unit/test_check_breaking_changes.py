"""
Unit tests for breaking_changes check handler.

Tests cover:
- Vector search querying via Lambda
- Handler name extraction from file paths
- Change type detection
- Handler summary filtering
- Confidence calculation
- Tailored query creation
- Main handler orchestration
"""

import json
import pytest
import sys
import os
import importlib.util
from unittest.mock import Mock, patch
from moto import mock_aws
import boto3
from botocore.exceptions import ClientError

# Load the breaking_changes handler module
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/breaking_changes.py'
)
spec = importlib.util.spec_from_file_location("breaking_changes_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['breaking_changes_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import classes and functions from loaded module
KbVectorResult = handler_module.KbVectorResult
Dependency = handler_module.Dependency
query_vector_search = handler_module.query_vector_search
extract_handler_name = handler_module.extract_handler_name
detect_change_type = handler_module.detect_change_type
filter_handler_summaries = handler_module.filter_handler_summaries
calculate_confidence = handler_module.calculate_confidence
create_tailored_query = handler_module.create_tailored_query
check_breaking_changes = handler_module.check_breaking_changes


class TestKbVectorResult:
    """Test KbVectorResult data class"""

    def test_kb_vector_result_initialization(self):
        """Test: KbVectorResult initializes correctly"""
        # Act
        result = KbVectorResult(0.95, "handler text", "repo/handler.py")

        # Assert
        assert result.score == 0.95
        assert result.text == "handler text"
        assert result.source == "repo/handler.py"


class TestDependency:
    """Test Dependency data class"""

    def test_dependency_initialization(self):
        """Test: Dependency initializes correctly"""
        # Act
        dep = Dependency("hello", "Consumes hello events", "HIGH", "repo/consumer.py")

        # Assert
        assert dep.handler_name == "hello"
        assert dep.description == "Consumes hello events"
        assert dep.confidence == "HIGH"
        assert dep.source == "repo/consumer.py"


class TestQueryVectorSearch:
    """Test vector search querying via Lambda"""

    @mock_aws()
    @patch('breaking_changes_handler.lambda_client')
    def test_query_vector_search_success(self, mock_lambda):
        """Test: Successfully query vector search"""
        # Arrange
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps([
                {"score": 0.95, "text": "handler text", "source": "repo/handler.py"}
            ]).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        # Act
        results = query_vector_search("query-kb", "search query", 5)

        # Assert
        assert len(results) == 1
        assert results[0].score == 0.95
        assert results[0].text == "handler text"
        assert results[0].source == "repo/handler.py"

    @mock_aws()
    @patch('breaking_changes_handler.lambda_client')
    def test_query_vector_search_lambda_error(self, mock_lambda):
        """Test: Lambda invocation fails"""
        # Arrange
        mock_lambda.invoke.side_effect = ClientError(
            {"Error": {"Code": "ServiceException", "Message": "Service error"}},
            "invoke"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            query_vector_search("query-kb", "search query", 5)


class TestExtractHandlerName:
    """Test handler name extraction from file paths"""

    def test_extract_handler_name_lambda(self):
        """Test: Extract handler name from Lambda file path"""
        # Arrange
        file_path = "lambda/hello/handler.py"

        # Act
        result = extract_handler_name(file_path)

        # Assert
        assert result == "hello"

    def test_extract_handler_name_lambda_with_dash(self):
        """Test: Extract handler name with dashes"""
        # Arrange
        file_path = "lambda/analyze-pr/handler.py"

        # Act
        result = extract_handler_name(file_path)

        # Assert
        assert result == "analyze-pr"

    def test_extract_handler_name_terraform(self):
        """Test: Extract name from Terraform file"""
        # Arrange
        file_path = "terraform/lambda.tf"

        # Act
        result = extract_handler_name(file_path)

        # Assert
        assert result == "lambda"


class TestDetectChangeType:
    """Test change type detection"""

    def test_detect_change_type_infra(self):
        """Test: Detect infrastructure change"""
        # Arrange
        file_path = "terraform/lambda.tf"

        # Act
        result = detect_change_type(file_path)

        # Assert
        assert result == "infra"

    def test_detect_change_type_logic(self):
        """Test: Detect logic change"""
        # Arrange
        file_path = "lambda/hello/handler.py"

        # Act
        result = detect_change_type(file_path)

        # Assert
        assert result == "logic"

    def test_detect_change_type_schema(self):
        """Test: Detect schema change"""
        # Arrange
        file_path = "lambda/hello/schema.py"

        # Act
        result = detect_change_type(file_path)

        # Assert
        assert result == "schema"


class TestFilterHandlerSummaries:
    """Test filtering of handler summaries"""

    def test_filter_handler_summaries_keeps_handlers(self):
        """Test: Keep handler-group-summary results"""
        # Arrange
        results = [
            KbVectorResult(0.95, "text", "handler-group-summary: hello"),
            KbVectorResult(0.90, "text", "handler-group-summary: world")
        ]

        # Act
        filtered = filter_handler_summaries(results)

        # Assert
        assert len(filtered) == 2

    def test_filter_handler_summaries_removes_code_maps(self):
        """Test: Remove code-map results"""
        # Arrange
        results = [
            KbVectorResult(0.95, "text", "handler-group-summary: hello"),
            KbVectorResult(0.90, "text", "code-map: architecture")
        ]

        # Act
        filtered = filter_handler_summaries(results)

        # Assert
        assert len(filtered) == 1
        assert "hello" in filtered[0].source

    def test_filter_handler_summaries_removes_repo_overviews(self):
        """Test: Remove repo-overview results"""
        # Arrange
        results = [
            KbVectorResult(0.95, "text", "handler-group-summary: hello"),
            KbVectorResult(0.90, "text", "repo-overview: project")
        ]

        # Act
        filtered = filter_handler_summaries(results)

        # Assert
        assert len(filtered) == 1
        assert "hello" in filtered[0].source


class TestCalculateConfidence:
    """Test confidence calculation"""

    def test_calculate_confidence_high_with_queue(self):
        """Test: HIGH confidence with handler name + queue"""
        # Arrange
        handler_name = "hello"
        text = "This handler processes messages from the hello-queue using SQS"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "HIGH"

    def test_calculate_confidence_high_with_invoke(self):
        """Test: HIGH confidence with handler name + invoke"""
        # Arrange
        handler_name = "hello"
        text = "This service invokes the hello Lambda function"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "HIGH"

    def test_calculate_confidence_medium(self):
        """Test: MEDIUM confidence with generic dependency"""
        # Arrange
        handler_name = "hello"
        text = "This handler depends on hello for processing"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "MEDIUM"

    def test_calculate_confidence_low(self):
        """Test: LOW confidence with vague reference"""
        # Arrange
        handler_name = "hello"
        text = "Some unrelated text without specific references"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "LOW"


class TestCreateTailoredQuery:
    """Test tailored query creation"""

    def test_create_tailored_query_schema(self):
        """Test: Create schema change query"""
        # Act
        result = create_tailored_query("hello", "schema")

        # Assert
        assert "consume" in result
        assert "hello" in result
        assert "schema" in result

    def test_create_tailored_query_logic(self):
        """Test: Create logic change query"""
        # Act
        result = create_tailored_query("hello", "logic")

        # Assert
        assert "invoke" in result or "depend" in result
        assert "hello" in result

    def test_create_tailored_query_infra(self):
        """Test: Create infrastructure change query"""
        # Act
        result = create_tailored_query("hello", "infra")

        # Assert
        assert "depend" in result
        assert "hello" in result
        assert "infrastructure" in result


class TestCheckBreakingChanges:
    """Test main check_breaking_changes handler"""

    def test_check_breaking_changes_no_relevant_files(self):
        """Test: No relevant files changed"""
        # Arrange
        changed_files = ["README.md", "docs/guide.md"]

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            changed_files,
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        assert "No schema, logic, or infrastructure changes" in result["message"]

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_high_confidence_found(self, mock_query):
        """Test: HIGH confidence dependencies found"""
        # Arrange
        mock_query.return_value = [
            KbVectorResult(
                0.95,
                "This handler processes messages from the hello queue using SQS",
                "handler-group-summary: consumer"
            )
        ]

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "WARN"
        assert "consumer(s) detected" in result["message"]

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_no_high_confidence(self, mock_query):
        """Test: No HIGH confidence dependencies found"""
        # Arrange
        mock_query.return_value = [
            KbVectorResult(
                0.80,
                "Some unrelated text",
                "handler-group-summary: other"
            )
        ]

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        assert "No dependencies found" in result["message"]

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_infra_only_skipped(self, mock_query):
        """Test: Infrastructure-only changes skip KB query"""
        # Arrange
        # Should not call query_vector_search for infra-only changes

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["terraform/lambda.tf"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        mock_query.assert_not_called()

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_query_error(self, mock_query):
        """Test: KB query error handled gracefully"""
        # Arrange
        mock_query.side_effect = Exception("KB error")

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        # Should still return PASS since no high confidence dependencies were found
