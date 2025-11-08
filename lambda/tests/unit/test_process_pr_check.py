"""
Unit tests for process-pr-check Lambda handler.

Tests cover:
- Schema validation (Pydantic models)
- SQS message processing
- Check job routing
- GitHub comment posting
- DynamoDB result storage
- End-to-end orchestration
"""

import json
import os
import sys
from datetime import datetime
from unittest.mock import Mock, patch
import importlib.util

import pytest
from pydantic import ValidationError

# Add process-pr-check directory to Python path for check_handlers import
process_pr_check_dir = os.path.join(os.path.dirname(__file__), '../../process-pr-check')
sys.path.insert(0, process_pr_check_dir)

# Load the process-pr-check handler module with unique name
handler_path = os.path.join(process_pr_check_dir, 'handler.py')
spec = importlib.util.spec_from_file_location("process_pr_check_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['process_pr_check_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Clean up sys.path after import
sys.path.remove(process_pr_check_dir)

# Import from the loaded module
handler = handler_module.handler
CheckType = handler_module.CheckType
CheckStatus = handler_module.CheckStatus
CheckJob = handler_module.CheckJob
CheckResult = handler_module.CheckResult
process_check_job = handler_module.process_check_job
format_check_comment = handler_module.format_check_comment
get_github_token = handler_module.get_github_token
post_pr_comment = handler_module.post_pr_comment
store_check_result = handler_module.store_check_result
run_check = handler_module.run_check


class TestSchemas:
    """Test Pydantic schema validation."""

    def test_check_job_valid(self):
        """Test valid CheckJob creation."""
        # Arrange & Act
        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["lambda/hello/handler.py"]
        )

        # Assert
        assert job.check_type == CheckType.ADR_COMPLIANCE
        assert job.pr_number == 123
        assert job.repository == "owner/repo"
        assert job.changed_files == ["lambda/hello/handler.py"]

    def test_check_job_invalid_pr_number(self):
        """Test CheckJob with invalid PR number."""
        # Arrange & Act & Assert
        with pytest.raises(ValidationError):
            CheckJob(
                checkType="ADR_COMPLIANCE",
                pr_number=-1,  # Invalid: must be > 0
                repository="owner/repo",
                changedFiles=[]
            )

    def test_check_result_valid(self):
        """Test valid CheckResult creation."""
        # Arrange & Act
        result = CheckResult(
            PK="PR#123",
            SK="CHECK#adr_compliance",
            checkType="ADR_COMPLIANCE",
            status="PASS",
            message="All files follow ADR standards",
            details=["Handler uses Pydantic schemas"],
            timestamp="2025-01-15T10:00:00Z",
            commentUrl="https://github.com/owner/repo/pull/123#issuecomment-456"
        )

        # Assert
        assert result.PK == "PR#123"
        assert result.SK == "CHECK#adr_compliance"
        assert result.check_type == CheckType.ADR_COMPLIANCE
        assert result.status == CheckStatus.PASS
        assert result.comment_url == "https://github.com/owner/repo/pull/123#issuecomment-456"


class TestFormatCheckComment:
    """Test formatting check results as GitHub comments."""

    def test_format_pass_comment(self):
        """Test formatting a PASS result."""
        # Arrange
        result = CheckResult(
            PK="PR#123",
            SK="CHECK#test_coverage",
            checkType="TEST_COVERAGE",
            status="PASS",
            message="All handlers have test coverage",
            details=["lambda/hello/handler.py: Has test_hello.py"],
            timestamp="2025-01-15T10:00:00Z"
        )

        # Act
        comment = format_check_comment(result)

        # Assert
        assert ":white_check_mark:" in comment
        assert "TEST COVERAGE" in comment
        assert "All handlers have test coverage" in comment
        assert "lambda/hello/handler.py" in comment
        assert "2025-01-15T10:00:00Z" in comment

    def test_format_warn_comment(self):
        """Test formatting a WARN result."""
        # Arrange
        result = CheckResult(
            PK="PR#123",
            SK="CHECK#readme_freshness",
            checkType="README_FRESHNESS",
            status="WARN",
            message="README.md not updated",
            details=["Infrastructure files changed but README not updated"],
            timestamp="2025-01-15T10:00:00Z"
        )

        # Act
        comment = format_check_comment(result)

        # Assert
        assert ":warning:" in comment
        assert "README FRESHNESS" in comment
        assert "README.md not updated" in comment


class TestGetGitHubToken:
    """Test fetching GitHub token from SSM."""

    @patch('process_pr_check_handler.ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test successful token retrieval."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "test-token-123"}
        }

        # Act
        token = get_github_token()

        # Assert
        assert token == "test-token-123"
        mock_ssm.get_parameter.assert_called_once()

    @patch('process_pr_check_handler.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test token not found in SSM."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": ""}
        }

        # Act & Assert
        with pytest.raises(Exception, match="GitHub token not found"):
            get_github_token()


class TestPostPrComment:
    """Test posting comments to GitHub PR."""

    @patch('process_pr_check_handler.requests.post')
    def test_post_pr_comment_success(self, mock_post):
        """Test successful comment posting."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": 456,
            "html_url": "https://github.com/owner/repo/pull/123#issuecomment-456",
            "body": "Test comment"
        }
        mock_post.return_value = mock_response

        # Act
        result = post_pr_comment("owner/repo", 123, "Test comment", "test-token")

        # Assert
        assert result["id"] == 456
        assert "issuecomment-456" in result["html_url"]
        mock_post.assert_called_once()

    @patch('process_pr_check_handler.requests.post')
    def test_post_pr_comment_api_error(self, mock_post):
        """Test comment posting with API error."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.raise_for_status.side_effect = Exception("Forbidden")
        mock_post.return_value = mock_response

        # Act & Assert
        with pytest.raises(Exception):
            post_pr_comment("owner/repo", 123, "Test comment", "test-token")


class TestStoreCheckResult:
    """Test storing check results in DynamoDB."""

    @patch('process_pr_check_handler.dynamodb')
    def test_store_check_result_success(self, mock_dynamodb):
        """Test successful result storage."""
        # Arrange
        mock_table = Mock()
        mock_dynamodb.Table.return_value = mock_table

        result = CheckResult(
            PK="PR#123",
            SK="CHECK#adr_compliance",
            checkType="ADR_COMPLIANCE",
            status="PASS",
            message="All files follow ADR standards",
            details=["Handler uses Pydantic schemas"],
            timestamp="2025-01-15T10:00:00Z",
            commentUrl="https://github.com/owner/repo/pull/123#issuecomment-456"
        )

        # Act
        store_check_result(result)

        # Assert
        mock_table.put_item.assert_called_once()
        call_args = mock_table.put_item.call_args[1]
        assert call_args["Item"]["PK"] == "PR#123"
        assert call_args["Item"]["SK"] == "CHECK#adr_compliance"
        assert call_args["Item"]["status"] == "PASS"


class TestRunCheck:
    """Test routing to check handlers."""

    @patch('process_pr_check_handler.check_test_coverage')
    def test_run_check_test_coverage(self, mock_check):
        """Test routing to test coverage check."""
        # Arrange
        job = CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["lambda/hello/handler.py"]
        )

        mock_check.return_value = {
            "checkType": "TEST_COVERAGE",
            "status": "PASS",
            "message": "All handlers have test coverage",
            "details": []
        }

        # Act
        result = run_check(job)

        # Assert
        assert result["status"] == "PASS"
        mock_check.assert_called_once()

    def test_run_check_unknown_type(self):
        """Test routing with unknown check type (should not happen with enum)."""
        # This test verifies that the enum prevents invalid check types
        # If we somehow bypass the enum, we should get an error

        # Arrange - Create job with bypassed enum
        job_dict = {
            "checkType": "INVALID_CHECK",
            "pr_number": 123,
            "repository": "owner/repo",
            "changedFiles": []
        }

        # Act & Assert
        with pytest.raises(ValidationError):
            CheckJob(**job_dict)


class TestProcessCheckJob:
    """Test end-to-end check job processing."""

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler.post_pr_comment')
    @patch('process_pr_check_handler.get_github_token')
    @patch('process_pr_check_handler.run_check')
    def test_process_check_job_success(
        self,
        mock_run_check,
        mock_get_token,
        mock_post_comment,
        mock_store
    ):
        """Test successful check job processing."""
        # Arrange
        job = CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["lambda/hello/handler.py"]
        )

        mock_run_check.return_value = {
            "checkType": "TEST_COVERAGE",
            "status": "PASS",
            "message": "All handlers have test coverage",
            "details": ["lambda/hello/handler.py: Has test_hello.py"]
        }

        mock_get_token.return_value = "test-token"

        mock_post_comment.return_value = {
            "id": 456,
            "html_url": "https://github.com/owner/repo/pull/123#issuecomment-456"
        }

        # Act
        result = process_check_job(job)

        # Assert
        assert result.PK == "PR#123"
        assert result.SK == "CHECK#test_coverage"
        assert result.status == CheckStatus.PASS
        assert result.comment_url == "https://github.com/owner/repo/pull/123#issuecomment-456"

        mock_run_check.assert_called_once()
        mock_get_token.assert_called_once()
        mock_post_comment.assert_called_once()
        assert mock_store.call_count == 2  # Once before comment, once after


class TestHandler:
    """Test main Lambda handler with SQS events."""

    @patch('process_pr_check_handler.process_check_job')
    def test_handler_success(self, mock_process):
        """Test successful SQS message processing."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-123",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": 123,
                        "repository": "owner/repo",
                        "changedFiles": ["lambda/hello/handler.py"]
                    })
                }
            ]
        }

        mock_process.return_value = Mock(spec=CheckResult)

        # Act
        result = handler(event, None)

        # Assert
        assert result["batchItemFailures"] == []
        mock_process.assert_called_once()

    @patch('process_pr_check_handler.process_check_job')
    def test_handler_with_failure(self, mock_process):
        """Test handler with processing failure."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-123",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": 123,
                        "repository": "owner/repo",
                        "changedFiles": ["lambda/hello/handler.py"]
                    })
                }
            ]
        }

        mock_process.side_effect = Exception("Processing failed")

        # Act
        result = handler(event, None)

        # Assert
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-123"

    def test_handler_invalid_message(self):
        """Test handler with invalid SQS message."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-123",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": -1,  # Invalid
                        "repository": "owner/repo",
                        "changedFiles": []
                    })
                }
            ]
        }

        # Act
        result = handler(event, None)

        # Assert - Should be in batch failures
        assert len(result["batchItemFailures"]) == 1

    @patch('process_pr_check_handler.process_check_job')
    def test_handler_multiple_messages(self, mock_process):
        """Test handler with multiple SQS messages."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-1",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": 123,
                        "repository": "owner/repo",
                        "changedFiles": []
                    })
                },
                {
                    "messageId": "msg-2",
                    "body": json.dumps({
                        "checkType": "ADR_COMPLIANCE",
                        "pr_number": 124,
                        "repository": "owner/repo",
                        "changedFiles": []
                    })
                }
            ]
        }

        mock_process.return_value = Mock(spec=CheckResult)

        # Act
        result = handler(event, None)

        # Assert
        assert result["batchItemFailures"] == []
        assert mock_process.call_count == 2
