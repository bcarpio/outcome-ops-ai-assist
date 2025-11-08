"""
SQS client for code generation queue.

Handles sending step execution messages to the FIFO queue.
"""

import hashlib
import json
import logging
import os
from typing import Dict, Any

import boto3
from botocore.exceptions import ClientError

from models import StepExecutionMessage

logger = logging.getLogger()

# AWS clients
sqs_client = boto3.client("sqs")
ssm_client = boto3.client("ssm")

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# FIFO queue group ID (ensures sequential processing)
MESSAGE_GROUP_ID = "code-generation"


# ============================================================================
# Queue URL Retrieval
# ============================================================================


def get_queue_url() -> str:
    """
    Fetch SQS queue URL from SSM Parameter Store.

    Returns:
        str: SQS queue URL

    Raises:
        Exception: If queue URL not found in SSM
    """
    param_name = f"/{ENV}/{APP_NAME}/sqs/code-generation-queue-url"

    try:
        response = ssm_client.get_parameter(Name=param_name)

        queue_url = response.get("Parameter", {}).get("Value")
        if not queue_url:
            raise Exception(f"Queue URL not found in parameter: {param_name}")

        logger.info(f"[sqs] Retrieved queue URL from SSM: {param_name}")
        return queue_url

    except ClientError as e:
        logger.error(f"[sqs] Failed to retrieve queue URL: {e}")
        raise


# ============================================================================
# Message Sending
# ============================================================================


def send_step_message(message: StepExecutionMessage) -> Dict[str, Any]:
    """
    Send step execution message to SQS FIFO queue.

    Uses MessageDeduplicationId based on message content hash to prevent duplicates.
    Uses MessageGroupId to ensure sequential processing within the same issue.

    Args:
        message: Step execution message to send

    Returns:
        dict: Send message result with MessageId

    Raises:
        Exception: If sending message fails
    """
    queue_url = get_queue_url()

    # Convert message to JSON
    message_body = message.json()

    # Generate deduplication ID from message content hash
    # This prevents duplicate messages if Lambda retries
    content_hash = hashlib.sha256(message_body.encode()).hexdigest()
    deduplication_id = content_hash[:128]  # Max 128 chars

    # Use issue number as part of group ID to ensure sequential processing per issue
    group_id = f"{MESSAGE_GROUP_ID}-{message.issue_number}"

    try:
        logger.info(
            f"[sqs] Sending step {message.current_step}/{message.total_steps} "
            f"for issue #{message.issue_number} to queue"
        )

        response = sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=message_body,
            MessageGroupId=group_id,
            MessageDeduplicationId=deduplication_id
        )

        message_id = response.get("MessageId")
        logger.info(f"[sqs] Message sent successfully: {message_id}")

        return {
            "success": True,
            "message_id": message_id
        }

    except ClientError as e:
        logger.error(f"[sqs] Failed to send message to queue: {e}")
        raise Exception(f"SQS error: {str(e)}")
