# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0

# === Legacy dict -> unified Response adapter ===
# Tests written before chunk 09 mocked the boto3 Converse-shaped dict
# response. The fixer code now consumes unified Response objects from
# shared.model_client.invoke. Wrap legacy dicts so they expose the new
# attribute surface (resp.text, resp.usage.input_tokens, etc.) without
# rewriting every fixture.
from types import SimpleNamespace
from shared.model_client_types import StopReason

class _LegacyDictResponse:
    def __init__(self, d):
        content = d.get("output", {}).get("message", {}).get("content", [])
        self.text = content[0].get("text", "") if content else ""
        self.content_blocks = [{"text": self.text}] if self.text else []
        usage = d.get("usage", {})
        self.usage = SimpleNamespace(
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )
        self.stop_reason = StopReason.END_TURN
        self.tool_calls = None
        self.raw = d

"""
Unit tests for the tests_fixer module.
"""

import base64
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# Set environment variables before importing
os.environ.setdefault("ENV", "dev")
os.environ.setdefault("APP_NAME", "outcome-ops-ai-assist")

# Add generate-code to path
generate_code_dir = os.path.join(os.path.dirname(__file__), "../../generate-code")
sys.path.insert(0, os.path.abspath(generate_code_dir))

import importlib.util

# Load tests_fixer module
tests_fixer_path = os.path.join(generate_code_dir, "tests_fixer.py")
spec = importlib.util.spec_from_file_location("tests_fixer", tests_fixer_path)
tests_fixer_module = importlib.util.module_from_spec(spec)
sys.modules["tests_fixer"] = tests_fixer_module
spec.loader.exec_module(tests_fixer_module)


class TestFetchTestAnalysis:
    """Tests for fetch_test_analysis function."""

    def test_finds_test_analysis_comment(self):
        """Successfully extract test analysis from comments."""
        comments = [
            {"body": "Some random comment"},
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

The tests are failing because of import errors.

---
**Next Steps:**
- Fix the imports
"""
            },
        ]

        result = tests_fixer_module.fetch_test_analysis(comments)

        assert result is not None
        assert "import errors" in result

    def test_returns_none_when_no_analysis(self):
        """Return None when no analysis comment exists."""
        comments = [
            {"body": "Regular comment"},
            {"body": "Another comment"},
        ]

        result = tests_fixer_module.fetch_test_analysis(comments)

        assert result is None

    def test_returns_most_recent_analysis(self):
        """Return the most recent analysis when multiple exist."""
        comments = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

Old analysis.

---
"""
            },
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

New analysis.

---
"""
            },
        ]

        result = tests_fixer_module.fetch_test_analysis(comments)

        assert result is not None
        assert "New analysis" in result

    def test_handles_empty_comments_list(self):
        """Handle empty comments list."""
        result = tests_fixer_module.fetch_test_analysis([])

        assert result is None


class TestFetchTestFailureOutput:
    """Tests for fetch_test_failure_output function."""

    def test_extracts_test_output(self):
        """Successfully extract test output from comment."""
        comments = [
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

### Test Output (last 2000 chars)
```
FAILED tests/test_foo.py::test_bar - AssertionError
```
"""
            }
        ]

        result = tests_fixer_module.fetch_test_failure_output(comments)

        assert result is not None
        assert "FAILED" in result
        assert "test_foo.py" in result

    def test_returns_none_when_no_test_results(self):
        """Return None when no test results comment exists."""
        comments = [
            {"body": "Not a test results comment"},
        ]

        result = tests_fixer_module.fetch_test_failure_output(comments)

        assert result is None

    def test_handles_test_output_without_code_block(self):
        """Handle test results comment without code block."""
        comments = [
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

Some failure text here.
"""
            }
        ]

        result = tests_fixer_module.fetch_test_failure_output(comments)

        # Should return body as fallback
        assert result is not None


class TestExtractFailedTestFiles:
    """Tests for extract_failed_test_files function."""

    def test_extracts_failed_pattern(self):
        """Extract files from FAILED pattern."""
        test_output = """
FAILED lambda/tests/unit/test_foo.py::test_bar - AssertionError
FAILED lambda/tests/unit/test_baz.py::test_qux - ValueError
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_foo.py" in result
        assert "lambda/tests/unit/test_baz.py" in result

    def test_extracts_traceback_pattern(self):
        """Extract files from traceback line numbers."""
        test_output = """
lambda/tests/unit/test_example.py:42: AssertionError
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_example.py" in result

    def test_normalizes_unit_paths(self):
        """Normalize unit/ paths to lambda/tests/unit/."""
        test_output = """
FAILED unit/test_something.py::test_func - Error
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_something.py" in result

    def test_filters_venv_paths(self):
        """Filter out venv/site-packages paths."""
        test_output = """
FAILED lambda/tests/unit/test_real.py::test_func - Error
venv/lib/python3.12/site-packages/_pytest/python.py:123: in collect
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_real.py" in result
        assert not any("venv" in f for f in result)
        assert not any("site-packages" in f for f in result)

    def test_filters_absolute_paths(self):
        """Filter out absolute paths."""
        test_output = """
FAILED lambda/tests/unit/test_real.py::test_func - Error
/tmp/tests-workspace/repo/lambda/tests/unit/test_other.py:10: Error
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_real.py" in result
        assert not any(f.startswith("/") for f in result)

    def test_filters_tmp_paths(self):
        """Filter out /tmp/ paths."""
        test_output = """
FAILED lambda/tests/unit/test_real.py::test_func - Error
/tmp/something/test_file.py:10: Error
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_real.py" in result
        assert not any("/tmp/" in f for f in result)

    def test_filters_paths_with_dotdot(self):
        """Filter out paths with .. in them."""
        test_output = """
FAILED lambda/tests/unit/test_real.py::test_func - Error
lambda/tests/unit/../../generate-code/models.py:10: Error
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_real.py" in result
        assert not any(".." in f for f in result)

    def test_extracts_backtick_paths(self):
        """Extract file paths from backticks."""
        test_output = """
The file `lambda/tests/unit/test_example.py` has errors.
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_example.py" in result

    def test_handles_empty_output(self):
        """Handle empty test output."""
        result = tests_fixer_module.extract_failed_test_files("")

        assert result == []

    def test_returns_unique_files(self):
        """Return unique file paths only."""
        test_output = """
FAILED lambda/tests/unit/test_foo.py::test_one - Error
FAILED lambda/tests/unit/test_foo.py::test_two - Error
lambda/tests/unit/test_foo.py:10: in test_one
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        # Should only have one entry for test_foo.py
        assert result.count("lambda/tests/unit/test_foo.py") == 1


class TestParseDelimitedResponse:
    """Tests for parse_delimited_response function."""

    def test_parses_single_file(self):
        """Parse response with single file."""
        response = """===FILE: lambda/tests/unit/test_example.py===
def test_something():
    assert True
===END==="""

        result = tests_fixer_module.parse_delimited_response(response)

        assert "lambda/tests/unit/test_example.py" in result
        assert "def test_something():" in result["lambda/tests/unit/test_example.py"]

    def test_parses_multiple_files(self):
        """Parse response with multiple files."""
        response = """===FILE: test_one.py===
def test_one():
    pass
===FILE: test_two.py===
def test_two():
    pass
===END==="""

        result = tests_fixer_module.parse_delimited_response(response)

        assert len(result) == 2
        assert "test_one.py" in result
        assert "test_two.py" in result

    def test_preserves_newlines(self):
        """Preserve newlines in file content."""
        response = """===FILE: test.py===
import pytest

def test_func():
    x = 1
    y = 2
    assert x + y == 3
===END==="""

        result = tests_fixer_module.parse_delimited_response(response)

        content = result["test.py"]
        assert content.count("\n") >= 5

    def test_returns_empty_dict_when_no_delimiters(self):
        """Return empty dict when no file delimiters found."""
        response = "Just some random text without delimiters"

        result = tests_fixer_module.parse_delimited_response(response)

        assert result == {}

    def test_handles_file_without_end_marker(self):
        """Handle file without ===END=== marker."""
        response = """===FILE: test.py===
def test_something():
    pass"""

        result = tests_fixer_module.parse_delimited_response(response)

        assert "test.py" in result
        assert "def test_something():" in result["test.py"]

    def test_strips_whitespace_from_content(self):
        """Strip leading/trailing whitespace from content."""
        response = """===FILE: test.py===

def test_func():
    pass

===END==="""

        result = tests_fixer_module.parse_delimited_response(response)

        content = result["test.py"]
        assert not content.startswith("\n")
        assert not content.endswith("\n\n")


class TestInvokeRunTests:
    """Tests for invoke_run_tests function."""

    @patch.object(tests_fixer_module, "lambda_client")
    def test_invokes_run_tests_successfully(self, mock_lambda):
        """Successfully invoke run-tests Lambda."""
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.invoke_run_tests(message)

        assert result["status"] == "triggered"
        mock_lambda.invoke.assert_called_once()

    @patch.object(tests_fixer_module, "lambda_client")
    def test_handles_invoke_error(self, mock_lambda):
        """Handle Lambda invocation error."""
        mock_lambda.invoke.side_effect = Exception("Lambda error")

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.invoke_run_tests(message)

        assert result["status"] == "error"
        assert "error" in result

    @patch.object(tests_fixer_module, "lambda_client")
    def test_handles_non_202_status(self, mock_lambda):
        """Handle non-202 status code."""
        mock_lambda.invoke.return_value = {"StatusCode": 500}

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.invoke_run_tests(message)

        assert result["status"] == "warning"


class TestQueryKbForTestPatterns:
    """Tests for query_kb_for_test_patterns function."""

    @patch.object(tests_fixer_module, "lambda_client")
    def test_queries_kb_successfully(self, mock_lambda):
        """Successfully query knowledge base."""
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(return_value=json.dumps({
                    "statusCode": 200,
                    "body": json.dumps({
                        "results": [
                            {"text": "Test pattern 1", "source": "adr", "score": 0.9},
                            {"text": "Test pattern 2", "source": "doc", "score": 0.8},
                        ]
                    })
                }).encode())
            )
        }

        result = tests_fixer_module.query_kb_for_test_patterns("test-repo")

        assert "Test pattern 1" in result
        assert "Test pattern 2" in result
        assert "Source: adr" in result

    @patch.object(tests_fixer_module, "lambda_client")
    def test_handles_kb_error(self, mock_lambda):
        """Handle KB query error gracefully."""
        mock_lambda.invoke.side_effect = Exception("KB error")

        result = tests_fixer_module.query_kb_for_test_patterns("test-repo")

        assert result == ""

    @patch.object(tests_fixer_module, "lambda_client")
    def test_handles_empty_results(self, mock_lambda):
        """Handle empty KB results."""
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(return_value=json.dumps({
                    "statusCode": 200,
                    "body": json.dumps({"results": []})
                }).encode())
            )
        }

        result = tests_fixer_module.query_kb_for_test_patterns("test-repo")

        assert result == ""


class TestGenerateTestFixes:
    """Tests for generate_test_fixes function."""

    @patch.object(tests_fixer_module, "invoke_model")
    def test_generates_fixes_successfully(self, mock_invoke):
        """Successfully generate test fixes."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": """===FILE: lambda/tests/unit/test_example.py===
def test_fixed():
    assert True
===END==="""
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        result = tests_fixer_module.generate_test_fixes(
            test_analysis="Test is failing",
            test_files={"lambda/tests/unit/test_example.py": "def test_broken(): pass"},
            kb_patterns="Use pytest"
        )

        assert result is not None
        assert "lambda/tests/unit/test_example.py" in result
        assert "def test_fixed():" in result["lambda/tests/unit/test_example.py"]

    @patch.object(tests_fixer_module, "invoke_model")
    def test_handles_empty_response(self, mock_invoke):
        """Handle empty Claude response."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": ""}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 0}
        })

        result = tests_fixer_module.generate_test_fixes(
            test_analysis="Test is failing",
            test_files={"test.py": "code"},
            kb_patterns=""
        )

        assert result is None

    @patch.object(tests_fixer_module, "invoke_model")
    def test_handles_bedrock_error(self, mock_invoke):
        """Handle Bedrock invocation error."""
        mock_invoke.side_effect = Exception("Bedrock error")

        result = tests_fixer_module.generate_test_fixes(
            test_analysis="Test is failing",
            test_files={"test.py": "code"},
            kb_patterns=""
        )

        assert result is None

    @patch.object(tests_fixer_module, "invoke_model")
    def test_includes_user_guidance(self, mock_invoke):
        """Include user guidance in prompt."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": """===FILE: test.py===
fixed
===END==="""
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        result = tests_fixer_module.generate_test_fixes(
            test_analysis="Analysis",
            test_files={"test.py": "code"},
            kb_patterns="",
            user_guidance="Use Decimal instead of float"
        )

        # Verify guidance was passed (check the call args)
        call_args = mock_invoke.call_args
        prompt = call_args.kwargs["messages"][0]["content"][0]["text"]
        assert "User Instructions" in prompt
        assert "Decimal" in prompt


class TestHandleFixTests:
    """Tests for handle_fix_tests function."""

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    def test_handles_no_comments(self, mock_post, mock_get_comments, mock_token):
        """Handle PR with no comments."""
        mock_token.return_value = "fake-token"
        mock_get_comments.return_value = []

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "no_comments"
        mock_post.assert_called_once()

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    def test_handles_no_analysis(self, mock_post, mock_get_comments, mock_token):
        """Handle missing test analysis."""
        mock_token.return_value = "fake-token"
        mock_get_comments.return_value = [{"body": "random comment"}]

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "no_analysis"

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    def test_handles_no_test_output(self, mock_post, mock_get_comments, mock_token):
        """Handle missing test failure output."""
        mock_token.return_value = "fake-token"
        mock_get_comments.return_value = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

Some analysis here.

---
"""
            }
        ]

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "no_test_output"

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    @patch.object(tests_fixer_module, "extract_failed_test_files")
    def test_handles_no_files_found(self, mock_extract, mock_post, mock_get_comments, mock_token):
        """Handle when no test files can be extracted."""
        mock_token.return_value = "fake-token"
        mock_get_comments.return_value = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

Some analysis here.

---
"""
            },
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

### Test Output (last 2000 chars)
```
Some output here
```
"""
            }
        ]
        mock_extract.return_value = []

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "no_files_found"

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    @patch.object(tests_fixer_module, "get_file")
    def test_handles_files_not_readable(self, mock_get_file, mock_post, mock_get_comments, mock_token):
        """Handle when test files cannot be read."""
        mock_token.return_value = "fake-token"
        mock_get_comments.return_value = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

FAILED lambda/tests/unit/test_foo.py::test_bar - Error

---
"""
            },
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

### Test Output (last 2000 chars)
```
FAILED lambda/tests/unit/test_foo.py::test_bar - Error
```
"""
            }
        ]
        mock_get_file.return_value = None

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "files_not_found"

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    @patch.object(tests_fixer_module, "get_file")
    @patch.object(tests_fixer_module, "query_kb_for_test_patterns")
    @patch.object(tests_fixer_module, "generate_test_fixes")
    def test_handles_generation_failed(
        self, mock_generate, mock_kb, mock_get_file, mock_post, mock_get_comments, mock_token
    ):
        """Handle when test fix generation fails."""
        mock_token.return_value = "fake-token"
        mock_get_comments.return_value = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

FAILED lambda/tests/unit/test_foo.py::test_bar - Error

---
"""
            },
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

### Test Output (last 2000 chars)
```
FAILED lambda/tests/unit/test_foo.py::test_bar - Error
```
"""
            }
        ]
        mock_get_file.return_value = {
            "content": base64.b64encode(b"def test_foo(): pass").decode()
        }
        mock_kb.return_value = ""
        mock_generate.return_value = None

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "generation_failed"

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    @patch.object(tests_fixer_module, "get_file")
    @patch.object(tests_fixer_module, "query_kb_for_test_patterns")
    @patch.object(tests_fixer_module, "generate_test_fixes")
    def test_handles_no_changes_needed(
        self, mock_generate, mock_kb, mock_get_file, mock_post, mock_get_comments, mock_token
    ):
        """Handle when generated fix is same as original."""
        mock_token.return_value = "fake-token"
        original_content = "def test_foo(): pass"
        mock_get_comments.return_value = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

FAILED lambda/tests/unit/test_foo.py::test_bar - Error

---
"""
            },
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

### Test Output (last 2000 chars)
```
FAILED lambda/tests/unit/test_foo.py::test_bar - Error
```
"""
            }
        ]
        mock_get_file.return_value = {
            "content": base64.b64encode(original_content.encode()).decode()
        }
        mock_kb.return_value = ""
        # Return same content as original
        mock_generate.return_value = {
            "lambda/tests/unit/test_foo.py": original_content
        }

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "no_changes_needed"

    @patch.object(tests_fixer_module, "get_github_token")
    @patch.object(tests_fixer_module, "get_pr_comments")
    @patch.object(tests_fixer_module, "post_pr_comment")
    @patch.object(tests_fixer_module, "get_file")
    @patch.object(tests_fixer_module, "query_kb_for_test_patterns")
    @patch.object(tests_fixer_module, "generate_test_fixes")
    @patch.object(tests_fixer_module, "update_file")
    @patch.object(tests_fixer_module, "invoke_run_tests")
    def test_handles_success(
        self, mock_run_tests, mock_update, mock_generate, mock_kb,
        mock_get_file, mock_post, mock_get_comments, mock_token
    ):
        """Handle successful test fix."""
        mock_token.return_value = "fake-token"
        mock_get_comments.return_value = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

FAILED lambda/tests/unit/test_foo.py::test_bar - Error

---
"""
            },
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

### Test Output (last 2000 chars)
```
FAILED lambda/tests/unit/test_foo.py::test_bar - Error
```
"""
            }
        ]
        mock_get_file.return_value = {
            "content": base64.b64encode(b"def test_foo(): pass").decode()
        }
        mock_kb.return_value = ""
        mock_generate.return_value = {
            "lambda/tests/unit/test_foo.py": "def test_foo(): assert True"
        }
        mock_run_tests.return_value = {"status": "triggered"}

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "success"
        assert "lambda/tests/unit/test_foo.py" in result["files_fixed"]
        mock_update.assert_called_once()
        mock_run_tests.assert_called_once()

    @patch.object(tests_fixer_module, "get_github_token")
    def test_handles_exception(self, mock_token):
        """Handle unexpected exception."""
        mock_token.side_effect = Exception("Token error")

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            base_branch="main"
        )

        result = tests_fixer_module.handle_fix_tests(message)

        assert result["status"] == "error"
        assert "Token error" in result["error"]


class TestQueryKbFiltersByScore:
    """Additional tests for KB query score filtering."""

    @patch.object(tests_fixer_module, "lambda_client")
    def test_filters_low_score_results(self, mock_lambda):
        """Filter out results with score below threshold."""
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "Payload": MagicMock(
                read=MagicMock(return_value=json.dumps({
                    "statusCode": 200,
                    "body": json.dumps({
                        "results": [
                            {"text": "High score", "source": "adr", "score": 0.9},
                            {"text": "Low score", "source": "doc", "score": 0.2},
                        ]
                    })
                }).encode())
            )
        }

        result = tests_fixer_module.query_kb_for_test_patterns("test-repo")

        assert "High score" in result
        assert "Low score" not in result

    @patch.object(tests_fixer_module, "lambda_client")
    def test_handles_function_error(self, mock_lambda):
        """Handle Lambda function error."""
        mock_lambda.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": MagicMock(
                read=MagicMock(return_value=json.dumps({
                    "errorMessage": "Some error"
                }).encode())
            )
        }

        result = tests_fixer_module.query_kb_for_test_patterns("test-repo")

        # Should return empty string on function error
        assert result == ""


class TestExtractFailedTestFilesPatterns:
    """Additional pattern tests for extract_failed_test_files."""

    def test_extracts_integration_tests(self):
        """Extract integration test paths."""
        test_output = """
FAILED lambda/tests/integration/test_api.py::test_endpoint - Error
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/integration/test_api.py" in result

    def test_extracts_integration_short_path(self):
        """Extract integration/ short paths."""
        test_output = """
FAILED integration/test_api.py::test_endpoint - Error
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/integration/test_api.py" in result

    def test_filters_non_test_files(self):
        """Filter out files without 'test' in name."""
        test_output = """
lambda/generate-code/handler.py:50: Error
lambda/tests/unit/test_example.py:42: AssertionError
"""

        result = tests_fixer_module.extract_failed_test_files(test_output)

        assert "lambda/tests/unit/test_example.py" in result
        assert not any("handler.py" in f for f in result)


class TestParseDelimitedResponseEdgeCases:
    """Edge case tests for parse_delimited_response."""

    def test_handles_empty_content(self):
        """Handle file with empty content."""
        response = """===FILE: test.py===
===END==="""

        result = tests_fixer_module.parse_delimited_response(response)

        assert "test.py" in result
        assert result["test.py"] == ""

    def test_handles_whitespace_in_path(self):
        """Handle whitespace in file path."""
        response = """===FILE:   test.py   ===
content
===END==="""

        result = tests_fixer_module.parse_delimited_response(response)

        assert "test.py" in result


class TestInvokeRunTestsEdgeCases:
    """Edge case tests for invoke_run_tests."""

    @patch.object(tests_fixer_module, "lambda_client")
    def test_includes_skip_plan_update(self, mock_lambda):
        """Verify skipPlanUpdate is set to True."""
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        message = tests_fixer_module.PRCommandMessage(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="user",
            base_branch="main"
        )

        tests_fixer_module.invoke_run_tests(message)

        call_args = mock_lambda.invoke.call_args
        payload = json.loads(call_args.kwargs["Payload"])
        assert payload["detail"]["skipPlanUpdate"] is True


class TestFetchTestAnalysisEdgeCases:
    """Edge case tests for fetch_test_analysis."""

    def test_extracts_full_analysis_without_next_steps(self):
        """Extract analysis that continues past typical markers."""
        comments = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

Line one.
Line two.
Line three.

---
**Footer**
"""
            }
        ]

        result = tests_fixer_module.fetch_test_analysis(comments)

        assert "Line one" in result
        assert "Line two" in result
        assert "Line three" in result

    def test_fallback_when_no_separator(self):
        """Use fallback when no --- separator."""
        comments = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

Analysis without separator.
"""
            }
        ]

        result = tests_fixer_module.fetch_test_analysis(comments)

        assert "Analysis without separator" in result


class TestGenerateTestFixesWithContext:
    """Tests for generate_test_fixes with various contexts."""

    @patch.object(tests_fixer_module, "invoke_model")
    def test_includes_kb_patterns_in_prompt(self, mock_invoke):
        """Include KB patterns in prompt."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": """===FILE: test.py===
fixed
===END==="""
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        tests_fixer_module.generate_test_fixes(
            test_analysis="Analysis",
            test_files={"test.py": "code"},
            kb_patterns="Use pytest fixtures for mocking"
        )

        call_args = mock_invoke.call_args
        prompt = call_args.kwargs["messages"][0]["content"][0]["text"]
        assert "Testing Standards" in prompt
        assert "pytest fixtures" in prompt

    @patch.object(tests_fixer_module, "invoke_model")
    def test_handles_multiple_files(self, mock_invoke):
        """Handle generating fixes for multiple files."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{
                        "text": """===FILE: test_one.py===
def test_one(): pass
===FILE: test_two.py===
def test_two(): pass
===END==="""
                    }]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        result = tests_fixer_module.generate_test_fixes(
            test_analysis="Analysis",
            test_files={"test_one.py": "code1", "test_two.py": "code2"},
            kb_patterns=""
        )

        assert len(result) == 2
        assert "test_one.py" in result
        assert "test_two.py" in result
