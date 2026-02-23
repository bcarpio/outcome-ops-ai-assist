"""
Audit Alert Lambda - DynamoDB Streams consumer for refusal detection.

Triggered by DynamoDB Streams on the audit-logs table when a new record
is inserted with status="refusal". Sends an SNS notification with audit metadata.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
sns_client = boto3.client("sns")


def handler(event, context):
    """
    Process DynamoDB Stream events for refusal alerts.

    The event source mapping filter ensures only INSERT events with
    status="refusal" reach this handler.
    """
    if not SNS_TOPIC_ARN:
        logger.warning("[audit-alert] SNS_TOPIC_ARN not configured, skipping")
        return {"statusCode": 200, "body": "No SNS topic configured"}

    for record in event.get("Records", []):
        try:
            if record.get("eventName") != "INSERT":
                continue

            new_image = record.get("dynamodb", {}).get("NewImage", {})
            if not new_image:
                continue

            # Extract fields from DynamoDB stream record (typed attribute values)
            status = new_image.get("status", {}).get("S", "")
            if status != "refusal":
                continue

            lambda_name = new_image.get("lambda_name", {}).get("S", "unknown")
            model_id = new_image.get("model_id", {}).get("S", "unknown")
            flagged_reason = new_image.get("flagged_reason", {}).get("S", "")
            user_email = new_image.get("user_email", {}).get("S", "")
            workspace_id = new_image.get("workspace_id", {}).get("S", "")
            timestamp = new_image.get("timestamp", {}).get("S", "")
            request_id = new_image.get("request_id", {}).get("S", "")
            input_text = new_image.get("input_text", {}).get("S", "")
            output_text = new_image.get("output_text", {}).get("S", "")

            env = os.environ.get("ENV", "unknown")

            if flagged_reason == "test_refusal_trigger":
                subject = f"[TEST] AI Refusal Alert - {env.upper()}"
            else:
                subject = f"[{env.upper()}] AI Refusal Alert - {lambda_name}"

            message = (
                f"AI Refusal Detected\n"
                f"{'=' * 50}\n\n"
                f"Environment: {env}\n"
                f"Lambda: {lambda_name}\n"
                f"Model: {model_id}\n"
                f"Timestamp: {timestamp}\n"
                f"Request ID: {request_id}\n"
                f"User: {user_email or 'N/A (pipeline)'}\n"
                f"Workspace: {workspace_id or 'N/A'}\n"
                f"Flagged Reason: {flagged_reason}\n\n"
                f"Input (first 500 chars):\n"
                f"{input_text[:500]}\n\n"
                f"Output (first 500 chars):\n"
                f"{output_text[:500]}\n"
            )

            sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=subject[:100],  # SNS subject max 100 chars
                Message=message,
            )

            logger.info(
                f"[audit-alert] Sent refusal alert for {lambda_name} "
                f"(request_id={request_id})"
            )

        except Exception as e:
            logger.error(f"[audit-alert] Failed to process record: {e}")

    return {"statusCode": 200, "body": "Processed"}
