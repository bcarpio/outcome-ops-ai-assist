"""
Unit tests for DynamoDB error handling in the Lambda handler.

Covers AWS service errors:
- DynamoDB table not found
- DynamoDB service unavailable
- Scan operation throttling
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError

import handler


class TestDynamoDBErrorHandling:
    """
    Test cases for DynamoDB error handling.
    
    Verifies that the handler:
    - Returns 500 status codes for AWS service errors
    - Logs errors with exc_info=True
    - Handles different error types gracefully
    """

    def setup_method(self):
        """Set up test fixtures before each test."""
        self.context = MagicMock()
        self.context.aws_request_id = "test-request-id"
        self.context.function_name = "test-function"
        
        self.event = {
            "httpMethod": "GET",
            "path": "/health",
            "headers": {},
            "queryStringParameters": None,
            "body": None
        }

    @patch('handler.logger')
    @patch('handler.dynamodb_client')
    def test_dynamodb_table_not_found(self, mock_dynamodb, mock_logger):
        """
        Test handler response when DynamoDB table does not exist.
        
        Verifies:
        - 500 status code is returned
        - Error is logged with exc_info=True
        - Error message is included in response
        """
        # Simulate ResourceNotFoundException from DynamoDB
        mock_dynamodb.scan.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "Requested resource not found"
                }
            },
            "scan"
        )
        
        # Call the handler
        response = handler.handler(self.event, self.context)
        
        # Verify response
        assert response["statusCode"] == 500, "Should return 500 for table not found"
        
        # Verify error message in response
        body = json.loads(response["body"])
        assert "error" in body, "Response should contain error message"
        assert "Internal server error" in body["error"], "Should include generic error message"
        
        # Verify error was logged with exc_info=True
        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args
        assert call_args.kwargs.get("exc_info") is True, "Should log with exc_info=True"

    @patch('handler.logger')
    @patch('handler.dynamodb_client')
    def test_dynamodb_service_unavailable(self, mock_dynamodb, mock_logger):
        """
        Test handler response when DynamoDB service is unavailable.
        
        Verifies:
        - 500 status code is returned
        - Error is logged with exc_info=True
        - Error message is included in response
        """
        # Simulate ServiceUnavailableException from DynamoDB
        mock_dynamodb.scan.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ServiceUnavailableException",
                    "Message": "Service is currently unavailable"
                }
            },
            "scan"
        )
        
        # Call the handler
        response = handler.handler(self.event, self.context)
        
        # Verify response
        assert response["statusCode"] == 500, "Should return 500 for service unavailable"
        
        # Verify error message in response
        body = json.loads(response["body"])
        assert "error" in body, "Response should contain error message"
        assert "Internal server error" in body["error"], "Should include generic error message"
        
        # Verify error was logged with exc_info=True
        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args
        assert call_args.kwargs.get("exc_info") is True, "Should log with exc_info=True"

    @patch('handler.logger')
    @patch('handler.dynamodb_client')
    def test_dynamodb_throttling(self, mock_dynamodb, mock_logger):
        """
        Test handler response when DynamoDB scan operation is throttled.
        
        Verifies:
        - 500 status code is returned
        - Error is logged with exc_info=True
        - Error message is included in response
        """
        # Simulate ProvisionedThroughputExceededException from DynamoDB
        mock_dynamodb.scan.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "You exceeded your maximum allowed provisioned throughput"
                }
            },
            "scan"
        )
        
        # Call the handler
        response = handler.handler(self.event, self.context)
        
        # Verify response
        assert response["statusCode"] == 500, "Should return 500 for throttling"
        
        # Verify error message in response
        body = json.loads(response["body"])
        assert "error" in body, "Response should contain error message"
        assert "Internal server error" in body["error"], "Should include generic error message"
        
        # Verify error was logged with exc_info=True
        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args
        assert call_args.kwargs.get("exc_info") is True, "Should log with exc_info=True"

    @patch('handler.logger')
    @patch('handler.dynamodb_client')
    def test_dynamodb_generic_client_error(self, mock_dynamodb, mock_logger):
        """
        Test handler response for generic DynamoDB client error.
        
        Verifies:
        - 500 status code is returned
        - Error is logged with exc_info=True
        - Error message is included in response
        """
        # Simulate generic ClientError from DynamoDB
        mock_dynamodb.scan.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InternalError",
                    "Message": "An error occurred on the server side"
                }
            },
            "scan"
        )
        
        # Call the handler
        response = handler.handler(self.event, self.context)
        
        # Verify response
        assert response["statusCode"] == 500, "Should return 500 for client error"
        
        # Verify error message in response
        body = json.loads(response["body"])
        assert "error" in body, "Response should contain error message"
        assert "Internal server error" in body["error"], "Should include generic error message"
        
        # Verify error was logged with exc_info=True
        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args
        assert call_args.kwargs.get("exc_info") is True, "Should log with exc_info=True"

    @patch('handler.logger')
    @patch('handler.dynamodb_client')
    def test_dynamodb_internal_server_error(self, mock_dynamodb, mock_logger):
        """
        Test handler response for DynamoDB internal server error.
        
        Verifies:
        - 500 status code is returned
        - Error is logged with exc_info=True
        - Error message is included in response
        """
        # Simulate InternalServerError from DynamoDB
        mock_dynamodb.scan.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InternalServerError",
                    "Message": "The server encountered an internal error"
                }
            },
            "scan"
        )
        
        # Call the handler
        response = handler.handler(self.event, self.context)
        
        # Verify response
        assert response["statusCode"] == 500, "Should return 500 for internal server error"
        
        # Verify error message in response
        body = json.loads(response["body"])
        assert "error" in body, "Response should contain error message"
        assert "Internal server error" in body["error"], "Should include generic error message"
        
        # Verify error was logged with exc_info=True
        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args
        assert call_args.kwargs.get("exc_info") is True, "Should log with exc_info=True"
