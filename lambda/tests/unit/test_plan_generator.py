"""
Unit tests for plan_generator module.

Tests cover:
- Prompt building with various inputs
- Plan generation from Claude responses
- Webhook handling flow
- Error cases (truncation, JSON parsing failures, empty responses)
"""

import json
import pytest
import sys
import os
import importlib.util
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass

# Add generate-code directory to Python path
generate_code_dir = os.path.join(os.path.dirname(__file__), '../../generate-code')
sys.path.insert(0, os.path.abspath(generate_code_dir))

# Load plan_generator module
plan_gen_path = os.path.join(generate_code_dir, 'plan_generator.py')
spec = importlib.util.spec_from_file_location("plan_generator", plan_gen_path)
plan_generator = importlib.util.module_from_spec(spec)
sys.modules['plan_generator'] = plan_generator
spec.loader.exec_module(plan_generator)

# Import functions to test
build_plan_generation_prompt = plan_generator.build_plan_generation_prompt
generate_execution_plan = plan_generator.generate_execution_plan
handle_webhook = plan_generator.handle_webhook

# Import models
from models import GitHubWebhookEvent, GitHubIssue, GitHubRepository, GitHubLabel, ClaudeResponse, BedrockUsage


class TestBuildPlanGenerationPrompt:
    """Test prompt building for plan generation."""

    def test_build_prompt_with_all_standards(self):
        """Test: Build prompt with all standards provided"""
        # Arrange
        issue_number = 123
        issue_title = "Add user authentication"
        issue_description = "Implement OAuth2 authentication"
        lambda_standards = ["Standard 1: Error handling", "Standard 2: Logging"]
        terraform_standards = ["TF Standard 1: Naming", "TF Standard 2: Tags"]
        testing_standards = ["Test Standard 1: AAA pattern", "Test Standard 2: Mocking"]

        # Act
        prompt = build_plan_generation_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_description=issue_description,
            lambda_standards=lambda_standards,
            terraform_standards=terraform_standards,
            testing_standards=testing_standards
        )

        # Assert
        assert f"Issue: #{issue_number}" in prompt
        assert issue_title in prompt
        assert issue_description in prompt
        assert "Standard 1: Error handling" in prompt
        assert "TF Standard 1: Naming" in prompt
        assert "Test Standard 1: AAA pattern" in prompt
        assert "IMPORTANT: When creating test-related steps" in prompt

    def test_build_prompt_with_empty_description(self):
        """Test: Build prompt when issue has no description"""
        # Arrange
        issue_number = 456
        issue_title = "Fix bug"
        issue_description = None
        lambda_standards = []
        terraform_standards = []
        testing_standards = []

        # Act
        prompt = build_plan_generation_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_description=issue_description,
            lambda_standards=lambda_standards,
            terraform_standards=terraform_standards,
            testing_standards=testing_standards
        )

        # Assert
        assert "No description provided" in prompt
        assert "No Lambda standards available" in prompt
        assert "No Terraform standards available" in prompt
        assert "No testing standards available" in prompt

    def test_build_prompt_includes_test_granularity_instruction(self):
        """Test: Prompt includes instruction to break down test steps"""
        # Arrange
        prompt = build_plan_generation_prompt(
            issue_number=1,
            issue_title="Test",
            issue_description="Test desc",
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )

        # Assert
        assert "break them into multiple focused steps" in prompt
        assert "success cases, error handling, edge cases" in prompt
        assert "1-3 test functions each" in prompt


class TestGenerateExecutionPlan:
    """Test execution plan generation."""

    @patch('plan_generator.get_lambda_standards')
    @patch('plan_generator.get_terraform_standards')
    @patch('plan_generator.get_testing_standards')
    @patch('plan_generator.invoke_claude')
    @patch('plan_generator.generate_branch_name')
    def test_generate_execution_plan_success(
        self,
        mock_branch_name,
        mock_invoke_claude,
        mock_testing,
        mock_terraform,
        mock_lambda
    ):
        """Test: Successfully generate execution plan"""
        # Arrange
        mock_lambda.return_value = ["Lambda standard 1"]
        mock_terraform.return_value = ["TF standard 1"]
        mock_testing.return_value = ["Test standard 1"]
        mock_branch_name.return_value = "123-add-feature"

        claude_response = ClaudeResponse(
            text=json.dumps({
                "steps": [
                    {
                        "stepNumber": 1,
                        "title": "Create handler",
                        "description": "Create Lambda handler",
                        "filesToCreate": ["lambda/handler.py"],
                        "kbQueries": ["Lambda examples"],
                        "status": "pending"
                    }
                ]
            }),
            usage=BedrockUsage(inputTokens=100, outputTokens=200),
            stop_reason="end_turn"
        )
        mock_invoke_claude.return_value = claude_response

        webhook_event = GitHubWebhookEvent(
            action="labeled",
            label=GitHubLabel(name="approved-for-generation", color="00ff00"),
            issue=GitHubIssue(
                number=123,
                title="Add feature",
                body="Feature description",
                html_url="https://github.com/owner/repo/issues/123",
                state="open"
            ),
            repository=GitHubRepository(
                name="repo",
                full_name="owner/repo",
                owner={"login": "owner"},
                default_branch="main"
            )
        )

        # Act
        plan = generate_execution_plan(webhook_event)

        # Assert
        assert plan.issue_number == 123
        assert plan.issue_title == "Add feature"
        assert plan.branch_name == "123-add-feature"
        assert len(plan.steps) == 1
        assert plan.steps[0].title == "Create handler"
        assert plan.steps[0].step_number == 1
        mock_invoke_claude.assert_called_once()

    @patch('plan_generator.get_lambda_standards')
    @patch('plan_generator.get_terraform_standards')
    @patch('plan_generator.get_testing_standards')
    @patch('plan_generator.invoke_claude')
    def test_generate_execution_plan_truncated_response(
        self,
        mock_invoke_claude,
        mock_testing,
        mock_terraform,
        mock_lambda
    ):
        """Test: Handle truncated Claude response (max_tokens reached)"""
        # Arrange
        mock_lambda.return_value = []
        mock_terraform.return_value = []
        mock_testing.return_value = []

        claude_response = ClaudeResponse(
            text='{"steps": [',
            usage=BedrockUsage(inputTokens=100, outputTokens=4000),
            stop_reason="max_tokens"
        )
        mock_invoke_claude.return_value = claude_response

        webhook_event = GitHubWebhookEvent(
            action="labeled",
            label=GitHubLabel(name="approved-for-generation", color="00ff00"),
            issue=GitHubIssue(
                number=123,
                title="Add feature",
                body="Feature description",
                html_url="https://github.com/owner/repo/issues/123",
                state="open"
            ),
            repository=GitHubRepository(
                name="repo",
                full_name="owner/repo",
                owner={"login": "owner"},
                default_branch="main"
            )
        )

        # Act & Assert
        with pytest.raises(Exception, match="Plan generation response truncated"):
            generate_execution_plan(webhook_event)

    @patch('plan_generator.get_lambda_standards')
    @patch('plan_generator.get_terraform_standards')
    @patch('plan_generator.get_testing_standards')
    @patch('plan_generator.invoke_claude')
    def test_generate_execution_plan_invalid_json(
        self,
        mock_invoke_claude,
        mock_testing,
        mock_terraform,
        mock_lambda
    ):
        """Test: Handle invalid JSON in Claude response"""
        # Arrange
        mock_lambda.return_value = []
        mock_terraform.return_value = []
        mock_testing.return_value = []

        claude_response = ClaudeResponse(
            text="This is not valid JSON",
            usage=BedrockUsage(inputTokens=100, outputTokens=50),
            stop_reason="end_turn"
        )
        mock_invoke_claude.return_value = claude_response

        webhook_event = GitHubWebhookEvent(
            action="labeled",
            label=GitHubLabel(name="approved-for-generation", color="00ff00"),
            issue=GitHubIssue(
                number=123,
                title="Add feature",
                body="Feature description",
                html_url="https://github.com/owner/repo/issues/123",
                state="open"
            ),
            repository=GitHubRepository(
                name="repo",
                full_name="owner/repo",
                owner={"login": "owner"},
                default_branch="main"
            )
        )

        # Act & Assert
        with pytest.raises(ValueError):
            generate_execution_plan(webhook_event)

    @patch('plan_generator.get_lambda_standards')
    @patch('plan_generator.get_terraform_standards')
    @patch('plan_generator.get_testing_standards')
    @patch('plan_generator.invoke_claude')
    @patch('plan_generator.generate_branch_name')
    def test_generate_execution_plan_no_steps(
        self,
        mock_branch_name,
        mock_invoke_claude,
        mock_testing,
        mock_terraform,
        mock_lambda
    ):
        """Test: Handle response with no steps"""
        # Arrange
        mock_lambda.return_value = []
        mock_terraform.return_value = []
        mock_testing.return_value = []
        mock_branch_name.return_value = "123-add-feature"

        claude_response = ClaudeResponse(
            text=json.dumps({"steps": []}),
            usage=BedrockUsage(inputTokens=100, outputTokens=50),
            stop_reason="end_turn"
        )
        mock_invoke_claude.return_value = claude_response

        webhook_event = GitHubWebhookEvent(
            action="labeled",
            label=GitHubLabel(name="approved-for-generation", color="00ff00"),
            issue=GitHubIssue(
                number=123,
                title="Add feature",
                body="Feature description",
                html_url="https://github.com/owner/repo/issues/123",
                state="open"
            ),
            repository=GitHubRepository(
                name="repo",
                full_name="owner/repo",
                owner={"login": "owner"},
                default_branch="main"
            )
        )

        # Act & Assert
        with pytest.raises(Exception, match="No steps in plan JSON"):
            generate_execution_plan(webhook_event)


class TestHandleWebhook:
    """Test webhook handling flow."""

    @patch('plan_generator.generate_execution_plan')
    @patch('plan_generator.serialize_plan_to_markdown')
    @patch('plan_generator.get_github_token')
    @patch('plan_generator.commit_file')
    @patch('plan_generator.send_step_message')
    @patch('plan_generator.generate_branch_name')
    def test_handle_webhook_success(
        self,
        mock_branch_name,
        mock_send_message,
        mock_commit,
        mock_get_token,
        mock_serialize,
        mock_generate_plan
    ):
        """Test: Successfully handle webhook and create plan"""
        # Arrange
        mock_branch_name.return_value = "123-add-feature"
        mock_get_token.return_value = "ghp_test_token"
        mock_serialize.return_value = "# Plan markdown"

        from models import ExecutionPlan, PlanStep
        mock_plan = ExecutionPlan(
            issue_number=123,
            issue_title="Add feature",
            issue_description="Feature description",
            branch_name="123-add-feature",
            repo_full_name="owner/repo",
            steps=[
                PlanStep(
                    step_number=1,
                    title="Create handler",
                    description="Create Lambda handler",
                    files_to_create=["lambda/handler.py"],
                    kb_queries=[],
                    status="pending"
                )
            ],
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )
        mock_generate_plan.return_value = mock_plan

        webhook_event = GitHubWebhookEvent(
            action="labeled",
            label=GitHubLabel(name="approved-for-generation", color="00ff00"),
            issue=GitHubIssue(
                number=123,
                title="Add feature",
                body="Feature description",
                html_url="https://github.com/owner/repo/issues/123",
                state="open"
            ),
            repository=GitHubRepository(
                name="repo",
                full_name="owner/repo",
                owner={"login": "owner"},
                default_branch="main"
            )
        )

        # Act
        result = handle_webhook(webhook_event)

        # Assert
        assert result["success"] is True
        assert result["issue_number"] == 123
        assert result["branch_name"] == "123-add-feature"
        assert result["total_steps"] == 1
        assert "github.com/owner/repo/tree/123-add-feature" in result["branch_url"]
        assert "issues/123-add-feature-plan.md" in result["plan_url"]
        mock_commit.assert_called_once()
        mock_send_message.assert_called_once()

    @patch('plan_generator.generate_execution_plan')
    @patch('plan_generator.serialize_plan_to_markdown')
    @patch('plan_generator.get_github_token')
    @patch('plan_generator.commit_file')
    @patch('plan_generator.generate_branch_name')
    def test_handle_webhook_no_steps(
        self,
        mock_branch_name,
        mock_commit,
        mock_get_token,
        mock_serialize,
        mock_generate_plan
    ):
        """Test: Handle webhook when plan has no steps (edge case)"""
        # Arrange
        mock_branch_name.return_value = "123-add-feature"
        mock_get_token.return_value = "ghp_test_token"
        mock_serialize.return_value = "# Plan markdown"

        from models import ExecutionPlan
        mock_plan = ExecutionPlan(
            issue_number=123,
            issue_title="Add feature",
            issue_description="Feature description",
            branch_name="123-add-feature",
            repo_full_name="owner/repo",
            steps=[],  # No steps
            lambda_standards=[],
            terraform_standards=[],
            testing_standards=[]
        )
        mock_generate_plan.return_value = mock_plan

        webhook_event = GitHubWebhookEvent(
            action="labeled",
            label=GitHubLabel(name="approved-for-generation", color="00ff00"),
            issue=GitHubIssue(
                number=123,
                title="Add feature",
                body="Feature description",
                html_url="https://github.com/owner/repo/issues/123",
                state="open"
            ),
            repository=GitHubRepository(
                name="repo",
                full_name="owner/repo",
                owner={"login": "owner"},
                default_branch="main"
            )
        )

        # Act
        result = handle_webhook(webhook_event)

        # Assert
        assert result["success"] is True
        assert result["total_steps"] == 0
        mock_commit.assert_called_once()
        # send_step_message should NOT be called when there are no steps
