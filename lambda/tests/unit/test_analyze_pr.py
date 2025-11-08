"""
Unit tests for analyze-pr Lambda function.

Tests cover:
- Request/response schema validation
- GitHub API integration (PR fetch, files, comments)
- File change parsing and check determination
- SQS job queueing
- End-to-end handler orchestration
- Error handling
"""

import json
from unittest.mock import Mock, patch, MagicMock
import pytest
import sys
import os
import importlib.util
from botocore.exceptions import ClientError

# Load the analyze-pr handler module with unique name to avoid conflicts
handler_path = os.path.join(os.path.dirname(__file__), '../../analyze-pr/handler.py')
spec = importlib.util.spec_from_file_location("analyze_pr_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['analyze_pr_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import from the loaded module
handler = handler_module.handler
CheckType = handler_module.CheckType
AnalyzePrRequest = handler_module.AnalyzePrRequest
AnalyzePrResponse = handler_module.AnalyzePrResponse
CheckJob = handler_module.CheckJob
GitHubPullRequest = handler_module.GitHubPullRequest
GitHubFile = handler_module.GitHubFile
get_github_token = handler_module.get_github_token
fetch_pull_request = handler_module.fetch_pull_request
fetch_pr_files = handler_module.fetch_pr_files
post_pr_comment = handler_module.post_pr_comment
parse_changed_files = handler_module.parse_changed_files
queue_check_jobs = handler_module.queue_check_jobs


class TestSchemas:
    """Test Pydantic schema validation."""

    def test_analyze_pr_request_valid(self):
        """Test valid request schema."""
        # Arrange & Act
        request = AnalyzePrRequest(
            pr_number=123,
            repository="owner/repo"
        )

        # Assert
        assert request.pr_number == 123
        assert request.repository == "owner/repo"

    def test_analyze_pr_request_invalid_pr_number(self):
        """Test request with invalid PR number."""
        # Act & Assert
        with pytest.raises(ValueError):
            AnalyzePrRequest(pr_number=-1, repository="owner/repo")

    def test_analyze_pr_request_invalid_repository_format(self):
        """Test request with invalid repository format."""
        # Act & Assert
        with pytest.raises(ValueError):
            AnalyzePrRequest(pr_number=123, repository="invalid")

        with pytest.raises(ValueError):
            AnalyzePrRequest(pr_number=123, repository="owner/repo/extra")

    def test_analyze_pr_response_valid(self):
        """Test valid response schema."""
        # Arrange & Act
        response = AnalyzePrResponse(
            message="Success",
            pr_number=123,
            checks_queued=3
        )

        # Assert
        assert response.message == "Success"
        assert response.pr_number == 123
        assert response.checks_queued == 3

    def test_check_job_valid(self):
        """Test valid check job schema."""
        # Arrange & Act
        job = CheckJob(
            check_type=CheckType.ADR_COMPLIANCE,
            pr_number=123,
            repository="owner/repo",
            changed_files=["lambda/handler/handler.py"]
        )

        # Assert
        assert job.check_type == CheckType.ADR_COMPLIANCE
        assert job.pr_number == 123
        assert len(job.changed_files) == 1


class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM."""

    @patch('analyze_pr_handler.ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test successful token retrieval."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {
                "Value": "ghp_testtoken123"
            }
        }

        # Act
        token = get_github_token()

        # Assert
        assert token == "ghp_testtoken123"
        mock_ssm.get_parameter.assert_called_once()
        call_kwargs = mock_ssm.get_parameter.call_args[1]
        assert call_kwargs["WithDecryption"] is True

    @patch('analyze_pr_handler.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test token not found in SSM."""
        # Arrange
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            get_github_token()


class TestFetchPullRequest:
    """Test fetching PR details from GitHub API."""

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pull_request_success(self, mock_get):
        """Test successful PR fetch."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "number": 123,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/123",
            "base": {"ref": "main"},
            "head": {"ref": "feature-branch"}
        }
        mock_get.return_value = mock_response

        # Act
        pr = fetch_pull_request("owner/repo", 123, "test-token")

        # Assert
        assert pr.number == 123
        assert pr.title == "Test PR"
        assert pr.html_url == "https://github.com/owner/repo/pull/123"
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args[1]
        assert "Bearer test-token" in call_kwargs["headers"]["Authorization"]

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pull_request_api_error(self, mock_get):
        """Test PR fetch with API error."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("Not Found")
        mock_get.return_value = mock_response

        # Act & Assert
        with pytest.raises(Exception):
            fetch_pull_request("owner/repo", 123, "test-token")


class TestFetchPrFiles:
    """Test fetching PR file changes from GitHub API."""

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pr_files_success(self, mock_get):
        """Test successful file fetch."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "filename": "lambda/test-handler/handler.py",
                "status": "modified",
                "additions": 10,
                "deletions": 5,
                "changes": 15
            },
            {
                "filename": "terraform/main.tf",
                "status": "added",
                "additions": 50,
                "deletions": 0,
                "changes": 50
            }
        ]
        mock_get.return_value = mock_response

        # Act
        files = fetch_pr_files("owner/repo", 123, "test-token")

        # Assert
        assert len(files) == 2
        assert files[0].filename == "lambda/test-handler/handler.py"
        assert files[0].status == "modified"
        assert files[1].filename == "terraform/main.tf"
        assert files[1].status == "added"

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pr_files_empty(self, mock_get):
        """Test file fetch with no changes."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        # Act
        files = fetch_pr_files("owner/repo", 123, "test-token")

        # Assert
        assert len(files) == 0


class TestPostPrComment:
    """Test posting comments to PR."""

    @patch('analyze_pr_handler.requests.post')
    def test_post_pr_comment_success(self, mock_post):
        """Test successful comment posting."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 456, "body": "Test comment"}
        mock_post.return_value = mock_response

        # Act
        result = post_pr_comment("owner/repo", 123, "Test comment", "test-token")

        # Assert
        assert result["id"] == 456
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["body"] == "Test comment"

    @patch('analyze_pr_handler.requests.post')
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


class TestParseChangedFiles:
    """Test file change parsing and check determination."""

    def test_parse_changed_files_lambda_handler(self):
        """Test parsing Lambda handler changes."""
        # Arrange
        files = [
            GitHubFile(
                filename="lambda/test-handler/handler.py",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert "lambda/test-handler/handler.py" in result["changed_files"]
        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.BREAKING_CHANGES.value in result["checks_to_run"]
        assert CheckType.ARCHITECTURAL_DUPLICATION.value in result["checks_to_run"]

    def test_parse_changed_files_terraform(self):
        """Test parsing Terraform changes."""
        # Arrange
        files = [
            GitHubFile(
                filename="terraform/main.tf",
                status="modified",
                additions=20,
                deletions=10,
                changes=30
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.README_FRESHNESS.value in result["checks_to_run"]

    def test_parse_changed_files_new_lambda_handler(self):
        """Test parsing new Lambda handler."""
        # Arrange
        files = [
            GitHubFile(
                filename="lambda/new-handler/handler.py",
                status="added",
                additions=100,
                deletions=0,
                changes=100
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.TEST_COVERAGE.value in result["checks_to_run"]
        assert CheckType.ARCHITECTURAL_DUPLICATION.value in result["checks_to_run"]

    def test_parse_changed_files_test_files_excluded(self):
        """Test that test files don't trigger checks."""
        # Arrange
        files = [
            GitHubFile(
                filename="lambda/tests/unit/test_handler.py",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.ADR_COMPLIANCE.value not in result["checks_to_run"]
        assert CheckType.ARCHITECTURAL_DUPLICATION.value not in result["checks_to_run"]

    def test_parse_changed_files_documentation(self):
        """Test parsing documentation changes."""
        # Arrange
        files = [
            GitHubFile(
                filename="docs/architecture.md",
                status="modified",
                additions=5,
                deletions=2,
                changes=7
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.README_FRESHNESS.value in result["checks_to_run"]

    def test_parse_changed_files_no_checks(self):
        """Test parsing files that don't trigger checks."""
        # Arrange
        files = [
            GitHubFile(
                filename="README.md",
                status="modified",
                additions=5,
                deletions=2,
                changes=7
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert len(result["checks_to_run"]) == 0


class TestQueueCheckJobs:
    """Test queueing check jobs to SQS."""

    @patch('analyze_pr_handler.ssm_client')
    @patch('analyze_pr_handler.sqs_client')
    def test_queue_check_jobs_success(self, mock_sqs, mock_ssm):
        """Test successful job queueing."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo"}
        }
        mock_sqs.send_message.return_value = {"MessageId": "test-id"}

        checks = [CheckType.ADR_COMPLIANCE.value, CheckType.TEST_COVERAGE.value]
        changed_files = ["lambda/handler/handler.py"]

        # Act
        jobs_queued = queue_check_jobs("owner/repo", 123, checks, changed_files)

        # Assert
        assert jobs_queued == 2
        assert mock_sqs.send_message.call_count == 2

        # Verify message format
        first_call_kwargs = mock_sqs.send_message.call_args_list[0][1]
        assert "MessageBody" in first_call_kwargs
        assert "MessageGroupId" in first_call_kwargs
        assert "MessageDeduplicationId" in first_call_kwargs
        assert "pr-owner-repo-123" in first_call_kwargs["MessageGroupId"]

    @patch('analyze_pr_handler.ssm_client')
    @patch('analyze_pr_handler.sqs_client')
    def test_queue_check_jobs_sqs_error(self, mock_sqs, mock_ssm):
        """Test job queueing with SQS error."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo"}
        }
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist"}}, "SendMessage"
        )

        checks = [CheckType.ADR_COMPLIANCE.value]
        changed_files = ["lambda/handler/handler.py"]

        # Act & Assert
        with pytest.raises(ClientError):
            queue_check_jobs("owner/repo", 123, checks, changed_files)


class TestHandler:
    """Test end-to-end handler orchestration."""

    @patch('analyze_pr_handler.queue_check_jobs')
    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.fetch_pr_files')
    @patch('analyze_pr_handler.fetch_pull_request')
    @patch('analyze_pr_handler.get_github_token')
    def test_handler_success(
        self,
        mock_get_token,
        mock_fetch_pr,
        mock_fetch_files,
        mock_post_comment,
        mock_queue_jobs
    ):
        """Test successful PR analysis."""
        # Arrange
        event = {
            "pr_number": 123,
            "repository": "owner/repo"
        }

        mock_get_token.return_value = "test-token"
        mock_fetch_pr.return_value = GitHubPullRequest(
            number=123,
            title="Test PR",
            html_url="https://github.com/owner/repo/pull/123",
            base={"ref": "main"},
            head={"ref": "feature"}
        )
        mock_fetch_files.return_value = [
            GitHubFile(
                filename="lambda/handler/handler.py",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]
        mock_post_comment.return_value = {"id": 456}
        mock_queue_jobs.return_value = 3

        # Act
        result = handler(event, None)

        # Assert
        assert result["pr_number"] == 123
        assert result["checks_queued"] == 3
        assert "Analysis started" in result["message"]

        mock_get_token.assert_called_once()
        mock_fetch_pr.assert_called_once()
        mock_fetch_files.assert_called_once()
        assert mock_post_comment.call_count == 1
        mock_queue_jobs.assert_called_once()

    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.fetch_pr_files')
    @patch('analyze_pr_handler.fetch_pull_request')
    @patch('analyze_pr_handler.get_github_token')
    def test_handler_no_checks_needed(
        self,
        mock_get_token,
        mock_fetch_pr,
        mock_fetch_files,
        mock_post_comment
    ):
        """Test PR with no relevant changes."""
        # Arrange
        event = {
            "pr_number": 123,
            "repository": "owner/repo"
        }

        mock_get_token.return_value = "test-token"
        mock_fetch_pr.return_value = GitHubPullRequest(
            number=123,
            title="Test PR",
            html_url="https://github.com/owner/repo/pull/123",
            base={"ref": "main"},
            head={"ref": "feature"}
        )
        mock_fetch_files.return_value = [
            GitHubFile(
                filename=".gitignore",
                status="modified",
                additions=1,
                deletions=0,
                changes=1
            )
        ]
        mock_post_comment.return_value = {"id": 456}

        # Act
        result = handler(event, None)

        # Assert
        assert result["pr_number"] == 123
        assert result["checks_queued"] == 0
        assert "No checks needed" in result["message"]

    def test_handler_invalid_request(self):
        """Test handler with invalid request."""
        # Arrange
        event = {
            "pr_number": -1,
            "repository": "invalid"
        }

        # Act & Assert
        with pytest.raises(Exception):
            handler(event, None)

    @patch('analyze_pr_handler.get_github_token')
    def test_handler_github_api_error(self, mock_get_token):
        """Test handler with GitHub API error."""
        # Arrange
        event = {
            "pr_number": 123,
            "repository": "owner/repo"
        }

        mock_get_token.side_effect = Exception("GitHub API error")

        # Act & Assert
        with pytest.raises(Exception):
            handler(event, None)
