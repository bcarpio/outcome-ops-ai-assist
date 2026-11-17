# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0

# === Legacy dict -> unified Response adapter ===
# Tests written before mocked the boto3 Converse-shaped dict
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
Unit tests for readme_fixer module.

Tests cover:
- Handler name extraction from file paths
- Infrastructure name extraction from Terraform files
- File diff extraction from full PR diff
- KB query for README patterns
- README update generation with Claude
- Main handler orchestration
"""

import base64
import json
import os
import sys
import importlib.util
from unittest.mock import Mock, patch, MagicMock

import pytest
from botocore.exceptions import ClientError


# Load the readme_fixer module with isolated mocks
# We use unique module names to avoid polluting sys.modules for other tests
readme_fixer_path = os.path.join(
    os.path.dirname(__file__),
    '../../generate-code/readme_fixer.py'
)

# Create isolated mock modules with unique names for this test file
_mock_github_api = MagicMock()
_mock_models = MagicMock()

# Create PRCommandMessage mock class
class MockPRCommandMessage:
    def __init__(self, pr_number, repo_full_name, branch_name, commenter="testuser", base_branch="main"):
        self.pr_number = pr_number
        self.repo_full_name = repo_full_name
        self.branch_name = branch_name
        self.commenter = commenter
        self.base_branch = base_branch

_mock_models.PRCommandMessage = MockPRCommandMessage

# Store original modules if they exist
_original_github_api = sys.modules.get('github_api')
_original_models = sys.modules.get('models')

# Add generate-code to path for imports
generate_code_dir = os.path.join(os.path.dirname(__file__), '../../generate-code')
sys.path.insert(0, generate_code_dir)

# Temporarily inject mock modules
sys.modules['github_api'] = _mock_github_api
sys.modules['models'] = _mock_models

# Load the module with a unique name
spec = importlib.util.spec_from_file_location("readme_fixer_test_module", readme_fixer_path)
readme_fixer = importlib.util.module_from_spec(spec)
sys.modules['readme_fixer_test_module'] = readme_fixer
spec.loader.exec_module(readme_fixer)

# Restore original modules or remove mocks
if _original_github_api is not None:
    sys.modules['github_api'] = _original_github_api
else:
    del sys.modules['github_api']

if _original_models is not None:
    sys.modules['models'] = _original_models
else:
    del sys.modules['models']

# Clean up path
sys.path.remove(generate_code_dir)

# Import functions from loaded module
extract_handler_names = readme_fixer.extract_handler_names
extract_infrastructure_names = readme_fixer.extract_infrastructure_names
extract_file_diff_from_full_diff = readme_fixer.extract_file_diff_from_full_diff
query_kb_for_readme_patterns = readme_fixer.query_kb_for_readme_patterns
generate_readme_update = readme_fixer.generate_readme_update
handle_fix_readme = readme_fixer.handle_fix_readme


class TestExtractHandlerNames:
    """Test extracting handler names from file paths."""

    def test_extract_single_handler(self):
        """Test: Extract single handler name."""
        files = ["lambda/hello/handler.py"]
        result = extract_handler_names(files)
        assert result == ["hello"]

    def test_extract_multiple_handlers(self):
        """Test: Extract multiple handler names."""
        files = [
            "lambda/hello/handler.py",
            "lambda/world/handler.py",
            "lambda/process/handler.py"
        ]
        result = extract_handler_names(files)
        assert len(result) == 3
        assert "hello" in result
        assert "world" in result
        assert "process" in result

    def test_excludes_test_files(self):
        """Test: Exclude test handler files."""
        files = [
            "lambda/hello/handler.py",
            "lambda/tests/unit/test_hello.py"
        ]
        result = extract_handler_names(files)
        assert result == ["hello"]

    def test_excludes_non_handler_files(self):
        """Test: Exclude non-handler Python files."""
        files = [
            "lambda/hello/handler.py",
            "lambda/hello/utils.py",
            "README.md"
        ]
        result = extract_handler_names(files)
        assert result == ["hello"]

    def test_handles_empty_list(self):
        """Test: Handle empty file list."""
        result = extract_handler_names([])
        assert result == []

    def test_deduplicates_handlers(self):
        """Test: Deduplicate handlers when multiple files in same dir."""
        files = [
            "lambda/hello/handler.py",
            "lambda/hello/handler.py",  # Duplicate
        ]
        result = extract_handler_names(files)
        assert result == ["hello"]


class TestExtractInfrastructureNames:
    """Test extracting infrastructure names from Terraform files."""

    def test_extract_single_terraform_file(self):
        """Test: Extract single Terraform file name."""
        files = ["terraform/main.tf"]
        result = extract_infrastructure_names(files)
        assert result == ["main"]

    def test_extract_multiple_terraform_files(self):
        """Test: Extract multiple Terraform file names."""
        files = [
            "terraform/main.tf",
            "terraform/variables.tf",
            "terraform/lambda.tf"
        ]
        result = extract_infrastructure_names(files)
        assert len(result) == 3
        assert "main" in result
        assert "variables" in result
        assert "lambda" in result

    def test_excludes_non_terraform_files(self):
        """Test: Exclude non-Terraform files."""
        files = [
            "terraform/main.tf",
            "lambda/handler.py",
            "README.md"
        ]
        result = extract_infrastructure_names(files)
        assert result == ["main"]

    def test_handles_empty_list(self):
        """Test: Handle empty file list."""
        result = extract_infrastructure_names([])
        assert result == []


class TestExtractFileDiffFromFullDiff:
    """Test extracting specific file diff from full PR diff."""

    def test_extract_single_file_diff(self):
        """Test: Extract diff for a single file."""
        full_diff = """diff --git a/README.md b/README.md
index abc123..def456 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,5 @@
+New content
 Existing content
diff --git a/other.py b/other.py
+other changes"""

        result = extract_file_diff_from_full_diff(full_diff, "README.md")

        assert "README.md" in result
        assert "New content" in result
        assert "other changes" not in result

    def test_extract_handler_diff(self):
        """Test: Extract handler file diff."""
        full_diff = """diff --git a/lambda/hello/handler.py b/lambda/hello/handler.py
+def handler(event, context):
+    return "hello"
diff --git a/README.md b/README.md
+readme changes"""

        result = extract_file_diff_from_full_diff(full_diff, "lambda/hello/handler.py")

        assert "handler.py" in result
        assert "def handler" in result
        assert "readme changes" not in result

    def test_file_not_in_diff(self):
        """Test: File not in diff returns empty string."""
        full_diff = "diff --git a/other.py b/other.py\n+changes"
        result = extract_file_diff_from_full_diff(full_diff, "missing.py")
        assert result == ""

    def test_empty_diff(self):
        """Test: Empty diff returns empty string."""
        result = extract_file_diff_from_full_diff("", "README.md")
        assert result == ""


class TestQueryKbForReadmePatterns:
    """Test KB query for README patterns."""

    @patch.object(readme_fixer, 'lambda_client')
    def test_query_kb_success(self, mock_lambda):
        """Test: Successfully query KB for patterns."""
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "body": json.dumps({
                    "results": [
                        {"score": 0.8, "source": "ADR-001", "text": "README structure guide"}
                    ]
                })
            }).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        result = query_kb_for_readme_patterns("test-repo")

        assert "README structure guide" in result
        assert mock_lambda.invoke.call_count >= 1

    @patch.object(readme_fixer, 'lambda_client')
    def test_query_kb_filters_low_scores(self, mock_lambda):
        """Test: KB filters out low-score results."""
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "body": json.dumps({
                    "results": [
                        {"score": 0.1, "source": "ADR-001", "text": "Low relevance"},
                        {"score": 0.8, "source": "ADR-002", "text": "High relevance"}
                    ]
                })
            }).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        result = query_kb_for_readme_patterns("test-repo")

        assert "High relevance" in result
        assert "Low relevance" not in result

    @patch.object(readme_fixer, 'lambda_client')
    def test_query_kb_handles_error(self, mock_lambda):
        """Test: KB query handles errors gracefully."""
        mock_lambda.invoke.side_effect = Exception("Lambda error")

        result = query_kb_for_readme_patterns("test-repo")

        assert result == ""


class TestGenerateReadmeUpdate:
    """Test README update generation with Claude."""

    @patch.object(readme_fixer, 'invoke_model')
    def test_generate_readme_success(self, mock_invoke):
        """Test: Successfully generate README update."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": "# Updated README\n\n## Lambda Functions\n- hello: Handler function"}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 200}
        })

        result = generate_readme_update(
            current_readme="# README\n\nOld content",
            handler_names=["hello"],
            infra_names=[],
            handler_diffs={"hello": "+def handler(): pass"},
            kb_patterns="Use structured format"
        )

        assert result is not None
        assert "Updated README" in result
        mock_invoke.assert_called_once()

    @patch.object(readme_fixer, 'invoke_model')
    def test_generate_readme_strips_code_fences(self, mock_invoke):
        """Test: Strips markdown code fences from response."""
        # Content must be > 50 chars after stripping
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": "```markdown\n# README\n\nThis is a longer content section that will pass the length check.\n\n## Lambda Functions\n- hello: Handler function\n```"}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        result = generate_readme_update(
            current_readme="# Old",
            handler_names=["hello"],
            infra_names=[],
            handler_diffs={},
            kb_patterns=""
        )

        assert result is not None
        assert "```markdown" not in result
        assert not result.endswith("```")

    @patch.object(readme_fixer, 'invoke_model')
    def test_generate_readme_handles_empty_response(self, mock_invoke):
        """Test: Handle empty Claude response."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": ""}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 0}
        })

        result = generate_readme_update(
            current_readme="# README",
            handler_names=["hello"],
            infra_names=[],
            handler_diffs={},
            kb_patterns=""
        )

        assert result is None

    @patch.object(readme_fixer, 'invoke_model')
    def test_generate_readme_handles_error(self, mock_invoke):
        """Test: Handle Claude API error."""
        mock_invoke.side_effect = Exception("API error")

        result = generate_readme_update(
            current_readme="# README",
            handler_names=["hello"],
            infra_names=[],
            handler_diffs={},
            kb_patterns=""
        )

        assert result is None


class TestHandleFixReadme:
    """Test main handle_fix_readme handler."""

    @patch.object(readme_fixer, 'get_github_token', return_value="token")
    @patch.object(readme_fixer, 'get_pr_changed_files')
    @patch.object(readme_fixer, 'post_pr_comment')
    def test_handle_no_changed_files(self, mock_comment, mock_files, mock_token):
        """Test: No changed files in PR."""
        mock_files.return_value = []

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_readme(message)

        assert result["status"] == "no_changes"
        mock_comment.assert_called_once()

    @patch.object(readme_fixer, 'get_github_token', return_value="token")
    @patch.object(readme_fixer, 'get_pr_changed_files')
    @patch.object(readme_fixer, 'post_pr_comment')
    def test_handle_no_handlers_or_infra(self, mock_comment, mock_files, mock_token):
        """Test: No handlers or terraform changes."""
        mock_files.return_value = ["docs/guide.md", "scripts/test.sh"]

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_readme(message)

        assert result["status"] == "no_doc_needed"
        mock_comment.assert_called_once()

    @patch.object(readme_fixer, 'get_github_token', return_value="token")
    @patch.object(readme_fixer, 'get_pr_changed_files')
    @patch.object(readme_fixer, 'get_file')
    @patch.object(readme_fixer, 'post_pr_comment')
    def test_handle_no_readme_found(self, mock_comment, mock_get_file, mock_files, mock_token):
        """Test: No README.md in repository."""
        mock_files.return_value = ["lambda/hello/handler.py"]
        mock_get_file.return_value = None

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_readme(message)

        assert result["status"] == "no_readme"
        mock_comment.assert_called_once()

    @patch.object(readme_fixer, 'get_github_token', return_value="token")
    @patch.object(readme_fixer, 'get_pr_changed_files')
    @patch.object(readme_fixer, 'get_file')
    @patch.object(readme_fixer, 'get_pr_diff')
    @patch.object(readme_fixer, 'query_kb_for_readme_patterns')
    @patch.object(readme_fixer, 'generate_readme_update')
    @patch.object(readme_fixer, 'post_pr_comment')
    def test_handle_generation_failed(
        self, mock_comment, mock_generate, mock_kb, mock_diff, mock_get_file, mock_files, mock_token
    ):
        """Test: README generation failed."""
        mock_files.return_value = ["lambda/hello/handler.py"]
        mock_get_file.return_value = {"content": base64.b64encode(b"# README").decode()}
        mock_diff.return_value = "+new code"
        mock_kb.return_value = ""
        mock_generate.return_value = None

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_readme(message)

        assert result["status"] == "generation_failed"

    @patch.object(readme_fixer, 'get_github_token', return_value="token")
    @patch.object(readme_fixer, 'get_pr_changed_files')
    @patch.object(readme_fixer, 'get_file')
    @patch.object(readme_fixer, 'get_pr_diff')
    @patch.object(readme_fixer, 'query_kb_for_readme_patterns')
    @patch.object(readme_fixer, 'generate_readme_update')
    @patch.object(readme_fixer, 'post_pr_comment')
    def test_handle_no_update_needed(
        self, mock_comment, mock_generate, mock_kb, mock_diff, mock_get_file, mock_files, mock_token
    ):
        """Test: README already documents changes."""
        readme_content = "# README"
        mock_files.return_value = ["lambda/hello/handler.py"]
        mock_get_file.return_value = {"content": base64.b64encode(readme_content.encode()).decode()}
        mock_diff.return_value = "+new code"
        mock_kb.return_value = ""
        mock_generate.return_value = readme_content  # Same as current

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_readme(message)

        assert result["status"] == "no_update_needed"

    @patch.object(readme_fixer, 'get_github_token', return_value="token")
    @patch.object(readme_fixer, 'get_pr_changed_files')
    @patch.object(readme_fixer, 'get_file')
    @patch.object(readme_fixer, 'get_pr_diff')
    @patch.object(readme_fixer, 'query_kb_for_readme_patterns')
    @patch.object(readme_fixer, 'generate_readme_update')
    @patch.object(readme_fixer, 'update_file')
    @patch.object(readme_fixer, 'post_pr_comment')
    def test_handle_success(
        self, mock_comment, mock_update, mock_generate, mock_kb, mock_diff, mock_get_file, mock_files, mock_token
    ):
        """Test: Successfully update README."""
        mock_files.return_value = ["lambda/hello/handler.py"]
        mock_get_file.return_value = {"content": base64.b64encode(b"# Old README").decode()}
        mock_diff.return_value = "+def handler(): pass"
        mock_kb.return_value = "patterns"
        mock_generate.return_value = "# Updated README\n\n## Lambda\n- hello"

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_readme(message)

        assert result["status"] == "success"
        assert "hello" in result["handlers_documented"]
        mock_update.assert_called_once()
        mock_comment.assert_called()

    @patch.object(readme_fixer, 'get_github_token')
    @patch.object(readme_fixer, 'post_pr_comment')
    def test_handle_error_posts_comment(self, mock_comment, mock_token):
        """Test: Error handling posts comment."""
        mock_token.side_effect = [Exception("Token error"), "token"]  # First fails, second for error comment

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_readme(message)

        assert result["status"] == "error"
        assert "Token error" in result["error"]
