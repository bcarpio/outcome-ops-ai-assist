
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
Unit tests for adr_fixer module.

Tests cover:
- Categorizing architectural changes from file paths
- Getting existing ADRs from repository
- Determining next ADR number
- KB query for ADR context
- ADR analysis and generation with Claude
- Main handler orchestration
"""

import base64
import json
import os
import sys
import importlib.util
from unittest.mock import Mock, patch, MagicMock

import pytest


# Load the adr_fixer module with isolated mocks
adr_fixer_path = os.path.join(
    os.path.dirname(__file__),
    '../../generate-code/adr_fixer.py'
)

# Create isolated mock modules with unique names for this test file
_mock_github_api = MagicMock()
_mock_models = MagicMock()


# Create PRCommandMessage mock class
class MockPRCommandMessage:
    def __init__(
        self,
        pr_number,
        repo_full_name,
        branch_name,
        commenter="testuser",
        base_branch="main",
        guidance=None
    ):
        self.pr_number = pr_number
        self.repo_full_name = repo_full_name
        self.branch_name = branch_name
        self.commenter = commenter
        self.base_branch = base_branch
        self.guidance = guidance


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
spec = importlib.util.spec_from_file_location("adr_fixer_test_module", adr_fixer_path)
adr_fixer = importlib.util.module_from_spec(spec)
sys.modules['adr_fixer_test_module'] = adr_fixer
spec.loader.exec_module(adr_fixer)

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
categorize_architectural_changes = adr_fixer.categorize_architectural_changes
get_existing_adrs = adr_fixer.get_existing_adrs
get_next_adr_number = adr_fixer.get_next_adr_number
query_kb_for_adr_context = adr_fixer.query_kb_for_adr_context
analyze_and_generate_adr = adr_fixer.analyze_and_generate_adr
handle_fix_adr = adr_fixer.handle_fix_adr


class TestCategorizeArchitecturalChanges:
    """Test categorizing changed files by architectural type."""

    def test_categorize_new_lambdas(self):
        """Test: Detect new Lambda handlers."""
        files = ["lambda/hello/handler.py"]
        diff = "+++ b/lambda/hello/handler.py\n--- /dev/null\n+new content"

        result = categorize_architectural_changes(files, diff)

        assert "hello" in result["new_lambdas"]

    def test_categorize_modified_lambdas(self):
        """Test: Detect modified Lambda handlers."""
        files = ["lambda/hello/handler.py"]
        diff = "+++ b/lambda/hello/handler.py\n--- a/lambda/hello/handler.py\n+modified"

        result = categorize_architectural_changes(files, diff)

        assert "hello" in result["modified_lambdas"]

    def test_categorize_new_terraform(self):
        """Test: Detect new Terraform files."""
        files = ["terraform/lambda.tf"]
        diff = "+++ b/terraform/lambda.tf\n--- /dev/null\n+resource"

        result = categorize_architectural_changes(files, diff)

        assert "terraform/lambda.tf" in result["new_terraform"]

    def test_categorize_modified_terraform(self):
        """Test: Detect modified Terraform files."""
        files = ["terraform/lambda.tf"]
        diff = "+++ b/terraform/lambda.tf\n--- a/terraform/lambda.tf\n+resource"

        result = categorize_architectural_changes(files, diff)

        assert "terraform/lambda.tf" in result["modified_terraform"]

    def test_categorize_excludes_test_files(self):
        """Test: Exclude test files from Lambda categorization."""
        files = [
            "lambda/hello/handler.py",
            "lambda/tests/unit/test_hello.py"
        ]
        diff = "+++ b/lambda/hello/handler.py\n--- /dev/null"

        result = categorize_architectural_changes(files, diff)

        assert "hello" in result["new_lambdas"]
        assert not any("test" in item for item in result["new_modules"])

    def test_categorize_python_modules(self):
        """Test: Detect non-handler Python modules."""
        files = ["lambda/shared/utils.py"]
        diff = "+++ b/lambda/shared/utils.py\n--- /dev/null"

        result = categorize_architectural_changes(files, diff)

        assert "lambda/shared/utils.py" in result["new_modules"]

    def test_categorize_dependency_changes(self):
        """Test: Detect dependency file changes."""
        files = ["requirements.txt", "pyproject.toml"]
        diff = "changes"

        result = categorize_architectural_changes(files, diff)

        assert "requirements.txt" in result["dependency_changes"]
        assert "pyproject.toml" in result["dependency_changes"]

    def test_categorize_config_changes(self):
        """Test: Detect config file changes."""
        files = ["config/settings.yaml", "terraform/dev.tfvars"]
        diff = "changes"

        result = categorize_architectural_changes(files, diff)

        # yaml and tfvars (not .tf) but tfvars doesn't match .yaml/.yml/.json/.toml
        # Only config/settings.yaml should match
        assert "config/settings.yaml" in result["config_changes"]

    def test_categorize_empty_list(self):
        """Test: Handle empty file list."""
        result = categorize_architectural_changes([], "")

        for key in result:
            assert result[key] == []


class TestGetNextAdrNumber:
    """Test determining next ADR number."""

    def test_first_adr(self):
        """Test: First ADR when none exist."""
        result = get_next_adr_number([])
        assert result == "001"

    def test_sequential_numbering(self):
        """Test: Sequential numbering after existing ADRs."""
        existing = [
            {"number": "001", "name": "ADR-001-first.md"},
            {"number": "002", "name": "ADR-002-second.md"},
            {"number": "003", "name": "ADR-003-third.md"}
        ]

        result = get_next_adr_number(existing)

        assert result == "004"

    def test_gap_in_numbering(self):
        """Test: Uses next after highest, not gap."""
        existing = [
            {"number": "001", "name": "ADR-001-first.md"},
            {"number": "005", "name": "ADR-005-fifth.md"}
        ]

        result = get_next_adr_number(existing)

        assert result == "006"

    def test_handles_missing_numbers(self):
        """Test: Handle ADRs without numbers."""
        existing = [
            {"number": None, "name": "TEMPLATE.md"},
            {"number": "003", "name": "ADR-003-third.md"}
        ]

        result = get_next_adr_number(existing)

        assert result == "004"


class TestQueryKbForAdrContext:
    """Test KB query for ADR context."""

    @patch.object(adr_fixer, 'lambda_client')
    def test_query_kb_success(self, mock_lambda):
        """Test: Successfully query KB for ADR patterns."""
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "body": json.dumps({
                    "results": [
                        {"score": 0.8, "source": "ADR-001", "text": "ADR template guide"}
                    ]
                })
            }).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        result = query_kb_for_adr_context("test-repo")

        assert "ADR template guide" in result
        assert mock_lambda.invoke.call_count >= 1

    @patch.object(adr_fixer, 'lambda_client')
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

        result = query_kb_for_adr_context("test-repo")

        assert "High relevance" in result
        assert "Low relevance" not in result

    @patch.object(adr_fixer, 'lambda_client')
    def test_query_kb_handles_error(self, mock_lambda):
        """Test: KB query handles errors gracefully."""
        mock_lambda.invoke.side_effect = Exception("Lambda error")

        result = query_kb_for_adr_context("test-repo")

        assert result == ""


class TestAnalyzeAndGenerateAdr:
    """Test ADR analysis and generation with Claude."""

    def _make_full_categories(self, **overrides):
        """Helper to create a complete categories dict with defaults."""
        defaults = {
            "new_lambdas": [],
            "modified_lambdas": [],
            "new_terraform": [],
            "modified_terraform": [],
            "new_modules": [],
            "api_changes": [],
            "dependency_changes": [],
            "config_changes": [],
            "other": []
        }
        defaults.update(overrides)
        return defaults

    @patch.object(adr_fixer, 'invoke_model')
    def test_generate_adr_success(self, mock_invoke):
        """Test: Successfully generate ADR."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": json.dumps({
                        "create_adr": True,
                        "filename": "ADR-014-new-lambda-pattern.md",
                        "content": "# ADR-014: New Lambda Pattern\n\n## Status: Accepted\n\n## Context\nNew pattern..."
                    })}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 200}
        })

        categories = self._make_full_categories(new_lambdas=["process-events"])
        result = analyze_and_generate_adr(
            categories=categories,
            diff="+def handler(): pass",
            existing_adrs=[{"number": "013", "name": "ADR-013-test.md"}],
            next_adr_number="014",
            kb_context="ADR patterns",
            guidance=None
        )

        assert result is not None
        assert result["filename"] == "ADR-014-new-lambda-pattern.md"
        assert "ADR-014" in result["content"]

    @patch.object(adr_fixer, 'invoke_model')
    def test_generate_adr_not_needed(self, mock_invoke):
        """Test: Claude determines no ADR needed."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": json.dumps({
                        "create_adr": False,
                        "reason": "Minor bug fix, no architectural impact"
                    })}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        })

        categories = self._make_full_categories(modified_lambdas=["hello"])
        result = analyze_and_generate_adr(
            categories=categories,
            diff="+fix typo",
            existing_adrs=[],
            next_adr_number="001",
            kb_context="",
            guidance=None
        )

        assert result is None

    @patch.object(adr_fixer, 'invoke_model')
    def test_generate_adr_no_changes(self, mock_invoke):
        """Test: No architectural changes returns None without calling Claude."""
        categories = {
            "new_lambdas": [],
            "modified_lambdas": [],
            "new_terraform": [],
            "modified_terraform": [],
            "new_modules": [],
            "api_changes": [],
            "dependency_changes": [],
            "config_changes": [],
            "other": ["README.md"]
        }

        result = analyze_and_generate_adr(
            categories=categories,
            diff="+doc update",
            existing_adrs=[],
            next_adr_number="001",
            kb_context="",
            guidance=None
        )

        assert result is None
        mock_invoke.assert_not_called()

    @patch.object(adr_fixer, 'invoke_model')
    def test_generate_adr_handles_error(self, mock_invoke):
        """Test: Handle Claude API error."""
        mock_invoke.side_effect = Exception("API error")

        categories = self._make_full_categories(new_lambdas=["test"])
        result = analyze_and_generate_adr(
            categories=categories,
            diff="+new handler",
            existing_adrs=[],
            next_adr_number="001",
            kb_context="",
            guidance=None
        )

        assert result is None


class TestHandleFixAdr:
    """Test main handle_fix_adr handler."""

    @patch.object(adr_fixer, 'get_github_token', return_value="token")
    @patch.object(adr_fixer, 'get_pr_changed_files')
    @patch.object(adr_fixer, 'post_pr_comment')
    def test_handle_no_changed_files(self, mock_comment, mock_files, mock_token):
        """Test: No changed files in PR."""
        mock_files.return_value = []

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_adr(message)

        assert result["status"] == "no_changes"
        mock_comment.assert_called_once()

    @patch.object(adr_fixer, 'get_github_token', return_value="token")
    @patch.object(adr_fixer, 'get_pr_changed_files')
    @patch.object(adr_fixer, 'get_pr_diff')
    @patch.object(adr_fixer, 'get_existing_adrs')
    @patch.object(adr_fixer, 'query_kb_for_adr_context')
    @patch.object(adr_fixer, 'analyze_and_generate_adr')
    @patch.object(adr_fixer, 'post_pr_comment')
    def test_handle_no_adr_needed(
        self, mock_comment, mock_generate, mock_kb, mock_adrs, mock_diff, mock_files, mock_token
    ):
        """Test: Claude determines no ADR needed."""
        mock_files.return_value = ["lambda/hello/handler.py"]
        mock_diff.return_value = "+fix typo"
        mock_adrs.return_value = []
        mock_kb.return_value = ""
        mock_generate.return_value = None

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_adr(message)

        assert result["status"] == "no_adr_needed"
        mock_comment.assert_called_once()

    @patch.object(adr_fixer, 'get_github_token', return_value="token")
    @patch.object(adr_fixer, 'get_pr_changed_files')
    @patch.object(adr_fixer, 'get_pr_diff')
    @patch.object(adr_fixer, 'get_existing_adrs')
    @patch.object(adr_fixer, 'query_kb_for_adr_context')
    @patch.object(adr_fixer, 'analyze_and_generate_adr')
    @patch.object(adr_fixer, 'post_pr_comment')
    def test_handle_generation_failed(
        self, mock_comment, mock_generate, mock_kb, mock_adrs, mock_diff, mock_files, mock_token
    ):
        """Test: ADR generation failed (content too short)."""
        mock_files.return_value = ["lambda/hello/handler.py"]
        mock_diff.return_value = "+new handler"
        mock_adrs.return_value = []
        mock_kb.return_value = ""
        mock_generate.return_value = {"filename": "ADR-001.md", "content": "Short"}

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_adr(message)

        assert result["status"] == "generation_failed"

    @patch.object(adr_fixer, 'get_github_token', return_value="token")
    @patch.object(adr_fixer, 'get_pr_changed_files')
    @patch.object(adr_fixer, 'get_pr_diff')
    @patch.object(adr_fixer, 'get_existing_adrs')
    @patch.object(adr_fixer, 'query_kb_for_adr_context')
    @patch.object(adr_fixer, 'analyze_and_generate_adr')
    @patch.object(adr_fixer, 'get_file')
    @patch.object(adr_fixer, 'commit_file')
    @patch.object(adr_fixer, 'post_pr_comment')
    def test_handle_success_new_adr(
        self, mock_comment, mock_commit, mock_get_file, mock_generate,
        mock_kb, mock_adrs, mock_diff, mock_files, mock_token
    ):
        """Test: Successfully create new ADR."""
        mock_files.return_value = ["lambda/process-events/handler.py"]
        mock_diff.return_value = "+++ b/lambda/process-events/handler.py\n--- /dev/null\n+def handler(): pass"
        mock_adrs.return_value = [{"number": "013", "name": "ADR-013-test.md"}]
        mock_kb.return_value = "ADR patterns"
        mock_generate.return_value = {
            "filename": "ADR-014-process-events-handler.md",
            "content": "# ADR-014: Process Events Handler\n\n## Status: Accepted\n\n" + "x" * 100
        }
        mock_get_file.return_value = None  # File doesn't exist yet

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_adr(message)

        assert result["status"] == "success"
        assert "docs/adr/ADR-014" in result["adr_path"]
        mock_commit.assert_called_once()
        assert mock_comment.call_count >= 1

    @patch.object(adr_fixer, 'get_github_token', return_value="token")
    @patch.object(adr_fixer, 'get_pr_changed_files')
    @patch.object(adr_fixer, 'get_pr_diff')
    @patch.object(adr_fixer, 'get_existing_adrs')
    @patch.object(adr_fixer, 'query_kb_for_adr_context')
    @patch.object(adr_fixer, 'analyze_and_generate_adr')
    @patch.object(adr_fixer, 'get_file')
    @patch.object(adr_fixer, 'update_file')
    @patch.object(adr_fixer, 'post_pr_comment')
    def test_handle_success_update_existing_adr(
        self, mock_comment, mock_update, mock_get_file, mock_generate,
        mock_kb, mock_adrs, mock_diff, mock_files, mock_token
    ):
        """Test: Successfully update existing ADR file."""
        mock_files.return_value = ["lambda/process-events/handler.py"]
        mock_diff.return_value = "+new code"
        mock_adrs.return_value = []
        mock_kb.return_value = ""
        mock_generate.return_value = {
            "filename": "ADR-001-test.md",
            "content": "# ADR-001: Test\n\n## Status: Accepted\n\n" + "x" * 100
        }
        mock_get_file.return_value = {"content": "existing", "sha": "abc123"}  # File exists

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_adr(message)

        assert result["status"] == "success"
        mock_update.assert_called_once()

    @patch.object(adr_fixer, 'get_github_token')
    @patch.object(adr_fixer, 'post_pr_comment')
    def test_handle_error_posts_comment(self, mock_comment, mock_token):
        """Test: Error handling posts comment."""
        mock_token.side_effect = [Exception("Token error"), "token"]

        message = MockPRCommandMessage(123, "owner/repo", "feature-branch")
        result = handle_fix_adr(message)

        assert result["status"] == "error"
        assert "Token error" in result["error"]


class TestGetExistingAdrs:
    """Test fetching existing ADRs from repository."""

    @patch('requests.get')
    def test_get_existing_adrs_success(self, mock_get):
        """Test: Successfully fetch existing ADRs."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"type": "file", "name": "ADR-001-first.md", "path": "docs/adr/ADR-001-first.md"},
            {"type": "file", "name": "ADR-002-second.md", "path": "docs/adr/ADR-002-second.md"},
            {"type": "file", "name": "TEMPLATE.md", "path": "docs/adr/TEMPLATE.md"}
        ]
        mock_get.return_value = mock_response

        result = get_existing_adrs("owner/repo", "main", "token")

        assert len(result) == 3
        assert result[0]["number"] == "001"
        assert result[1]["number"] == "002"
        assert result[2]["number"] is None  # TEMPLATE.md

    @patch('requests.get')
    def test_get_existing_adrs_no_directory(self, mock_get):
        """Test: Handle when docs/adr directory doesn't exist."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = get_existing_adrs("owner/repo", "main", "token")

        assert result == []

    @patch('requests.get')
    def test_get_existing_adrs_api_error(self, mock_get):
        """Test: Handle API error gracefully."""
        mock_get.side_effect = Exception("API error")

        result = get_existing_adrs("owner/repo", "main", "token")

        assert result == []
