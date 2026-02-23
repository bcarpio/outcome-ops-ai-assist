"""
Unit tests for knowledge_base module.

Tests cover:
- query_knowledge_base: Main KB query function
- get_lambda_standards: Lambda handler standards query
- get_terraform_standards: Terraform standards query
- get_testing_standards: Testing standards query
- get_github_standards: GitHub standards query
- get_standards_for_backend: Backend-aware standards query
- get_step_context: Step-specific context query
"""

import os
import sys
import json
import pytest
from io import BytesIO
from unittest.mock import patch, MagicMock, Mock

# Add generate-code directory to Python path
generate_code_dir = os.path.join(os.path.dirname(__file__), '../../generate-code')
sys.path.insert(0, os.path.abspath(generate_code_dir))

# Mock boto3.client BEFORE importing modules that create clients at import time
mock_lambda_client = MagicMock()
mock_boto3_client = MagicMock(return_value=mock_lambda_client)

with patch('boto3.client', mock_boto3_client):
    from knowledge_base import (
        query_knowledge_base,
        get_lambda_standards,
        get_terraform_standards,
        get_testing_standards,
        get_github_standards,
        get_standards_for_backend,
        get_step_context,
        fetch_required_adrs,
    )


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def mock_backend():
    """Create a mock code generator backend."""
    backend = MagicMock()
    backend.name = "python"
    backend.display_name = "Python"
    backend.get_handler_standards_queries.return_value = ["Lambda handler standards"]
    backend.get_testing_standards_queries.return_value = ["Testing standards"]
    backend.get_infrastructure_standards_queries.return_value = ["Terraform standards"]
    backend.get_language_standards_queries.return_value = ["Python standards"]
    backend.get_framework_standards_queries.return_value = []
    return backend


@pytest.fixture
def mock_lambda_response():
    """Create a mock Lambda response with KB results."""
    def create_response(query: str, answer: str = None):
        if answer is None:
            answer = f"Answer for: {query}"
        body = {"answer": answer}
        payload = {"statusCode": 200, "body": json.dumps(body)}
        return {
            "StatusCode": 200,
            "Payload": BytesIO(json.dumps(payload).encode())
        }
    return create_response


def create_raw_kb_response(results: list):
    """Create a mock query-kb raw response with vector results."""
    body = {"results": results}
    payload = {"statusCode": 200, "body": json.dumps(body)}
    return {
        "StatusCode": 200,
        "Payload": BytesIO(json.dumps(payload).encode())
    }


def create_ask_claude_response(answer: str, sources: list = None):
    """Create a mock ask-claude response."""
    if sources is None:
        sources = ["ADR: test-source"]
    body = {"answer": answer, "sources": sources}
    payload = {"statusCode": 200, "body": json.dumps(body)}
    return {
        "StatusCode": 200,
        "Payload": BytesIO(json.dumps(payload).encode())
    }


def mock_kb_and_claude(mock_client, raw_results: list, answer: str, sources: list = None):
    """
    Configure mock client to handle both query-kb and ask-claude calls.

    The first call is to query-kb (returns raw results),
    the second is to ask-claude (returns summarized answer).
    """
    def side_effect(*args, **kwargs):
        function_name = kwargs.get('FunctionName', '')
        if 'query-kb' in function_name:
            return create_raw_kb_response(raw_results)
        elif 'ask-claude' in function_name:
            return create_ask_claude_response(answer, sources)
        return create_raw_kb_response([])

    mock_client.invoke.side_effect = side_effect


# ============================================================================
# Test query_knowledge_base
# ============================================================================


class TestQueryKnowledgeBase:
    """Test query_knowledge_base function."""

    def test_returns_empty_list_for_empty_queries(self):
        """Test: Returns empty list when no queries provided."""
        # Act
        result = query_knowledge_base([])

        # Assert
        assert result == []

    def test_invokes_lambda_for_each_query(self, mock_lambda_response):
        """Test: Invokes query-kb and ask-claude Lambda for each query."""
        # Arrange
        queries = ["Query 1", "Query 2", "Query 3"]
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            query_knowledge_base(queries)

            # Assert - 2 calls per query (query-kb + ask-claude)
            assert mock_client.invoke.call_count == 6

    def test_returns_formatted_results(self, mock_lambda_response):
        """Test: Returns properly formatted results with query headers."""
        # Arrange
        queries = ["Lambda standards"]
        raw_results = [{"score": 0.9, "text": "Lambda ADR content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(
                mock_client,
                raw_results=raw_results,
                answer="Use structlog for logging",
                sources=["ADR-001"]
            )

            # Act
            result = query_knowledge_base(queries)

            # Assert
            assert len(result) == 1
            assert "# Lambda standards" in result[0]
            assert "Use structlog for logging" in result[0]

    def test_handles_function_error(self, mock_lambda_response):
        """Test: Handles Lambda function errors gracefully."""
        # Arrange
        queries = ["Query 1"]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_client.invoke.return_value = {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": BytesIO(json.dumps({"errorMessage": "Test error"}).encode())
            }

            # Act
            result = query_knowledge_base(queries)

            # Assert - should return empty list on error
            assert result == []

    def test_handles_missing_body_in_response(self):
        """Test: Handles responses without body gracefully."""
        # Arrange
        queries = ["Query 1"]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_client.invoke.return_value = {
                "StatusCode": 200,
                "Payload": BytesIO(json.dumps({"statusCode": 200}).encode())
            }

            # Act
            result = query_knowledge_base(queries)

            # Assert
            assert result == []

    def test_handles_empty_answer(self, mock_lambda_response):
        """Test: Handles empty answer in response."""
        # Arrange
        queries = ["Query 1"]

        with patch('knowledge_base.lambda_client') as mock_client:
            body = {"answer": ""}
            payload = {"statusCode": 200, "body": json.dumps(body)}
            mock_client.invoke.return_value = {
                "StatusCode": 200,
                "Payload": BytesIO(json.dumps(payload).encode())
            }

            # Act
            result = query_knowledge_base(queries)

            # Assert - empty answers are filtered out
            assert result == []

    def test_handles_client_error(self):
        """Test: Handles boto3 ClientError gracefully."""
        # Arrange
        from botocore.exceptions import ClientError
        queries = ["Query 1"]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_client.invoke.side_effect = ClientError(
                {"Error": {"Code": "ServiceException", "Message": "Service error"}},
                "Invoke"
            )

            # Act
            result = query_knowledge_base(queries)

            # Assert
            assert result == []

    def test_handles_unexpected_error(self):
        """Test: Handles unexpected errors gracefully."""
        # Arrange
        queries = ["Query 1"]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_client.invoke.side_effect = Exception("Unexpected error")

            # Act
            result = query_knowledge_base(queries)

            # Assert
            assert result == []

    def test_uses_correct_function_name(self, mock_lambda_response):
        """Test: Uses correct Lambda function name based on env vars."""
        # Arrange
        queries = ["Query 1"]

        with patch('knowledge_base.lambda_client') as mock_client:
            with patch('knowledge_base.ENV', 'prod'):
                with patch('knowledge_base.APP_NAME', 'my-app'):
                    mock_client.invoke.return_value = mock_lambda_response("test")

                    # Act
                    query_knowledge_base(queries)

                    # Assert
                    call_args = mock_client.invoke.call_args
                    assert call_args[1]["FunctionName"] == "prod-my-app-query-kb"

    def test_passes_correct_top_k(self, mock_lambda_response):
        """Test: Passes correct top_k parameter to Lambda."""
        # Arrange
        queries = ["Query 1"]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_client.invoke.return_value = mock_lambda_response("test")

            # Act
            query_knowledge_base(queries, top_k=5)

            # Assert
            call_args = mock_client.invoke.call_args
            payload = json.loads(call_args[1]["Payload"])
            assert payload["topK"] == 5

    def test_aggregates_multiple_results(self, mock_lambda_response):
        """Test: Aggregates results from multiple queries."""
        # Arrange
        queries = ["Query 1", "Query 2"]
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            # Return different responses for each query (query-kb, ask-claude pairs)
            mock_client.invoke.side_effect = [
                create_raw_kb_response(raw_results),
                create_ask_claude_response("Answer 1"),
                create_raw_kb_response(raw_results),
                create_ask_claude_response("Answer 2"),
            ]

            # Act
            result = query_knowledge_base(queries)

            # Assert
            assert len(result) == 2
            assert any("Answer 1" in r for r in result)
            assert any("Answer 2" in r for r in result)


# ============================================================================
# Test Standard Query Helpers
# ============================================================================


class TestGetLambdaStandards:
    """Test get_lambda_standards function."""

    def test_queries_lambda_handler_standards(self, mock_lambda_response):
        """Test: Queries for Lambda handler standards."""
        # Arrange
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            get_lambda_standards()

            # Assert - should make 4 calls (2 queries × 2 lambdas each)
            assert mock_client.invoke.call_count == 4

    def test_returns_kb_results(self, mock_lambda_response):
        """Test: Returns results from knowledge base."""
        # Arrange
        raw_results = [{"score": 0.9, "text": "Lambda ADR content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            # Use side_effect to return fresh responses for each call (query-kb, ask-claude pairs)
            mock_client.invoke.side_effect = [
                create_raw_kb_response(raw_results),
                create_ask_claude_response("Lambda standards content"),
                create_raw_kb_response(raw_results),
                create_ask_claude_response("Lambda error handling"),
            ]

            # Act
            result = get_lambda_standards()

            # Assert
            assert len(result) == 2
            assert any("Lambda standards content" in r for r in result)


class TestGetTerraformStandards:
    """Test get_terraform_standards function."""

    def test_queries_terraform_standards(self, mock_lambda_response):
        """Test: Queries for Terraform standards."""
        # Arrange
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            get_terraform_standards()

            # Assert - should make 4 calls (2 queries × 2 lambdas each)
            assert mock_client.invoke.call_count == 4


class TestGetTestingStandards:
    """Test get_testing_standards function."""

    def test_queries_testing_standards(self, mock_lambda_response):
        """Test: Queries for testing standards."""
        # Arrange
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            get_testing_standards()

            # Assert - should make 4 calls (2 queries × 2 lambdas each)
            assert mock_client.invoke.call_count == 4


class TestGetGithubStandards:
    """Test get_github_standards function."""

    def test_queries_github_standards(self, mock_lambda_response):
        """Test: Queries for GitHub standards."""
        # Arrange
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            get_github_standards()

            # Assert - should make 4 calls (2 queries × 2 lambdas each)
            assert mock_client.invoke.call_count == 4


# ============================================================================
# Test get_standards_for_backend
# ============================================================================


class TestGetStandardsForBackend:
    """Test get_standards_for_backend function."""

    def test_queries_all_standard_types(self, mock_backend, mock_lambda_response):
        """Test: Queries for all standard types (handler, testing, infra, language)."""
        # Arrange
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Standard content")

            # Act
            result = get_standards_for_backend(mock_backend)

            # Assert - backend returns 4 query types, each with 2 lambda calls
            # Handler (1) + Testing (1) + Infra (1) + Language (1) + Framework (0) + CodeStyle (0) = 4 queries × 2 = 8
            assert mock_client.invoke.call_count == 8

    def test_returns_standards_context(self, mock_backend, mock_lambda_response):
        """Test: Returns StandardsContext with all fields populated."""
        # Arrange
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Standard content")

            # Act
            result = get_standards_for_backend(mock_backend)

            # Assert
            assert hasattr(result, 'handler_standards')
            assert hasattr(result, 'testing_standards')
            assert hasattr(result, 'infrastructure_standards')
            assert hasattr(result, 'language_standards')
            assert hasattr(result, 'framework_standards')

    def test_includes_additional_queries(self, mock_backend, mock_lambda_response):
        """Test: Includes additional repo-specific queries."""
        # Arrange
        additional = ["Custom query 1", "Custom query 2"]
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Standard content")

            # Act
            result = get_standards_for_backend(mock_backend, additional_queries=additional)

            # Assert - (4 backend queries + 2 additional) × 2 calls each = 12 total
            assert mock_client.invoke.call_count == 12

    def test_additional_queries_added_to_language_standards(
        self, mock_backend, mock_lambda_response
    ):
        """Test: Additional query results are added to language_standards."""
        # Arrange
        additional = ["Custom query"]
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            # Return different answers based on function and query
            def mock_invoke(**kwargs):
                function_name = kwargs.get('FunctionName', '')
                payload = json.loads(kwargs["Payload"])

                if 'ask-claude' in function_name:
                    query = payload.get("query", "")
                    if query == "Custom query":
                        return create_ask_claude_response("Custom result")
                    return create_ask_claude_response("Standard result")
                else:
                    return create_raw_kb_response(raw_results)

            mock_client.invoke.side_effect = mock_invoke

            # Act
            result = get_standards_for_backend(mock_backend, additional_queries=additional)

            # Assert - custom query result should be in language_standards
            all_language = " ".join(result.language_standards)
            assert "Custom result" in all_language

    def test_handles_backend_without_framework_queries(
        self, mock_backend, mock_lambda_response
    ):
        """Test: Handles backend that returns no framework queries."""
        # Arrange
        mock_backend.get_framework_standards_queries.return_value = []
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            result = get_standards_for_backend(mock_backend)

            # Assert
            assert result.framework_standards == []


# ============================================================================
# Test get_step_context
# ============================================================================


class TestGetStepContext:
    """Test get_step_context function."""

    def test_returns_empty_list_for_empty_queries(self, mock_backend):
        """Test: Returns empty list when no queries provided."""
        # Act
        result = get_step_context([], mock_backend)

        # Assert
        assert result == []

    def test_queries_knowledge_base(self, mock_backend, mock_lambda_response):
        """Test: Queries knowledge base with step-specific queries."""
        # Arrange
        queries = ["Step context query 1", "Step context query 2"]
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            result = get_step_context(queries, mock_backend)

            # Assert - 2 queries × 2 calls each = 4
            assert mock_client.invoke.call_count == 4

    def test_uses_provided_top_k(self, mock_backend, mock_lambda_response):
        """Test: Uses provided top_k parameter."""
        # Arrange
        queries = ["Query"]
        raw_results = [{"score": 0.9, "text": "Content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(mock_client, raw_results=raw_results, answer="Summary")

            # Act
            get_step_context(queries, mock_backend, top_k=10)

            # Assert - first call should be to query-kb with topK
            call_args = mock_client.invoke.call_args_list[0]
            payload = json.loads(call_args[1]["Payload"])
            assert payload["topK"] == 10

    def test_returns_query_results(self, mock_backend, mock_lambda_response):
        """Test: Returns results from knowledge base."""
        # Arrange
        queries = ["Step query"]
        raw_results = [{"score": 0.9, "text": "Step ADR content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(
                mock_client,
                raw_results=raw_results,
                answer="Step context content"
            )

            # Act
            result = get_step_context(queries, mock_backend)

            # Assert
            assert len(result) == 1
            assert "Step context content" in result[0]


# ============================================================================
# Test fetch_required_adrs
# ============================================================================


class TestFetchRequiredAdrs:
    """Test fetch_required_adrs function."""

    def test_returns_empty_list_for_empty_input(self):
        """Test: Returns empty list when no ADRs provided."""
        # Act
        result = fetch_required_adrs([])

        # Assert
        assert result == []

    def test_returns_empty_list_for_none_input(self):
        """Test: Returns empty list when None provided."""
        # Act
        result = fetch_required_adrs(None)

        # Assert
        assert result == []

    def test_fetches_single_adr(self, mock_lambda_response):
        """Test: Fetches single ADR by name and returns (name, content) tuple."""
        # Arrange
        adr_names = ["ADR-001-terraform-infrastructure-patterns"]
        raw_results = [{"score": 0.9, "text": "Terraform ADR content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(
                mock_client,
                raw_results=raw_results,
                answer="Terraform infrastructure patterns content"
            )

            # Act
            result = fetch_required_adrs(adr_names)

            # Assert - returns list of (name, content) tuples
            assert len(result) == 1
            adr_name, adr_content = result[0]
            assert adr_name == "ADR-001-terraform-infrastructure-patterns"
            assert "Terraform infrastructure patterns content" in adr_content

    def test_fetches_multiple_adrs(self, mock_lambda_response):
        """Test: Fetches multiple ADRs by name."""
        # Arrange
        adr_names = [
            "ADR-001-terraform-infrastructure-patterns",
            "ADR-004-lambda-handler-standards"
        ]
        raw_results = [{"score": 0.9, "text": "ADR content", "source": "ADR"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            def mock_invoke(**kwargs):
                function_name = kwargs.get('FunctionName', '')
                payload = json.loads(kwargs["Payload"])

                if 'ask-claude' in function_name:
                    query = payload.get("query", "")
                    if "ADR-001" in query:
                        return create_ask_claude_response("Terraform content")
                    return create_ask_claude_response("Lambda handler content")
                else:
                    return create_raw_kb_response(raw_results)

            mock_client.invoke.side_effect = mock_invoke

            # Act
            result = fetch_required_adrs(adr_names)

            # Assert
            assert len(result) == 2

    def test_handles_missing_adr(self, mock_lambda_response):
        """Test: Handles case where ADR is not found."""
        # Arrange
        adr_names = ["ADR-999-nonexistent"]

        with patch('knowledge_base.lambda_client') as mock_client:
            # Return empty results
            response = {
                "StatusCode": 200,
                "Payload": BytesIO(json.dumps({
                    "statusCode": 200,
                    "body": json.dumps({"results": []})
                }).encode())
            }
            mock_client.invoke.return_value = response

            # Act
            result = fetch_required_adrs(adr_names)

            # Assert
            assert result == []

    def test_uses_adr_doc_type_filter(self, mock_lambda_response):
        """Test: Filters to ADR document type only."""
        # Arrange
        adr_names = ["ADR-001-terraform-infrastructure-patterns"]
        raw_results = [{"score": 0.9, "text": "ADR content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(
                mock_client,
                raw_results=raw_results,
                answer="ADR content summary"
            )

            # Act
            fetch_required_adrs(adr_names)

            # Assert - first call should be to query-kb with docTypes
            call_args = mock_client.invoke.call_args_list[0]
            payload = json.loads(call_args[1]["Payload"])
            assert payload["docTypes"] == ["adr"]

    def test_uses_prioritized_query_with_target_repo(self, mock_lambda_response):
        """Test: Uses prioritized query when target_repo provided."""
        # Arrange
        adr_names = ["ADR-001-terraform-infrastructure-patterns"]
        target_repo = "owner/my-repo"
        raw_results = [{"score": 0.9, "text": "ADR content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            mock_kb_and_claude(
                mock_client,
                raw_results=raw_results,
                answer="ADR content summary"
            )

            # Act
            fetch_required_adrs(adr_names, target_repo=target_repo)

            # Assert - prioritized query makes 4 calls:
            # repo query (query-kb + ask-claude) + global query (query-kb + ask-claude)
            assert mock_client.invoke.call_count == 4

            # First call should be query-kb with repo filter
            first_call = mock_client.invoke.call_args_list[0]
            first_payload = json.loads(first_call[1]["Payload"])
            assert first_payload["repo"] == "my-repo"


class TestGetStandardsForBackendWithRequiredAdrs:
    """Test get_standards_for_backend with required_adrs parameter."""

    def test_includes_global_required_adrs_categorized_by_type(
        self, mock_backend, mock_lambda_response
    ):
        """Test: Global required ADRs are categorized into appropriate standards lists."""
        # Arrange
        required = MagicMock()
        required.repo = []
        # Terraform ADR should go to infrastructure_standards
        required.global_adrs = ["ADR-001-terraform-infrastructure-patterns"]
        raw_results = [{"score": 0.9, "text": "ADR content", "source": "ADR-001"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            def mock_invoke(**kwargs):
                function_name = kwargs.get('FunctionName', '')
                payload = json.loads(kwargs["Payload"])

                if 'ask-claude' in function_name:
                    query = payload.get("query", "")
                    if "ADR-001" in query:
                        return create_ask_claude_response("Required Terraform ADR content")
                    return create_ask_claude_response("Standard content")
                else:
                    return create_raw_kb_response(raw_results)

            mock_client.invoke.side_effect = mock_invoke

            # Act
            result = get_standards_for_backend(mock_backend, required_adrs=required)

            # Assert - Terraform ADR should be in infrastructure_standards, not handler_standards
            all_infrastructure = " ".join(result.infrastructure_standards)
            assert "Required Terraform ADR content" in all_infrastructure

    def test_includes_repo_required_adrs_in_handler_standards(
        self, mock_backend, mock_lambda_response
    ):
        """Test: Repo-specific required ADRs are included in handler_standards."""
        # Arrange
        required = MagicMock()
        required.repo = ["ADR-100-repo-specific"]
        required.global_adrs = []
        raw_results = [{"score": 0.9, "text": "ADR content", "source": "ADR-100"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            def mock_invoke(**kwargs):
                function_name = kwargs.get('FunctionName', '')
                payload = json.loads(kwargs["Payload"])

                if 'ask-claude' in function_name:
                    query = payload.get("query", "")
                    if "ADR-100" in query:
                        return create_ask_claude_response("Repo-specific ADR content")
                    return create_ask_claude_response("Standard content")
                else:
                    return create_raw_kb_response(raw_results)

            mock_client.invoke.side_effect = mock_invoke

            # Act
            result = get_standards_for_backend(
                mock_backend,
                required_adrs=required,
                target_repo="owner/my-repo"
            )

            # Assert - non-terraform ADRs default to handler_standards
            all_handler = " ".join(result.handler_standards)
            assert "Repo-specific ADR content" in all_handler

    def test_categorizes_lambda_adrs_as_handler_standards(
        self, mock_backend, mock_lambda_response
    ):
        """Test: Lambda/handler ADRs are categorized as handler_standards."""
        # Arrange
        required = MagicMock()
        required.repo = []
        required.global_adrs = ["ADR-004-lambda-handler-standards"]
        raw_results = [{"score": 0.9, "text": "ADR content", "source": "ADR-004"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            def mock_invoke(**kwargs):
                function_name = kwargs.get('FunctionName', '')
                payload = json.loads(kwargs["Payload"])

                if 'ask-claude' in function_name:
                    query = payload.get("query", "")
                    if "ADR-004" in query:
                        return create_ask_claude_response("Lambda handler ADR content")
                    return create_ask_claude_response("Standard content")
                else:
                    return create_raw_kb_response(raw_results)

            mock_client.invoke.side_effect = mock_invoke

            # Act
            result = get_standards_for_backend(mock_backend, required_adrs=required)

            # Assert - Lambda ADR should be in handler_standards
            all_handler = " ".join(result.handler_standards)
            assert "Lambda handler ADR content" in all_handler

    def test_categorizes_testing_adrs_as_testing_standards(
        self, mock_backend, mock_lambda_response
    ):
        """Test: Testing ADRs are categorized as testing_standards."""
        # Arrange
        required = MagicMock()
        required.repo = []
        required.global_adrs = ["ADR-003-python-testing-standards"]
        raw_results = [{"score": 0.9, "text": "ADR content", "source": "ADR-003"}]

        with patch('knowledge_base.lambda_client') as mock_client:
            def mock_invoke(**kwargs):
                function_name = kwargs.get('FunctionName', '')
                payload = json.loads(kwargs["Payload"])

                if 'ask-claude' in function_name:
                    query = payload.get("query", "")
                    if "ADR-003" in query:
                        return create_ask_claude_response("Testing ADR content")
                    return create_ask_claude_response("Standard content")
                else:
                    return create_raw_kb_response(raw_results)

            mock_client.invoke.side_effect = mock_invoke

            # Act
            result = get_standards_for_backend(mock_backend, required_adrs=required)

            # Assert - Testing ADR should be in testing_standards
            all_testing = " ".join(result.testing_standards)
            assert "Testing ADR content" in all_testing
