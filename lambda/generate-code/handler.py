"""
Generate Code Lambda Handler.

Main handler that routes between two execution paths:
1. GitHub webhook (API Gateway) → Plan generation
2. SQS message → Step execution

GitHub Webhook Event: https://docs.github.com/en/webhooks/webhook-events-and-payloads#label
"""

import json
import logging
import os
from typing import Any, Dict

import boto3

from github_api import create_branch, get_github_token, get_webhook_secret
from models import (
    APIGatewayProxyEvent,
    GitHubWebhookEvent,
    SQSEvent,
    StepExecutionMessage
)
from plan_generator import handle_webhook
from step_executor import handle_step_message
from utils import verify_webhook_signature, generate_branch_name

# Logging setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")


# ============================================================================
# Event Type Detection
# ============================================================================


def is_sqs_event(event: Dict[str, Any]) -> bool:
    """
    Check if event is from SQS.

    Args:
        event: Lambda event

    Returns:
        bool: True if SQS event
    """
    return "Records" in event and event["Records"] and "eventSource" in event["Records"][0] and event["Records"][0]["eventSource"] == "aws:sqs"


def is_api_gateway_event(event: Dict[str, Any]) -> bool:
    """
    Check if event is from API Gateway.

    Args:
        event: Lambda event

    Returns:
        bool: True if API Gateway event
    """
    return "requestContext" in event and "body" in event


# ============================================================================
# Webhook Handler (API Gateway Path)
# ============================================================================


def handle_api_gateway_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle GitHub webhook event from API Gateway.

    Flow:
    1. Verify webhook signature
    2. Parse webhook payload
    3. Create branch (or verify it exists)
    4. Generate execution plan
    5. Commit plan to branch
    6. Send first step to SQS

    Args:
        event: API Gateway event

    Returns:
        dict: API Gateway response (always 200 for webhooks)
    """
    logger.info(f"[generate-code] Received API Gateway event")

    try:
        # Parse API Gateway event
        gateway_event = APIGatewayProxyEvent(**event)

        # Verify webhook signature
        signature = gateway_event.headers.get("x-hub-signature-256", "")
        webhook_secret = get_webhook_secret()

        if not verify_webhook_signature(gateway_event.body, signature, webhook_secret):
            logger.warning("Webhook signature validation failed")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Invalid signature"})
            }

        # Parse webhook payload
        try:
            webhook_event = GitHubWebhookEvent(**gateway_event.parsed_body)
        except ValueError as e:
            # Not a "labeled" action or wrong label - ignore silently
            logger.info(f"Ignoring event: {str(e)}")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Event ignored"})
            }

        logger.info(
            f"Processing issue #{webhook_event.issue.number} "
            f"in {webhook_event.repository.full_name}"
        )

        # Generate branch name
        branch_name = generate_branch_name(
            webhook_event.issue.number,
            webhook_event.issue.title
        )

        logger.info(f"Generated branch name: {branch_name}")

        # Get GitHub token
        github_token = get_github_token()

        # Create branch
        result = create_branch(
            repo_full_name=webhook_event.repository.full_name,
            branch_name=branch_name,
            base_branch=webhook_event.repository.default_branch,
            github_token=github_token
        )

        if not result.get("success"):
            logger.error(f"Failed to create branch: {result.get('error')}")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "message": "Failed to create branch",
                    "error": result.get("error")
                })
            }

        # Generate execution plan and send first step to SQS
        plan_result = handle_webhook(webhook_event)

        return {
            "statusCode": 200,
            "body": json.dumps(plan_result)
        }

    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        # Always return 200 to prevent webhook retries
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Internal error",
                "error": str(e)
            })
        }


# ============================================================================
# SQS Handler (Step Execution Path)
# ============================================================================


def handle_sqs_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle SQS step execution message.

    Flow:
    1. Parse SQS message
    2. Get plan from branch
    3. Execute current step (query KB, generate code, commit files)
    4. Update plan
    5. If more steps: send next step to SQS
    6. If all steps done: create PR

    Args:
        event: SQS event

    Returns:
        dict: SQS response with batchItemFailures
    """
    logger.info(f"[generate-code] Received SQS event")

    sqs_event = SQSEvent(**event)

    # Track failures for partial batch failure reporting
    batch_item_failures = []

    for record in sqs_event.Records:
        try:
            # Parse step execution message
            step_message = StepExecutionMessage(**record.parsed_body)

            logger.info(
                f"Processing step {step_message.current_step}/{step_message.total_steps} "
                f"for issue #{step_message.issue_number}"
            )

            # Execute step
            result = handle_step_message(step_message)

            logger.info(f"Step execution result: {result}")

        except Exception as e:
            logger.error(f"Error processing SQS message: {e}", exc_info=True)

            # Add to batch failures (SQS will retry)
            batch_item_failures.append({
                "itemIdentifier": record.messageId
            })

    # Return batch item failures for SQS to retry
    return {
        "batchItemFailures": batch_item_failures
    }


# ============================================================================
# Lambda Handler (Entry Point)
# ============================================================================


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for GitHub webhook events and SQS step execution.

    Routes to appropriate handler based on event type:
    - API Gateway → Webhook handler (plan generation)
    - SQS → Step execution handler

    Args:
        event: Lambda event (API Gateway or SQS)
        context: Lambda context (unused)

    Returns:
        dict: Response based on event type
    """
    logger.info(f"[generate-code] Received event: {json.dumps(event)}")

    # Route based on event type
    if is_sqs_event(event):
        logger.info("[generate-code] Routing to SQS handler (step execution)")
        return handle_sqs_event(event)

    elif is_api_gateway_event(event):
        logger.info("[generate-code] Routing to API Gateway handler (webhook)")
        return handle_api_gateway_event(event)

    else:
        logger.error(f"[generate-code] Unknown event type: {event}")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Unknown event type"})
        }
