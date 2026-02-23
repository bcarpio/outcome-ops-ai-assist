"""
Unit tests for step_executor module.

Tests cover:
- validate_requirements_file: Validating requirements.txt files
- validate_files_with_backend: Backend-based file validation
- build_code_generation_prompt: Building prompts for code generation
- publish_code_generation_completed_event: EventBridge event emission
- finalize_and_publish_event: Finalization flow
- handle_step_message: SQS message handling
"""

import os
import sys
import json
import base64
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock
from dataclasses import dataclass

# Add generate-code directory to Python path
generate_code_dir = os.path.join(os.path.dirname(__file__), '../../generate-code')
sys.path.insert(0, os.path.abspath(generate_code_dir))

# Mock boto3.client BEFORE importing modules that create clients at import time
mock_events_client = MagicMock()
mock_boto3_client = MagicMock(return_value=mock_events_client)

with patch('boto3.client', mock_boto3_client):
    from step_executor import (
        validate_requirements_file,
        validate_files_with_backend,
        build_code_generation_prompt,
        build_syntax_fix_prompt,
        publish_code_generation_completed_event,
        finalize_and_publish_event,
        handle_step_message,
        sanitize_for_logging,
        parse_delimited_files,
        parse_json_fallback,
        CODE_GENERATION_SYSTEM_PROMPT,
    )
    from models import (
        ExecutionPlan,
        PlanStep,
        StepExecutionMessage,
        TokenUsage,
    )


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def basic_step():
    """Create a basic plan step for testing."""
    return PlanStep(
        step_number=1,
        title="Create handler",
        description="Create Lambda handler for authentication",
        files_to_create=["lambda/auth/handler.py"],
        kb_queries=["Lambda authentication examples"],
        status="pending"
    )


@pytest.fixture
def basic_plan():
    """Create a basic execution plan for testing."""
    return ExecutionPlan(
        issue_number=123,
        issue_title="Add user authentication",
        issue_description="Implement OAuth2 authentication for the API",
        branch_name="123-add-user-authentication",
        repo_full_name="owner/repo",
        backend_name="python",
        created_at="2024-01-15T10:30:00",
        steps=[
            PlanStep(
                step_number=1,
                title="Create auth handler",
                description="Create Lambda handler for authentication",
                files_to_create=["lambda/auth/handler.py"],
                kb_queries=["Lambda authentication examples"],
                status="pending"
            ),
            PlanStep(
                step_number=2,
                title="Add tests",
                description="Write unit tests for auth handler",
                files_to_create=["lambda/auth/test_handler.py"],
                kb_queries=["pytest patterns"],
                status="pending"
            )
        ],
        lambda_standards=["Error handling required", "Logging with structlog"],
        terraform_standards=["Use tags", "Enable encryption"],
        testing_standards=["AAA pattern", "Mock external services"]
    )


@pytest.fixture
def step_message():
    """Create a step execution message for testing."""
    return StepExecutionMessage(
        action="execute_step",
        issue_number=123,
        issue_title="Add user authentication",
        issue_description="Implement OAuth2 authentication",
        repo_full_name="owner/repo",
        branch_name="123-add-user-authentication",
        current_step=1,
        total_steps=2,
        base_branch="main"
    )


@pytest.fixture
def mock_backend():
    """Create a mock code generator backend."""
    backend = MagicMock()
    backend.name = "python"
    backend.display_name = "Python"
    backend.get_code_generation_system_prompt.return_value = CODE_GENERATION_SYSTEM_PROMPT
    backend.should_format_file.return_value = False

    # Mock validation result
    validation_result = MagicMock()
    validation_result.valid = True
    validation_result.error_message = None
    backend.validate_files.return_value = [validation_result]

    return backend


# ============================================================================
# Test validate_requirements_file
# ============================================================================


class TestValidateRequirementsFile:
    """Test validate_requirements_file function."""

    def test_returns_none_for_non_requirements_file(self):
        """Test: Returns None for files that aren't requirements.txt."""
        # Arrange
        file_path = "lambda/handler.py"
        content = "import boto3"

        # Act
        result = validate_requirements_file(file_path, content)

        # Assert
        assert result is None

    @patch('step_executor.subprocess.run')
    def test_returns_none_for_valid_requirements(self, mock_run):
        """Test: Returns None for valid requirements.txt."""
        # Arrange
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        file_path = "lambda/requirements.txt"
        content = "boto3>=1.26.0\nrequests>=2.28.0"

        # Act
        result = validate_requirements_file(file_path, content)

        # Assert
        assert result is None
        mock_run.assert_called_once()

    @patch('step_executor.subprocess.run')
    def test_returns_error_for_invalid_package(self, mock_run):
        """Test: Returns error message for invalid package."""
        # Arrange
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Could not find a version that satisfies the requirement nonexistent-package"
        )
        file_path = "lambda/requirements.txt"
        content = "nonexistent-package>=1.0.0"

        # Act
        result = validate_requirements_file(file_path, content)

        # Assert
        assert result is not None
        assert "validation failed" in result
        assert "Could not find a version" in result

    @patch('step_executor.subprocess.run')
    def test_handles_timeout(self, mock_run):
        """Test: Handles subprocess timeout gracefully."""
        # Arrange
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pip", timeout=30)
        file_path = "lambda/requirements.txt"
        content = "boto3>=1.26.0"

        # Act
        result = validate_requirements_file(file_path, content)

        # Assert
        assert result is not None
        assert "timed out" in result

    @patch('step_executor.subprocess.run')
    def test_handles_unexpected_error(self, mock_run):
        """Test: Handles unexpected errors gracefully."""
        # Arrange
        mock_run.side_effect = Exception("Unexpected error")
        file_path = "lambda/requirements.txt"
        content = "boto3>=1.26.0"

        # Act
        result = validate_requirements_file(file_path, content)

        # Assert
        assert result is not None
        assert "validation error" in result
        assert "Unexpected error" in result


# ============================================================================
# Test validate_files_with_backend
# ============================================================================


class TestValidateFilesWithBackend:
    """Test validate_files_with_backend function."""

    def test_returns_empty_list_for_valid_files(self, mock_backend):
        """Test: Returns empty list when all files are valid."""
        # Arrange
        files = [
            {"path": "handler.py", "content": "def handler(): pass"},
            {"path": "utils.py", "content": "def util(): pass"},
        ]

        # Act
        errors = validate_files_with_backend(files, mock_backend)

        # Assert
        assert errors == []
        mock_backend.validate_files.assert_called_once_with(files)

    def test_returns_errors_for_invalid_files(self, mock_backend):
        """Test: Returns error messages for invalid files."""
        # Arrange
        invalid_result = MagicMock()
        invalid_result.valid = False
        invalid_result.file_path = "handler.py"
        invalid_result.error_message = "Syntax error on line 5"
        mock_backend.validate_files.return_value = [invalid_result]

        files = [{"path": "handler.py", "content": "def handler( pass"}]

        # Act
        errors = validate_files_with_backend(files, mock_backend)

        # Assert
        assert len(errors) == 1
        assert "handler.py" in errors[0]
        assert "Syntax error" in errors[0]

    def test_returns_multiple_errors(self, mock_backend):
        """Test: Returns all error messages for multiple invalid files."""
        # Arrange
        result1 = MagicMock()
        result1.valid = False
        result1.file_path = "handler.py"
        result1.error_message = "Missing import"

        result2 = MagicMock()
        result2.valid = False
        result2.file_path = "utils.py"
        result2.error_message = "Invalid syntax"

        mock_backend.validate_files.return_value = [result1, result2]

        files = [
            {"path": "handler.py", "content": "..."},
            {"path": "utils.py", "content": "..."},
        ]

        # Act
        errors = validate_files_with_backend(files, mock_backend)

        # Assert
        assert len(errors) == 2
        assert any("handler.py" in e and "Missing import" in e for e in errors)
        assert any("utils.py" in e and "Invalid syntax" in e for e in errors)


# ============================================================================
# Test build_code_generation_prompt
# ============================================================================


class TestBuildCodeGenerationPrompt:
    """Test build_code_generation_prompt function."""

    def test_includes_step_info(self, basic_step):
        """Test: Prompt includes step number, title, and description."""
        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test issue",
            current_step=1,
            total_steps=3,
            kb_results=[]
        )

        # Assert
        assert "step 1 of 3" in prompt
        assert "Create handler" in prompt
        assert "Create Lambda handler for authentication" in prompt

    def test_includes_issue_description(self, basic_step):
        """Test: Prompt includes the issue description."""
        # Arrange
        issue_desc = "Implement OAuth2 authentication for the API"

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description=issue_desc,
            current_step=1,
            total_steps=1,
            kb_results=[]
        )

        # Assert
        assert issue_desc in prompt

    def test_includes_files_to_create(self, basic_step):
        """Test: Prompt includes files to create."""
        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[]
        )

        # Assert
        assert "lambda/auth/handler.py" in prompt

    def test_includes_lambda_standards(self, basic_step):
        """Test: Prompt includes Lambda standards."""
        # Arrange
        standards = ["Error handling required", "Use structlog"]

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[],
            lambda_standards=standards
        )

        # Assert
        assert "Lambda Handler Standards" in prompt
        assert "Error handling required" in prompt
        assert "Use structlog" in prompt

    def test_includes_terraform_standards(self, basic_step):
        """Test: Prompt includes Terraform standards."""
        # Arrange
        standards = ["Use tags", "Enable encryption"]

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[],
            terraform_standards=standards
        )

        # Assert
        assert "Terraform Standards" in prompt
        assert "Use tags" in prompt
        assert "Enable encryption" in prompt

    def test_includes_testing_standards(self, basic_step):
        """Test: Prompt includes Testing standards."""
        # Arrange
        standards = ["AAA pattern", "Mock external services"]

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[],
            testing_standards=standards
        )

        # Assert
        assert "Testing Standards" in prompt
        assert "AAA pattern" in prompt
        assert "Mock external services" in prompt

    def test_includes_kb_results(self, basic_step):
        """Test: Prompt includes knowledge base results."""
        # Arrange
        kb_results = [
            "Example: Lambda handler pattern",
            "Best practice: Error handling"
        ]

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=kb_results
        )

        # Assert
        assert "Step-Specific Context" in prompt
        assert "Lambda handler pattern" in prompt
        assert "Error handling" in prompt

    def test_syntax_fix_prompt_includes_error(self, basic_step):
        """Test: Syntax fix prompt includes the validation error."""
        # Arrange
        validation_error = "SyntaxError: unexpected EOF while parsing"
        previous_code = "## test.py\n```python\ndef broken(\n```"

        # Act
        prompt = build_syntax_fix_prompt(
            validation_error=validation_error,
            previous_code=previous_code
        )

        # Assert
        assert "Fix the syntax error" in prompt
        assert "SyntaxError" in prompt
        assert "def broken" in prompt
        assert "ONLY the JSON" in prompt

    def test_syntax_fix_prompt_includes_previous_code(self, basic_step):
        """Test: Syntax fix prompt includes previous code that failed."""
        # Arrange
        validation_error = "Python syntax error at line 10: invalid syntax"
        previous_code = "## test_handler.py\n```python\ndef test_broken(\n    pass\n```"

        # Act
        prompt = build_syntax_fix_prompt(
            validation_error=validation_error,
            previous_code=previous_code
        )

        # Assert
        assert "Fix the syntax error" in prompt
        assert "test_handler.py" in prompt
        assert "def test_broken" in prompt
        assert "Code to fix" in prompt

    def test_includes_existing_files_section(self, basic_step):
        """Test: Prompt includes existing file content for extension."""
        # Arrange
        existing_files = {
            "lambda/auth/handler.py": "def existing_handler():\n    pass"
        }

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[],
            existing_files=existing_files
        )

        # Assert
        assert "Existing File Content" in prompt
        assert "lambda/auth/handler.py" in prompt
        assert "def existing_handler" in prompt
        assert "EXTENDED (not replaced" in prompt

    def test_truncates_long_existing_files(self, basic_step):
        """Test: Long existing files are truncated."""
        # Arrange
        long_content = "x" * 15000  # More than 10000 chars
        existing_files = {"handler.py": long_content}

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[],
            existing_files=existing_files
        )

        # Assert
        assert "truncated" in prompt
        # Should not contain full 15000 chars
        assert len(prompt) < len(long_content)

    def test_includes_previous_step_files(self, basic_step):
        """Test: Prompt includes files from previous steps."""
        # Arrange
        previous_files = {
            "lambda/models.py": "class User:\n    pass"
        }

        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=2,
            total_steps=3,
            kb_results=[],
            previous_step_files=previous_files
        )

        # Assert
        assert "Code from Previous Steps" in prompt
        assert "lambda/models.py" in prompt
        assert "class User" in prompt
        assert "reference for imports" in prompt

    def test_handles_none_issue_description(self, basic_step):
        """Test: Handles None issue description gracefully."""
        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description=None,
            current_step=1,
            total_steps=1,
            kb_results=[]
        )

        # Assert
        assert "No description provided" in prompt

    def test_handles_empty_kb_results(self, basic_step):
        """Test: Handles empty KB results gracefully."""
        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[]
        )

        # Assert
        assert "No context available" in prompt

    def test_includes_truncation_recovery_section(self, basic_step):
        """Test: Prompt includes truncation recovery instructions when enabled."""
        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[],
            truncation_recovery=True
        )

        # Assert
        assert "OUTPUT SIZE LIMIT" in prompt
        assert "GENERATE MINIMAL CODE" in prompt
        assert "Previous attempts" in prompt
        assert "truncated" in prompt.lower()

    def test_excludes_truncation_section_by_default(self, basic_step):
        """Test: Prompt excludes truncation recovery when not enabled."""
        # Act
        prompt = build_code_generation_prompt(
            step=basic_step,
            issue_description="Test",
            current_step=1,
            total_steps=1,
            kb_results=[],
            truncation_recovery=False
        )

        # Assert
        assert "OUTPUT SIZE LIMIT" not in prompt
        assert "GENERATE MINIMAL CODE" not in prompt


# ============================================================================
# Test publish_code_generation_completed_event
# ============================================================================


class TestPublishCodeGenerationCompletedEvent:
    """Test publish_code_generation_completed_event function."""

    def test_publishes_event_with_pr_info(self, basic_plan, step_message):
        """Test: Publishes event with PR URL and number."""
        # Arrange
        with patch('step_executor.events_client') as mock_client:
            # Act
            publish_code_generation_completed_event(
                plan=basic_plan,
                step_message=step_message,
                pr_url="https://github.com/owner/repo/pull/1",
                pr_number=1,
                plan_file_path="issues/123-plan.md",
                commit_sha="abc123"
            )

            # Assert
            mock_client.put_events.assert_called_once()
            call_args = mock_client.put_events.call_args
            entries = call_args[1]["Entries"]
            assert len(entries) == 1

            entry = entries[0]
            assert entry["Source"] == "outcomeops.generate-code"
            assert entry["DetailType"] == "OutcomeOps.CodeGeneration.Completed"

            detail = json.loads(entry["Detail"])
            assert detail["issueNumber"] == 123
            assert detail["repoFullName"] == "owner/repo"
            assert detail["branchName"] == "123-add-user-authentication"
            assert detail["prUrl"] == "https://github.com/owner/repo/pull/1"
            assert detail["prNumber"] == 1
            assert detail["commitSha"] == "abc123"

    def test_publishes_event_without_pr_info(self, basic_plan, step_message):
        """Test: Publishes event without PR info (deferred PR creation)."""
        # Arrange
        with patch('step_executor.events_client') as mock_client:
            # Act
            publish_code_generation_completed_event(
                plan=basic_plan,
                step_message=step_message,
                pr_url=None,
                pr_number=None,
                plan_file_path="issues/123-plan.md",
                commit_sha="abc123"
            )

            # Assert
            mock_client.put_events.assert_called_once()
            call_args = mock_client.put_events.call_args
            detail = json.loads(call_args[1]["Entries"][0]["Detail"])

            assert "prUrl" not in detail
            assert "prNumber" not in detail

    def test_handles_client_error(self, basic_plan, step_message):
        """Test: Handles EventBridge client error gracefully."""
        # Arrange
        from botocore.exceptions import ClientError
        with patch('step_executor.events_client') as mock_client:
            mock_client.put_events.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Access denied"}},
                "PutEvents"
            )

            # Act - should not raise
            publish_code_generation_completed_event(
                plan=basic_plan,
                step_message=step_message,
                pr_url=None,
                pr_number=None,
                plan_file_path="issues/123-plan.md",
                commit_sha="abc123"
            )

            # Assert - no exception raised


# ============================================================================
# Test finalize_and_publish_event
# ============================================================================


class TestFinalizeAndPublishEvent:
    """Test finalize_and_publish_event function."""

    @patch('step_executor.publish_code_generation_completed_event')
    @patch('step_executor.get_branch_head_sha')
    @patch('step_executor.generate_branch_name')
    def test_publishes_event_with_deferred_pr(
        self,
        mock_branch_name,
        mock_get_sha,
        mock_publish,
        basic_plan,
        step_message
    ):
        """Test: Publishes event with None PR values (deferred creation)."""
        # Arrange
        mock_branch_name.return_value = "123-add-user-authentication"
        mock_get_sha.return_value = "abc123def456"

        # Act
        finalize_and_publish_event(
            plan=basic_plan,
            step_message=step_message,
            github_token="ghp_test"
        )

        # Assert
        mock_publish.assert_called_once()
        call_kwargs = mock_publish.call_args[1]
        assert call_kwargs["pr_url"] is None
        assert call_kwargs["pr_number"] is None
        assert "123-add-user-authentication-plan.md" in call_kwargs["plan_file_path"]

    @patch('step_executor.publish_code_generation_completed_event')
    @patch('step_executor.get_branch_head_sha')
    @patch('step_executor.generate_branch_name')
    def test_tracks_pr_generation(
        self,
        mock_branch_name,
        mock_get_sha,
        mock_publish,
        basic_plan,
        step_message
    ):
        """Test: Tracks PR generation for license usage."""
        # Arrange
        mock_branch_name.return_value = "123-add-user-authentication"
        mock_get_sha.return_value = "abc123"

        # Mock the entire outcomeops_license module since it's imported dynamically
        mock_license_module = MagicMock()
        with patch.dict('sys.modules', {'outcomeops_license': mock_license_module}):
            # Act
            finalize_and_publish_event(
                plan=basic_plan,
                step_message=step_message,
                github_token="ghp_test"
            )

            # Assert - the module was imported and track_pr_generated was called
            mock_license_module.track_pr_generated.assert_called_once_with(
                repo="owner/repo",
                issue_number=123
            )

    @patch('step_executor.publish_code_generation_completed_event')
    @patch('step_executor.get_branch_head_sha')
    @patch('step_executor.generate_branch_name')
    def test_handles_missing_license_layer(
        self,
        mock_branch_name,
        mock_get_sha,
        mock_publish,
        basic_plan,
        step_message
    ):
        """Test: Handles missing license layer gracefully."""
        # Arrange
        mock_branch_name.return_value = "123-add-user-authentication"
        mock_get_sha.return_value = "abc123"

        # Act - should not raise even without license layer
        finalize_and_publish_event(
            plan=basic_plan,
            step_message=step_message,
            github_token="ghp_test"
        )

        # Assert
        mock_publish.assert_called_once()


# ============================================================================
# Test handle_step_message
# ============================================================================


class TestHandleStepMessage:
    """Test handle_step_message function."""

    @patch('step_executor.execute_step')
    @patch('step_executor.parse_plan_from_markdown')
    @patch('step_executor.get_file')
    @patch('step_executor.get_github_token')
    @patch('step_executor.generate_branch_name')
    def test_handles_step_message(
        self,
        mock_branch_name,
        mock_get_token,
        mock_get_file,
        mock_parse,
        mock_execute,
        step_message,
        basic_plan
    ):
        """Test: Handles step message successfully."""
        # Arrange
        mock_branch_name.return_value = "123-add-user-authentication"
        mock_get_token.return_value = "ghp_test"
        mock_get_file.return_value = {
            "content": base64.b64encode(b"# Plan markdown").decode()
        }
        mock_parse.return_value = basic_plan
        mock_execute.return_value = {"success": True, "step": 1}

        # Act
        result = handle_step_message(step_message)

        # Assert
        assert result["success"] is True
        mock_execute.assert_called_once()

    @patch('step_executor.get_file')
    @patch('step_executor.get_github_token')
    @patch('step_executor.generate_branch_name')
    def test_raises_for_missing_plan_file(
        self,
        mock_branch_name,
        mock_get_token,
        mock_get_file,
        step_message
    ):
        """Test: Raises exception when plan file not found."""
        # Arrange
        mock_branch_name.return_value = "123-add-user-authentication"
        mock_get_token.return_value = "ghp_test"
        mock_get_file.return_value = None

        # Act & Assert
        with pytest.raises(Exception, match="Plan file not found"):
            handle_step_message(step_message)


# ============================================================================
# Test CODE_GENERATION_SYSTEM_PROMPT
# ============================================================================


class TestCodeGenerationSystemPrompt:
    """Test the code generation system prompt constant."""

    def test_system_prompt_exists(self):
        """Test: System prompt is defined."""
        assert CODE_GENERATION_SYSTEM_PROMPT is not None
        assert len(CODE_GENERATION_SYSTEM_PROMPT) > 0

    def test_system_prompt_mentions_json_escaping(self):
        """Test: System prompt mentions JSON string escaping."""
        # Note: CODE_GENERATION_SYSTEM_PROMPT is a legacy constant used only for retry prompts
        # Main code generation now uses backend-specific prompts with delimiter format
        assert "JSON" in CODE_GENERATION_SYSTEM_PROMPT
        assert "\\n" in CODE_GENERATION_SYSTEM_PROMPT  # Must escape newlines

    def test_system_prompt_mentions_json_format(self):
        """Test: System prompt specifies JSON format."""
        assert "JSON" in CODE_GENERATION_SYSTEM_PROMPT
        assert '"files"' in CODE_GENERATION_SYSTEM_PROMPT

    def test_system_prompt_mentions_content_field(self):
        """Test: System prompt specifies content field."""
        assert '"content"' in CODE_GENERATION_SYSTEM_PROMPT


class TestParseDelimitedFiles:
    """Test the parse_delimited_files function for delimiter-based output format."""

    def test_parses_single_file(self):
        """Test: Parses single file from delimiter format."""
        response = """===FILE: lambda/my-func/handler.py===
import json

def handler(event, context):
    return {"statusCode": 200}
===END==="""
        files = parse_delimited_files(response)
        assert len(files) == 1
        assert files[0].path == "lambda/my-func/handler.py"
        assert "def handler" in files[0].content

    def test_parses_multiple_files(self):
        """Test: Parses multiple files from delimiter format."""
        response = """===FILE: handler.py===
def handler(event, context):
    pass
===FILE: requirements.txt===
boto3==1.34.0
pydantic==2.5.0
===END==="""
        files = parse_delimited_files(response)
        assert len(files) == 2
        assert files[0].path == "handler.py"
        assert files[1].path == "requirements.txt"
        assert "boto3" in files[1].content

    def test_handles_whitespace_in_path(self):
        """Test: Handles whitespace around file path."""
        response = """===FILE:  path/to/file.py  ===
content here
===END==="""
        files = parse_delimited_files(response)
        assert files[0].path == "path/to/file.py"

    def test_handles_no_end_marker(self):
        """Test: Handles response without ===END=== marker."""
        response = """===FILE: test.py===
def test():
    pass"""
        files = parse_delimited_files(response)
        assert len(files) == 1
        assert "def test" in files[0].content

    def test_raises_on_no_delimiters_and_no_json(self):
        """Test: Raises ValueError when no file delimiters and not JSON."""
        response = "Just some text without any file markers"
        with pytest.raises(ValueError) as exc_info:
            parse_delimited_files(response)
        assert "No file delimiters" in str(exc_info.value)

    def test_falls_back_to_json_parsing(self):
        """Test: Falls back to JSON parsing when delimiters not found."""
        response = '''```json
{
  "files": [
    {
      "path": "lambda/handler.py",
      "content": "def handler(event, context):\\n    return {\\"statusCode\\": 200}"
    }
  ]
}
```'''
        files = parse_delimited_files(response)
        assert len(files) == 1
        assert files[0].path == "lambda/handler.py"
        assert "def handler" in files[0].content

    def test_falls_back_to_json_without_code_fence(self):
        """Test: Falls back to JSON parsing without code fence."""
        response = '{"files": [{"path": "test.py", "content": "print(\\"hello\\")"}]}'
        files = parse_delimited_files(response)
        assert len(files) == 1
        assert files[0].path == "test.py"
        assert 'print("hello")' in files[0].content

    def test_content_is_raw_not_encoded(self):
        """Test: File content is raw text (no base64/JSON encoding)."""
        response = """===FILE: test.py===
expected = {"key": "value"}
print(f"Test: {expected}")
===END==="""
        files = parse_delimited_files(response)
        # Content should be raw, not JSON-escaped
        assert 'expected = {"key": "value"}' in files[0].content


class TestParseJsonFallback:
    """Test the parse_json_fallback function for JSON response format."""

    def test_parses_files_dict_format(self):
        """Test: Parses {"files": [...]} format."""
        response = '{"files": [{"path": "handler.py", "content": "def handler(): pass"}]}'
        files = parse_json_fallback(response)
        assert len(files) == 1
        assert files[0].path == "handler.py"
        assert "def handler" in files[0].content

    def test_parses_array_format(self):
        """Test: Parses direct array format."""
        response = '[{"path": "test.py", "content": "import pytest"}]'
        files = parse_json_fallback(response)
        assert len(files) == 1
        assert files[0].path == "test.py"

    def test_strips_markdown_code_fence(self):
        """Test: Strips ```json code fence."""
        response = '''```json
{"files": [{"path": "app.py", "content": "print(\\"hello\\")"}]}
```'''
        files = parse_json_fallback(response)
        assert len(files) == 1
        assert files[0].path == "app.py"

    def test_parses_multiple_files(self):
        """Test: Parses multiple files from JSON."""
        response = '''{"files": [
            {"path": "handler.py", "content": "def handler(): pass"},
            {"path": "requirements.txt", "content": "boto3==1.34.0"}
        ]}'''
        files = parse_json_fallback(response)
        assert len(files) == 2
        assert files[0].path == "handler.py"
        assert files[1].path == "requirements.txt"

    def test_raises_on_invalid_json(self):
        """Test: Raises ValueError for invalid JSON."""
        response = "not valid json {{"
        with pytest.raises(ValueError) as exc_info:
            parse_json_fallback(response)
        assert "JSON fallback parsing failed" in str(exc_info.value)

    def test_raises_on_unexpected_structure(self):
        """Test: Raises ValueError for unexpected JSON structure."""
        response = '{"unexpected": "structure"}'
        with pytest.raises(ValueError) as exc_info:
            parse_json_fallback(response)
        assert "Unexpected JSON structure" in str(exc_info.value)

    def test_raises_on_empty_files(self):
        """Test: Raises ValueError when no files extracted."""
        response = '{"files": []}'
        with pytest.raises(ValueError) as exc_info:
            parse_json_fallback(response)
        assert "No files extracted" in str(exc_info.value)

    def test_skips_items_missing_path_or_content(self):
        """Test: Skips items without path or content."""
        response = '''{"files": [
            {"path": "valid.py", "content": "code"},
            {"path": "missing_content.py"},
            {"content": "missing_path"}
        ]}'''
        files = parse_json_fallback(response)
        assert len(files) == 1
        assert files[0].path == "valid.py"

    def test_handles_escaped_content(self):
        """Test: Handles JSON-escaped content with newlines and quotes."""
        response = '{"files": [{"path": "test.py", "content": "line1\\nline2\\n\\"quoted\\""}]}'
        files = parse_json_fallback(response)
        assert len(files) == 1
        assert "line1\nline2" in files[0].content
        assert '"quoted"' in files[0].content


class TestSanitizeForLogging:
    """Test the sanitize_for_logging function."""

    def test_redacts_github_pat_token(self):
        """Test: Redacts github_pat_ tokens from strings."""
        # Arrange
        text = "git clone https://x-access-token:github_pat_11AALK4WQ0test@github.com/user/repo.git"

        # Act
        result = sanitize_for_logging(text)

        # Assert
        assert "github_pat_" not in result
        assert "[REDACTED]" in result
        assert "github.com/user/repo.git" in result

    def test_redacts_ghp_token(self):
        """Test: Redacts ghp_ tokens from strings."""
        # Arrange
        text = "Authorization: token ghp_abcdefghijklmnopqrstuvwxyz123456789012"

        # Act
        result = sanitize_for_logging(text)

        # Assert
        assert "ghp_" not in result
        assert "[REDACTED]" in result

    def test_redacts_access_token_from_url(self):
        """Test: Redacts x-access-token from git URLs."""
        # Arrange
        text = "https://x-access-token:secret123@github.com/user/repo.git"

        # Act
        result = sanitize_for_logging(text)

        # Assert
        assert "secret123" not in result
        assert "[REDACTED]" in result

    def test_handles_list_input(self):
        """Test: Handles list input (command args)."""
        # Arrange
        cmd = ["git", "clone", "https://x-access-token:secret@github.com/user/repo.git"]

        # Act
        result = sanitize_for_logging(cmd)

        # Assert
        assert "secret" not in result
        assert "[REDACTED]" in result
        assert "git clone" in result

    def test_handles_none_input(self):
        """Test: Returns empty string for None input."""
        # Act
        result = sanitize_for_logging(None)

        # Assert
        assert result == ""

    def test_preserves_safe_text(self):
        """Test: Preserves text without sensitive tokens."""
        # Arrange
        text = "git clone https://github.com/user/public-repo.git"

        # Act
        result = sanitize_for_logging(text)

        # Assert
        assert result == text
