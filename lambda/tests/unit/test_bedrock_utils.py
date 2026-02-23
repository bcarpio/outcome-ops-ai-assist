"""
Unit tests for bedrock_utils module.

Tests cover:
- BedrockUsage dataclass (cost calculation, token tracking)
- UsageTracker aggregation and formatting
- call_bedrock function with Bedrock API
"""

import json
import pytest
import sys
import os
import importlib.util
from unittest.mock import Mock, patch, MagicMock
from moto import mock_aws
import boto3
from botocore.exceptions import ClientError

# Load the bedrock_utils module
module_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/bedrock_utils.py'
)
spec = importlib.util.spec_from_file_location("bedrock_utils", module_path)
bedrock_utils = importlib.util.module_from_spec(spec)
sys.modules['bedrock_utils'] = bedrock_utils
spec.loader.exec_module(bedrock_utils)

# Import classes and functions from loaded module
BedrockUsage = bedrock_utils.BedrockUsage
UsageTracker = bedrock_utils.UsageTracker
call_bedrock = bedrock_utils.call_bedrock
SONNET_MODEL_ID = bedrock_utils.SONNET_MODEL_ID
HAIKU_MODEL_ID = bedrock_utils.HAIKU_MODEL_ID
MODEL_PRICING = bedrock_utils.MODEL_PRICING


class TestBedrockUsage:
    """Test BedrockUsage dataclass"""

    def test_total_tokens(self):
        """Test: total_tokens property returns sum of input and output"""
        # Arrange
        usage = BedrockUsage(
            model_id=SONNET_MODEL_ID,
            input_tokens=1000,
            output_tokens=500
        )

        # Act & Assert
        assert usage.total_tokens == 1500

    def test_cost_calculation_sonnet(self):
        """Test: cost calculation for Sonnet model"""
        # Arrange - Sonnet: $3/1M input, $15/1M output
        usage = BedrockUsage(
            model_id=SONNET_MODEL_ID,
            input_tokens=1_000_000,  # 1M tokens = $3.00
            output_tokens=1_000_000   # 1M tokens = $15.00
        )

        # Act & Assert
        assert usage.cost == 18.00  # $3 + $15

    def test_cost_calculation_haiku(self):
        """Test: cost calculation for Haiku model"""
        # Arrange - Haiku: $0.80/1M input, $4/1M output
        usage = BedrockUsage(
            model_id=HAIKU_MODEL_ID,
            input_tokens=1_000_000,  # 1M tokens = $0.80
            output_tokens=1_000_000   # 1M tokens = $4.00
        )

        # Act & Assert
        assert usage.cost == 4.80  # $0.80 + $4.00

    def test_cost_unknown_model(self):
        """Test: unknown model returns zero cost"""
        # Arrange
        usage = BedrockUsage(
            model_id="unknown-model",
            input_tokens=1_000_000,
            output_tokens=1_000_000
        )

        # Act & Assert
        assert usage.cost == 0.0

    def test_model_name_sonnet(self):
        """Test: model_name property returns display name for Sonnet"""
        # Arrange
        usage = BedrockUsage(model_id=SONNET_MODEL_ID)

        # Act & Assert
        assert usage.model_name == "Claude Sonnet 4.5"

    def test_model_name_haiku(self):
        """Test: model_name property returns display name for Haiku"""
        # Arrange
        usage = BedrockUsage(model_id=HAIKU_MODEL_ID)

        # Act & Assert
        assert usage.model_name == "Claude 3.5 Haiku"

    def test_model_name_unknown(self):
        """Test: unknown model returns model_id as name"""
        # Arrange
        usage = BedrockUsage(model_id="custom-model-123")

        # Act & Assert
        assert usage.model_name == "custom-model-123"

    def test_add_usage(self):
        """Test: add method combines usage from another instance"""
        # Arrange
        usage1 = BedrockUsage(
            model_id=SONNET_MODEL_ID,
            input_tokens=1000,
            output_tokens=500
        )
        usage2 = BedrockUsage(
            model_id=SONNET_MODEL_ID,
            input_tokens=2000,
            output_tokens=1000
        )

        # Act
        usage1.add(usage2)

        # Assert
        assert usage1.input_tokens == 3000
        assert usage1.output_tokens == 1500

    def test_add_usage_preserves_first_model_id(self):
        """Test: add method keeps first non-empty model_id"""
        # Arrange
        usage1 = BedrockUsage(
            model_id=SONNET_MODEL_ID,
            input_tokens=1000,
            output_tokens=500
        )
        usage2 = BedrockUsage(
            model_id=HAIKU_MODEL_ID,
            input_tokens=2000,
            output_tokens=1000
        )

        # Act
        usage1.add(usage2)

        # Assert
        assert usage1.model_id == SONNET_MODEL_ID

    def test_add_usage_takes_model_id_if_empty(self):
        """Test: add method takes model_id from second if first is empty"""
        # Arrange
        usage1 = BedrockUsage(
            model_id="",
            input_tokens=1000,
            output_tokens=500
        )
        usage2 = BedrockUsage(
            model_id=HAIKU_MODEL_ID,
            input_tokens=2000,
            output_tokens=1000
        )

        # Act
        usage1.add(usage2)

        # Assert
        assert usage1.model_id == HAIKU_MODEL_ID


class TestUsageTracker:
    """Test UsageTracker aggregation class"""

    def test_add_usage(self):
        """Test: add method appends usage to list"""
        # Arrange
        tracker = UsageTracker()
        usage = BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=100, output_tokens=50)

        # Act
        tracker.add(usage)

        # Assert
        assert len(tracker.usages) == 1
        assert tracker.usages[0] == usage

    def test_total_input_tokens(self):
        """Test: total_input_tokens sums all input tokens"""
        # Arrange
        tracker = UsageTracker()
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=100, output_tokens=50))
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=200, output_tokens=100))

        # Act & Assert
        assert tracker.total_input_tokens == 300

    def test_total_output_tokens(self):
        """Test: total_output_tokens sums all output tokens"""
        # Arrange
        tracker = UsageTracker()
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=100, output_tokens=50))
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=200, output_tokens=100))

        # Act & Assert
        assert tracker.total_output_tokens == 150

    def test_total_tokens(self):
        """Test: total_tokens sums all tokens"""
        # Arrange
        tracker = UsageTracker()
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=100, output_tokens=50))
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=200, output_tokens=100))

        # Act & Assert
        assert tracker.total_tokens == 450  # 300 input + 150 output

    def test_total_cost(self):
        """Test: total_cost sums costs from all usages"""
        # Arrange
        tracker = UsageTracker()
        # Sonnet: $3/1M input, $15/1M output
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=100_000, output_tokens=50_000))
        # Haiku: $0.80/1M input, $4/1M output
        tracker.add(BedrockUsage(model_id=HAIKU_MODEL_ID, input_tokens=200_000, output_tokens=100_000))

        # Act
        total = tracker.total_cost

        # Assert - Sonnet: 0.3 + 0.75 = 1.05, Haiku: 0.16 + 0.4 = 0.56
        assert abs(total - 1.61) < 0.01

    def test_primary_model_empty_tracker(self):
        """Test: primary_model returns Unknown for empty tracker"""
        # Arrange
        tracker = UsageTracker()

        # Act & Assert
        assert tracker.primary_model == "Unknown"

    def test_primary_model_single_model(self):
        """Test: primary_model returns model name when single model used"""
        # Arrange
        tracker = UsageTracker()
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=1000, output_tokens=500))

        # Act & Assert
        assert tracker.primary_model == "Claude Sonnet 4.5"

    def test_primary_model_multiple_models(self):
        """Test: primary_model returns model with most tokens"""
        # Arrange
        tracker = UsageTracker()
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=100, output_tokens=50))  # 150 total
        tracker.add(BedrockUsage(model_id=HAIKU_MODEL_ID, input_tokens=1000, output_tokens=500))  # 1500 total

        # Act & Assert
        assert tracker.primary_model == "Claude 3.5 Haiku"

    def test_format_usage_block_empty(self):
        """Test: format_usage_block returns empty string for empty tracker"""
        # Arrange
        tracker = UsageTracker()

        # Act & Assert
        assert tracker.format_usage_block() == ""

    def test_format_usage_block_with_usage(self):
        """Test: format_usage_block returns formatted markdown"""
        # Arrange
        tracker = UsageTracker()
        tracker.add(BedrockUsage(model_id=SONNET_MODEL_ID, input_tokens=1000, output_tokens=500))

        # Act
        result = tracker.format_usage_block()

        # Assert
        assert "**Usage:**" in result
        assert "Model: Claude Sonnet 4.5" in result
        assert "1,000 input" in result
        assert "500 output" in result
        assert "1,500 total" in result
        assert "Cost: $" in result


class TestCallBedrock:
    """Test call_bedrock function"""

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_success(self, mock_bedrock):
        """Test: Successfully call Bedrock and get response with usage"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "Test response from Claude"}]
                }
            },
            "usage": {
                "inputTokens": 100,
                "outputTokens": 50
            }
        }

        # Act
        response_text, usage = call_bedrock("Test prompt")

        # Assert
        assert response_text == "Test response from Claude"
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.model_id == SONNET_MODEL_ID
        mock_bedrock.converse.assert_called_once()

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_with_haiku_model(self, mock_bedrock):
        """Test: Call Bedrock with Haiku model"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "Haiku response"}]
                }
            },
            "usage": {
                "inputTokens": 200,
                "outputTokens": 100
            }
        }

        # Act
        response_text, usage = call_bedrock("Test prompt", model_id=HAIKU_MODEL_ID)

        # Assert
        assert response_text == "Haiku response"
        assert usage.model_id == HAIKU_MODEL_ID

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_with_custom_params(self, mock_bedrock):
        """Test: Call Bedrock with custom max_tokens and temperature"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "Response"}]
                }
            },
            "usage": {"inputTokens": 50, "outputTokens": 25}
        }

        # Act
        call_bedrock("Prompt", max_tokens=2000, temperature=0.5)

        # Assert
        call_args = mock_bedrock.converse.call_args
        assert call_args[1]["inferenceConfig"]["maxTokens"] == 2000
        assert call_args[1]["inferenceConfig"]["temperature"] == 0.5

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_api_error(self, mock_bedrock):
        """Test: Bedrock API error raises exception"""
        # Arrange
        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "converse"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            call_bedrock("Test prompt")

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_empty_response(self, mock_bedrock):
        """Test: Empty response from Bedrock raises exception"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": []  # Empty content
                }
            }
        }

        # Act & Assert
        with pytest.raises(Exception, match="No content"):
            call_bedrock("Test prompt")

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_missing_text(self, mock_bedrock):
        """Test: Response without text field raises exception"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"image": "base64data"}]  # No text field
                }
            }
        }

        # Act & Assert
        with pytest.raises(Exception, match="No content"):
            call_bedrock("Test prompt")

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_strips_response(self, mock_bedrock):
        """Test: Response text is stripped of whitespace"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "  Response with spaces  \n"}]
                }
            },
            "usage": {"inputTokens": 10, "outputTokens": 5}
        }

        # Act
        response_text, _ = call_bedrock("Prompt")

        # Assert
        assert response_text == "Response with spaces"

    @patch('bedrock_utils.bedrock_client')
    def test_call_bedrock_missing_usage(self, mock_bedrock):
        """Test: Missing usage data defaults to zero"""
        # Arrange
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "Response"}]
                }
            }
            # No usage field
        }

        # Act
        _, usage = call_bedrock("Prompt")

        # Assert
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
