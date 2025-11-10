"""
Unit tests for generate-code Lambda handler.

Tests cover:
- GitHub webhook signature validation
- Label filtering (action=labeled, label=approved-for-generation)
- Branch name generation (kebab-case)
- GitHub API branch creation
- Error handling and edge cases
"""

import hashlib
import hmac
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

# Add generate-code directory to Python path so module imports work
generate_code_dir = os.path.join(os.path.dirname(__file__), '../../generate-code')
sys.path.insert(0, os.path.abspath(generate_code_dir))

# Load the generate-code handler module
handler_path = os.path.join(generate_code_dir, 'handler.py')
spec = importlib.util.spec_from_file_location("generate_code_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['generate_code_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import modules for functions that moved
import github_api
import utils
import step_executor
from models import ExecutionPlan, PlanStep, StepExecutionMessage

# Import functions from appropriate modules
get_github_token = github_api.get_github_token
get_webhook_secret = github_api.get_webhook_secret
verify_webhook_signature = utils.verify_webhook_signature
to_kebab_case = utils.to_kebab_case
generate_branch_name = utils.generate_branch_name
create_github_branch = github_api.create_branch
handler = handler_module.handler


class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM."""

    @mock_aws()
    @patch('github_api.ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test: Successfully retrieve GitHub token from SSM"""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ghp_test_token_12345"}
        }

        # Act
        token = get_github_token()

        # Assert
        assert token == "ghp_test_token_12345"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/dev/outcome-ops-ai-assist/github/token",
            WithDecryption=True
        )

    @mock_aws()
    @patch('github_api.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test: SSM parameter not found"""
        # Arrange
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "get_parameter"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            get_github_token()


class TestGetWebhookSecret:
    """Test webhook secret retrieval from SSM."""

    @mock_aws()
    @patch('github_api.ssm_client')
    def test_get_webhook_secret_success(self, mock_ssm):
        """Test: Successfully retrieve webhook secret from SSM"""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "test_webhook_secret_12345"}
        }

        # Act
        secret = get_webhook_secret()

        # Assert
        assert secret == "test_webhook_secret_12345"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/dev/outcome-ops-ai-assist/github/webhook-secret",
            WithDecryption=True
        )


class TestVerifyWebhookSignature:
    """Test GitHub webhook signature validation."""

    def test_verify_webhook_signature_valid(self):
        """Test: Valid webhook signature"""
        # Arrange
        payload = '{"action":"labeled"}'
        secret = "test_secret"

        # Compute expected signature
        expected_signature = hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        signature_header = f"sha256={expected_signature}"

        # Act
        is_valid = verify_webhook_signature(payload, signature_header, secret)

        # Assert
        assert is_valid is True

    def test_verify_webhook_signature_invalid(self):
        """Test: Invalid webhook signature"""
        # Arrange
        payload = '{"action":"labeled"}'
        secret = "test_secret"
        signature_header = "sha256=invalid_signature"

        # Act
        is_valid = verify_webhook_signature(payload, signature_header, secret)

        # Assert
        assert is_valid is False

    def test_verify_webhook_signature_missing_header(self):
        """Test: Missing signature header"""
        # Arrange
        payload = '{"action":"labeled"}'
        secret = "test_secret"

        # Act
        is_valid = verify_webhook_signature(payload, "", secret)

        # Assert
        assert is_valid is False

    def test_verify_webhook_signature_wrong_format(self):
        """Test: Wrong signature format (not sha256=)"""
        # Arrange
        payload = '{"action":"labeled"}'
        secret = "test_secret"
        signature_header = "invalid_format"

        # Act
        is_valid = verify_webhook_signature(payload, signature_header, secret)

        # Assert
        assert is_valid is False


class TestToKebabCase:
    """Test kebab-case conversion."""

    def test_to_kebab_case_simple(self):
        """Test: Simple text conversion"""
        # Arrange
        text = "Add User Authentication"

        # Act
        result = to_kebab_case(text)

        # Assert
        assert result == "add-user-authentication"

    def test_to_kebab_case_special_chars(self):
        """Test: Text with special characters"""
        # Arrange
        text = "Fix bug: API returns 500 error!"

        # Act
        result = to_kebab_case(text)

        # Assert
        assert result == "fix-bug-api-returns-500-error"

    def test_to_kebab_case_long_text(self):
        """Test: Long text gets truncated to 50 chars"""
        # Arrange
        text = "This is a very long issue title that should be truncated to fifty characters maximum"

        # Act
        result = to_kebab_case(text)

        # Assert
        assert len(result) <= 50
        assert result.startswith("this-is-a-very-long-issue-title")

    def test_to_kebab_case_leading_trailing_hyphens(self):
        """Test: Remove leading/trailing hyphens"""
        # Arrange
        text = "---leading and trailing---"

        # Act
        result = to_kebab_case(text)

        # Assert
        assert result == "leading-and-trailing"


class TestGenerateBranchName:
    """Test branch name generation."""

    def test_generate_branch_name_simple(self):
        """Test: Simple branch name"""
        # Arrange
        issue_number = 123
        title = "Add User Authentication"

        # Act
        result = generate_branch_name(issue_number, title)

        # Assert
        assert result == "123-add-user-authentication"

    def test_generate_branch_name_with_special_chars(self):
        """Test: Branch name with special chars in title"""
        # Arrange
        issue_number = 456
        title = "Fix: API returns 500 error!"

        # Act
        result = generate_branch_name(issue_number, title)

        # Assert
        assert result == "456-fix-api-returns-500-error"


class TestCreateGitHubBranch:
    """Test GitHub branch creation."""

    @patch('github_api.requests.get')
    @patch('github_api.requests.post')
    def test_create_github_branch_success(self, mock_post, mock_get):
        """Test: Successfully create GitHub branch"""
        # Arrange
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "object": {"sha": "abc123def456"}
        }
        mock_get_response.raise_for_status = Mock()
        mock_get.return_value = mock_get_response

        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.raise_for_status = Mock()
        mock_post.return_value = mock_post_response

        # Act
        result = create_github_branch(
            repo_full_name="owner/repo",
            branch_name="123-test-branch",
            base_branch="main",
            github_token="ghp_test_token"
        )

        # Assert
        assert result["success"] is True
        assert result["branch_name"] == "123-test-branch"
        assert result["sha"] == "abc123def456"
        mock_get.assert_called_once()
        mock_post.assert_called_once()

    @patch('github_api.requests.get')
    @patch('github_api.requests.post')
    def test_create_github_branch_already_exists(self, mock_post, mock_get):
        """Test: Branch already exists (422 error)"""
        # Arrange
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "object": {"sha": "abc123def456"}
        }
        mock_get_response.raise_for_status = Mock()
        mock_get.return_value = mock_get_response

        mock_post_response = Mock()
        mock_post_response.status_code = 422
        mock_post_response.json.return_value = {
            "message": "Reference already exists"
        }
        mock_post.return_value = mock_post_response

        # Act
        result = create_github_branch(
            repo_full_name="owner/repo",
            branch_name="123-existing-branch",
            base_branch="main",
            github_token="ghp_test_token"
        )

        # Assert
        assert result["success"] is True
        assert result["already_exists"] is True

    @patch('github_api.requests.get')
    def test_create_github_branch_get_ref_fails(self, mock_get):
        """Test: Failed to get base branch ref"""
        # Arrange
        mock_get.side_effect = requests.exceptions.RequestException("API Error")

        # Act & Assert
        with pytest.raises(Exception, match="GitHub API error"):
            create_github_branch(
                repo_full_name="owner/repo",
                branch_name="123-test-branch",
                base_branch="main",
                github_token="ghp_test_token"
            )


class TestHandler:
    """Test main Lambda handler."""

    @patch('generate_code_handler.send_plan_generation_message')
    @patch('generate_code_handler.verify_webhook_signature')
    @patch('generate_code_handler.get_webhook_secret')
    @patch('generate_code_handler.get_github_token')
    @patch('generate_code_handler.create_branch')
    def test_handler_success(self, mock_create_branch, mock_get_token, mock_get_secret, mock_verify_sig, mock_send_message):
        """Test: Successful webhook processing"""
        # Arrange
        mock_get_secret.return_value = "test_webhook_secret"
        mock_verify_sig.return_value = True  # Bypass signature validation
        mock_get_token.return_value = "ghp_test_token"
        mock_create_branch.return_value = {
            "success": True,
            "branch_name": "123-add-feature",
            "sha": "abc123"
        }

        # Mock the SQS send message (no return value needed)
        mock_send_message.return_value = {
            "MessageId": "test-message-id"
        }

        webhook_payload = {
            "action": "labeled",
            "label": {"name": "approved-for-generation", "color": "00ff00"},
            "issue": {
                "number": 123,
                "title": "Add Feature",
                "body": "Issue description",
                "html_url": "https://github.com/owner/repo/issues/123",
                "state": "open"
            },
            "repository": {
                "name": "repo",
                "full_name": "owner/repo",
                "owner": {"login": "owner"},
                "default_branch": "main"
            }
        }

        payload_str = json.dumps(webhook_payload)

        event = {
            "body": payload_str,
            "headers": {
                "x-hub-signature-256": "sha256=fake_signature"
            },
            "requestContext": {}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["message"] == "Code generation started"
        assert body["issue_number"] == 123
        assert body["branch_name"] == "123-add-feature"
        mock_send_message.assert_called_once()

    @patch('generate_code_handler.get_webhook_secret')
    def test_handler_invalid_signature(self, mock_get_secret):
        """Test: Invalid webhook signature"""
        # Arrange
        mock_get_secret.return_value = "test_secret"

        webhook_payload = {"action": "labeled"}
        event = {
            "body": json.dumps(webhook_payload),
            "headers": {
                "x-hub-signature-256": "sha256=invalid_signature"
            },
            "requestContext": {}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["message"] == "Invalid signature"

    @patch('generate_code_handler.verify_webhook_signature')
    @patch('generate_code_handler.get_webhook_secret')
    def test_handler_wrong_action(self, mock_get_secret, mock_verify_sig):
        """Test: Wrong action (not 'labeled')"""
        # Arrange
        mock_get_secret.return_value = "test_webhook_secret"
        mock_verify_sig.return_value = True  # Bypass signature validation

        webhook_payload = {
            "action": "unlabeled",  # Wrong action
            "label": {"name": "approved-for-generation", "color": "00ff00"},
            "issue": {"number": 123, "title": "Test", "html_url": "url", "state": "open"},
            "repository": {
                "name": "repo",
                "full_name": "owner/repo",
                "owner": {"login": "owner"},
                "default_branch": "main"
            }
        }

        payload_str = json.dumps(webhook_payload)

        event = {
            "body": payload_str,
            "headers": {
                "x-hub-signature-256": "sha256=fake_signature"
            },
            "requestContext": {}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["message"] == "Event ignored"

    @patch('generate_code_handler.verify_webhook_signature')
    @patch('generate_code_handler.get_webhook_secret')
    def test_handler_wrong_label(self, mock_get_secret, mock_verify_sig):
        """Test: Wrong label (not 'approved-for-generation')"""
        # Arrange
        mock_get_secret.return_value = "test_webhook_secret"
        mock_verify_sig.return_value = True  # Bypass signature validation

        webhook_payload = {
            "action": "labeled",
            "label": {"name": "bug", "color": "ff0000"},  # Wrong label
            "issue": {"number": 123, "title": "Test", "html_url": "url", "state": "open"},
            "repository": {
                "name": "repo",
                "full_name": "owner/repo",
                "owner": {"login": "owner"},
                "default_branch": "main"
            }
        }

        payload_str = json.dumps(webhook_payload)

        event = {
            "body": payload_str,
            "headers": {
                "x-hub-signature-256": "sha256=fake_signature"
            },
            "requestContext": {}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["message"] == "Event ignored"

    @patch('generate_code_handler.verify_webhook_signature')
    @patch('generate_code_handler.get_webhook_secret')
    @patch('generate_code_handler.get_github_token')
    @patch('generate_code_handler.create_branch')
    def test_handler_branch_creation_fails(
        self, mock_create_branch, mock_get_token, mock_get_secret, mock_verify_sig
    ):
        """Test: Branch creation fails"""
        # Arrange
        mock_get_secret.return_value = "test_webhook_secret"
        mock_verify_sig.return_value = True  # Bypass signature validation
        mock_get_token.return_value = "ghp_test_token"
        mock_create_branch.return_value = {
            "success": False,
            "error": "API rate limit exceeded"
        }

        webhook_payload = {
            "action": "labeled",
            "label": {"name": "approved-for-generation", "color": "00ff00"},
            "issue": {
                "number": 123,
                "title": "Add Feature",
                "body": "Issue description",
                "html_url": "https://github.com/owner/repo/issues/123",
                "state": "open"
            },
            "repository": {
                "name": "repo",
                "full_name": "owner/repo",
                "owner": {"login": "owner"},
                "default_branch": "main"
            }
        }

        payload_str = json.dumps(webhook_payload)

        event = {
            "body": payload_str,
            "headers": {
                "x-hub-signature-256": "sha256=fake_signature"
            },
            "requestContext": {}
        }

        # Act
        response = handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["message"] == "Failed to create branch"
        assert "error" in body


class TestCodeGenerationCompletionEvent:
    """Verify EventBridge event emission for completed code generation."""

    @patch.object(step_executor, "events_client")
    def test_publish_event_payload(self, mock_events):
        """Ensure detail payload contains repo, branch, and plan info."""
        plan = ExecutionPlan(
            issue_number=6,
            issue_title="Add handler",
            issue_description="Test",
            branch_name="feature-branch",
            repo_full_name="bcarpio/outcome-ops-ai-assist",
            steps=[PlanStep(step_number=1, title="Step 1", description="Desc")]
        )
        step_message = StepExecutionMessage(
            issue_number=6,
            issue_title="Add handler",
            issue_description="Test",
            repo_full_name="bcarpio/outcome-ops-ai-assist",
            branch_name="feature-branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        step_executor.publish_code_generation_completed_event(
            plan=plan,
            step_message=step_message,
            pr_url="https://github.com/bcarpio/outcome-ops-ai-assist/pull/123",
            pr_number=123,
            plan_file_path="issues/plan.md",
            commit_sha="abc123"
        )

        mock_events.put_events.assert_called_once()
        entry = mock_events.put_events.call_args.kwargs["Entries"][0]
        assert entry["Source"] == "outcomeops.generate-code"
        detail = json.loads(entry["Detail"])
        assert detail["branchName"] == "feature-branch"
        assert detail["commitSha"] == "abc123"
        assert detail["planFile"] == "issues/plan.md"
        assert detail["environment"] == "dev"
        assert detail["appName"] == "outcome-ops-ai-assist"

    @patch.object(step_executor, "publish_code_generation_completed_event")
    @patch.object(step_executor, "get_branch_head_sha")
    def test_finalize_triggers_event(
        self,
        mock_head_sha,
        mock_publish
    ):
        """finalize_and_publish_event emits event without creating PR (deferred to run-tests)."""
        plan = ExecutionPlan(
            issue_number=6,
            issue_title="Add handler",
            issue_description="Test",
            branch_name="feature-branch",
            repo_full_name="bcarpio/outcome-ops-ai-assist",
            steps=[PlanStep(step_number=1, title="Step", description="desc")]
        )
        step_message = StepExecutionMessage(
            issue_number=6,
            issue_title="Add handler",
            issue_description="Test",
            repo_full_name="bcarpio/outcome-ops-ai-assist",
            branch_name="feature-branch",
            current_step=1,
            total_steps=1,
            base_branch="main"
        )

        mock_head_sha.return_value = "def456"

        step_executor.finalize_and_publish_event(plan, step_message, github_token="token")

        mock_publish.assert_called_once()
        _, kwargs = mock_publish.call_args
        assert kwargs["pr_number"] is None  # PR creation deferred to run-tests
        assert kwargs["pr_url"] is None  # PR creation deferred to run-tests
        assert kwargs["commit_sha"] == "def456"
