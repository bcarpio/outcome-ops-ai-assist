"""
Lambda handler for hello world test.

This is a simple test Lambda to validate the PR analysis workflow.
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Simple hello world Lambda handler.

    Args:
        event: Lambda event payload
        context: Lambda context object

    Returns:
        dict: API Gateway response with hello world message
    """
    logger.info(f"Hello world handler invoked: {json.dumps(event)}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Hello, World!",
            "requestId": context.request_id
        })
    }
