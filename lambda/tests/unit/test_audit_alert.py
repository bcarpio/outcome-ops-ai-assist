# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for audit-alert Lambda handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

mock_sns_client = MagicMock()


def _load_handler_module():
    """Load or reload the audit-alert handler module."""
    if 'audit_alert_handler' in sys.modules:
        del sys.modules['audit_alert_handler']

    handler_dir = os.path.join(os.path.dirname(__file__), '../../audit-alert')
    if handler_dir not in sys.path:
        sys.path.insert(0, handler_dir)

    handler_path = os.path.join(handler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("audit_alert_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['audit_alert_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 client with mock
    handler_module.sns_client = mock_sns_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
handler = handler_module.handler


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks between tests."""
    # side_effect=True: the error-handling tests set a raising side_effect,
    # which reset_mock() alone keeps and would leak into later tests.
    mock_sns_client.reset_mock(side_effect=True)
    handler_module.SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:test-topic"
    yield


@pytest.fixture
def dynamodb_refusal_record():
    """A single DynamoDB Streams INSERT record with status=refusal."""
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "status": {"S": "refusal"},
                "lambda_name": {"S": "chat-handler"},
                "model_id": {"S": "anthropic.claude-3-sonnet"},
                "flagged_reason": {"S": "harmful_content"},
                "user_email": {"S": "user@example.com"},
                "workspace_id": {"S": "ws_abc123"},
                "timestamp": {"S": "2026-01-15T10:30:00Z"},
                "request_id": {"S": "req-uuid-001"},
                "input_text": {"S": "Some user input text"},
                "output_text": {"S": "Some output text"},
            }
        },
    }


@pytest.fixture
def dynamodb_event(dynamodb_refusal_record):
    """A DynamoDB Streams event containing one refusal record."""
    return {"Records": [dynamodb_refusal_record]}


@pytest.fixture
def dynamodb_test_refusal_record():
    """A DynamoDB Streams INSERT record with flagged_reason=test_refusal_trigger."""
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "status": {"S": "refusal"},
                "lambda_name": {"S": "chat-handler"},
                "model_id": {"S": "anthropic.claude-3-sonnet"},
                "flagged_reason": {"S": "test_refusal_trigger"},
                "user_email": {"S": "tester@example.com"},
                "workspace_id": {"S": "ws_test"},
                "timestamp": {"S": "2026-01-15T11:00:00Z"},
                "request_id": {"S": "req-uuid-test"},
                "input_text": {"S": "Test input"},
                "output_text": {"S": "Test output"},
            }
        },
    }


# ============================================================================
# SNS Topic Not Configured
# ============================================================================

class TestSnsTopicNotConfigured:
    """Test handler behavior when SNS_TOPIC_ARN is not set."""

    def test_handler_returns_200_when_no_topic(self, dynamodb_event):
        """Handler returns 200 with skip message when SNS_TOPIC_ARN is empty."""
        handler_module.SNS_TOPIC_ARN = ""

        result = handler(dynamodb_event, None)

        assert result["statusCode"] == 200
        assert "No SNS topic configured" in result["body"]
        mock_sns_client.publish.assert_not_called()

    def test_handler_returns_200_when_topic_is_none(self, dynamodb_event):
        """Handler returns 200 with skip message when SNS_TOPIC_ARN is falsy."""
        handler_module.SNS_TOPIC_ARN = ""

        result = handler(dynamodb_event, None)

        assert result["statusCode"] == 200
        assert "No SNS topic configured" in result["body"]
        mock_sns_client.publish.assert_not_called()


# ============================================================================
# Record Filtering
# ============================================================================

class TestRecordFiltering:
    """Test that handler correctly filters records before processing."""

    def test_handler_skips_non_insert_events(self):
        """Handler skips MODIFY and REMOVE events."""
        event = {
            "Records": [
                {
                    "eventName": "MODIFY",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                        }
                    },
                },
                {
                    "eventName": "REMOVE",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                        }
                    },
                },
            ]
        }

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"] == "Processed"
        mock_sns_client.publish.assert_not_called()

    def test_handler_skips_records_without_new_image(self):
        """Handler skips records missing dynamodb.NewImage."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {},
                },
            ]
        }

        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_sns_client.publish.assert_not_called()

    def test_handler_skips_records_with_empty_new_image(self):
        """Handler skips records where NewImage is empty dict."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {"NewImage": {}},
                },
            ]
        }

        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_sns_client.publish.assert_not_called()

    def test_handler_skips_non_refusal_status(self):
        """Handler skips records where status is not 'refusal'."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "success"},
                            "lambda_name": {"S": "chat-handler"},
                        }
                    },
                },
            ]
        }

        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_sns_client.publish.assert_not_called()

    def test_handler_skips_record_with_missing_status(self):
        """Handler skips records that have no status field at all."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "lambda_name": {"S": "chat-handler"},
                        }
                    },
                },
            ]
        }

        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_sns_client.publish.assert_not_called()

    def test_handler_handles_empty_records_list(self):
        """Handler handles event with empty Records list."""
        event = {"Records": []}

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"] == "Processed"
        mock_sns_client.publish.assert_not_called()

    def test_handler_handles_missing_records_key(self):
        """Handler handles event without Records key."""
        event = {}

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"] == "Processed"
        mock_sns_client.publish.assert_not_called()


# ============================================================================
# SNS Publishing - Successful Refusal Alerts
# ============================================================================

class TestSnsPublishing:
    """Test SNS message publishing for valid refusal records."""

    def test_handler_publishes_refusal_alert(self, dynamodb_event):
        """Handler publishes SNS notification for a refusal record."""
        result = handler(dynamodb_event, None)

        assert result["statusCode"] == 200
        assert result["body"] == "Processed"
        mock_sns_client.publish.assert_called_once()

    def test_handler_publish_uses_correct_topic_arn(self, dynamodb_event):
        """Handler uses the configured SNS_TOPIC_ARN."""
        handler(dynamodb_event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert call_kwargs["TopicArn"] == "arn:aws:sns:us-east-1:123456789012:test-topic"

    @patch.dict(os.environ, {"ENV": "prd"})
    def test_handler_subject_contains_env_and_lambda_name(self, dynamodb_event):
        """Subject includes environment (uppercase) and lambda name."""
        # Reload to pick up ENV
        handler(dynamodb_event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "[PRD]" in call_kwargs["Subject"]
        assert "chat-handler" in call_kwargs["Subject"]
        assert "AI Refusal Alert" in call_kwargs["Subject"]

    @patch.dict(os.environ, {"ENV": "dev"})
    def test_handler_subject_for_test_refusal(self, dynamodb_test_refusal_record):
        """Subject uses [TEST] prefix for test_refusal_trigger."""
        event = {"Records": [dynamodb_test_refusal_record]}

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "[TEST]" in call_kwargs["Subject"]
        assert "AI Refusal Alert" in call_kwargs["Subject"]
        assert "DEV" in call_kwargs["Subject"]

    @patch.dict(os.environ, {"ENV": "prd"})
    def test_handler_message_contains_all_fields(self, dynamodb_event):
        """Message body includes all audit metadata fields."""
        handler(dynamodb_event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        message = call_kwargs["Message"]

        assert "AI Refusal Detected" in message
        assert "prd" in message
        assert "chat-handler" in message
        assert "anthropic.claude-3-sonnet" in message
        assert "2026-01-15T10:30:00Z" in message
        assert "req-uuid-001" in message
        assert "user@example.com" in message
        assert "ws_abc123" in message
        assert "harmful_content" in message
        assert "Some user input text" in message
        assert "Some output text" in message

    def test_handler_message_shows_na_for_missing_user_email(self):
        """Message shows 'N/A (pipeline)' when user_email is empty."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "process-repo-summary"},
                            "model_id": {"S": "anthropic.claude-3-sonnet"},
                            "flagged_reason": {"S": "safety_filter"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": "ws_001"},
                            "timestamp": {"S": "2026-01-15T10:30:00Z"},
                            "request_id": {"S": "req-002"},
                            "input_text": {"S": "pipeline input"},
                            "output_text": {"S": "pipeline output"},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "N/A (pipeline)" in call_kwargs["Message"]

    def test_handler_message_shows_na_for_missing_workspace(self):
        """Message shows 'N/A' when workspace_id is empty."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "chat-handler"},
                            "model_id": {"S": "anthropic.claude-3-sonnet"},
                            "flagged_reason": {"S": "safety_filter"},
                            "user_email": {"S": "user@example.com"},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": "2026-01-15T10:30:00Z"},
                            "request_id": {"S": "req-003"},
                            "input_text": {"S": "some input"},
                            "output_text": {"S": "some output"},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        message = call_kwargs["Message"]
        assert "Workspace: N/A" in message

    def test_handler_subject_truncated_to_100_chars(self):
        """SNS subject is truncated to 100 characters max."""
        long_lambda_name = "a" * 200
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": long_lambda_name},
                            "model_id": {"S": "model"},
                            "flagged_reason": {"S": "reason"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": ""},
                            "request_id": {"S": ""},
                            "input_text": {"S": ""},
                            "output_text": {"S": ""},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert len(call_kwargs["Subject"]) <= 100

    def test_handler_input_text_truncated_to_500_chars(self):
        """Input text in message is truncated to first 500 characters."""
        long_input = "x" * 1000
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "chat-handler"},
                            "model_id": {"S": "model"},
                            "flagged_reason": {"S": "reason"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": ""},
                            "request_id": {"S": ""},
                            "input_text": {"S": long_input},
                            "output_text": {"S": "short"},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        message = call_kwargs["Message"]
        # The message should contain exactly 500 x's, not 1000
        assert "x" * 500 in message
        assert "x" * 501 not in message

    def test_handler_output_text_truncated_to_500_chars(self):
        """Output text in message is truncated to first 500 characters."""
        long_output = "y" * 1000
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "chat-handler"},
                            "model_id": {"S": "model"},
                            "flagged_reason": {"S": "reason"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": ""},
                            "request_id": {"S": ""},
                            "input_text": {"S": "short"},
                            "output_text": {"S": long_output},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        message = call_kwargs["Message"]
        assert "y" * 500 in message
        assert "y" * 501 not in message


# ============================================================================
# Multiple Records
# ============================================================================

class TestMultipleRecords:
    """Test handler processing multiple records in a single event."""

    def test_handler_processes_multiple_refusal_records(self):
        """Handler publishes an alert for each refusal record."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "lambda-a"},
                            "model_id": {"S": "model-1"},
                            "flagged_reason": {"S": "reason-a"},
                            "user_email": {"S": "a@example.com"},
                            "workspace_id": {"S": "ws_a"},
                            "timestamp": {"S": "2026-01-15T10:00:00Z"},
                            "request_id": {"S": "req-a"},
                            "input_text": {"S": "input a"},
                            "output_text": {"S": "output a"},
                        }
                    },
                },
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "lambda-b"},
                            "model_id": {"S": "model-2"},
                            "flagged_reason": {"S": "reason-b"},
                            "user_email": {"S": "b@example.com"},
                            "workspace_id": {"S": "ws_b"},
                            "timestamp": {"S": "2026-01-15T11:00:00Z"},
                            "request_id": {"S": "req-b"},
                            "input_text": {"S": "input b"},
                            "output_text": {"S": "output b"},
                        }
                    },
                },
            ]
        }

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert mock_sns_client.publish.call_count == 2

    def test_handler_mixed_records_only_publishes_refusals(self):
        """Handler only publishes for INSERT records with status=refusal."""
        event = {
            "Records": [
                {
                    "eventName": "MODIFY",
                    "dynamodb": {
                        "NewImage": {"status": {"S": "refusal"}}
                    },
                },
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {"status": {"S": "success"}}
                    },
                },
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "chat-handler"},
                            "model_id": {"S": "model"},
                            "flagged_reason": {"S": "reason"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": ""},
                            "request_id": {"S": ""},
                            "input_text": {"S": ""},
                            "output_text": {"S": ""},
                        }
                    },
                },
            ]
        }

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert mock_sns_client.publish.call_count == 1


# ============================================================================
# Default / Missing Fields
# ============================================================================

class TestDefaultFieldValues:
    """Test that missing DynamoDB fields fall back to defaults."""

    def test_handler_defaults_lambda_name_to_unknown(self):
        """Missing lambda_name defaults to 'unknown'."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            # No lambda_name
                            "model_id": {"S": "model"},
                            "flagged_reason": {"S": "reason"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": ""},
                            "request_id": {"S": ""},
                            "input_text": {"S": ""},
                            "output_text": {"S": ""},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "unknown" in call_kwargs["Subject"]
        assert "Lambda: unknown" in call_kwargs["Message"]

    def test_handler_defaults_model_id_to_unknown(self):
        """Missing model_id defaults to 'unknown'."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "chat-handler"},
                            # No model_id
                            "flagged_reason": {"S": "reason"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": ""},
                            "request_id": {"S": ""},
                            "input_text": {"S": ""},
                            "output_text": {"S": ""},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "Model: unknown" in call_kwargs["Message"]

    def test_handler_handles_all_fields_missing_except_status(self):
        """Handler processes record with only status=refusal, all else defaulting."""
        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        mock_sns_client.publish.assert_called_once()
        call_kwargs = mock_sns_client.publish.call_args[1]
        message = call_kwargs["Message"]
        assert "Lambda: unknown" in message
        assert "Model: unknown" in message
        assert "N/A (pipeline)" in message
        assert "Workspace: N/A" in message


# ============================================================================
# Error Handling
# ============================================================================

class TestErrorHandling:
    """Test handler resilience to errors during processing."""

    @staticmethod
    def _refusal(name):
        return {
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "status": {"S": "refusal"},
                    "lambda_name": {"S": name},
                    "request_id": {"S": f"req-{name}"},
                }
            },
        }

    def test_handler_raises_on_sns_publish_error(self):
        """A failed publish must fail the invocation so the stream retries,
        the on_failure destination fires, and the Errors alarm sees it."""
        mock_sns_client.publish.side_effect = ClientError(
            {"Error": {"Code": "KMSAccessDenied", "Message": "denied"}},
            "Publish",
        )

        with pytest.raises(ClientError):
            handler({"Records": [self._refusal("chat")]}, None)

    def test_handler_raises_on_unexpected_publish_exception(self):
        """Non-ClientError failures propagate too."""
        mock_sns_client.publish.side_effect = RuntimeError("Unexpected failure")

        with pytest.raises(RuntimeError):
            handler({"Records": [self._refusal("chat")]}, None)

    def test_handler_stops_at_first_failed_record(self):
        """Records after a failed publish are not sent in this invocation;
        the stream retry delivers them."""
        mock_sns_client.publish.side_effect = Exception("Total failure")

        with pytest.raises(Exception, match="Total failure"):
            handler(
                {"Records": [self._refusal("a"), self._refusal("b")]}, None
            )
        assert mock_sns_client.publish.call_count == 1


# ============================================================================
# Environment Variable Handling
# ============================================================================

class TestEnvironmentVariables:
    """Test behavior dependent on environment variables."""

    @patch.dict(os.environ, {"ENV": "prd"})
    def test_handler_env_prd_in_subject(self, dynamodb_event):
        """ENV=prd appears as PRD in subject."""
        handler(dynamodb_event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "[PRD]" in call_kwargs["Subject"]

    @patch.dict(os.environ, {"ENV": "dev"})
    def test_handler_env_dev_in_subject(self, dynamodb_event):
        """ENV=dev appears as DEV in subject."""
        handler(dynamodb_event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "[DEV]" in call_kwargs["Subject"]

    @patch.dict(os.environ, {}, clear=True)
    def test_handler_env_defaults_to_unknown(self):
        """When ENV is not set, defaults to 'unknown'."""
        # Must also set SNS_TOPIC_ARN since clear=True wipes env
        handler_module.SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:test-topic"

        event = {
            "Records": [
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "status": {"S": "refusal"},
                            "lambda_name": {"S": "chat-handler"},
                            "model_id": {"S": "model"},
                            "flagged_reason": {"S": "reason"},
                            "user_email": {"S": ""},
                            "workspace_id": {"S": ""},
                            "timestamp": {"S": ""},
                            "request_id": {"S": ""},
                            "input_text": {"S": ""},
                            "output_text": {"S": ""},
                        }
                    },
                }
            ]
        }

        handler(event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "[UNKNOWN]" in call_kwargs["Subject"]
        assert "Environment: unknown" in call_kwargs["Message"]

    @patch.dict(os.environ, {"ENV": "dev"})
    def test_handler_env_in_message_body(self, dynamodb_event):
        """ENV value appears in the message body."""
        handler(dynamodb_event, None)

        call_kwargs = mock_sns_client.publish.call_args[1]
        assert "Environment: dev" in call_kwargs["Message"]
