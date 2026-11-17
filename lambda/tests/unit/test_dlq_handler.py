# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the DLQ handler.

Tests the dead letter queue handler that processes failed code generation steps.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add generate-code directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "generate-code"))

from dlq_handler import (
    handler,
    handle_dlq_message,
    get_open_pr_for_branch,
    add_issue_comment,
)
from models import StepExecutionMessage


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def sample_message():
    """Create a sample failed step message."""
    return StepExecutionMessage(
        action="execute_step",
        issue_number=123,
        issue_title="Add new feature",
        issue_description="Implement the feature",
        repo_full_name="owner/repo",
        branch_name="feature/issue-123-add-new-feature",
        current_step=3,
        total_steps=5,
        base_branch="main"
    )


@pytest.fixture
def sqs_event(sample_message):
    """Create a sample SQS event with the failed message."""
    return {
        "Records": [
            {
                "body": sample_message.model_dump_json(),
                "messageId": "test-message-id",
                "receiptHandle": "test-receipt-handle"
            }
        ]
    }


# ============================================================================
# Test get_open_pr_for_branch
# ============================================================================


class TestGetOpenPrForBranch:
    """Test get_open_pr_for_branch function."""

    @patch("dlq_handler.requests.get")
    @patch("dlq_handler.get_headers")
    def test_returns_pr_when_exists(self, mock_headers, mock_get):
        """Test: Returns PR data when a PR exists for the branch."""
        # Arrange
        mock_headers.return_value = {"Authorization": "token test"}
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"html_url": "https://github.com/owner/repo/pull/1", "number": 1}
        ]
        mock_get.return_value = mock_response

        # Act
        result = get_open_pr_for_branch(
            repo_full_name="owner/repo",
            branch_name="feature/test",
            github_token="test-token"
        )

        # Assert
        assert result is not None
        assert result["number"] == 1

    @patch("dlq_handler.requests.get")
    @patch("dlq_handler.get_headers")
    def test_returns_none_when_no_pr(self, mock_headers, mock_get):
        """Test: Returns None when no PR exists for the branch."""
        # Arrange
        mock_headers.return_value = {"Authorization": "token test"}
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        # Act
        result = get_open_pr_for_branch(
            repo_full_name="owner/repo",
            branch_name="feature/test",
            github_token="test-token"
        )

        # Assert
        assert result is None

    @patch("dlq_handler.requests.get")
    @patch("dlq_handler.get_headers")
    def test_returns_none_on_error(self, mock_headers, mock_get):
        """Test: Returns None when API call fails."""
        # Arrange
        mock_headers.return_value = {"Authorization": "token test"}
        mock_get.side_effect = Exception("API error")

        # Act
        result = get_open_pr_for_branch(
            repo_full_name="owner/repo",
            branch_name="feature/test",
            github_token="test-token"
        )

        # Assert
        assert result is None


# ============================================================================
# Test add_issue_comment
# ============================================================================


class TestAddIssueComment:
    """Test add_issue_comment function."""

    @patch("dlq_handler.requests.post")
    @patch("dlq_handler.get_headers")
    def test_returns_true_on_success(self, mock_headers, mock_post):
        """Test: Returns True when comment is added successfully."""
        # Arrange
        mock_headers.return_value = {"Authorization": "token test"}
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        # Act
        result = add_issue_comment(
            repo_full_name="owner/repo",
            issue_number=123,
            body="Test comment",
            github_token="test-token"
        )

        # Assert
        assert result is True
        mock_post.assert_called_once()

    @patch("dlq_handler.requests.post")
    @patch("dlq_handler.get_headers")
    def test_returns_false_on_error(self, mock_headers, mock_post):
        """Test: Returns False when API call fails."""
        # Arrange
        mock_headers.return_value = {"Authorization": "token test"}
        mock_post.side_effect = Exception("API error")

        # Act
        result = add_issue_comment(
            repo_full_name="owner/repo",
            issue_number=123,
            body="Test comment",
            github_token="test-token"
        )

        # Assert
        assert result is False


# ============================================================================
# Test handle_dlq_message
# ============================================================================


class TestHandleDlqMessage:
    """Test handle_dlq_message function."""

    @patch("dlq_handler.add_issue_comment")
    @patch("dlq_handler.create_pull_request")
    @patch("dlq_handler.get_open_pr_for_branch")
    @patch("dlq_handler.get_branch_head_sha")
    @patch("dlq_handler.get_github_token")
    def test_creates_pr_when_branch_exists(
        self,
        mock_token,
        mock_branch_sha,
        mock_get_pr,
        mock_create_pr,
        mock_comment,
        sample_message
    ):
        """Test: Creates PR when branch exists and no PR exists."""
        # Arrange
        mock_token.return_value = "test-token"
        mock_branch_sha.return_value = "abc123"
        mock_get_pr.return_value = None
        mock_create_pr.return_value = {
            "html_url": "https://github.com/owner/repo/pull/42",
            "number": 42
        }
        mock_comment.return_value = True

        # Act
        result = handle_dlq_message(sample_message)

        # Assert
        assert result["success"] is True
        assert result["reason"] == "pr_created"
        assert result["pr_number"] == 42
        mock_create_pr.assert_called_once()

    @patch("dlq_handler.log_admin_action")
    @patch("dlq_handler.add_issue_comment")
    @patch("dlq_handler.create_pull_request")
    @patch("dlq_handler.get_open_pr_for_branch")
    @patch("dlq_handler.get_branch_head_sha")
    @patch("dlq_handler.get_github_token")
    def test_emits_dlq_salvage_pr_audit(
        self,
        mock_token,
        mock_branch_sha,
        mock_get_pr,
        mock_create_pr,
        mock_comment,
        mock_audit,
        sample_message,
    ):
        """plan/audit-completeness — successful salvage PR
        creation emits ACTION_CODE_GEN_DLQ_SALVAGE_PR_CREATED."""
        mock_token.return_value = "test-token"
        mock_branch_sha.return_value = "abc123"
        mock_get_pr.return_value = None
        mock_create_pr.return_value = {
            "html_url": "https://github.com/owner/repo/pull/99",
            "number": 99,
        }
        mock_comment.return_value = True

        with patch.dict(os.environ, {"AUDIT_TABLE": "test-audit"}):
            handle_dlq_message(sample_message)

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "CODE_GEN_DLQ_SALVAGE_PR_CREATED"
        d = kwargs["details"]
        assert d["repo"] == sample_message.repo_full_name
        assert d["salvage_pr_url"] == "https://github.com/owner/repo/pull/99"
        assert d["salvage_pr_number"] == 99

    @patch("dlq_handler.get_branch_head_sha")
    @patch("dlq_handler.get_github_token")
    def test_returns_branch_not_found_when_no_branch(
        self,
        mock_token,
        mock_branch_sha,
        sample_message
    ):
        """Test: Returns branch_not_found when branch doesn't exist."""
        # Arrange
        mock_token.return_value = "test-token"
        mock_branch_sha.return_value = None

        # Act
        result = handle_dlq_message(sample_message)

        # Assert
        assert result["success"] is False
        assert result["reason"] == "branch_not_found"

    @patch("dlq_handler.get_open_pr_for_branch")
    @patch("dlq_handler.get_branch_head_sha")
    @patch("dlq_handler.get_github_token")
    def test_returns_pr_already_exists_when_pr_exists(
        self,
        mock_token,
        mock_branch_sha,
        mock_get_pr,
        sample_message
    ):
        """Test: Returns pr_already_exists when PR already exists."""
        # Arrange
        mock_token.return_value = "test-token"
        mock_branch_sha.return_value = "abc123"
        mock_get_pr.return_value = {
            "html_url": "https://github.com/owner/repo/pull/42",
            "number": 42
        }

        # Act
        result = handle_dlq_message(sample_message)

        # Assert
        assert result["success"] is True
        assert result["reason"] == "pr_already_exists"
        assert result["pr_number"] == 42

    @patch("dlq_handler.create_pull_request")
    @patch("dlq_handler.get_open_pr_for_branch")
    @patch("dlq_handler.get_branch_head_sha")
    @patch("dlq_handler.get_github_token")
    def test_returns_pr_creation_failed_on_error(
        self,
        mock_token,
        mock_branch_sha,
        mock_get_pr,
        mock_create_pr,
        sample_message
    ):
        """Test: Returns pr_creation_failed when PR creation fails."""
        # Arrange
        mock_token.return_value = "test-token"
        mock_branch_sha.return_value = "abc123"
        mock_get_pr.return_value = None
        mock_create_pr.side_effect = Exception("PR creation failed")

        # Act
        result = handle_dlq_message(sample_message)

        # Assert
        assert result["success"] is False
        assert result["reason"] == "pr_creation_failed"


# ============================================================================
# Test handler
# ============================================================================


class TestHandler:
    """Test main Lambda handler."""

    @patch("dlq_handler.handle_dlq_message")
    def test_processes_sqs_records(self, mock_handle, sqs_event):
        """Test: Processes all SQS records in the event."""
        # Arrange
        mock_handle.return_value = {
            "success": True,
            "reason": "pr_created",
            "pr_number": 42
        }

        # Act
        result = handler(sqs_event, None)

        # Assert
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["processed"] == 1
        assert body["successful"] == 1

    @patch("dlq_handler.handle_dlq_message")
    def test_handles_multiple_records(self, mock_handle):
        """Test: Handles multiple records in a single event."""
        # Arrange
        message = StepExecutionMessage(
            action="execute_step",
            issue_number=123,
            issue_title="Test",
            issue_description="Test",
            repo_full_name="owner/repo",
            branch_name="feature/test",
            current_step=1,
            total_steps=3,
            base_branch="main"
        )
        event = {
            "Records": [
                {"body": message.model_dump_json()},
                {"body": message.model_dump_json()},
            ]
        }
        mock_handle.return_value = {"success": True, "reason": "pr_created"}

        # Act
        result = handler(event, None)

        # Assert
        body = json.loads(result["body"])
        assert body["processed"] == 2
        assert body["successful"] == 2

    def test_handles_invalid_message_gracefully(self):
        """Test: Handles invalid message body gracefully."""
        # Arrange
        event = {
            "Records": [
                {"body": "invalid json"}
            ]
        }

        # Act
        result = handler(event, None)

        # Assert
        body = json.loads(result["body"])
        assert body["processed"] == 1
        assert body["successful"] == 0
        assert body["results"][0]["reason"] == "processing_error"
