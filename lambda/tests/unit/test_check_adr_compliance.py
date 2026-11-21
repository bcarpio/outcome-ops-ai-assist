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
Unit tests for adr_compliance check handler.

Tests cover:
- GitHub token retrieval from SSM
- PR diff fetching from GitHub API
- File diff extraction from full PR diff
- Knowledge base querying via Lambda
- Claude analysis of code compliance
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

# First, load bedrock_utils module (dependency for adr_compliance)
bedrock_utils_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/bedrock_utils.py'
)
bedrock_spec = importlib.util.spec_from_file_location("bedrock_utils", bedrock_utils_path)
bedrock_module = importlib.util.module_from_spec(bedrock_spec)
bedrock_spec.loader.exec_module(bedrock_module)

# Create a fake check_handlers package that contains bedrock_utils
# This is needed because check handlers use relative import: from .bedrock_utils import ...
check_handlers_package = ModuleType("check_handlers")
check_handlers_package.__path__ = [os.path.dirname(bedrock_utils_path)]
check_handlers_package.bedrock_utils = bedrock_module
sys.modules['check_handlers'] = check_handlers_package
sys.modules['check_handlers.bedrock_utils'] = bedrock_module

# Load the adr_compliance handler module as part of check_handlers package
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/adr_compliance.py'
)
spec = importlib.util.spec_from_file_location(
    "check_handlers.adr_compliance",
    handler_path,
    submodule_search_locations=[os.path.dirname(handler_path)]
)
handler_module = importlib.util.module_from_spec(spec)
handler_module.__package__ = "check_handlers"
sys.modules['check_handlers.adr_compliance'] = handler_module
sys.modules['adr_compliance_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from loaded module
get_github_token = handler_module.get_github_token
fetch_pr_file_diff = handler_module.fetch_pr_file_diff
extract_file_diff_from_full_diff = handler_module.extract_file_diff_from_full_diff
query_knowledge_base = handler_module.query_knowledge_base
analyze_code_with_claude = handler_module.analyze_code_with_claude
check_adr_compliance = handler_module.check_adr_compliance
_extract_adr_json = handler_module._extract_adr_json


class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM"""

    @mock_aws()
    @patch('adr_compliance_handler.ssm_client')
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
    @patch('adr_compliance_handler.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test: SSM parameter not found"""
        # Arrange
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "get_parameter"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            get_github_token("/dev/app/nonexistent")

    @mock_aws()
    @patch('adr_compliance_handler.ssm_client')
    def test_get_github_token_empty_value(self, mock_ssm):
        """Test: SSM parameter exists but has no value"""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {}  # Missing Value key
        }

        # Act & Assert
        with pytest.raises(Exception, match="GitHub token not found"):
            get_github_token("/dev/app/token")


class TestFetchPrFileDiff:
    """Test GitHub PR diff fetching"""

    @patch('adr_compliance_handler.requests.get')
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
        mock_get.assert_called_once()
        # Verify headers
        call_args = mock_get.call_args
        assert call_args[1]['headers']['Authorization'] == 'Bearer token'
        assert 'application/vnd.github.v3.diff' in call_args[1]['headers']['Accept']

    @patch('adr_compliance_handler.requests.get')
    def test_fetch_pr_file_diff_api_error(self, mock_get):
        """Test: GitHub API returns error"""
        # Arrange
        mock_get.side_effect = requests.exceptions.RequestException("API Error")

        # Act & Assert
        with pytest.raises(Exception, match="GitHub API error"):
            fetch_pr_file_diff("owner/repo", 123, "file.py", "token")

    @patch('adr_compliance_handler.requests.get')
    def test_fetch_pr_file_diff_timeout(self, mock_get):
        """Test: GitHub API timeout"""
        # Arrange
        mock_get.side_effect = requests.exceptions.Timeout("Timeout")

        # Act & Assert
        with pytest.raises(Exception, match="GitHub API error"):
            fetch_pr_file_diff("owner/repo", 123, "file.py", "token")

    @patch('adr_compliance_handler.requests.get')
    def test_fetch_pr_file_diff_file_not_in_pr(self, mock_get):
        """Test: Requested file is not in the PR diff"""
        # Arrange
        mock_response = Mock()
        mock_response.text = "diff --git a/other.py b/other.py\n+other changes"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        # Act
        diff = fetch_pr_file_diff("owner/repo", 123, "missing.py", "token")

        # Assert
        assert "(No diff available)" in diff
        assert "missing.py" in diff


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

    def test_extract_file_diff_file_not_found(self):
        """Test: File not in diff returns empty string"""
        # Arrange
        full_diff = "diff --git a/other.py b/other.py\n+content"

        # Act
        result = extract_file_diff_from_full_diff(full_diff, "missing.py")

        # Assert
        assert result == ""

    def test_extract_file_diff_nested_path(self):
        """Test: Extract diff for file with nested path"""
        # Arrange
        full_diff = """diff --git a/lambda/handler/file.py b/lambda/handler/file.py
+content in nested file
diff --git a/other.py b/other.py
+other content"""

        # Act
        result = extract_file_diff_from_full_diff(full_diff, "lambda/handler/file.py")

        # Assert
        assert "lambda/handler/file.py" in result
        assert "content in nested file" in result
        assert "other content" not in result


class TestQueryKnowledgeBase:
    """Test knowledge base querying via Lambda"""

    @mock_aws()
    @patch('adr_compliance_handler.lambda_client')
    def test_query_knowledge_base_success(self, mock_lambda):
        """Test: Successfully query knowledge base"""
        # Arrange
        # query-kb Lambda returns answer/sources inside a JSON-stringified body field
        mock_payload = Mock()
        mock_payload.read.return_value = json.dumps({
            "body": json.dumps({
                "answer": "Test answer from KB",
                "sources": ["ADR-001", "ADR-002"]
            })
        }).encode()

        mock_response = {"Payload": mock_payload}
        mock_lambda.invoke.return_value = mock_response

        # Act
        result = query_knowledge_base("query-kb", "What are the standards?", 3)

        # Assert
        assert result["answer"] == "Test answer from KB"
        assert "ADR-001" in result["sources"]
        assert "ADR-002" in result["sources"]
        mock_lambda.invoke.assert_called_once()

    @mock_aws()
    @patch('adr_compliance_handler.lambda_client')
    def test_query_knowledge_base_lambda_error(self, mock_lambda):
        """Test: Lambda invocation fails"""
        # Arrange
        mock_lambda.invoke.side_effect = ClientError(
            {"Error": {"Code": "ServiceException", "Message": "Service error"}},
            "invoke"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            query_knowledge_base("query-kb", "query", 3)


class TestAnalyzeCodeWithClaude:
    """Test Claude analysis of code compliance"""

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_code_with_claude_compliant(self, mock_invoke):
        """Test: Claude determines code is compliant"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '{"compliant": true, "explanation": "Code follows standards", "suggestions": []}'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = analyze_code_with_claude(
            "lambda/handler.py",
            "+def handler(): pass",
            "Use Pydantic schemas",
            "lambda",
            usage_tracker
        )

        # Assert
        assert result["compliant"] is True
        assert "follows standards" in result["explanation"]
        assert result["suggestions"] == []
        assert usage_tracker.total_tokens > 0

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_code_with_claude_non_compliant(self, mock_invoke):
        """Test: Claude finds compliance issues"""
        # Arrange
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": '{"compliant": false, "explanation": "Missing schema", "suggestions": ["Add Pydantic model", "Add error handling"]}'
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })
        usage_tracker = bedrock_module.UsageTracker()

        # Act
        result = analyze_code_with_claude(
            "lambda/handler.py",
            "+def handler(): pass",
            "Use Pydantic schemas",
            "lambda",
            usage_tracker
        )

        # Assert
        assert result["compliant"] is False
        assert "Missing schema" in result["explanation"]
        assert len(result["suggestions"]) == 2
        assert "Add Pydantic model" in result["suggestions"]

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_code_with_claude_bedrock_error(self, mock_invoke):
        """Test: Bedrock API error"""
        # Arrange
        mock_invoke.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "converse"
        )
        usage_tracker = bedrock_module.UsageTracker()

        # Act: a governance check must fail CLOSED on a model error, not raise
        # and let the caller skip the file (which read as compliant).
        result = analyze_code_with_claude("file.py", "+code", "standards", "lambda", usage_tracker)

        # Assert: fail-closed manual-review WARN, no fabricated violations
        assert result["compliant"] is False
        assert "manual review" in result["explanation"].lower()
        assert result["violations"] == []

    @patch.object(bedrock_module, 'invoke_model')
    def test_analyze_code_with_claude_invalid_json_response(self, mock_invoke):
        """Test: Claude returns invalid JSON -> fails closed (manual review)"""
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
        result = analyze_code_with_claude("file.py", "+code", "standards", "lambda", usage_tracker)

        # Assert: unparseable response fails CLOSED (never guessed compliant)
        assert result["compliant"] is False
        assert "manual review" in result["explanation"].lower()
        assert result["violations"] == []


class TestCheckAdrCompliance:
    """Test main check_adr_compliance handler"""

    @mock_aws()
    @patch('adr_compliance_handler.get_github_token')
    @patch('adr_compliance_handler.query_knowledge_base')
    @patch('adr_compliance_handler.fetch_pr_file_diff')
    @patch('adr_compliance_handler.analyze_code_with_claude')
    def test_check_adr_compliance_all_files_compliant(
        self, mock_analyze, mock_fetch, mock_query, mock_token
    ):
        """Test: All changed files are ADR compliant"""
        # Arrange
        mock_token.return_value = "token"
        mock_query.return_value = {"answer": "Standards here", "sources": []}
        mock_fetch.return_value = "+new code"
        mock_analyze.return_value = {
            "compliant": True,
            "explanation": "Follows standards",
            "suggestions": []
        }

        # Act
        result = check_adr_compliance(
            "ADR_COMPLIANCE",
            123,
            "owner/repo",
            ["lambda/test/handler.py"],
            "query-kb-lambda",
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "ADR_COMPLIANCE"
        assert result["status"] == "PASS"
        assert "follow" in result["message"].lower() or "compliant" in result["message"].lower()

    @mock_aws()
    @patch('adr_compliance_handler.get_github_token')
    @patch('adr_compliance_handler.query_knowledge_base')
    @patch('adr_compliance_handler.fetch_pr_file_diff')
    @patch('adr_compliance_handler.analyze_code_with_claude')
    def test_check_adr_compliance_has_violations(
        self, mock_analyze, mock_fetch, mock_query, mock_token
    ):
        """Test: Some files have ADR violations"""
        # Arrange
        mock_token.return_value = "token"
        mock_query.return_value = {"answer": "Standards", "sources": []}
        mock_fetch.return_value = "+code"
        mock_analyze.return_value = {
            "compliant": False,
            "explanation": "Missing schema",
            "suggestions": ["Add Pydantic model"]
        }

        # Act
        result = check_adr_compliance(
            "ADR_COMPLIANCE",
            123,
            "owner/repo",
            ["lambda/test/handler.py"],
            "query-kb-lambda",
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "ADR_COMPLIANCE"
        assert result["status"] == "WARN"
        assert len(result["details"]) > 0
        assert "Missing schema" in str(result["details"])

    @mock_aws()
    @patch('adr_compliance_handler.get_github_token')
    def test_check_adr_compliance_no_relevant_files(self, mock_token):
        """Test: No Lambda or Terraform files in PR"""
        # Arrange
        mock_token.return_value = "token"

        # Act
        result = check_adr_compliance(
            "ADR_COMPLIANCE",
            123,
            "owner/repo",
            ["README.md", "docs/guide.md"],
            "query-kb-lambda",
            "/dev/github/token"
        )

        # Assert
        assert result["checkType"] == "ADR_COMPLIANCE"
        assert result["status"] == "PASS"
        assert "follow" in result["message"].lower() or "pass" in result["status"].lower()


def test_extract_adr_json_skips_prose_brace_snippet():
    # Reproduces the 2026-10-09 PR #92 failure: reasoning prose + a numbered list
    # containing a quoted {"statusCode": 200, ...} snippet BEFORE the real JSON.
    # The old first-'{'-to-last-'}' slice spanned the snippet brace -> unparseable.
    resp = (
        "I need to determine what type of Lambda this is first.\n"
        '6. Error handling: returns `{"statusCode": 200, "body": x}` instead of '
        "using _response(). No CORS headers present.\n"
        '{"compliant": false, "explanation": "missing JWT auth and CORS", '
        '"suggestions": ["add decode_token(event)"], '
        '"violations": [{"line": 17, "message": "no JWT authentication"}, '
        '{"line": 22, "message": "exception swallowed without logging"}]}'
    )
    out = _extract_adr_json(resp)
    assert out is not None
    assert out["compliant"] is False
    assert [v["line"] for v in out["violations"]] == [17, 22]
    assert out["violations"][1]["message"] == "exception swallowed without logging"


def test_extract_adr_json_plain_and_fenced():
    assert _extract_adr_json('{"compliant": true, "violations": []}')["compliant"] is True
    fenced = '```json\n{"compliant": true, "violations": []}\n```'
    assert _extract_adr_json(fenced)["compliant"] is True
    # A response with only prose braces and no compliant-object returns None.
    assert _extract_adr_json('see `{"foo": 1}` for details') is None


# --- Provider passthrough to the scope resolver (ADR-052 provider parity) ---

from contextlib import ExitStack  # noqa: E402

_PROVIDER_JOBS = [
    ("gitlab", "group/sub/project", {}),
    ("azure_devops", "org/project/repo",
     {"ado_org": "org", "ado_project": "project", "ado_repo": "repo"}),
]


def _enter_provider_auth(stack, provider, module):
    stack.enter_context(patch(
        'shared.pr_metadata.fetch_pr_metadata',
        return_value=MagicMock(base_sha="b", head_sha="h"),
    ))
    if provider == "gitlab":
        stack.enter_context(patch('check_handlers.gitlab_context.get_gitlab_token', return_value="gl"))
        stack.enter_context(patch('check_handlers.gitlab_context.get_gitlab_base_url', return_value="https://gl"))
    else:
        stack.enter_context(patch.object(module, 'refresh_ado_token', return_value="ado"))


@pytest.mark.parametrize("provider,repository,ado_kwargs", _PROVIDER_JOBS)
def test_adr_compliance_passes_provider_to_scope_resolvers(provider, repository, ado_kwargs):
    empty = {"status": "PASS", "message": "", "details": [], "findings": []}
    with ExitStack() as stack:
        _enter_provider_auth(stack, provider, handler_module)
        scope = stack.enter_context(patch(
            'shared.pr_check_scope.resolve_pr_check_workspace_scope', return_value=["ws-a"],
        ))
        owners = stack.enter_context(patch(
            'shared.pr_check_scope.find_workspaces_for_repo', return_value=["ws-a"],
        ))
        lam = stack.enter_context(patch.object(handler_module, 'analyze_lambda_handlers', return_value=empty))
        stack.enter_context(patch.object(handler_module, 'analyze_terraform_files', return_value=empty))
        result = check_adr_compliance(
            "ADR_COMPLIANCE", 1, repository, ["lambda/x/handler.py"], "qkb", "/p",
            provider=provider, connection_id="c", **ado_kwargs,
        )

    assert result["status"] == "PASS"
    scope.assert_called_once_with(
        check_type="ADR_COMPLIANCE", repo_name=repository, provider=provider,
    )
    owners.assert_called_once_with(repository, provider=provider)
    assert lam.call_args.kwargs["workspace_ids"] == ["ws-a"]
    assert lam.call_args.kwargs["workspace_id"] == "ws-a"


def test_adr_compliance_scope_resolver_failure_falls_back_workspace_blind():
    empty = {"status": "PASS", "message": "", "details": [], "findings": []}
    with ExitStack() as stack:
        _enter_provider_auth(stack, "gitlab", handler_module)
        stack.enter_context(patch(
            'shared.pr_check_scope.resolve_pr_check_workspace_scope', side_effect=RuntimeError("ddb"),
        ))
        stack.enter_context(patch(
            'shared.pr_check_scope.find_workspaces_for_repo', side_effect=RuntimeError("ddb"),
        ))
        lam = stack.enter_context(patch.object(handler_module, 'analyze_lambda_handlers', return_value=empty))
        stack.enter_context(patch.object(handler_module, 'analyze_terraform_files', return_value=empty))
        result = check_adr_compliance(
            "ADR_COMPLIANCE", 1, "group/project", ["lambda/x/handler.py"], "qkb", "/p",
            provider="gitlab", connection_id="c",
        )

    assert result["status"] == "PASS"
    assert lam.call_args.kwargs["workspace_ids"] is None
    assert lam.call_args.kwargs["workspace_id"] == ""
