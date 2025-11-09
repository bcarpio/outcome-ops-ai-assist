"""
Lambda handler for listing recently accessed documents.

Fetches documents from DynamoDB where last_accessed_at exists,
sorts by last_accessed_at descending, and returns the most recent documents.
"""

import boto3
import json
import os
from boto3.dynamodb.conditions import Attr
from decimal import Decimal
from typing import Any, Dict, List

# Initialize AWS clients (once per container)
dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")

# Load environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Load configuration from SSM Parameter Store (happens once per container)
table_param = f"/{ENV}/{APP_NAME}/dynamodb/table"
TABLE_NAME = ssm.get_parameter(Name=table_param)["Parameter"]["Value"]
table = dynamodb.Table(TABLE_NAME)

# Default limit for number of documents to return
DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def decimal_default(val: Any) -> Any:
    """Convert Decimal objects to float for JSON serialization."""
    if isinstance(val, Decimal):
        return float(val)
    raise TypeError(f"Type {type(val)} not JSON serializable")


def validate_request(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate request parameters and return validated values.

    Args:
        event: Lambda event object

    Returns:
        Dictionary with validated parameters

    Raises:
        ValueError: If parameters are invalid
    """
    # Extract query parameters (handles both API Gateway and direct invocation)
    query_params = event.get("queryStringParameters", {}) or {}

    # Get limit parameter
    limit_str = query_params.get("limit", str(DEFAULT_LIMIT))

    try:
        limit = int(limit_str)
        if limit < 1 or limit > MAX_LIMIT:
            raise ValueError(
                f"Limit must be between 1 and {MAX_LIMIT}, got {limit}"
            )
    except ValueError as e:
        if "must be between" in str(e):
            raise
        raise ValueError(f"Invalid limit parameter: {limit_str}") from e

    return {"limit": limit}


def scan_documents() -> List[Dict[str, Any]]:
    """
    Scan DynamoDB for documents with last_accessed_at attribute.

    Returns:
        List of document items
    """
    documents = []

    # Scan with filter expression to only get documents with last_accessed_at
    filter_expression = Attr("last_accessed_at").exists()

    # Projection expression to only retrieve needed attributes
    projection_expression = ("PK, SK, document_id, document_name, "
                             "last_accessed_at, access_count")

    # Handle pagination for large tables
    scan_kwargs = {
        "FilterExpression": filter_expression,
        "ProjectionExpression": projection_expression,
    }

    while True:
        response = table.scan(**scan_kwargs)
        documents.extend(response.get("Items", []))

        # Check if there are more items to scan
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

    return documents


def sort_and_limit_documents(
    documents: List[Dict[str, Any]], limit: int
) -> List[Dict[str, Any]]:
    """
    Sort documents by last_accessed_at descending and limit results.

    Args:
        documents: List of document items
        limit: Maximum number of documents to return

    Returns:
        Sorted and limited list of documents
    """
    # Sort by last_accessed_at descending (reverse=True)
    sorted_docs = sorted(
        documents,
        key=lambda x: x.get("last_accessed_at", ""),
        reverse=True,
    )

    # Limit results
    return sorted_docs[:limit]


def format_response(
    status_code: int,
    body: Dict[str, Any],
    headers: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """
    Format Lambda response for API Gateway.

    Args:
        status_code: HTTP status code
        body: Response body dictionary
        headers: Optional custom headers

    Returns:
        Formatted Lambda response
    """
    default_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "GET,OPTIONS",
    }

    if headers:
        default_headers.update(headers)

    return {
        "statusCode": status_code,
        "headers": default_headers,
        "body": json.dumps(body, default=decimal_default),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for listing recently accessed documents.

    Args:
        event: Lambda event object
        context: Lambda context object

    Returns:
        API Gateway response object
    """
    try:
        # Validate request parameters
        params = validate_request(event)
        limit = params["limit"]

        # Scan documents from DynamoDB
        documents = scan_documents()

        # Sort and limit results
        recent_docs = sort_and_limit_documents(documents, limit)

        # Format response
        response_body = {
            "documents": recent_docs,
            "count": len(recent_docs),
            "total_scanned": len(documents),
        }

        return format_response(200, response_body)

    except ValueError as e:
        # Validation errors
        return format_response(
            400, {"error": "Bad Request", "message": str(e)}
        )

    except Exception as e:
        # Unexpected errors
        print(f"Error processing request: {str(e)}")
        return format_response(
            500,
            {
                "error": "Internal Server Error",
                "message": "An unexpected error occurred",
            },
        )
