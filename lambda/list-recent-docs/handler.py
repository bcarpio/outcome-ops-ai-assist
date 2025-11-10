"""
Lambda handler for listing recent documents.

This handler retrieves a list of recent documents from DynamoDB,
filtered by optional query parameters (limit, status, document_type).
"""

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict

from pydantic import BaseModel, Field, ValidationError

from dynamodb_utils import get_recent_documents

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
TABLE_NAME = os.getenv("TABLE_NAME")
DEFAULT_LIMIT = int(os.getenv("DEFAULT_LIMIT", "10"))
MAX_LIMIT = int(os.getenv("MAX_LIMIT", "100"))


class QueryParameters(BaseModel):
    """Query parameters for listing recent documents."""
    
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    status: str | None = Field(default=None)
    document_type: str | None = Field(default=None)


class DocumentResponse(BaseModel):
    """Response model for a single document."""
    
    document_id: str
    user_id: str
    file_name: str
    document_type: str
    status: str
    upload_date: str
    file_size: int | None = None
    s3_key: str | None = None
    metadata: Dict[Any, Any] | None = None


class ListDocumentsResponse(BaseModel):
    """Response model for listing documents."""
    
    documents: list[DocumentResponse]
    count: int
    limit: int
    filters: Dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """Error response model."""
    
    error: str
    message: str
    details: Dict[Any, Any] | None = None


def decimal_to_number(obj: Any) -> Any:
    """
    Convert Decimal objects to int or float for JSON serialization.
    
    Args:        obj: Object to convert
    
    Returns:     Converted object
    """
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        else:
            return float(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_number(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_number(i) for i in obj]
    return obj


def create_response(status_code: int, body: Dict[Any, Any]) -> Dict[str, Any]:
    """
    Create a standardized API Gateway response.
    
    Args:
        status_code: HTTP status code
        body: Response body dictionary
    
    Returns:
        API Gateway response dictionary
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
            "Access-Control-Allow-Methods": "GET,ROST"
        },
        "body": json.dumps(body)
    }


def parse_query_parameters(event: Dict[str, Any]) -> QueryParameters:
    """
    Parse and validate query parameters from the event.
    
    Args:
        event: Lambda event dictionary
    
    Returns:
        Validated QueryParameters object
    
    Raises:
        ValidationError: If parameters are invalid
    """
    query_params = event.get("queryStringParameters", {}) or {}
    
    # Convert limit to int if present
    if "limit" in query_params:
        try:
            query_params["limit"] = int(query_params["limit"])
        except ValueError as e:
            raise ValidationError(f"Invalid limit value: {str(e)}")
    
    return QueryParameters(**query_params)


def format_document(doc: Dict[Any, Any]) -> DocumentResponse:
    """
    Format a DynamoDB document into a DocumentResponse object.
    
    Args:
        doc: Raw DynamoDB document
    
    Returns:
        Formatted DocumentResponse object
    """
    # Convert Decimal values
    doc = decimal_to_number(doc)
    
    return DocumentResponse(
        document_id=doc["document_id"],
        user_id=doc["user_id"],
        file_name=doc["file_name"],
        document_type=doc["document_type"],
        status=doc["status"],
        upload_date=doc["upload_date"],
        file_size=doc.get("file_size"),
        s3_key=doc.get("s3_key"),
        metadata=doc.get("metadata")
    )


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for listing recent documents.
    
    Args:
        event: Lambda event dictionary
        context: Lambda context object
    
    Returns:
        API Gateway response dictionary
    """
    logger.info("Received event", extra={
        "request_id": context.request_id,
        "http_method": event.get("httpMethod"),
        "path": event.get("path")
    })
    
    # Validate environment variables
    if not TABLE_NAME:
        logger.error("TABLE_NAME environment variable not set")
        return create_response(
            500,
            ErrorResponse(
                error="ConfigurationError",
                message="Server configuration error"
            ).model_dump()
        )
    
    try:
        # Parse and validate query parameters
        try:
            params = parse_query_parameters(event)
            logger.info("Parsed query parameters", extra={
                "limit": params.limit,
                "status": params.status,
                "document_type": params.document_type
            })
        except ValidationError as e:
            logger.warning("Validation error", extra={"error": str(e)})
            return create_response(
                400,
                ErrorResponse(
                    error="ValidationError",
                    message="Invalid query parameters",
                    details={"errors": str(e)}
                ).model_dump()
            )
        
        # Build filters
        filters = {}
        if params.status:
            filters["status"] = params.status
        if params.document_type:
            filters["document_type"] = params.document_type
        
        # Query DynamoDB
        logger.info("Querying DynamoDB", extra={
            "table_name": TABLE_NAME,
            "limit": params.limit,
            "filters": filters
        })
        
        documents = get_recent_documents(
            table_name=TABLE_NAME,
            limit=params.limit,
            filters=filters if filters else None
        )
        
        logger.info("Retrieved documents", extra={
            "count": len(documents)
        })
        
        # Format response
        formatted_docs = [format_document(doc) for doc in documents]
        
        response = ListDocumentsResponse(
            documents=formatted_docs,
            count=len(formatted_docs),
            limit=params.limit,
            filters=filters if filters else None
        )
        
        logger.info("Request completed successfully")
        return create_response(200, response.model_dump())
        
    except ValueError as e:
        logger.error("Value error", extra={"error": str(e)}, exc_info=True)
        return create_response(
            400,
            ErrorResponse(
                error="ValidationError",
                message=str(e)
            ).model_dump()
        )
    
    except Exception as e:
        logger.error("Unexpected error", extra={"error": str(e)}, exc_info=True)
        return create_response(
            500,
            ErrorResponse(
                error="InternalServerError",
                message="An unexpected error occurred"
            ).model_dump()
        )
