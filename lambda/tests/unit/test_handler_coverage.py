# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests to increase coverage for generate-code/handler.py.

Covers:
- Event type detection (SQS, API Gateway, Jira)
- Jira event handling (valid, invalid componentKey, exceptions)
- SQS event handling (plan generation, step execution, PR commands, unknown action)
- Handler routing (unknown event type)
- get_queue_url and send_plan_generation_message
- extract_issue_number_from_jira_key
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest
from botocore.exceptions import ClientError

# Add generate-code directory to path
GENERATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../generate-code")
)
if GENERATE_CODE_DIR not in sys.path:
    sys.path.insert(0, GENERATE_CODE_DIR)

LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, LAMBDA_ROOT)

# Load the handler module
handler_path = os.path.join(GENERATE_CODE_DIR, "handler.py")
spec = importlib.util.spec_from_file_location("gen_code_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules["gen_code_handler"] = handler_module
spec.loader.exec_module(handler_module)

# Import functions
is_sqs_event = handler_module.is_sqs_event
is_api_gateway_event = handler_module.is_api_gateway_event
is_jira_event = handler_module.is_jira_event
extract_issue_number_from_jira_key = handler_module.extract_issue_number_from_jira_key
get_queue_url = handler_module.get_queue_url
send_plan_generation_message = handler_module.send_plan_generation_message
get_customer_id_from_cache = handler_module.get_customer_id_from_cache
handle_jira_event = handler_module.handle_jira_event
handle_sqs_event = handler_module.handle_sqs_event
handler = handler_module.handler


# ############################################################################
# EVENT TYPE DETECTION TESTS
# ############################################################################


class TestEventTypeDetection:
    """Test event type detection functions."""

    def test_is_sqs_event_true(self):
        """Test SQS event detection."""
        event = {
            "Records": [
                {"eventSource": "aws:sqs", "body": "{}"}
            ]
        }
        assert is_sqs_event(event) is True

    def test_is_sqs_event_false_empty_records(self):
        """Test SQS event detection with empty records."""
        assert not is_sqs_event({"Records": []})

    def test_is_sqs_event_false_no_records(self):
        """Test SQS event detection without Records key."""
        assert is_sqs_event({"body": "test"}) is False

    def test_is_sqs_event_false_wrong_source(self):
        """Test SQS event detection with wrong event source."""
        event = {
            "Records": [
                {"eventSource": "aws:dynamodb", "body": "{}"}
            ]
        }
        assert is_sqs_event(event) is False

    def test_is_api_gateway_event_true(self):
        """Test API Gateway event detection."""
        event = {"requestContext": {}, "body": "{}"}
        assert is_api_gateway_event(event) is True

    def test_is_api_gateway_event_false(self):
        """Test API Gateway event detection negative case."""
        assert is_api_gateway_event({"body": "test"}) is False
        assert is_api_gateway_event({"requestContext": {}}) is False

    def test_is_jira_event_true(self):
        """Test Jira event detection."""
        event = {
            "source": "jira",
            "issueKey": "PROJ-123",
            "componentKey": "owner/repo",
        }
        assert is_jira_event(event) is True

    def test_is_jira_event_false_wrong_source(self):
        """Test Jira event detection with wrong source."""
        event = {
            "source": "github",
            "issueKey": "PROJ-123",
            "componentKey": "owner/repo",
        }
        assert is_jira_event(event) is False

    def test_is_jira_event_false_missing_keys(self):
        """Test Jira event detection with missing keys."""
        assert is_jira_event({"source": "jira"}) is False
        assert is_jira_event({"source": "jira", "issueKey": "X"}) is False


# ############################################################################
# JIRA HELPERS TESTS
# ############################################################################


class TestExtractIssueNumberFromJiraKey:
    """Test Jira issue key parsing."""

    def test_standard_key(self):
        """Test standard Jira key parsing."""
        assert extract_issue_number_from_jira_key("PROJ-123") == 123

    def test_long_project_key(self):
        """Test long project key."""
        assert extract_issue_number_from_jira_key("MYPROJECT-456") == 456

    def test_no_match_uses_hash_fallback(self):
        """Test fallback to hash when no match."""
        result = extract_issue_number_from_jira_key("invalid")
        assert isinstance(result, int)
        assert 0 <= result < 100000


# ############################################################################
# get_queue_url TESTS
# ############################################################################


class TestGetQueueUrl:
    """Test SQS queue URL retrieval from SSM."""

    @patch.object(handler_module, "ssm_client")
    def test_get_queue_url_success(self, mock_ssm):
        """Test successful queue URL retrieval."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-east-1.amazonaws.com/123/queue"}
        }
        result = get_queue_url()
        assert result == "https://sqs.us-east-1.amazonaws.com/123/queue"

    @patch.object(handler_module, "ssm_client")
    def test_get_queue_url_empty_value_raises(self, mock_ssm):
        """Test exception when parameter value is empty."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": ""}
        }
        with pytest.raises(Exception, match="Queue URL not found"):
            get_queue_url()

    @patch.object(handler_module, "ssm_client")
    def test_get_queue_url_client_error_raises(self, mock_ssm):
        """Test ClientError is re-raised."""
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
            "GetParameter"
        )
        with pytest.raises(ClientError):
            get_queue_url()

    @patch.object(handler_module, "ssm_client")
    def test_get_queue_url_missing_value_key(self, mock_ssm):
        """Test exception when Value key is missing."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {}
        }
        with pytest.raises(Exception, match="Queue URL not found"):
            get_queue_url()


# ############################################################################
# send_plan_generation_message TESTS
# ############################################################################


class TestSendPlanGenerationMessage:
    """Test SQS message sending."""

    @patch.object(handler_module, "sqs_client")
    @patch.object(handler_module, "ssm_client")
    def test_send_message_success(self, mock_ssm, mock_sqs):
        """Test successful message send."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.example.com/queue"}
        }
        mock_sqs.send_message.return_value = {"MessageId": "msg-123"}

        result = send_plan_generation_message(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="owner/repo",
            branch_name="branch",
            base_branch="main"
        )
        assert result["MessageId"] == "msg-123"

    @patch.object(handler_module, "sqs_client")
    @patch.object(handler_module, "ssm_client")
    def test_send_message_client_error(self, mock_ssm, mock_sqs):
        """Test ClientError on message send is re-raised."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.example.com/queue"}
        }
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "queue not found"}},
            "SendMessage"
        )

        with pytest.raises(ClientError):
            send_plan_generation_message(
                issue_number=1,
                issue_title="Test",
                issue_description="Desc",
                repo_full_name="owner/repo",
                branch_name="branch",
                base_branch="main"
            )


# ############################################################################
# JIRA EVENT HANDLER TESTS
# ############################################################################


class TestHandleJiraEvent:
    """Test Jira event processing."""

    @patch("gen_code_handler.send_plan_generation_message")
    @patch("gen_code_handler.create_branch")
    @patch("gen_code_handler.get_github_token")
    @patch("gen_code_handler.get_customer_id_from_cache")
    def test_jira_event_success(self, mock_cache, mock_token, mock_branch, mock_send):
        """Test successful Jira event processing."""
        mock_cache.return_value = None
        mock_token.return_value = "token"
        mock_branch.return_value = {"success": True}
        mock_send.return_value = {"MessageId": "msg-1"}

        event = {
            "source": "jira",
            "issueKey": "PROJ-42",
            "issueSummary": "Add feature",
            "issueDescription": "Details here",
            "componentKey": "owner/repo",
            "label": "approved-for-generation",
        }
        result = handle_jira_event(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "Code generation started" in body["message"]

    @patch("gen_code_handler.send_plan_generation_message")
    @patch("gen_code_handler.create_branch")
    @patch("gen_code_handler.get_github_token")
    @patch("gen_code_handler.get_customer_id_from_cache")
    def test_jira_event_plan_only(self, mock_cache, mock_token, mock_branch, mock_send):
        """Test Jira event with plan-only mode."""
        mock_cache.return_value = None
        mock_token.return_value = "token"
        mock_branch.return_value = {"success": True}
        mock_send.return_value = {"MessageId": "msg-1"}

        event = {
            "source": "jira",
            "issueKey": "PROJ-42",
            "issueSummary": "Add feature",
            "issueDescription": "Details",
            "componentKey": "owner/repo",
            "label": "approved-for-plan",
        }
        result = handle_jira_event(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["mode"] == "plan only"

    def test_jira_event_invalid_component_key(self):
        """Test Jira event with invalid componentKey."""
        event = {
            "source": "jira",
            "issueKey": "PROJ-42",
            "issueSummary": "Add feature",
            "issueDescription": "Details",
            "componentKey": "no-slash-here",
        }
        result = handle_jira_event(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "Configuration error" in body["message"]

    def test_jira_event_empty_component_key(self):
        """Test Jira event with empty componentKey."""
        event = {
            "source": "jira",
            "issueKey": "PROJ-42",
            "issueSummary": "Add feature",
            "issueDescription": "Details",
            "componentKey": "",
        }
        result = handle_jira_event(event)
        assert result["statusCode"] == 400

    @patch("gen_code_handler.create_branch")
    @patch("gen_code_handler.get_github_token")
    def test_jira_event_branch_creation_fails(self, mock_token, mock_branch):
        """Test Jira event when branch creation fails."""
        mock_token.return_value = "token"
        mock_branch.return_value = {"success": False, "error": "API error"}

        event = {
            "source": "jira",
            "issueKey": "PROJ-42",
            "issueSummary": "Add feature",
            "issueDescription": "Details",
            "componentKey": "owner/repo",
        }
        result = handle_jira_event(event)
        assert result["statusCode"] == 500

    @patch("gen_code_handler.get_github_token")
    def test_jira_event_exception(self, mock_token):
        """Test Jira event with unexpected exception."""
        mock_token.side_effect = RuntimeError("boom")

        event = {
            "source": "jira",
            "issueKey": "PROJ-42",
            "issueSummary": "Add feature",
            "issueDescription": "Details",
            "componentKey": "owner/repo",
        }
        result = handle_jira_event(event)
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Internal error" in body["message"]


# ############################################################################
# SQS EVENT HANDLER TESTS
# ############################################################################


class TestHandleSqsEvent:
    """Test SQS event processing."""

    @patch("gen_code_handler.handle_webhook")
    def test_sqs_generate_plan(self, mock_handle_webhook):
        """Test SQS message routing for plan generation."""
        mock_handle_webhook.return_value = {"status": "plan_created"}

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "generate_plan",
                    "issue_number": 1,
                    "issue_title": "Test",
                    "issue_description": "Desc",
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "current_step": 0,
                    "total_steps": 0,
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        result = handle_sqs_event(event)
        assert result["batchItemFailures"] == []
        mock_handle_webhook.assert_called_once()

    @patch("gen_code_handler.handle_step_message")
    def test_sqs_execute_step(self, mock_handle_step):
        """Test SQS message routing for step execution."""
        mock_handle_step.return_value = {"status": "step_completed"}

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "execute_step",
                    "issue_number": 1,
                    "issue_title": "Test",
                    "issue_description": "Desc",
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "current_step": 1,
                    "total_steps": 3,
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        result = handle_sqs_event(event)
        assert result["batchItemFailures"] == []

    def test_sqs_unknown_action_fails(self):
        """Test SQS message with unknown action is added to failures."""
        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "unknown_action",
                    "issue_number": 1,
                    "issue_title": "Test",
                    "issue_description": "Desc",
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "current_step": 0,
                    "total_steps": 0,
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        result = handle_sqs_event(event)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-1"

    @patch("gen_code_handler.handle_fix_readme", create=True)
    def test_sqs_fix_readme_command(self, mock_fix_readme=None):
        """Test SQS message routing for fix_readme PR command."""
        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "fix_readme",
                    "pr_number": 10,
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "commenter": "testuser",
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        with patch("gen_code_handler.handle_fix_readme", create=True) as mock_fix:
            # Patch the import inside handle_sqs_event
            with patch.dict(sys.modules, {"readme_fixer": MagicMock()}):
                mock_readme_mod = sys.modules["readme_fixer"]
                mock_readme_mod.handle_fix_readme.return_value = {"status": "done"}
                result = handle_sqs_event(event)

        assert result["batchItemFailures"] == []

    def test_sqs_fix_tests_command(self):
        """Test SQS message routing for fix_tests PR command."""
        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "fix_tests",
                    "pr_number": 10,
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "commenter": "testuser",
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        with patch.dict(sys.modules, {"tests_fixer": MagicMock()}):
            mock_mod = sys.modules["tests_fixer"]
            mock_mod.handle_fix_tests.return_value = {"status": "done"}
            result = handle_sqs_event(event)

        assert result["batchItemFailures"] == []

    def test_sqs_fix_adr_command(self):
        """Test SQS message routing for fix_adr PR command."""
        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "fix_adr",
                    "pr_number": 10,
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "commenter": "testuser",
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        with patch.dict(sys.modules, {"adr_fixer": MagicMock()}):
            mock_mod = sys.modules["adr_fixer"]
            mock_mod.handle_fix_adr.return_value = {"status": "done"}
            result = handle_sqs_event(event)

        assert result["batchItemFailures"] == []

    def test_sqs_regenerate_command(self):
        """Test SQS message routing for regenerate PR command (not implemented)."""
        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "regenerate",
                    "pr_number": 10,
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "commenter": "testuser",
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        result = handle_sqs_event(event)
        assert result["batchItemFailures"] == []

    @patch("gen_code_handler.handle_step_message")
    def test_sqs_exception_adds_to_failures(self, mock_handle_step):
        """Test SQS message exception adds message to batch failures."""
        mock_handle_step.side_effect = RuntimeError("step failed")

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "execute_step",
                    "issue_number": 1,
                    "issue_title": "Test",
                    "issue_description": "Desc",
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "current_step": 1,
                    "total_steps": 1,
                    "base_branch": "main",
                }),
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        result = handle_sqs_event(event)
        assert len(result["batchItemFailures"]) == 1


# ############################################################################
# HANDLER ROUTING TESTS
# ############################################################################


class TestHandlerRouting:
    """Test main handler routing logic."""

    def test_handler_unknown_event_type(self):
        """Test handler returns 400 for unknown event types."""
        event = {"random_key": "random_value"}
        result = handler(event, None)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["message"] == "Unknown event type"

    @patch("gen_code_handler.handle_sqs_event")
    def test_handler_routes_sqs_event(self, mock_sqs_handler):
        """Test handler routes SQS events correctly."""
        mock_sqs_handler.return_value = {"batchItemFailures": []}

        event = {
            "Records": [{
                "eventSource": "aws:sqs",
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "action": "execute_step",
                    "issue_number": 1,
                    "issue_title": "Test",
                    "issue_description": "Desc",
                    "repo_full_name": "owner/repo",
                    "branch_name": "branch",
                    "current_step": 1,
                    "total_steps": 1,
                    "base_branch": "main",
                }),
                "eventSourceARN": "arn:aws:sqs:us-east-1:123:queue",
                "md5OfBody": "abc",
                "attributes": {},
                "messageAttributes": {},
                "awsRegion": "us-east-1",
            }]
        }

        result = handler(event, None)
        assert result["batchItemFailures"] == []
        mock_sqs_handler.assert_called_once()

    @patch("gen_code_handler.handle_jira_event")
    def test_handler_routes_jira_event(self, mock_jira_handler):
        """Test handler routes Jira events correctly."""
        mock_jira_handler.return_value = {"statusCode": 200, "body": "{}"}

        event = {
            "source": "jira",
            "issueKey": "PROJ-1",
            "componentKey": "owner/repo",
            "issueSummary": "Test",
            "issueDescription": "Desc",
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_jira_handler.assert_called_once()
