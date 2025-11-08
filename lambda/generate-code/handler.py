"""
Generate Code Lambda Handler.

Main handler that routes between two execution paths:
1. GitHub webhook (API Gateway) → Send message to SQS for async plan generation
2. SQS message → Route to plan generation OR step execution based on action field

GitHub Webhook Event: https://docs.github.com/en/webhooks/webhook-events-and-payloads#label
"""

import json
import logging
import os
from typing import Any, Dict
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

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

# AWS clients
ssm_client = boto3.client("ssm")
sqs_client = boto3.client("sqs")


# ============================================================================
# SQS Helper Functions
# ============================================================================


def get_queue_url() -> str:
    """
    Fetch SQS queue URL from SSM Parameter Store.

    Returns:
        str: SQS queue URL for code generation

    Raises:
        Exception: If queue URL not found in SSM
    """
    param_name = f"/{ENV}/{APP_NAME}/sqs/code-generation-queue-url"

    try:
        response = ssm_client.get_parameter(
            Name=param_name,
            WithDecryption=False  # Queue URL is not encrypted
        )

        queue_url = response.get("Parameter", {}).get("Value")
        if not queue_url:
            raise Exception(f"Queue URL not found in parameter: {param_name}")

        logger.info(f"Retrieved queue URL from SSM: {param_name}")
        return queue_url

    except ClientError as e:
        logger.error(f"Failed to retrieve queue URL: {e}")
        raise


def send_plan_generation_message(
    issue_number: int,
    issue_title: str,
    issue_description: str,
    repo_full_name: str,
    branch_name: str,
    base_branch: str
) -> Dict[str, Any]:
    """
    Send plan generation message to SQS queue.

    Args:
        issue_number: GitHub issue number
        issue_title: GitHub issue title
        issue_description: GitHub issue body/description
        repo_full_name: Full repository name (owner/repo)
        branch_name: Branch to create code in
        base_branch: Base branch (usually main)

    Returns:
        dict: SQS SendMessage response

    Raises:
        Exception: If message send fails
    """
    queue_url = get_queue_url()

    message_body = StepExecutionMessage(
        action="generate_plan",
        issue_number=issue_number,
        issue_title=issue_title,
        issue_description=issue_description,
        repo_full_name=repo_full_name,
        branch_name=branch_name,
        current_step=0,
        total_steps=0,
        base_branch=base_branch
    ).model_dump_json()

    try:
        response = sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=message_body,
            MessageGroupId=f"issue-{issue_number}",  # FIFO queue requirement
            MessageDeduplicationId=str(uuid4())  # Unique ID for deduplication
        )

        logger.info(
            f"Sent plan generation message to SQS for issue #{issue_number}. "
            f"MessageId: {response['MessageId']}"
        )

        return response

    except ClientError as e:
        logger.error(f"Failed to send message to SQS: {e}")
        raise


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
    4. Send plan generation message to SQS (returns immediately)

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

        # Send plan generation message to SQS for async processing
        send_plan_generation_message(
            issue_number=webhook_event.issue.number,
            issue_title=webhook_event.issue.title,
            issue_description=webhook_event.issue.body or "",
            repo_full_name=webhook_event.repository.full_name,
            branch_name=branch_name,
            base_branch=webhook_event.repository.default_branch
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Code generation started",
                "issue_number": webhook_event.issue.number,
                "branch_name": branch_name
            })
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
    Handle SQS message (either plan generation or step execution).

    Flow depends on message action field:

    For action="generate_plan":
    1. Parse SQS message
    2. Query knowledge base for context
    3. Generate execution plan with Claude
    4. Commit plan to branch
    5. Send first step to SQS

    For action="execute_step":
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

            # Route based on action field
            if step_message.action == "generate_plan":
                logger.info(
                    f"Generating plan for issue #{step_message.issue_number}"
                )

                # Convert StepExecutionMessage to GitHubWebhookEvent format
                # This is a bit hacky but allows reuse of existing plan_generator code
                from models import GitHubIssue, GitHubRepository

                mock_webhook_event = type('obj', (object,), {
                    'issue': GitHubIssue(
                        number=step_message.issue_number,
                        title=step_message.issue_title,
                        body=step_message.issue_description,
                        html_url=f"https://github.com/{step_message.repo_full_name}/issues/{step_message.issue_number}",
                        state="open"
                    ),
                    'repository': GitHubRepository(
                        name=step_message.repo_full_name.split('/')[-1],
                        full_name=step_message.repo_full_name,
                        owner={"login": step_message.repo_full_name.split('/')[0]},
                        default_branch=step_message.base_branch
                    )
                })()

                # Generate execution plan (this will send first step to SQS)
                result = handle_webhook(mock_webhook_event)

                logger.info(f"Plan generation result: {result}")

            elif step_message.action == "execute_step":
                logger.info(
                    f"Executing step {step_message.current_step}/{step_message.total_steps} "
                    f"for issue #{step_message.issue_number}"
                )

                # Execute step
                result = handle_step_message(step_message)

                logger.info(f"Step execution result: {result}")

            else:
                logger.error(f"Unknown action: {step_message.action}")
                raise ValueError(f"Unknown action: {step_message.action}")

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
