# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0

# === Legacy dict -> unified Response adapter (ADR-035 fallout) ===
from types import SimpleNamespace
from shared.model_client_types import StopReason
class _LegacyDictResponse:
    def __init__(self, d):
        content = d.get("output", {}).get("message", {}).get("content", [])
        self.text = content[0].get("text", "") if content else ""
        self.content_blocks = [{"text": self.text}] if self.text else []
        usage = d.get("usage", {})
        self.usage = SimpleNamespace(input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0))
        self.stop_reason = StopReason.END_TURN
        self.tool_calls = None
        self.raw = d
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
from types import ModuleType

# First, load bedrock_utils module (dependency for architectural_duplication)
bedrock_utils_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/bedrock_utils.py'
)
bedrock_spec = importlib.util.spec_from_file_location("bedrock_utils", bedrock_utils_path)
bedrock_module = importlib.util.module_from_spec(bedrock_spec)
bedrock_spec.loader.exec_module(bedrock_module)

# Create a fake check_handlers package that contains bedrock_utils
check_handlers_package = ModuleType("check_handlers")
check_handlers_package.__path__ = [os.path.dirname(bedrock_utils_path)]
check_handlers_package.bedrock_utils = bedrock_module
sys.modules['check_handlers'] = check_handlers_package
sys.modules['check_handlers.bedrock_utils'] = bedrock_module

# Load the architectural_duplication handler module as part of check_handlers package
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/architectural_duplication.py'
)
spec = importlib.util.spec_from_file_location(
    "check_handlers.architectural_duplication",
    handler_path,
    submodule_search_locations=[os.path.dirname(handler_path)]
)
handler_module = importlib.util.module_from_spec(spec)
handler_module.__package__ = "check_handlers"
sys.modules['check_handlers.architectural_duplication'] = handler_module
sys.modules['architectural_duplication_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from loaded module
get_github_token = handler_module.get_github_token
fetch_pr_file_diff = handler_module.fetch_pr_file_diff
extract_file_diff_from_full_diff = handler_module.extract_file_diff_from_full_diff
query_knowledge_base = handler_module.query_knowledge_base
summarize_pr_with_claude = handler_module.summarize_pr_with_claude
extract_functional_keywords = handler_module.extract_functional_keywords
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
        """Test: Successfully query knowledge base (raw mode)"""
        # Arrange - raw mode returns results array, not answer
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "statusCode": 200,
                "body": json.dumps({
                    "results": [
                        {
                            "source": "repo/handler.py",
                            "text": "Related functionality found in repo/handler",
                            "score": 0.95
                        }
                    ]
                })
            }).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        # Act
        result = query_knowledge_base("query-kb", "search for similar handlers", 20)

        # Assert - answer is now formatted from raw results
        assert "Related functionality" in result["answer"]
        assert "repo/handler.py" in result["sources"]
        mock_lambda.invoke.assert_called_once()


class TestSummarizePrWithClaude:
    """Test Claude PR summarization"""

    @patch.object(bedrock_module, 'invoke_model')
    def test_summarize_pr_with_claude_success(self, mock_invoke):
        """Test: Claude successfully summarizes PR"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": "This PR adds a Lambda handler that processes GitHub webhook events."
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        file_diffs = {
            "lambda/hello/handler.py": "+def handler(event, context): pass"
        }
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = summarize_pr_with_claude(
            "owner/repo",
            ["lambda/hello/handler.py"],
            file_diffs,
            usage_tracker
        )

        # Assert
        assert "Lambda handler" in result
        assert "GitHub webhook" in result
        assert usage_tracker.total_tokens > 0

    @patch.object(bedrock_module, 'invoke_model')
    def test_summarize_pr_with_claude_bedrock_error(self, mock_invoke):
        """Test: Bedrock API error"""
        # Arrange
        mock_invoke.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "converse"
        )
        usage_tracker = bedrock_module.UsageTracker()

        # Act & Assert
        with pytest.raises(Exception):
            summarize_pr_with_claude("owner/repo", ["lambda/hello/handler.py"], {}, usage_tracker)


class TestExtractFunctionalKeywords:
    """Test functional keyword extraction for multi-query KB search"""

    @patch.object(bedrock_module, 'invoke_model')
    def test_extract_functional_keywords_success(self, mock_invoke):
        """Test: Claude successfully extracts search queries"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '["list documents handler", "document listing service", "retrieve recent docs"]'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        file_diffs = {
            "lambda/list-docs/handler.py": "+def handler(event, context): pass"
        }
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = extract_functional_keywords(
            "Lists recently ingested documents",
            file_diffs,
            usage_tracker
        )

        # Assert
        assert isinstance(result, list)
        assert len(result) == 3
        assert "list documents handler" in result
        assert usage_tracker.total_tokens > 0

    @patch.object(bedrock_module, 'invoke_model')
    def test_extract_functional_keywords_invalid_json_fallback(self, mock_invoke):
        """Test: Falls back to summary when JSON parsing fails"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": "not valid json"
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = extract_functional_keywords(
            "Lists recently ingested documents",
            {},
            usage_tracker
        )

        # Assert
        assert result == ["Lists recently ingested documents"]

    @patch.object(bedrock_module, 'invoke_model')
    def test_extract_functional_keywords_limits_to_five(self, mock_invoke):
        """Test: Limits results to 5 queries max"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '["q1", "q2", "q3", "q4", "q5", "q6", "q7"]'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = extract_functional_keywords("Summary", {}, usage_tracker)

        # Assert
        assert len(result) == 5


class TestAnalyzeSimilarityWithClaude:
    """Test Claude similarity analysis"""

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_similarity_has_similar(self, mock_invoke):
        """Test: Claude finds similar functionality"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": ('{"hasSimilar": true, "findings": [{'
                                 '"changed_file": "lambda/hello/handler.py", '
                                 '"existing_ref": "repo/handler.py", '
                                 '"message": "same webhook processing - candidate for shared library"}]}')
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = analyze_similarity_with_claude(
            "PR adds webhook handler",
            "KB contains: repo/handler.py handles webhooks",
            ["lambda/hello/handler.py"],
            usage_tracker
        )

        # Assert -- structured findings carry changed_file for anchoring.
        assert result["hasSimilar"] is True
        assert result["parse_failed"] is False
        assert len(result["findings"]) == 1
        assert result["findings"][0]["changed_file"] == "lambda/hello/handler.py"
        assert "webhook" in result["findings"][0]["message"]
        assert usage_tracker.total_tokens > 0

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_similarity_drops_unstructured_findings(self, mock_invoke):
        """A finding missing changed_file (or not a dict) is dropped."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": ('{"hasSimilar": true, "findings": ['
                                 '"a bare string", '
                                 '{"existing_ref": "repo/x.py", "message": "no changed_file"}]}')
                    }]
                }
            },
            "usage": {"inputTokens": 10, "outputTokens": 5}
        })
        usage_tracker = bedrock_module.UsageTracker()
        result = analyze_similarity_with_claude(
            "PR", "KB", ["lambda/hello/handler.py"], usage_tracker)
        assert result["parse_failed"] is False
        assert result["findings"] == []  # both dropped

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_similarity_no_similar(self, mock_invoke):
        """Test: Claude finds no similar functionality"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '{"hasSimilar": false, "findings": []}'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = analyze_similarity_with_claude(
            "PR adds unique functionality",
            "KB contains unrelated code",
            ["lambda/hello/handler.py"],
            usage_tracker
        )

        # Assert
        assert result["hasSimilar"] is False
        assert result["findings"] == []

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_similarity_invalid_json(self, mock_invoke):
        """Test: Handle invalid JSON response (fallback parsing)"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": "not valid json at all"
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = analyze_similarity_with_claude("PR summary", "KB context", ["lambda/hello/handler.py"], usage_tracker)

        # Assert -- fail CLOSED: no findings, parse_failed flagged (never a
        # silent "unique"). The caller turns parse_failed into a WARN.
        assert "hasSimilar" in result
        assert result["hasSimilar"] is False
        assert result["findings"] == []
        assert result["parse_failed"] is True


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
            "findings": [{
                "changed_file": "lambda/hello/handler.py",
                "existing_ref": "repo/handler.py",
                "message": "similar webhook processing - candidate for shared library",
            }],
            "parse_failed": False,
        }

        # Act -- anchors supplied so the finding posts inline too.
        result = check_architectural_duplication(
            "ARCHITECTURAL_DUPLICATION",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda",
            "/dev/github/token",
            anchors={"lambda/hello/handler.py": [5, 9]},
        )

        # Assert
        assert result["checkType"] == "ARCHITECTURAL_DUPLICATION"
        assert result["status"] == "WARN"
        assert "Similar implementations found" in result["message"]
        assert any("webhook" in str(detail) for detail in result["details"])
        # One inline finding, anchored on the changed file's first added line.
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["path"] == "lambda/hello/handler.py"
        assert f["line"] == 5
        assert f["severity"] == "info"
        assert "repo/handler.py" in f["message"]

    @patch('architectural_duplication_handler.get_github_token')
    @patch('architectural_duplication_handler.fetch_pr_file_diff')
    @patch('architectural_duplication_handler.summarize_pr_with_claude')
    @patch('architectural_duplication_handler.query_knowledge_base')
    @patch('architectural_duplication_handler.analyze_similarity_with_claude')
    def test_parse_failed_fails_closed_to_warn(
        self, mock_analyze, mock_query_kb, mock_summarize, mock_fetch, mock_token
    ):
        """parse_failed -> WARN manual review, never a silent PASS/unique."""
        mock_token.return_value = "token"
        mock_fetch.return_value = "+new code"
        mock_summarize.return_value = "This PR does something"
        mock_query_kb.return_value = {"answer": "x", "sources": ["repo/h.py"]}
        mock_analyze.return_value = {
            "hasSimilar": False, "findings": [], "parse_failed": True}

        result = check_architectural_duplication(
            "ARCHITECTURAL_DUPLICATION", 123, "owner/repo",
            ["lambda/hello/handler.py"], "query-kb-lambda", "/dev/github/token")

        assert result["status"] == "WARN"
        assert "manual review" in result["message"].lower()
        assert not result.get("findings")  # no inline findings on a parse failure

    @patch('architectural_duplication_handler.get_github_token')
    @patch('architectural_duplication_handler.fetch_pr_file_diff')
    @patch('architectural_duplication_handler.summarize_pr_with_claude')
    @patch('architectural_duplication_handler.query_knowledge_base')
    @patch('architectural_duplication_handler.analyze_similarity_with_claude')
    def test_finding_without_anchor_stays_summary_only(
        self, mock_analyze, mock_query_kb, mock_summarize, mock_fetch, mock_token
    ):
        """A changed_file absent from anchors gets no inline Finding (ADR-031:
        never fabricate a line) but still appears in the summary details."""
        mock_token.return_value = "token"
        mock_fetch.return_value = "+new code"
        mock_summarize.return_value = "This PR adds webhook processing"
        mock_query_kb.return_value = {"answer": "x", "sources": ["repo/h.py"]}
        mock_analyze.return_value = {
            "hasSimilar": True,
            "findings": [{
                "changed_file": "lambda/hello/handler.py",
                "existing_ref": "repo/h.py",
                "message": "same webhook processing",
            }],
            "parse_failed": False,
        }

        # No anchors -> no safe line -> summary only.
        result = check_architectural_duplication(
            "ARCHITECTURAL_DUPLICATION", 123, "owner/repo",
            ["lambda/hello/handler.py"], "query-kb-lambda", "/dev/github/token",
            anchors={})

        assert result["status"] == "WARN"
        assert result.get("findings") == []  # nothing anchored
        assert any("webhook" in str(d) for d in result["details"])  # still in summary

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
