# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests to increase coverage for generate-code/step_executor.py.

Covers:
- parse_json_fallback: various JSON formats, errors, edge cases
- parse_delimited_files: delimiter parsing, fallbacks, multiple files
- sanitize_for_logging: token redaction for strings, lists, None
- validate_requirements_file: non-requirements files, pip validation
- validate_files_with_backend: valid/invalid files
- _extract_error_context: line number extraction and context display
- build_syntax_fix_prompt: fix prompt building
- build_code_generation_prompt: prompt building with various standards
- format_terraform_files: terraform formatting flow
- execute_step: step not found error
- handle_step_message: plan file not found error
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest

# Add generate-code directory to path
GENERATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../generate-code")
)
if GENERATE_CODE_DIR not in sys.path:
    sys.path.insert(0, GENERATE_CODE_DIR)

LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, LAMBDA_ROOT)

# Load step_executor module
from models import GeneratedFile, PlanStep, ExecutionPlan, StepExecutionMessage
import step_executor


# ############################################################################
# parse_json_fallback TESTS
# ############################################################################


class TestParseJsonFallback:
    """Test JSON fallback parsing for Claude responses."""

    def test_parse_json_dict_with_files_key(self):
        """Test parsing JSON dict with 'files' key."""
        json_text = json.dumps({
            "files": [
                {"path": "handler.py", "content": "print('hello')"},
                {"path": "utils.py", "content": "def util(): pass"},
            ]
        })
        result = step_executor.parse_json_fallback(json_text)
        assert len(result) == 2
        assert result[0].path == "handler.py"
        assert result[1].path == "utils.py"

    def test_parse_json_list_format(self):
        """Test parsing JSON list format."""
        json_text = json.dumps([
            {"path": "main.py", "content": "import os"},
        ])
        result = step_executor.parse_json_fallback(json_text)
        assert len(result) == 1
        assert result[0].path == "main.py"

    def test_parse_json_with_code_fences(self):
        """Test parsing JSON wrapped in markdown code fences."""
        json_text = '```json\n{"files": [{"path": "a.py", "content": "x=1"}]}\n```'
        result = step_executor.parse_json_fallback(json_text)
        assert len(result) == 1
        assert result[0].path == "a.py"

    def test_parse_json_with_code_fences_no_lang(self):
        """Test parsing JSON wrapped in code fences without language."""
        json_text = '```\n{"files": [{"path": "b.py", "content": "y=2"}]}\n```'
        result = step_executor.parse_json_fallback(json_text)
        assert len(result) == 1

    def test_parse_json_invalid_json(self):
        """Test ValueError on invalid JSON."""
        with pytest.raises(ValueError, match="JSON fallback parsing failed"):
            step_executor.parse_json_fallback("not valid json{{{")

    def test_parse_json_unexpected_structure(self):
        """Test ValueError on unexpected JSON structure (e.g., plain string)."""
        with pytest.raises(ValueError, match="Unexpected JSON structure"):
            step_executor.parse_json_fallback('"just a string"')

    def test_parse_json_empty_files_list(self):
        """Test ValueError when files list is empty."""
        json_text = json.dumps({"files": []})
        with pytest.raises(ValueError, match="No files extracted"):
            step_executor.parse_json_fallback(json_text)

    def test_parse_json_skips_non_dict_items(self):
        """Test that non-dict items in list are skipped."""
        json_text = json.dumps([
            "not a dict",
            {"path": "ok.py", "content": "ok"},
            42,
        ])
        result = step_executor.parse_json_fallback(json_text)
        assert len(result) == 1
        assert result[0].path == "ok.py"

    def test_parse_json_skips_items_without_path_or_content(self):
        """Test that items missing path or content are skipped."""
        json_text = json.dumps({"files": [
            {"path": "", "content": "has content"},
            {"path": "has_path.py", "content": ""},
            {"path": "good.py", "content": "good"},
        ]})
        result = step_executor.parse_json_fallback(json_text)
        assert len(result) == 1
        assert result[0].path == "good.py"


# ############################################################################
# parse_delimited_files TESTS
# ############################################################################


class TestParseDelimitedFiles:
    """Test delimiter-based file parsing."""

    def test_single_file_with_end_marker(self):
        """Test parsing single file with ===END=== marker."""
        text = "===FILE: handler.py===\nprint('hello')\n===END==="
        result = step_executor.parse_delimited_files(text)
        assert len(result) == 1
        assert result[0].path == "handler.py"
        assert "print('hello')" in result[0].content

    def test_multiple_files(self):
        """Test parsing multiple files."""
        text = (
            "===FILE: a.py===\ncode_a\n"
            "===FILE: b.py===\ncode_b\n"
            "===END==="
        )
        result = step_executor.parse_delimited_files(text)
        assert len(result) == 2
        assert result[0].path == "a.py"
        assert result[1].path == "b.py"

    def test_no_end_marker(self):
        """Test parsing without ===END=== marker."""
        text = "===FILE: main.py===\nimport os\n"
        result = step_executor.parse_delimited_files(text)
        assert len(result) == 1
        assert result[0].path == "main.py"

    def test_no_delimiters_raises_value_error(self):
        """Test ValueError when no delimiters found and no JSON."""
        with pytest.raises(ValueError, match="No file delimiters"):
            step_executor.parse_delimited_files("just some random text")

    def test_no_delimiters_json_fallback_success(self):
        """Test JSON fallback when no delimiters found but content is JSON."""
        json_text = json.dumps({"files": [{"path": "f.py", "content": "x=1"}]})
        result = step_executor.parse_delimited_files(json_text)
        assert len(result) == 1
        assert result[0].path == "f.py"

    def test_no_delimiters_json_fallback_failure(self):
        """Test ValueError when no delimiters and JSON fallback also fails."""
        with pytest.raises(ValueError, match="No file delimiters"):
            step_executor.parse_delimited_files("{invalid json content")

    def test_file_content_strips_trailing_end_marker(self):
        """Test that trailing ===END=== inside content is stripped."""
        text = "===FILE: only.py===\ncode here\n===END==="
        result = step_executor.parse_delimited_files(text)
        assert "===END===" not in result[0].content

    def test_file_path_whitespace_stripped(self):
        """Test that file path whitespace is stripped."""
        text = "===FILE:   path/to/file.py   ===\ncontent\n===END==="
        result = step_executor.parse_delimited_files(text)
        assert result[0].path == "path/to/file.py"

    def test_json_fallback_with_code_fences_prefix(self):
        """Test JSON fallback triggers when response starts with ```."""
        json_text = '```json\n{"files": [{"path": "z.py", "content": "z"}]}\n```'
        result = step_executor.parse_delimited_files(json_text)
        assert len(result) == 1

    def test_json_fallback_with_bracket_prefix(self):
        """Test JSON fallback triggers when response starts with [."""
        json_text = json.dumps([{"path": "arr.py", "content": "arr"}])
        result = step_executor.parse_delimited_files(json_text)
        assert len(result) == 1


# ############################################################################
# sanitize_for_logging TESTS
# ############################################################################


class TestSanitizeForLogging:
    """Test token redaction in log messages."""

    def test_sanitize_none(self):
        """Test None input returns empty string."""
        assert step_executor.sanitize_for_logging(None) == ""

    def test_sanitize_list(self):
        """Test list input is joined and sanitized."""
        result = step_executor.sanitize_for_logging(["git", "clone", "repo"])
        assert "git clone repo" == result

    def test_sanitize_redacts_pat_token(self):
        """Test GitHub PAT token is redacted."""
        text = "https://ghp_abcdefghijklmnopqrstuvwxyz1234567890@github.com/repo.git"
        result = step_executor.sanitize_for_logging(text)
        assert "ghp_" not in result
        assert "[REDACTED]" in result

    def test_sanitize_redacts_fine_grained_token(self):
        """Test GitHub fine-grained token is redacted."""
        text = "token: github_pat_abcdefghijklmnopqrstuvw"
        result = step_executor.sanitize_for_logging(text)
        assert "github_pat_" not in result
        assert "[REDACTED]" in result

    def test_sanitize_redacts_access_token_in_url(self):
        """Test x-access-token in URL is redacted."""
        text = "https://x-access-token:ghs_abc123def456@github.com/repo"
        result = step_executor.sanitize_for_logging(text)
        assert "x-access-token" not in result
        assert "[REDACTED]" in result

    def test_sanitize_no_tokens(self):
        """Test text without tokens is unchanged."""
        text = "normal log message without tokens"
        assert step_executor.sanitize_for_logging(text) == text


# ############################################################################
# validate_requirements_file TESTS
# ############################################################################


class TestValidateRequirementsFile:
    """Test requirements.txt validation."""

    def test_non_requirements_file_returns_none(self):
        """Test that non-requirements files are skipped."""
        result = step_executor.validate_requirements_file("handler.py", "import os")
        assert result is None

    @patch("step_executor.subprocess.run")
    def test_valid_requirements_returns_none(self, mock_run):
        """Test valid requirements passes validation."""
        mock_run.return_value = Mock(returncode=0)
        result = step_executor.validate_requirements_file(
            "requirements.txt", "boto3==1.26.0\n"
        )
        assert result is None

    @patch("step_executor.subprocess.run")
    def test_invalid_package_returns_error(self, mock_run):
        """Test invalid package returns error message."""
        mock_run.return_value = Mock(
            returncode=1,
            stderr="ERROR: Could not find a version that satisfies the requirement nonexistent-pkg\n"
        )
        result = step_executor.validate_requirements_file(
            "requirements.txt", "nonexistent-pkg==999.0.0\n"
        )
        assert result is not None
        assert "Could not find a version" in result

    @patch("step_executor.subprocess.run")
    def test_only_cache_warnings_treated_as_success(self, mock_run):
        """Test that only cache warnings are treated as success."""
        mock_run.return_value = Mock(
            returncode=1,
            stderr="WARNING: The directory '/home/user/.cache/pip' or its parent directory is not owned or is not writable\n"
        )
        result = step_executor.validate_requirements_file(
            "requirements.txt", "boto3==1.26.0\n"
        )
        assert result is None

    @patch("step_executor.subprocess.run")
    def test_other_errors_reported(self, mock_run):
        """Test non-package errors are reported."""
        mock_run.return_value = Mock(
            returncode=1,
            stderr="Some other error occurred\n"
        )
        result = step_executor.validate_requirements_file(
            "requirements.txt", "boto3==1.26.0\n"
        )
        assert result is not None
        assert "validation failed" in result

    @patch("step_executor.subprocess.run")
    def test_timeout_returns_error(self, mock_run):
        """Test timeout returns error message."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pip", timeout=30)
        result = step_executor.validate_requirements_file(
            "requirements.txt", "boto3==1.26.0\n"
        )
        assert result is not None
        assert "timed out" in result

    @patch("step_executor.subprocess.run")
    def test_generic_exception_returns_error(self, mock_run):
        """Test generic exception returns error message."""
        mock_run.side_effect = RuntimeError("unexpected error")
        result = step_executor.validate_requirements_file(
            "requirements.txt", "boto3==1.26.0\n"
        )
        assert result is not None
        assert "validation error" in result


# ############################################################################
# validate_files_with_backend TESTS
# ############################################################################


class TestValidateFilesWithBackend:
    """Test file validation using backend."""

    def test_all_valid_returns_empty_list(self):
        """Test no errors when all files are valid."""
        mock_backend = Mock()
        mock_backend.name = "test"
        mock_result = Mock(valid=True, error_message=None, file_path="a.py")
        mock_backend.validate_files.return_value = [mock_result]

        errors = step_executor.validate_files_with_backend(
            [{"path": "a.py", "content": "pass"}], mock_backend
        )
        assert errors == []

    def test_invalid_file_returns_errors(self):
        """Test error messages returned for invalid files."""
        mock_backend = Mock()
        mock_backend.name = "test"
        mock_result = Mock(valid=False, error_message="syntax error", file_path="bad.py")
        mock_backend.validate_files.return_value = [mock_result]

        errors = step_executor.validate_files_with_backend(
            [{"path": "bad.py", "content": "def ("}], mock_backend
        )
        assert len(errors) == 1
        assert "bad.py: syntax error" in errors[0]


# ############################################################################
# _extract_error_context TESTS
# ############################################################################


class TestExtractErrorContext:
    """Test error context extraction from code."""

    def test_no_line_number_returns_empty(self):
        """Test empty string when no line number in error."""
        result = step_executor._extract_error_context("some code", "no line info")
        assert result == ""

    def test_extracts_context_around_error_line(self):
        """Test context extraction around error line."""
        content = "\n".join([f"line {i}" for i in range(1, 11)])
        result = step_executor._extract_error_context(content, "error at line 5")
        assert ">>> " in result
        assert "line 5" in result
        assert "Error context" in result

    def test_invalid_line_number_returns_empty(self):
        """Test empty string for out-of-range line number."""
        content = "line 1\nline 2"
        result = step_executor._extract_error_context(content, "error at line 0")
        assert result == ""

    def test_line_number_beyond_content(self):
        """Test empty string for line number beyond content."""
        content = "line 1\nline 2"
        result = step_executor._extract_error_context(content, "error at line 100")
        assert result == ""


# ############################################################################
# build_syntax_fix_prompt TESTS
# ############################################################################


class TestBuildSyntaxFixPrompt:
    """Test syntax fix prompt building."""

    def test_basic_fix_prompt(self):
        """Test basic fix prompt generation."""
        result = step_executor.build_syntax_fix_prompt(
            validation_error="SyntaxError: unexpected EOF",
            previous_code="## handler.py\n```python\ndef hello(\n```"
        )
        assert "SyntaxError: unexpected EOF" in result
        assert "Fix the syntax error" in result
        assert "handler.py" in result

    def test_fix_prompt_with_error_context(self):
        """Test fix prompt includes error context when line number present."""
        code_lines = "\n".join([f"    line {i}" for i in range(1, 11)])
        previous_code = f"## test.py\n```python\n{code_lines}\n```"
        result = step_executor.build_syntax_fix_prompt(
            validation_error="SyntaxError at line 5",
            previous_code=previous_code
        )
        assert "Error context" in result

    def test_fix_prompt_no_code_blocks(self):
        """Test fix prompt handles text without code blocks."""
        result = step_executor.build_syntax_fix_prompt(
            validation_error="some error",
            previous_code="plain text without code blocks"
        )
        assert "Fix the syntax error" in result


# ############################################################################
# build_code_generation_prompt TESTS
# ############################################################################


class TestBuildCodeGenerationPrompt:
    """Test code generation prompt building."""

    def _make_step(self):
        return PlanStep(
            step_number=1,
            title="Add handler",
            description="Create a new Lambda handler",
            files_to_create=["handler.py", "utils.py"]
        )

    def test_basic_prompt(self):
        """Test basic prompt generation."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="Add a new feature",
            current_step=1,
            total_steps=3,
            kb_results=["Use boto3 for AWS calls"],
        )
        assert "step 1 of 3" in result
        assert "Add a new feature" in result
        assert "Add handler" in result
        assert "handler.py" in result

    def test_prompt_with_lambda_standards(self):
        """Test prompt includes lambda standards."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=1,
            total_steps=1,
            kb_results=[],
            lambda_standards=["Use structured logging"],
        )
        assert "Lambda Handler Standards" in result
        assert "Use structured logging" in result

    def test_prompt_with_terraform_standards(self):
        """Test prompt includes terraform standards."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=1,
            total_steps=1,
            kb_results=[],
            terraform_standards=["Use module version 8.1.2"],
        )
        assert "Terraform Standards" in result

    def test_prompt_with_testing_standards(self):
        """Test prompt includes testing standards."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=1,
            total_steps=1,
            kb_results=[],
            testing_standards=["Use pytest"],
        )
        assert "Testing Standards" in result

    def test_prompt_with_code_style_standards(self):
        """Test prompt includes code style standards."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=1,
            total_steps=1,
            kb_results=[],
            code_style_standards=["Be concise"],
        )
        assert "Code Style Standards" in result

    def test_prompt_with_existing_files(self):
        """Test prompt includes existing file content."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=1,
            total_steps=1,
            kb_results=[],
            existing_files={"handler.py": "existing code"},
        )
        assert "Existing File Content" in result
        assert "existing code" in result

    def test_prompt_with_previous_step_files(self):
        """Test prompt includes previous step file content."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=2,
            total_steps=3,
            kb_results=[],
            previous_step_files={"models.py": "class Model: pass"},
        )
        assert "Code from Previous Steps" in result
        assert "class Model: pass" in result

    def test_prompt_truncation_recovery(self):
        """Test prompt includes truncation recovery instructions."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=1,
            total_steps=1,
            kb_results=[],
            truncation_recovery=True,
        )
        assert "OUTPUT SIZE LIMIT" in result

    def test_prompt_truncates_long_existing_files(self):
        """Test that very long existing files are truncated."""
        long_content = "x" * 15000
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=1,
            total_steps=1,
            kb_results=[],
            existing_files={"big.py": long_content},
        )
        assert "(truncated)" in result

    def test_prompt_truncates_long_previous_step_files(self):
        """Test that very long previous step files are truncated."""
        long_content = "y" * 10000
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="desc",
            current_step=2,
            total_steps=2,
            kb_results=[],
            previous_step_files={"big_prev.py": long_content},
        )
        assert "(truncated)" in result

    def test_prompt_no_description(self):
        """Test prompt with no issue description."""
        result = step_executor.build_code_generation_prompt(
            step=self._make_step(),
            issue_description="",
            current_step=1,
            total_steps=1,
            kb_results=[],
        )
        assert "No description provided" in result


# ############################################################################
# format_terraform_files TESTS
# ############################################################################


class TestFormatTerraformFiles:
    """Test Terraform file formatting."""

    @patch("step_executor.subprocess.run")
    def test_format_no_changes(self, mock_run):
        """Test formatting when no files need changes."""
        # Clone succeeds
        mock_run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),  # git clone
            Mock(returncode=0, stdout="", stderr=""),  # terraform fmt
            Mock(returncode=0, stdout="", stderr=""),  # git status --porcelain
        ]

        # Create a temp dir to simulate repo_dir with the file
        with tempfile.TemporaryDirectory() as tmp:
            tf_path = Path(tmp) / "repo" / "main.tf"
            tf_path.parent.mkdir(parents=True, exist_ok=True)
            tf_path.write_text("resource {}")

            def run_side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if cmd[0] == "git" and cmd[1] == "clone":
                    # Create the directory structure
                    return Mock(returncode=0, stdout="", stderr="")
                elif cmd[0] == "terraform":
                    return Mock(returncode=0, stdout="", stderr="")
                elif cmd[0] == "git" and cmd[1] == "status":
                    return Mock(returncode=0, stdout="", stderr="")  # no changes
                return Mock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

        # Should not raise
        step_executor.format_terraform_files(
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            terraform_files=["main.tf"],
            github_token="token",
            step_number=1
        )

    @patch("step_executor.subprocess.run")
    def test_format_timeout(self, mock_run):
        """Test formatting handles timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=60)

        # Should not raise, just log error
        step_executor.format_terraform_files(
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            terraform_files=["main.tf"],
            github_token="token",
            step_number=1
        )

    @patch("step_executor.subprocess.run")
    def test_format_called_process_error(self, mock_run):
        """Test formatting handles CalledProcessError gracefully."""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["git", "clone"], stderr="auth failed"
        )

        # Should not raise, just log error
        step_executor.format_terraform_files(
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            terraform_files=["main.tf"],
            github_token="token",
            step_number=1
        )

    @patch("step_executor.subprocess.run")
    def test_format_unexpected_error(self, mock_run):
        """Test formatting handles unexpected errors gracefully."""
        mock_run.side_effect = RuntimeError("unexpected")

        # Should not raise, just log error
        step_executor.format_terraform_files(
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            terraform_files=["main.tf"],
            github_token="token",
            step_number=1
        )


# ############################################################################
# execute_step TESTS
# ############################################################################


class TestExecuteStep:
    """Test step execution."""

    def test_step_not_found_raises(self):
        """Test exception when step number not found in plan."""
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step 1", description="Desc")]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=99,  # non-existent step
            total_steps=1,
            base_branch="main"
        )

        with pytest.raises(Exception, match="Step 99 not found in plan"):
            step_executor.execute_step(step_message, plan, "token")

    @patch("step_executor.finalize_and_publish_event")
    @patch("step_executor.commit_file")
    @patch("step_executor.get_file")
    @patch("step_executor.update_file")
    @patch("step_executor.get_step_context")
    @patch("step_executor.invoke_claude")
    @patch("step_executor.validate_files_with_backend")
    def test_execute_step_happy_path_last_step(
        self, mock_validate, mock_invoke, mock_kb,
        mock_update_file, mock_get_file, mock_commit_file,
        mock_finalize
    ):
        """Test successful step execution for last step (all steps completed)."""
        # Setup mocks
        mock_kb.return_value = ["some context"]
        mock_get_file.return_value = None  # No existing files
        mock_validate.return_value = []  # No validation errors

        # Mock Claude response with delimiter format
        mock_response = Mock()
        mock_response.text = "===FILE: handler.py===\nprint('hello')\n===END==="
        mock_response.stop_reason = "end_turn"
        mock_response.usage = Mock(inputTokens=100, outputTokens=50)
        mock_invoke.return_value = mock_response

        # Create a mock backend
        mock_backend = Mock()
        mock_backend.name = "python"
        mock_backend.get_code_generation_system_prompt.return_value = "system prompt"
        mock_backend.should_format_file.return_value = False

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(
                step_number=1,
                title="Add handler",
                description="Create handler",
                files_to_create=["handler.py"]
            )]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        result = step_executor.execute_step(step_message, plan, "token", backend=mock_backend)

        assert result["success"] is True
        assert result["all_steps_completed"] is True
        mock_finalize.assert_called_once()

    @patch("step_executor.send_step_message")
    @patch("step_executor.commit_file")
    @patch("step_executor.get_file")
    @patch("step_executor.update_file")
    @patch("step_executor.get_step_context")
    @patch("step_executor.invoke_claude")
    @patch("step_executor.validate_files_with_backend")
    @patch("step_executor.ENABLE_BEDROCK_COOLDOWN", False)
    def test_execute_step_sends_next_step(
        self, mock_validate, mock_invoke, mock_kb,
        mock_update_file, mock_get_file, mock_commit_file,
        mock_send_step
    ):
        """Test step execution sends next step when not last step."""
        mock_kb.return_value = []
        mock_get_file.return_value = None
        mock_validate.return_value = []

        mock_response = Mock()
        mock_response.text = "===FILE: handler.py===\nprint('hello')\n===END==="
        mock_response.stop_reason = "end_turn"
        mock_response.usage = Mock(inputTokens=100, outputTokens=50)
        mock_invoke.return_value = mock_response

        mock_backend = Mock()
        mock_backend.name = "python"
        mock_backend.get_code_generation_system_prompt.return_value = "prompt"
        mock_backend.should_format_file.return_value = False

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[
                PlanStep(step_number=1, title="Step 1", description="D1", files_to_create=["handler.py"]),
                PlanStep(step_number=2, title="Step 2", description="D2", files_to_create=["tests.py"]),
            ]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=2,
            base_branch="main"
        )

        result = step_executor.execute_step(step_message, plan, "token", backend=mock_backend)

        assert result["success"] is True
        assert result["next_step"] == 2
        mock_send_step.assert_called_once()

    @patch("step_executor.commit_file")
    @patch("step_executor.get_file")
    @patch("step_executor.update_file")
    @patch("step_executor.get_step_context")
    @patch("step_executor.invoke_claude")
    def test_execute_step_truncation_raises(
        self, mock_invoke, mock_kb, mock_update_file, mock_get_file, mock_commit_file
    ):
        """Test step execution raises on truncated response."""
        mock_kb.return_value = []
        mock_get_file.return_value = None

        mock_response = Mock()
        mock_response.text = "partial output..."
        mock_response.stop_reason = "max_tokens"
        mock_response.usage = Mock(inputTokens=100, outputTokens=32000)
        mock_invoke.return_value = mock_response

        mock_backend = Mock()
        mock_backend.name = "python"
        mock_backend.get_code_generation_system_prompt.return_value = "prompt"

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step 1", description="D", files_to_create=["f.py"])]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        with pytest.raises(Exception, match="truncated"):
            step_executor.execute_step(step_message, plan, "token", backend=mock_backend)

    @patch("step_executor.commit_file")
    @patch("step_executor.get_file")
    @patch("step_executor.update_file")
    @patch("step_executor.get_step_context")
    @patch("step_executor.invoke_claude")
    def test_execute_step_parse_failure_raises(
        self, mock_invoke, mock_kb, mock_update_file, mock_get_file, mock_commit_file
    ):
        """Test step execution raises on unparseable response."""
        mock_kb.return_value = []
        mock_get_file.return_value = None

        mock_response = Mock()
        mock_response.text = "random text with no files"
        mock_response.stop_reason = "end_turn"
        mock_response.usage = Mock(inputTokens=100, outputTokens=50)
        mock_invoke.return_value = mock_response

        mock_backend = Mock()
        mock_backend.name = "python"
        mock_backend.get_code_generation_system_prompt.return_value = "prompt"

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step 1", description="D", files_to_create=["f.py"])]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        with pytest.raises(ValueError):
            step_executor.execute_step(step_message, plan, "token", backend=mock_backend)

    @patch("step_executor.finalize_and_publish_event")
    @patch("step_executor.update_file")
    @patch("step_executor.get_file")
    @patch("step_executor.get_step_context")
    @patch("step_executor.invoke_claude")
    @patch("step_executor.validate_files_with_backend")
    def test_execute_step_updates_existing_file(
        self, mock_validate, mock_invoke, mock_kb,
        mock_get_file, mock_update_file, mock_finalize
    ):
        """Test step execution updates existing file instead of creating new."""
        import base64
        mock_kb.return_value = []
        mock_validate.return_value = []

        # get_file returns existing file for step files_to_create check,
        # then for existing file check during commit
        existing_content = base64.b64encode(b"old content").decode()
        mock_get_file.side_effect = [
            None,  # step 2.5: feature branch check
            None,  # step 2.5: base branch check
            {"content": existing_content, "sha": "abc"},  # commit: file exists
        ]

        mock_response = Mock()
        mock_response.text = "===FILE: handler.py===\nprint('updated')\n===END==="
        mock_response.stop_reason = "end_turn"
        mock_response.usage = Mock(inputTokens=100, outputTokens=50)
        mock_invoke.return_value = mock_response

        mock_backend = Mock()
        mock_backend.name = "python"
        mock_backend.get_code_generation_system_prompt.return_value = "prompt"
        mock_backend.should_format_file.return_value = False

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step 1", description="D", files_to_create=["handler.py"])]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        result = step_executor.execute_step(step_message, plan, "token", backend=mock_backend)
        assert result["success"] is True

    @patch("step_executor.finalize_and_publish_event")
    @patch("step_executor.format_terraform_files")
    @patch("step_executor.commit_file")
    @patch("step_executor.update_file")
    @patch("step_executor.get_file")
    @patch("step_executor.get_step_context")
    @patch("step_executor.invoke_claude")
    @patch("step_executor.validate_files_with_backend")
    def test_execute_step_formats_terraform_files(
        self, mock_validate, mock_invoke, mock_kb,
        mock_get_file, mock_update_file, mock_commit_file,
        mock_format_tf, mock_finalize
    ):
        """Test step execution formats Terraform files."""
        mock_kb.return_value = []
        mock_get_file.return_value = None
        mock_validate.return_value = []

        mock_response = Mock()
        mock_response.text = "===FILE: main.tf===\nresource {}\n===END==="
        mock_response.stop_reason = "end_turn"
        mock_response.usage = Mock(inputTokens=100, outputTokens=50)
        mock_invoke.return_value = mock_response

        mock_backend = Mock()
        mock_backend.name = "terraform"
        mock_backend.get_code_generation_system_prompt.return_value = "prompt"
        mock_backend.should_format_file.return_value = True

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step 1", description="D", files_to_create=["main.tf"])]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        result = step_executor.execute_step(step_message, plan, "token", backend=mock_backend)
        assert result["success"] is True
        mock_format_tf.assert_called_once()

    @patch("step_executor.finalize_and_publish_event")
    @patch("step_executor.commit_file")
    @patch("step_executor.update_file")
    @patch("step_executor.get_file")
    @patch("step_executor.get_step_context")
    @patch("step_executor.invoke_claude")
    @patch("step_executor.validate_files_with_backend")
    def test_execute_step_with_existing_files_and_previous_steps(
        self, mock_validate, mock_invoke, mock_kb,
        mock_get_file, mock_update_file, mock_commit_file,
        mock_finalize
    ):
        """Test step execution fetches existing files and previous step files."""
        import base64
        mock_kb.return_value = []
        mock_validate.return_value = []

        existing_content = base64.b64encode(b"existing handler").decode()
        prev_content = base64.b64encode(b"previous model").decode()

        # Calls in order:
        # 1. get_file for handler.py on feature branch (exists)
        # 2. get_file for model.py from previous step
        # 3. get_file for handler.py during commit (exists -> update)
        mock_get_file.side_effect = [
            {"content": existing_content, "sha": "abc"},  # handler.py feature branch
            {"content": prev_content, "sha": "def"},  # model.py previous step
            {"content": existing_content, "sha": "abc"},  # handler.py commit check
        ]

        mock_response = Mock()
        mock_response.text = "===FILE: handler.py===\nprint('updated')\n===END==="
        mock_response.stop_reason = "end_turn"
        mock_response.usage = Mock(inputTokens=200, outputTokens=100)
        mock_invoke.return_value = mock_response

        mock_backend = Mock()
        mock_backend.name = "python"
        mock_backend.get_code_generation_system_prompt.return_value = "prompt"
        mock_backend.should_format_file.return_value = False

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[
                PlanStep(step_number=1, title="Step 1", description="D1",
                         files_to_create=["model.py"], status="completed"),
                PlanStep(step_number=2, title="Step 2", description="D2",
                         files_to_create=["handler.py"]),
            ]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=2,
            total_steps=2,
            base_branch="main"
        )

        result = step_executor.execute_step(step_message, plan, "token", backend=mock_backend)
        assert result["success"] is True


# ############################################################################
# handle_step_message TESTS
# ############################################################################


class TestHandleStepMessage:
    """Test SQS step message handler."""

    @patch("step_executor.get_github_token")
    @patch("step_executor.get_file")
    def test_plan_file_not_found_raises(self, mock_get_file, mock_token):
        """Test exception when plan file not found on branch."""
        mock_token.return_value = "token"
        mock_get_file.return_value = None

        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        with pytest.raises(Exception, match="Plan file not found"):
            step_executor.handle_step_message(step_message)

    @patch("step_executor.execute_step")
    @patch("step_executor.parse_plan_from_markdown")
    @patch("step_executor.get_github_token")
    @patch("step_executor.get_file")
    def test_handle_step_message_happy_path(
        self, mock_get_file, mock_token, mock_parse_plan, mock_execute
    ):
        """Test successful step message handling."""
        import base64
        mock_token.return_value = "token"

        plan_md = "# Plan\n## Step 1\nDo something"
        mock_get_file.return_value = {
            "content": base64.b64encode(plan_md.encode()).decode(),
            "sha": "abc"
        }

        mock_plan = Mock()
        mock_parse_plan.return_value = mock_plan
        mock_execute.return_value = {"success": True, "step": 1}

        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        result = step_executor.handle_step_message(step_message)
        assert result["success"] is True
        mock_execute.assert_called_once()


# ############################################################################
# publish_code_generation_completed_event TESTS
# ############################################################################


class TestPublishCodeGenerationCompletedEvent:
    """Test EventBridge event publishing."""

    @patch.object(step_executor, "events_client")
    def test_publish_event_client_error(self, mock_events):
        """Test event publishing handles ClientError gracefully."""
        from botocore.exceptions import ClientError
        mock_events.put_events.side_effect = ClientError(
            {"Error": {"Code": "InternalException", "Message": "error"}},
            "PutEvents"
        )

        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step", description="desc")]
        )
        step_message = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        # Should not raise, just log error
        step_executor.publish_code_generation_completed_event(
            plan=plan,
            step_message=step_message,
            pr_url="https://github.com/owner/repo/pull/1",
            pr_number=1,
            plan_file_path="issues/plan.md",
            commit_sha="abc123"
        )

    @patch.object(step_executor, "events_client")
    def test_publish_event_includes_pr_fields(self, mock_events):
        """Test event includes PR fields when provided."""
        mock_events.put_events.return_value = {}

        plan = ExecutionPlan(
            issue_number=5,
            issue_title="Feature",
            issue_description="Desc",
            branch_name="feature-branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step", description="desc")]
        )
        step_message = StepExecutionMessage(
            issue_number=5,
            issue_title="Feature",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        step_executor.publish_code_generation_completed_event(
            plan=plan,
            step_message=step_message,
            pr_url="https://github.com/owner/repo/pull/5",
            pr_number=5,
            plan_file_path="issues/plan.md",
            commit_sha="def456"
        )

        entry = mock_events.put_events.call_args.kwargs["Entries"][0]
        detail = json.loads(entry["Detail"])
        assert detail["prUrl"] == "https://github.com/owner/repo/pull/5"
        assert detail["prNumber"] == 5

    @patch.object(step_executor, "events_client")
    def test_publish_event_omits_pr_fields_when_none(self, mock_events):
        """Test event omits PR fields when None (deferred PR creation)."""
        mock_events.put_events.return_value = {}

        plan = ExecutionPlan(
            issue_number=5,
            issue_title="Feature",
            issue_description="Desc",
            branch_name="feature-branch",
            repo_full_name="owner/repo",
            steps=[PlanStep(step_number=1, title="Step", description="desc")]
        )
        step_message = StepExecutionMessage(
            issue_number=5,
            issue_title="Feature",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        step_executor.publish_code_generation_completed_event(
            plan=plan,
            step_message=step_message,
            pr_url=None,
            pr_number=None,
            plan_file_path="issues/plan.md",
            commit_sha="abc"
        )

        entry = mock_events.put_events.call_args.kwargs["Entries"][0]
        detail = json.loads(entry["Detail"])
        assert "prUrl" not in detail
        assert "prNumber" not in detail
