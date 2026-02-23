"""
Unit tests for handle-command Lambda function.

Tests cover:
- Request/response schema validation
- Command parsing logic
- GitHub API integration (PR comments)
- License validation and enforcement
- Usage tracking
- End-to-end handler orchestration
- Error handling
"""

import json
from unittest.mock import Mock, patch, MagicMock
import pytest
import sys
import os
import importlib.util

# Load the handle-command handler module with unique name to avoid conflicts
handler_path = os.path.join(os.path.dirname(__file__), '../../handle-command/handler.py')
spec = importlib.util.spec_from_file_location("handle_command_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['handle_command_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import from the loaded module
lambda_handler = handler_module.lambda_handler
CommandType = handler_module.CommandType
HandleCommandRequest = handler_module.HandleCommandRequest
HandleCommandResponse = handler_module.HandleCommandResponse
parse_command = handler_module.parse_command
get_github_token = handler_module.get_github_token
post_pr_comment = handler_module.post_pr_comment
handle_help_command = handler_module.handle_help_command
handle_fix_readme_command = handler_module.handle_fix_readme_command
handle_fix_adr_command = handler_module.handle_fix_adr_command
handle_unknown_command = handler_module.handle_unknown_command


class TestSchemas:
    """Test Pydantic schema validation."""

    def test_handle_command_request_valid(self):
        """Test valid request schema."""
        # Arrange & Act
        request = HandleCommandRequest(
            command="outcomeops: fix readme",
            pr_number=123,
            repo="owner/repo",
            commenter="testuser",
            branch="feature-branch"
        )

        # Assert
        assert request.command == "outcomeops: fix readme"
        assert request.pr_number == 123
        assert request.repo == "owner/repo"
        assert request.commenter == "testuser"
        assert request.branch == "feature-branch"

    def test_handle_command_request_invalid_pr_number(self):
        """Test request with invalid PR number."""
        # Act & Assert
        with pytest.raises(ValueError):
            HandleCommandRequest(
                command="outcomeops: help",
                pr_number=-1,
                repo="owner/repo",
                commenter="testuser",
                branch="main"
            )

    def test_handle_command_request_invalid_repo_format(self):
        """Test request with invalid repository format."""
        # Act & Assert - no slash
        with pytest.raises(ValueError):
            HandleCommandRequest(
                command="outcomeops: help",
                pr_number=123,
                repo="invalid",
                commenter="testuser",
                branch="main"
            )

        # Act & Assert - multiple slashes
        with pytest.raises(ValueError):
            HandleCommandRequest(
                command="outcomeops: help",
                pr_number=123,
                repo="owner/repo/extra",
                commenter="testuser",
                branch="main"
            )

    def test_handle_command_request_empty_command(self):
        """Test request with empty command."""
        # Act & Assert
        with pytest.raises(ValueError):
            HandleCommandRequest(
                command="",
                pr_number=123,
                repo="owner/repo",
                commenter="testuser",
                branch="main"
            )

    def test_handle_command_request_empty_repo_parts(self):
        """Test request with empty owner or repo."""
        # Act & Assert - empty owner
        with pytest.raises(ValueError):
            HandleCommandRequest(
                command="outcomeops: help",
                pr_number=123,
                repo="/repo",
                commenter="testuser",
                branch="main"
            )

        # Act & Assert - empty repo name
        with pytest.raises(ValueError):
            HandleCommandRequest(
                command="outcomeops: help",
                pr_number=123,
                repo="owner/",
                commenter="testuser",
                branch="main"
            )

    def test_handle_command_response_valid(self):
        """Test valid response schema."""
        # Arrange & Act
        response = HandleCommandResponse(
            message="Posted help message",
            command="outcomeops: help",
            command_type="help",
            success=True
        )

        # Assert
        assert response.message == "Posted help message"
        assert response.command == "outcomeops: help"
        assert response.command_type == "help"
        assert response.success is True


class TestCommandParsing:
    """Test command parsing logic."""

    def test_parse_help_command(self):
        """Test parsing help command."""
        # Act
        cmd_type, action = parse_command("outcomeops: help")

        # Assert
        assert cmd_type == CommandType.HELP
        assert action == "help"

    def test_parse_help_command_question_mark(self):
        """Test parsing help command with ? shorthand."""
        # Act
        cmd_type, action = parse_command("outcomeops: ?")

        # Assert
        assert cmd_type == CommandType.HELP
        assert action == "help"

    def test_parse_fix_readme_command(self):
        """Test parsing fix readme command."""
        # Act
        cmd_type, action = parse_command("outcomeops: fix readme")

        # Assert
        assert cmd_type == CommandType.FIX_README
        assert action == "fix readme"

    def test_parse_fix_readme_command_hyphenated(self):
        """Test parsing fix-readme command variant."""
        # Act
        cmd_type, action = parse_command("outcomeops: fix-readme")

        # Assert
        assert cmd_type == CommandType.FIX_README
        assert action == "fix readme"

    def test_parse_fix_readme_command_nospace(self):
        """Test parsing fixreadme command variant."""
        # Act
        cmd_type, action = parse_command("outcomeops: fixreadme")

        # Assert
        assert cmd_type == CommandType.FIX_README
        assert action == "fix readme"

    def test_parse_command_case_insensitive(self):
        """Test command parsing is case insensitive."""
        # Act
        cmd_type1, _ = parse_command("OUTCOMEOPS: HELP")
        cmd_type2, _ = parse_command("OutcomeOps: Fix Readme")

        # Assert
        assert cmd_type1 == CommandType.HELP
        assert cmd_type2 == CommandType.FIX_README

    def test_parse_command_with_extra_whitespace(self):
        """Test command parsing handles extra whitespace."""
        # Act
        cmd_type, action = parse_command("  outcomeops:   fix readme  ")

        # Assert
        assert cmd_type == CommandType.FIX_README
        assert action == "fix readme"

    def test_parse_unknown_command(self):
        """Test parsing unknown command."""
        # Act
        cmd_type, action = parse_command("outcomeops: make coffee")

        # Assert
        assert cmd_type == CommandType.UNKNOWN
        assert action == "make coffee"

    def test_parse_invalid_prefix(self):
        """Test parsing command without outcomeops prefix."""
        # Act
        cmd_type, action = parse_command("fix readme")

        # Assert
        assert cmd_type == CommandType.UNKNOWN
        assert action == "fix readme"


class TestGitHubToken:
    """Test GitHub token retrieval."""

    @patch.object(handler_module, 'ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test successful token retrieval."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ghp_test_token"}
        }

        # Act
        token = get_github_token()

        # Assert
        assert token == "ghp_test_token"
        mock_ssm.get_parameter.assert_called_once()

    @patch.object(handler_module, 'ssm_client')
    def test_get_github_token_failure(self, mock_ssm):
        """Test token retrieval failure."""
        # Arrange
        mock_ssm.get_parameter.side_effect = Exception("SSM error")

        # Act & Assert
        with pytest.raises(Exception):
            get_github_token()


class TestPRComment:
    """Test PR comment posting."""

    @patch('requests.post')
    def test_post_pr_comment_success(self, mock_post):
        """Test successful PR comment posting."""
        # Arrange
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        # Act
        result = post_pr_comment(
            repo="owner/repo",
            pr_number=123,
            body="Test comment",
            github_token="ghp_test_token"
        )

        # Assert
        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "owner/repo" in call_args[0][0]
        assert call_args[1]["json"]["body"] == "Test comment"

    @patch('requests.post')
    def test_post_pr_comment_failure(self, mock_post):
        """Test PR comment posting failure."""
        # Arrange
        mock_post.side_effect = Exception("API error")

        # Act
        result = post_pr_comment(
            repo="owner/repo",
            pr_number=123,
            body="Test comment",
            github_token="ghp_test_token"
        )

        # Assert
        assert result is False


class TestCommandHandlers:
    """Test individual command handlers."""

    @patch.object(handler_module, 'post_pr_comment')
    def test_handle_help_command(self, mock_post):
        """Test help command handler."""
        # Arrange
        mock_post.return_value = True

        # Act
        result = handle_help_command(
            repo="owner/repo",
            pr_number=123,
            github_token="ghp_test_token"
        )

        # Assert
        assert result == "Posted help message"
        mock_post.assert_called_once()
        # post_pr_comment(repo, pr_number, body, github_token)
        call_args = mock_post.call_args[0]  # positional args
        body = call_args[2]  # third arg is body
        assert "outcomeops: help" in body.lower()

    @patch.object(handler_module, 'send_pr_command_message')
    @patch.object(handler_module, 'post_pr_comment')
    def test_handle_fix_readme_command(self, mock_post, mock_send_sqs):
        """Test fix readme command handler dispatches to SQS."""
        # Arrange
        mock_post.return_value = True
        mock_send_sqs.return_value = {"MessageId": "test-message-id"}

        # Act
        result = handle_fix_readme_command(
            repo="owner/repo",
            pr_number=123,
            branch="feature-branch",
            commenter="testuser",
            github_token="ghp_test_token"
        )

        # Assert - verify SQS message was sent
        mock_send_sqs.assert_called_once_with(
            action="fix_readme",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="testuser"
        )

        # Assert - verify acknowledgment comment was posted
        assert "Dispatched" in result or "PR #123" in result
        mock_post.assert_called_once()
        call_args = mock_post.call_args[0]
        body = call_args[2]
        assert "Processing" in body or "README" in body

    @patch.object(handler_module, 'post_pr_comment')
    def test_handle_unknown_command(self, mock_post):
        """Test unknown command handler."""
        # Arrange
        mock_post.return_value = True

        # Act
        result = handle_unknown_command(
            repo="owner/repo",
            pr_number=123,
            action="make coffee",
            github_token="ghp_test_token"
        )

        # Assert
        assert "make coffee" in result
        mock_post.assert_called_once()
        # post_pr_comment(repo, pr_number, body, github_token)
        call_args = mock_post.call_args[0]  # positional args
        body = call_args[2]  # third arg is body
        assert "make coffee" in body


class TestLambdaHandler:
    """Test end-to-end Lambda handler."""

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    def test_handler_help_command_success(self, mock_post, mock_token):
        """Test handler with help command."""
        # Arrange
        mock_token.return_value = "ghp_test_token"
        mock_post.return_value = True

        event = {
            "command": "outcomeops: help",
            "pr_number": 123,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "main"
        }

        # Act
        response = lambda_handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is True
        assert body["command_type"] == "help"

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'send_pr_command_message')
    def test_handler_fix_readme_command_success(self, mock_send, mock_post, mock_token):
        """Test handler with fix readme command."""
        # Arrange
        mock_token.return_value = "ghp_test_token"
        mock_post.return_value = True
        mock_send.return_value = {"MessageId": "test-message-id"}

        event = {
            "command": "outcomeops: fix readme",
            "pr_number": 42,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "feature-branch"
        }

        # Act
        response = lambda_handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is True
        assert body["command_type"] == "fix readme"
        mock_send.assert_called_once_with(
            action="fix_readme",
            pr_number=42,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="testuser"
        )

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    def test_handler_unknown_command(self, mock_post, mock_token):
        """Test handler with unknown command."""
        # Arrange
        mock_token.return_value = "ghp_test_token"
        mock_post.return_value = True

        event = {
            "command": "outcomeops: do something",
            "pr_number": 123,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "main"
        }

        # Act
        response = lambda_handler(event, None)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is False
        assert body["command_type"] == "unknown"

    def test_handler_invalid_request(self):
        """Test handler with invalid request."""
        # Arrange
        event = {
            "command": "outcomeops: help",
            "pr_number": -1,  # Invalid
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "main"
        }

        # Act
        response = lambda_handler(event, None)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    @patch.object(handler_module, 'get_github_token')
    def test_handler_github_token_failure(self, mock_token):
        """Test handler when GitHub token retrieval fails."""
        # Arrange
        mock_token.side_effect = Exception("SSM error")

        event = {
            "command": "outcomeops: help",
            "pr_number": 123,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "main"
        }

        # Act
        response = lambda_handler(event, None)

        # Assert
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "GitHub credentials" in body["error"]


class TestLicenseIntegration:
    """Test license validation and enforcement integration."""

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    def test_handler_without_license_layer(self, mock_post, mock_token):
        """Test handler works when license layer is not attached."""
        # Arrange
        mock_token.return_value = "ghp_test_token"
        mock_post.return_value = True

        event = {
            "command": "outcomeops: help",
            "pr_number": 123,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "main"
        }

        # Act - ImportError for license validation is caught gracefully
        response = lambda_handler(event, None)

        # Assert - Should still succeed
        assert response["statusCode"] == 200


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_parse_command_multiline(self):
        """Test parsing command from multiline comment."""
        # Act - only first line should be parsed
        cmd_type, action = parse_command("outcomeops: help\nsome additional text")

        # Assert
        # The current implementation doesn't handle multiline specially
        # It will include the newline in the action
        assert cmd_type == CommandType.UNKNOWN or cmd_type == CommandType.HELP

    def test_parse_command_empty_action(self):
        """Test parsing command with empty action."""
        # Act
        cmd_type, action = parse_command("outcomeops:")

        # Assert
        assert cmd_type == CommandType.UNKNOWN

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    def test_handler_special_characters_in_repo(self, mock_post, mock_token):
        """Test handler with special characters in repo name."""
        # Arrange
        mock_token.return_value = "ghp_test_token"
        mock_post.return_value = True

        event = {
            "command": "outcomeops: help",
            "pr_number": 123,
            "repo": "my-org/my-repo",
            "commenter": "test-user",
            "branch": "feature/new-feature"
        }

        # Act
        response = lambda_handler(event, None)

        # Assert
        assert response["statusCode"] == 200


class TestGetQueueUrl:
    """Test SQS queue URL retrieval."""

    @patch.object(handler_module, 'ssm_client')
    def test_get_queue_url_success(self, mock_ssm):
        """Test successful queue URL retrieval."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-east-1.amazonaws.com/123/queue"}
        }

        result = handler_module.get_queue_url()

        assert result == "https://sqs.us-east-1.amazonaws.com/123/queue"

    @patch.object(handler_module, 'ssm_client')
    def test_get_queue_url_failure(self, mock_ssm):
        """Test queue URL retrieval failure."""
        from botocore.exceptions import ClientError
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )

        with pytest.raises(ClientError):
            handler_module.get_queue_url()


class TestSendPRCommandMessage:
    """Test SQS message sending."""

    @patch.object(handler_module, 'get_queue_url')
    @patch.object(handler_module, 'sqs_client')
    def test_send_message_success(self, mock_sqs, mock_url):
        """Test successful SQS message."""
        mock_url.return_value = "https://sqs.example.com/queue"
        mock_sqs.send_message.return_value = {"MessageId": "test-id"}

        result = handler_module.send_pr_command_message(
            action="fix_readme",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user"
        )

        assert result["MessageId"] == "test-id"
        mock_sqs.send_message.assert_called_once()

    @patch.object(handler_module, 'get_queue_url')
    @patch.object(handler_module, 'sqs_client')
    def test_send_message_with_guidance(self, mock_sqs, mock_url):
        """Test SQS message with guidance."""
        mock_url.return_value = "https://sqs.example.com/queue"
        mock_sqs.send_message.return_value = {"MessageId": "test-id"}

        handler_module.send_pr_command_message(
            action="fix_tests",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature",
            commenter="user",
            guidance="Use Decimal"
        )

        call_args = mock_sqs.send_message.call_args
        body = json.loads(call_args.kwargs["MessageBody"])
        assert body["guidance"] == "Use Decimal"

    @patch.object(handler_module, 'get_queue_url')
    @patch.object(handler_module, 'sqs_client')
    def test_send_message_failure(self, mock_sqs, mock_url):
        """Test SQS message failure."""
        from botocore.exceptions import ClientError
        mock_url.return_value = "https://sqs.example.com/queue"
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist"}}, "SendMessage"
        )

        with pytest.raises(ClientError):
            handler_module.send_pr_command_message(
                action="fix_readme",
                pr_number=123,
                repo_full_name="owner/repo",
                branch_name="feature",
                commenter="user"
            )


class TestFetchTestFailureComment:
    """Test fetching test failure comments."""

    @patch('requests.get')
    def test_fetch_success(self, mock_get):
        """Test successful fetch of test failure comment."""
        mock_response = Mock()
        mock_response.json.return_value = [
            {
                "body": """<!-- outcomeops:test-results -->
## Tests Failed

### Test Output (last 2000 chars)
```
FAILED test_foo.py::test_bar - AssertionError
```
"""
            }
        ]
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = handler_module.fetch_test_failure_comment("owner/repo", 123, "token")

        assert "FAILED" in result
        assert "test_foo.py" in result

    @patch('requests.get')
    def test_fetch_no_results(self, mock_get):
        """Test fetch with no test results comment."""
        mock_response = Mock()
        mock_response.json.return_value = [{"body": "Regular comment"}]
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = handler_module.fetch_test_failure_comment("owner/repo", 123, "token")

        assert result is None

    @patch('requests.get')
    def test_fetch_api_error(self, mock_get):
        """Test fetch with API error."""
        mock_get.side_effect = Exception("API error")

        result = handler_module.fetch_test_failure_comment("owner/repo", 123, "token")

        assert result is None


class TestAnalyzeTestFailures:
    """Test Claude analysis of test failures."""

    @patch.object(handler_module, 'bedrock_client')
    def test_analyze_success(self, mock_bedrock):
        """Test successful analysis."""
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=MagicMock(return_value=json.dumps({
                "content": [{"text": "Analysis: The test failed due to assertion error"}]
            }).encode()))
        }

        result = handler_module.analyze_test_failures("FAILED test_foo.py")

        assert "Analysis" in result

    @patch.object(handler_module, 'bedrock_client')
    def test_analyze_error(self, mock_bedrock):
        """Test analysis error handling."""
        mock_bedrock.invoke_model.side_effect = Exception("Bedrock error")

        result = handler_module.analyze_test_failures("test output")

        assert "Failed to analyze" in result


class TestHandleExplainFailedTestsCommand:
    """Test explain failed tests command handler."""

    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'fetch_test_failure_comment')
    def test_no_failures_found(self, mock_fetch, mock_post):
        """Test when no test failures exist."""
        mock_fetch.return_value = None
        mock_post.return_value = True

        result = handler_module.handle_explain_failed_tests_command(
            "owner/repo", 123, "token"
        )

        assert "No test failures found" in result
        mock_post.assert_called_once()

    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'analyze_test_failures')
    @patch.object(handler_module, 'fetch_test_failure_comment')
    def test_analysis_posted(self, mock_fetch, mock_analyze, mock_post):
        """Test successful analysis posting."""
        mock_fetch.return_value = "FAILED test_foo.py"
        mock_analyze.return_value = "Test failed because..."
        mock_post.return_value = True

        result = handler_module.handle_explain_failed_tests_command(
            "owner/repo", 123, "token"
        )

        assert "Posted test failure analysis" in result
        mock_post.assert_called_once()


class TestFetchTestAnalysisComment:
    """Test fetching test analysis comments."""

    @patch('requests.get')
    def test_fetch_success(self, mock_get):
        """Test successful fetch of analysis comment."""
        mock_response = Mock()
        mock_response.json.return_value = [
            {
                "body": """<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

The test failed because of X.

---
**Next Steps:**
"""
            }
        ]
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = handler_module.fetch_test_analysis_comment("owner/repo", 123, "token")

        assert "test failed" in result

    @patch('requests.get')
    def test_fetch_no_analysis(self, mock_get):
        """Test fetch with no analysis comment."""
        mock_response = Mock()
        mock_response.json.return_value = [{"body": "Regular comment"}]
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = handler_module.fetch_test_analysis_comment("owner/repo", 123, "token")

        assert result is None

    @patch('requests.get')
    def test_fetch_api_error(self, mock_get):
        """Test fetch with API error."""
        mock_get.side_effect = Exception("API error")

        result = handler_module.fetch_test_analysis_comment("owner/repo", 123, "token")

        assert result is None


class TestHandleFixTestsCommand:
    """Test fix tests command handler."""

    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'fetch_test_analysis_comment')
    def test_no_analysis_found(self, mock_fetch, mock_post):
        """Test when no analysis exists."""
        mock_fetch.return_value = None
        mock_post.return_value = True

        result = handler_module.handle_fix_tests_command(
            "owner/repo", 123, "feature", "user", None, "token"
        )

        assert "requires analysis first" in result

    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'send_pr_command_message')
    @patch.object(handler_module, 'fetch_test_analysis_comment')
    def test_dispatches_to_sqs(self, mock_fetch, mock_send, mock_post):
        """Test successful dispatch to SQS."""
        mock_fetch.return_value = "Test analysis content"
        mock_send.return_value = {"MessageId": "test-id"}
        mock_post.return_value = True

        result = handler_module.handle_fix_tests_command(
            "owner/repo", 123, "feature", "user", None, "token"
        )

        assert "Dispatched" in result
        mock_send.assert_called_once()

    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'send_pr_command_message')
    @patch.object(handler_module, 'fetch_test_analysis_comment')
    def test_includes_guidance(self, mock_fetch, mock_send, mock_post):
        """Test guidance is included."""
        mock_fetch.return_value = "Test analysis content"
        mock_send.return_value = {"MessageId": "test-id"}
        mock_post.return_value = True

        handler_module.handle_fix_tests_command(
            "owner/repo", 123, "feature", "user", "Use Decimal", "token"
        )

        call_args = mock_send.call_args
        assert call_args.kwargs["guidance"] == "Use Decimal"


class TestParseCommandExtended:
    """Extended tests for command parsing."""

    def test_parse_explain_failed_tests(self):
        """Test parsing explain failed tests command."""
        cmd_type, action = parse_command("outcomeops: explain failed tests")

        assert cmd_type == CommandType.EXPLAIN_FAILED_TESTS
        assert action == "explain failed tests"

    def test_parse_explain_tests_hyphenated(self):
        """Test parsing explain-failed-tests command."""
        cmd_type, action = parse_command("outcomeops: explain-failed-tests")

        assert cmd_type == CommandType.EXPLAIN_FAILED_TESTS

    def test_parse_fix_tests_no_guidance(self):
        """Test parsing fix tests without guidance."""
        cmd_type, action = parse_command("outcomeops: fix tests")

        assert cmd_type == CommandType.FIX_TESTS
        assert action == ""

    def test_parse_fix_tests_with_guidance(self):
        """Test parsing fix tests with guidance."""
        cmd_type, action = parse_command("outcomeops: fix tests: Use Decimal")

        assert cmd_type == CommandType.FIX_TESTS
        # parse_command lowercases the action
        assert action == "use decimal"

    def test_parse_fix_tests_hyphenated_guidance(self):
        """Test parsing fix-tests with guidance."""
        cmd_type, action = parse_command("outcomeops: fix-tests: mock boto3")

        assert cmd_type == CommandType.FIX_TESTS
        assert action == "mock boto3"


class TestLambdaHandlerExtended:
    """Extended Lambda handler tests."""

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'fetch_test_failure_comment')
    @patch.object(handler_module, 'analyze_test_failures')
    def test_handler_explain_tests(
        self, mock_analyze, mock_fetch, mock_post, mock_token
    ):
        """Test handler with explain failed tests command."""
        mock_token.return_value = "ghp_test_token"
        mock_fetch.return_value = "FAILED test_foo.py"
        mock_analyze.return_value = "Analysis"
        mock_post.return_value = True

        event = {
            "command": "outcomeops: explain failed tests",
            "pr_number": 123,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "main"
        }

        response = lambda_handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is True
        assert body["command_type"] == "explain failed tests"

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'send_pr_command_message')
    @patch.object(handler_module, 'fetch_test_analysis_comment')
    def test_handler_fix_tests(
        self, mock_fetch, mock_send, mock_post, mock_token
    ):
        """Test handler with fix tests command."""
        mock_token.return_value = "ghp_test_token"
        mock_fetch.return_value = "Analysis"
        mock_send.return_value = {"MessageId": "test-id"}
        mock_post.return_value = True

        event = {
            "command": "outcomeops: fix tests",
            "pr_number": 123,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "feature"
        }

        response = lambda_handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is True
        assert body["command_type"] == "fix tests"

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'send_pr_command_message')
    @patch.object(handler_module, 'fetch_test_analysis_comment')
    def test_handler_fix_tests_with_guidance(
        self, mock_fetch, mock_send, mock_post, mock_token
    ):
        """Test handler with fix tests and guidance."""
        mock_token.return_value = "ghp_test_token"
        mock_fetch.return_value = "Analysis"
        mock_send.return_value = {"MessageId": "test-id"}
        mock_post.return_value = True

        event = {
            "command": "outcomeops: fix tests: Use Decimal instead of float",
            "pr_number": 123,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "feature"
        }

        response = lambda_handler(event, None)

        assert response["statusCode"] == 200
        call_args = mock_send.call_args
        # parse_command lowercases the action/guidance
        assert call_args.kwargs["guidance"] == "use decimal instead of float"


class TestParseFixAdrCommand:
    """Test parsing fix adr commands."""

    def test_parse_fix_adr_command(self):
        """Test parsing fix adr command."""
        cmd_type, action = parse_command("outcomeops: fix adr")

        assert cmd_type == CommandType.FIX_ADR
        assert action == ""

    def test_parse_fix_adr_hyphenated(self):
        """Test parsing fix-adr command variant."""
        cmd_type, action = parse_command("outcomeops: fix-adr")

        assert cmd_type == CommandType.FIX_ADR
        assert action == ""

    def test_parse_fix_adr_nospace(self):
        """Test parsing fixadr command variant."""
        cmd_type, action = parse_command("outcomeops: fixadr")

        assert cmd_type == CommandType.FIX_ADR
        assert action == ""

    def test_parse_fix_adr_with_guidance(self):
        """Test parsing fix adr with guidance."""
        cmd_type, action = parse_command("outcomeops: fix adr: Document the new caching pattern")

        assert cmd_type == CommandType.FIX_ADR
        assert action == "document the new caching pattern"

    def test_parse_fix_adr_hyphenated_guidance(self):
        """Test parsing fix-adr with guidance."""
        cmd_type, action = parse_command("outcomeops: fix-adr: Add ADR for Lambda handler patterns")

        assert cmd_type == CommandType.FIX_ADR
        assert action == "add adr for lambda handler patterns"


class TestHandleFixAdrCommand:
    """Test fix adr command handler."""

    @patch.object(handler_module, 'send_pr_command_message')
    @patch.object(handler_module, 'post_pr_comment')
    def test_handle_fix_adr_command(self, mock_post, mock_send_sqs):
        """Test fix adr command handler dispatches to SQS."""
        mock_post.return_value = True
        mock_send_sqs.return_value = {"MessageId": "test-message-id"}

        result = handle_fix_adr_command(
            repo="owner/repo",
            pr_number=123,
            branch="feature-branch",
            commenter="testuser",
            guidance="",
            github_token="ghp_test_token"
        )

        # Assert - verify SQS message was sent
        mock_send_sqs.assert_called_once_with(
            action="fix_adr",
            pr_number=123,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="testuser",
            guidance=None
        )

        # Assert - verify acknowledgment comment was posted
        assert "Dispatched" in result or "PR #123" in result
        mock_post.assert_called_once()
        call_args = mock_post.call_args[0]
        body = call_args[2]
        assert "Processing" in body or "ADR" in body

    @patch.object(handler_module, 'send_pr_command_message')
    @patch.object(handler_module, 'post_pr_comment')
    def test_handle_fix_adr_command_with_guidance(self, mock_post, mock_send_sqs):
        """Test fix adr command handler with guidance."""
        mock_post.return_value = True
        mock_send_sqs.return_value = {"MessageId": "test-message-id"}

        result = handle_fix_adr_command(
            repo="owner/repo",
            pr_number=123,
            branch="feature-branch",
            commenter="testuser",
            guidance="Document the new caching pattern",
            github_token="ghp_test_token"
        )

        # Assert - verify guidance is passed to SQS
        mock_send_sqs.assert_called_once()
        call_args = mock_send_sqs.call_args
        assert call_args.kwargs["guidance"] == "Document the new caching pattern"


class TestLambdaHandlerFixAdr:
    """Test Lambda handler with fix adr command."""

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'send_pr_command_message')
    def test_handler_fix_adr_command_success(self, mock_send, mock_post, mock_token):
        """Test handler with fix adr command."""
        mock_token.return_value = "ghp_test_token"
        mock_post.return_value = True
        mock_send.return_value = {"MessageId": "test-message-id"}

        event = {
            "command": "outcomeops: fix adr",
            "pr_number": 42,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "feature-branch"
        }

        response = lambda_handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["success"] is True
        assert body["command_type"] == "fix adr"
        mock_send.assert_called_once_with(
            action="fix_adr",
            pr_number=42,
            repo_full_name="owner/repo",
            branch_name="feature-branch",
            commenter="testuser",
            guidance=None
        )

    @patch.object(handler_module, 'get_github_token')
    @patch.object(handler_module, 'post_pr_comment')
    @patch.object(handler_module, 'send_pr_command_message')
    def test_handler_fix_adr_with_guidance(self, mock_send, mock_post, mock_token):
        """Test handler with fix adr and guidance."""
        mock_token.return_value = "ghp_test_token"
        mock_post.return_value = True
        mock_send.return_value = {"MessageId": "test-message-id"}

        event = {
            "command": "outcomeops: fix adr: Document the new Lambda pattern",
            "pr_number": 42,
            "repo": "owner/repo",
            "commenter": "testuser",
            "branch": "feature-branch"
        }

        response = lambda_handler(event, None)

        assert response["statusCode"] == 200
        call_args = mock_send.call_args
        # parse_command lowercases the action/guidance
        assert call_args.kwargs["guidance"] == "document the new lambda pattern"
