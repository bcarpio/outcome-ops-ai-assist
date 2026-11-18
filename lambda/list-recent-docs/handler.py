# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Lambda handler for listing recently ingested knowledge base documents.

Internal worker (ADR-004), invoked directly by the ``outcome-ops-assist``
CLI (``outcome-ops-assist list-recent-docs``). Lets an operator confirm that
KB ingestion is landing documents without opening the console.

Embeddings live in S3 Vectors (not DynamoDB), so this reads the vector
index metadata via ``shared.retrieval.list_recent_documents``: one entry per
document (its first chunk), newest ``timestamp`` first.

Request:
    {
        "limit": 10,            // Optional, 1-100, default 10
        "workspace_id": "..."   // Optional, scope to one workspace
    }

Response:
    {
        "statusCode": 200,
        "body": {
            "documents": [
                {"key", "workspace_id", "repo", "doc_type", "file_path", "ingested_at"}
            ],
            "count": 10,
            "scanned": 48211,
            "truncated": false
        }
    }
"""

import json
import logging
import os
import sys
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import retrieval as shared_retrieval

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
ssm_client = boto3.client("ssm")
s3vectors_client = boto3.client("s3vectors")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
MAX_SCAN = int(os.environ.get("MAX_SCAN", str(shared_retrieval.DEFAULT_LIST_MAX_SCAN)))

# Configuration (loaded from SSM on first invocation, cached per container)
_vector_config: Optional[Dict[str, str]] = None


class ListRecentDocsRequest(BaseModel):
    """Request schema for list-recent-docs Lambda."""

    limit: int = Field(10, ge=1, le=100, description="Number of documents to return")
    workspace_id: Optional[str] = Field(
        None, min_length=1, description="Optional workspace scope"
    )


def load_vector_config() -> Dict[str, str]:
    """Load S3 Vectors bucket/index names from SSM (cached per container)."""
    global _vector_config
    if _vector_config is not None:
        return _vector_config

    prefix = f"/{ENVIRONMENT}/{APP_NAME}/s3vectors"
    try:
        bucket = ssm_client.get_parameter(Name=f"{prefix}/bucket-name")["Parameter"]["Value"]
        index = ssm_client.get_parameter(Name=f"{prefix}/index-name")["Parameter"]["Value"]
    except ClientError as e:
        logger.error(f"[list-recent-docs] Failed to load vector config from SSM: {e}")
        raise

    _vector_config = {"bucket": bucket, "index": index}
    logger.info(f"[list-recent-docs] Configuration loaded: BUCKET={bucket}, INDEX={index}")
    return _vector_config


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Structured internal-worker response (ADR-004)."""
    return {"statusCode": status_code, "body": body}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Main Lambda entry point."""
    logger.info(f"[list-recent-docs] Handler invoked: {json.dumps(event, default=str)}")

    try:
        request = ListRecentDocsRequest.model_validate(event or {})
    except ValidationError as e:
        logger.warning(f"[list-recent-docs] Invalid request: {e}")
        return _response(400, {"error": "Invalid request", "details": e.errors(include_url=False)})

    try:
        config = load_vector_config()
        result = shared_retrieval.list_recent_documents(
            s3vectors_client=s3vectors_client,
            vector_bucket=config["bucket"],
            vector_index=config["index"],
            limit=request.limit,
            workspace_id=request.workspace_id,
            max_scan=MAX_SCAN,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "ClientError")
        logger.error(f"[list-recent-docs] AWS error listing vectors: {e}", exc_info=True)
        return _response(502, {"error": f"Failed to list documents ({code})"})
    except Exception as e:
        logger.error(f"[list-recent-docs] Unexpected error: {e}", exc_info=True)
        return _response(500, {"error": "Internal error"})

    documents = result["documents"]
    logger.info(
        f"[list-recent-docs] Returning {len(documents)} document(s); "
        f"scanned={result['scanned']} truncated={result['truncated']}"
    )
    return _response(
        200,
        {
            "documents": documents,
            "count": len(documents),
            "scanned": result["scanned"],
            "truncated": result["truncated"],
        },
    )
