"""
Lambda handler for listing recent documents with embeddings.

This handler:
1. Validates request payload using Pydantic schemas
2. Scans DynamoDB for documents with embeddings
3. Sorts results by timestamp descending (newest first)
4. Returns limited result set
"""

import json
import logging
import os
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, Field, ValidationError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients once per container (outside handler)
dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")

# Load configuration from environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APPE_NAME", "fantacyai-api")

# Load DynamoDB table name from SSM Parameter Store (happens once per container)
table_param = f"/{ENV}/{APP_NAME}/dynamodb/table"
TABLE_NAME = ssm.get_parameter(Name=table_param)["Parameter"]["Value"]
table = dynamodb.Table(TABLE_NAME)


# Pydantic schemas for request validation
class ListRecentDocsRequest(BaseModel):
    """Request schema for listing recent documents."""

    limit: int = Field(default=10, ge=1, le=100, description="Number of documents to return (1-100)")


class DocumentItem(BaseModel):
    """Schema for a single document item."""

    document_id: str
    title: str
    timestamp: str
    has_embedding: bool


class ListRecentDocsResponse(BaseModel):
    """Response schema for listing recent documents."""

    documents: list[DocumentItem]
    count: int


def full_table_scan(table, filter_expression=None, projection_expression=None, expression_attr_names=None):
    """
    Perform a full table scan with automatic pagination.

    Args:
        table: DynamoDB table resource
        filter_expression: Optional filter expression
        projection_expression: Optional projection expression
        expression_attr_names: Optional expression attribute names

    Returns:
        List of items from the scan
    """
    items = []
    scan_kwargs = {}

    if filter_expression is not None:
        scan_kwargs["FilterExpression"] = filter_expression

    if projection_expression is not None:
        scan_kwargs["ProjectionExpression"] = projection_expression

    if expression_attr_names is not None:
        scan_kwargs["ExpressionAttributeNames"] = expression_attr_names

    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))

        # Check if there are more pages
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

    return items


def process_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process the Lambda event and return recent documents.

    Args:
        event: Lambda event object

    Returns:
        API Gateway response object
    """
    # Parse and validate request body
    try:
        body = json.loads(event.get("body", "{}"))
        request = ListRecentDocsRequest(**body)
        logger.info(f"[list-recent-docs] Validated request: limit={request.limit}")
    except json.JSONDecodeError as e:
        logger.error(f"[list-recent-docs] Invalid JSON: {str(e)}")
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": "Invalid JSON in request body",
                "details": str(e)
            })
        }
    except ValidationError as e:
        logger.error(f"[list-recent-docs] Validation error: {str(e)}")
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": "Validation error",
                "details": e.errors()
            })
        }

    # Scan DynamoDB for documents with embeddings
    try:
        logger.info(f"[list-recent-docs] Scanning table {TABLE_NAME} for documents with embeddings")

        # Scan with filter expression for documents that have embeddings
        # Assuming documents have an "embedding" attribute when processed
        items = full_table_scan(
            table,
            filter_expression=Attr("embedding").exists(),
            projection_expression="document_id, title, #ts, embedding",
            expression_attr_names={
                "#ts": "timestamp"  # "timestamp" might be a reserved word
            }
        )

        logger.info(f"[list-recent-docs] Found {len(items)} documents with embeddings")

        # Sort results by timestamp descending (newest first)
        sorted_items = sorted(
            items,
            key=lambda x: x.get("timestamp", ""),
            reverse=True
        )

        # Limit results
        limited_items = sorted_items[:request.limit]

        # Format response
        documents = [
            DocumentItem(
                document_id=item.get("document_id", ""),
                title=item.get("title", ""),
                timestamp=item.get("timestamp", ""),
                has_embedding=True  # All items have embeddings due to filter
            )
            for item in limited_items
        ]

        response = ListRecentDocsResponse(
            documents=documents,
            count=len(documents)
        )

        logger.info(f"[list-recent-docs] Returning {response.count} documents")

        return {
            "statusCode": 200,
            "body": json.dumps(response.model_dump())
        }

    except table.meta.client.exceptions.ResourceNotFoundException as e:
        logger.error(f"[list-recent-docs] DynamoDB table not found: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Database configuration error",
                "details": "Table not found"
            })
        }

    except Exception as e:
        logger.error(f"[list-recent-docs] DynamoDB scan error: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Internal server error",
                "details": "Failed to retrieve documents"
            })
        }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler entry point.

    Args:
        event: Lambda event object
        context: Lambda context object

    Returns:
        API Gateway response object
    """
    try:
        logger.info(f"[list-recent-docs] Processing request: {json.dumps(event)}")
        result = process_event(event)
        return result

    except Exception as e:
        logger.error(f"[list-recent-docs] Unexpected error: {str(e)}", exc_info=True)
        raise
