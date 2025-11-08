"""
Unit tests for architectural_duplication check handler.

Tests cover:
- GitHub token retrieval from SSM
- PR diff fetching from GitHub API
- File diff extraction from full PR diff
- Knowledge base querying via Lambda
- Claude PR summarization
- Claude similarity analysis
- Main handler orchestration
"""

import json
import pytest
import sys
import os
import importlib.util
from unittest.mock import Mock, patch, MagicMock
from moto import mock_aws
import boto3
import requests
from botocore.exceptions import ClientError

# Load the architectural_duplication handler module
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/architectural_duplication.py'
)
spec = importlib.util.spec_from_file_location("architectural_duplication_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['architectural_duplication_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from loaded module
get_github_token = handler_module.get_github_token
fetch_pr_file_diff = handler_module.fetch_pr_file_diff
extract_file_diff_from_full_diff = handler_module.extract_file_diff_from_full_diff
query_knowledge_base = handler_module.query_knowledge_base
summarize_pr_with_claude = handler_module.summarize_pr_with_claude
analyze_similarity_with_claude = handler_module.analyze_similarity_with_claude
check_architectural_duplication = handler_module.check_architectural_duplication


class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM"""

    @mock_aws()
    @patch('architectural_duplication_handler.ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test: Successfully retrieve GitHub token from SSM"""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ghp_test_token_12345"}
        }

        # Act
        token = get_github_token("/dev/app/github/token")

        # Assert
        assert token == "ghp_test_token_12345"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/dev/app/github/token",
            WithDecryption=True
        )

    @mock_aws()
    @patch('architectural_duplication_handler.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test: SSM parameter not found"""
        # Arrange
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "get_parameter"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            get_github_token("/dev/app/nonexistent")


class TestFetchPrFileDiff:
    """Test GitHub PR diff fetching"""

    @patch('architectural_duplication_handler.requests.get')
    def test_fetch_pr_file_diff_success(self, mock_get):
        """Test: Successfully fetch file diff from GitHub"""
        # Arrange
        mock_response = Mock()
        mock_response.text = """diff --git a/lambda/test/handler.py b/lambda/test/handler.py
index abc123..def456 100644
--- a/lambda/test/handler.py
+++ b/lambda/test/handler.py
@@ -1,3 +1,5 @@
+def new_function():
+    pass
 def old_function():
     pass"""
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        # Act
        diff = fetch_pr_file_diff("owner/repo", 123, "lambda/test/handler.py", "token")

        # Assert
        assert "new_function" in diff
        assert "lambda/test/handler.py" in diff


class TestExtractFileDiff:
    """Test extracting specific file diff from full PR diff"""

    def test_extract_file_diff_single_file(self):
        """Test: Extract diff for a single file"""
        # Arrange
        full_diff = """diff --git a/file1.py b/file1.py
+line1 in file1
diff --git a/file2.py b/file2.py
+line2 in file2"""

        # Act
        result = extract_file_diff_from_full_diff(full_diff, "file1.py")

        # Assert
        assert "file1.py" in result
        assert "line1 in file1" in result
        assert "line2 in file2" not in result


class TestQueryKnowledgeBase:
    """Test knowledge base querying via Lambda"""

    @mock_aws()
    @patch('architectural_duplication_handler.lambda_client')
    def test_query_knowledge_base_success(self, mock_lambda):
        """Test: Successfully query knowledge base"""
        # Arrange
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "answer": "Related functionality found in repo/handler",
                "sources": ["repo/handler.py"]
            }).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        # Act
        result = query_knowledge_base("query-kb", "search for similar handlers", 20)

        # Assert
        assert "Related functionality" in result["answer"]
        assert "repo/handler.py" in result["sources"]
        mock_lambda.invoke.assert_called_once()


class TestSummarizePrWithClaude:
    """Test Claude PR summarization"""

    @patch('architectural_duplication_handler.bedrock_client')
    def test_summarize_pr_with_claude_success(self, mock_bedrock):
        """Test: Claude successfully summarizes PR"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{
                        "text": "This PR adds a Lambda handler that processes GitHub webhook events."
                    }]
                }
            }
        }

        file_diffs = {
            "lambda/hello/handler.py": "+def handler(event, context): pass"
        }

        # Act
        result = summarize_pr_with_claude(
            "owner/repo",
            ["lambda/hello/handler.py"],
            file_diffs
        )

        # Assert
        assert "Lambda handler" in result
        assert "GitHub webhook" in result

    @patch('architectural_duplication_handler.bedrock_client')
    def test_summarize_pr_with_claude_bedrock_error(self, mock_bedrock):
        """Test: Bedrock API error"""
        # Arrange
        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "converse"
        )

        # Act & Assert
        with pytest.raises(Exception):
            summarize_pr_with_claude("owner/repo", ["lambda/hello/handler.py"], {})


class TestAnalyzeSimilarityWithClaude:
    """Test Claude similarity analysis"""

    @patch('architectural_duplication_handler.bedrock_client')
    def test_analyze_similarity_has_similar(self, mock_bedrock):
        """Test: Claude finds similar functionality"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{
                        "text": '{"hasSimilar": true, "findings": ["repo/handler.py has similar webhook processing"]}'
                    }]
                }
            }
        }

        # Act
        result = analyze_similarity_with_claude(
            "PR adds webhook handler",
            "KB contains: repo/handler.py handles webhooks"
        )

        # Assert
        assert result["hasSimilar"] is True
        assert len(result["findings"]) > 0
        assert "webhook" in result["findings"][0]

    @patch('architectural_duplication_handler.bedrock_client')
    def test_analyze_similarity_no_similar(self, mock_bedrock):
        """Test: Claude finds no similar functionality"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{
                        "text": '{"hasSimilar": false, "findings": []}'
                    }]
                }
            }
        }

        # Act
        result = analyze_similarity_with_claude(
            "PR adds unique functionality",
            "KB contains unrelated code"
        )

        # Assert
        assert result["hasSimilar"] is False
        assert result["findings"] == []

    @patch('architectural_duplication_handler.bedrock_client')
    def test_analyze_similarity_invalid_json(self, mock_bedrock):
        """Test: Handle invalid JSON response (fallback parsing)"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{
                        "text": "not valid json at all"
                    }]
                }
            }
        }

        # Act
        result = analyze_similarity_with_claude("PR summary", "KB context")

        # Assert
        assert "hasSimilar" in result
        assert result["hasSimilar"] is False
        assert "Could not parse" in result["findings"][0]


class TestCheckArchitecturalDuplication:
    """Test main check_architectural_duplication handler"""

    def test_check_architectural_duplication_no_handlers(self):
        """Test: No handler files changed"""
        # Arrange
        changed_files = ["README.md", "terraform/main.tf"]

        # Act
        result = check_architectural_duplication(
            "ARCHITECTURAL_DUPLICATION",
            123,
            "owner/repo",
            changed_files,
            "query-kb-lambda",
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "ARCHITECTURAL_DUPLICATION"
        assert result["status"] == "PASS"
        assert "No handler files changed" in result["message"]

    @patch('architectural_duplication_handler.get_github_token')
    @patch('architectural_duplication_handler.fetch_pr_file_diff')
    @patch('architectural_duplication_handler.summarize_pr_with_claude')
    @patch('architectural_duplication_handler.query_knowledge_base')
    @patch('architectural_duplication_handler.analyze_similarity_with_claude')
    def test_check_architectural_duplication_similar_found(
        self, mock_analyze, mock_query_kb, mock_summarize, mock_fetch, mock_token
    ):
        """Test: Similar functionality identified"""
        # Arrange
        mock_token.return_value = "token"
        mock_fetch.return_value = "+new code"
        mock_summarize.return_value = "This PR adds webhook processing"
        mock_query_kb.return_value = {
            "answer": "Found similar handlers",
            "sources": ["repo/handler.py"]
        }
        mock_analyze.return_value = {
            "hasSimilar": True,
            "findings": ["repo/handler.py has similar webhook processing"]
        }

        # Act
        result = check_architectural_duplication(
            "ARCHITECTURAL_DUPLICATION",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda",
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "ARCHITECTURAL_DUPLICATION"
        assert result["status"] == "WARN"
        assert "Related functionality identified" in result["message"]
        assert any("webhook" in str(detail) for detail in result["details"])

    @patch('architectural_duplication_handler.get_github_token')
    @patch('architectural_duplication_handler.fetch_pr_file_diff')
    @patch('architectural_duplication_handler.summarize_pr_with_claude')
    @patch('architectural_duplication_handler.query_knowledge_base')
    @patch('architectural_duplication_handler.analyze_similarity_with_claude')
    def test_check_architectural_duplication_no_similar(
        self, mock_analyze, mock_query_kb, mock_summarize, mock_fetch, mock_token
    ):
        """Test: No similar functionality identified"""
        # Arrange
        mock_token.return_value = "token"
        mock_fetch.return_value = "+new code"
        mock_summarize.return_value = "This PR adds unique functionality"
        mock_query_kb.return_value = {
            "answer": "No related code found",
            "sources": []
        }
        mock_analyze.return_value = {
            "hasSimilar": False,
            "findings": []
        }

        # Act
        result = check_architectural_duplication(
            "ARCHITECTURAL_DUPLICATION",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda",
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "ARCHITECTURAL_DUPLICATION"
        assert result["status"] == "PASS"
        assert "No related functionality" in result["message"]

    @patch('architectural_duplication_handler.get_github_token')
    def test_check_architectural_duplication_error(self, mock_token):
        """Test: Error during analysis (fallback to WARN)"""
        # Arrange
        mock_token.side_effect = Exception("API error")

        # Act
        result = check_architectural_duplication(
            "ARCHITECTURAL_DUPLICATION",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda",
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "ARCHITECTURAL_DUPLICATION"
        assert result["status"] == "WARN"
        assert "check failed" in result["message"]
        assert any("Error:" in detail for detail in result["details"])
