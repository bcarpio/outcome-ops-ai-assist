"""
Delete Worker Lambda for OutcomeOps AI Assist.

Handles document deletion from S3 and S3 Vectors:
1. Single document deletion
2. Prefix-based bulk deletion (e.g., all docs for a connection)
3. Orphan detection and cleanup

Triggered by SQS DELETE_QUEUE messages.

Message Format:
{
    "action": "delete_document" | "delete_prefix" | "delete_orphans",
    "workspace_id": "ws_xxx",
    "source": "confluence",
    "connection_id": "conn_xxx",      # For connection-scoped operations
    "s3_key": "workspaces/...",       # For single document
    "prefix": "space_key/",           # For prefix deletion
    "current_ids": ["id1", "id2"],    # For orphan detection
}
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Set
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
KB_BUCKET = os.environ.get("KB_BUCKET")

# AWS clients
s3_client = boto3.client("s3")
ssm_client = boto3.client("ssm")
s3vectors_client = boto3.client("s3vectors")

# Vector config (loaded from SSM)
_vector_config = None


def _load_vector_config() -> Dict[str, str]:
    """Load vector store configuration from SSM."""
    global _vector_config
    if _vector_config is not None:
        return _vector_config

    prefix = f"/{ENV}/{APP_NAME}/s3vectors"
    try:
        bucket = ssm_client.get_parameter(Name=f"{prefix}/bucket-name")["Parameter"]["Value"]
        index = ssm_client.get_parameter(Name=f"{prefix}/index-name")["Parameter"]["Value"]
        _vector_config = {"bucket": bucket, "index": index}
        return _vector_config
    except ClientError as e:
        logger.error(f"Failed to load vector config: {e}")
        raise


# ============================================================================
# S3 Operations
# ============================================================================


def list_s3_keys_with_prefix(prefix: str) -> List[str]:
    """
    List all S3 keys with a given prefix.

    Args:
        prefix: S3 key prefix

    Returns:
        List of S3 keys
    """
    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")

    try:
        for page in paginator.paginate(Bucket=KB_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except ClientError as e:
        logger.error(f"Error listing S3 keys with prefix {prefix}: {e}")

    return keys


def delete_s3_object(key: str) -> bool:
    """Delete a single S3 object."""
    try:
        s3_client.delete_object(Bucket=KB_BUCKET, Key=key)
        logger.debug(f"Deleted S3 object: {key}")
        return True
    except ClientError as e:
        logger.error(f"Error deleting S3 object {key}: {e}")
        return False


def delete_s3_objects_batch(keys: List[str]) -> int:
    """
    Delete multiple S3 objects in batch.

    Args:
        keys: List of S3 keys to delete

    Returns:
        Number of objects deleted
    """
    if not keys:
        return 0

    deleted = 0

    # S3 delete_objects allows max 1000 keys per request
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        try:
            response = s3_client.delete_objects(
                Bucket=KB_BUCKET,
                Delete={"Objects": [{"Key": k} for k in batch]},
            )
            deleted += len(response.get("Deleted", []))
        except ClientError as e:
            logger.error(f"Error deleting S3 objects batch: {e}")

    return deleted


# ============================================================================
# Vector Operations
# ============================================================================


def delete_vectors_for_s3_key(s3_key: str) -> int:
    """
    Delete all vectors associated with an S3 key.

    Parses the S3 key to build vector key prefix and deletes all chunks.

    Args:
        s3_key: S3 object key

    Returns:
        Number of vector keys deleted
    """
    config = _load_vector_config()

    # Parse S3 key: workspaces/{workspace_id}/{source}/{folder_path}/{filename}
    parts = s3_key.split("/")
    if len(parts) < 4 or parts[0] != "workspaces":
        logger.warning(f"Invalid S3 key format for vector deletion: {s3_key}")
        return 0

    workspace_id = parts[1]
    source = parts[2]
    filename = parts[-1]
    folder_path = "/".join(parts[3:-1]) if len(parts) > 4 else ""

    # Build vector key prefix
    safe_folder = folder_path.replace("/", "#") if folder_path else ""
    if safe_folder:
        key_prefix = f"{workspace_id}#{source}#{safe_folder}#{filename}_chunk_"
    else:
        key_prefix = f"{workspace_id}#{source}#{filename}_chunk_"

    # Delete chunks 0-99
    keys_to_delete = [f"{key_prefix}{i}" for i in range(100)]

    try:
        s3vectors_client.delete_vectors(
            vectorBucketName=config["bucket"],
            indexName=config["index"],
            keys=keys_to_delete,
        )
        logger.info(f"Deleted vectors with prefix {key_prefix}")
        return len(keys_to_delete)
    except ClientError as e:
        logger.warning(f"Error deleting vectors: {e}")
        return 0


def delete_vectors_with_prefix(workspace_id: str, source: str, folder_prefix: str) -> int:
    """
    Delete all vectors matching a prefix pattern.

    Note: S3 Vectors doesn't support prefix-based deletion, so we need to
    list vectors first or use a different approach.

    For now, we delete based on known S3 keys.

    Args:
        workspace_id: Workspace ID
        source: Source type
        folder_prefix: Folder prefix within source

    Returns:
        Number of deletion attempts
    """
    # List S3 keys to find all documents
    s3_prefix = f"workspaces/{workspace_id}/{source}/{folder_prefix}"
    s3_keys = list_s3_keys_with_prefix(s3_prefix)

    deleted = 0
    for key in s3_keys:
        deleted += delete_vectors_for_s3_key(key)

    return deleted


def delete_vectors_by_key_prefix(key_prefix: str) -> int:
    """
    Delete all vectors whose key starts with the given prefix.

    Lists all vectors in the index and deletes those matching the prefix.
    Used for GitHub repos where vector keys follow patterns like:
      {workspace_id}#github#{repo}#code-map#{id}
      {workspace_id}#github#{repo}#handler-group-summary#{group}

    Args:
        key_prefix: Vector key prefix to match (e.g., "ws_123#github#my-repo#")

    Returns:
        Number of vectors deleted
    """
    config = _load_vector_config()
    keys_to_delete = []

    try:
        next_token = None
        while True:
            params = {
                "vectorBucketName": config["bucket"],
                "indexName": config["index"],
                "maxResults": 100,
            }
            if next_token:
                params["nextToken"] = next_token

            response = s3vectors_client.list_vectors(**params)

            for vector in response.get("vectors", []):
                key = vector.get("key", "")
                if key.startswith(key_prefix):
                    keys_to_delete.append(key)

            next_token = response.get("nextToken")
            if not next_token:
                break

    except ClientError as e:
        logger.error(f"Error listing vectors for prefix {key_prefix}: {e}")
        return 0

    if not keys_to_delete:
        logger.info(f"No vectors found with prefix {key_prefix}")
        return 0

    # Delete in batches (delete_vectors API limit)
    deleted = 0
    for i in range(0, len(keys_to_delete), 100):
        batch = keys_to_delete[i:i + 100]
        try:
            s3vectors_client.delete_vectors(
                vectorBucketName=config["bucket"],
                indexName=config["index"],
                keys=batch,
            )
            deleted += len(batch)
        except ClientError as e:
            logger.error(f"Error deleting vector batch: {e}")

    logger.info(f"Deleted {deleted} vectors with prefix {key_prefix}")
    return deleted


# ============================================================================
# Orphan Detection
# ============================================================================


def extract_page_id_from_s3_key(s3_key: str) -> str:
    """
    Extract Confluence page ID from S3 key.

    Key format: workspaces/{workspace_id}/confluence/{space}/{title}_{page_id}.html

    Args:
        s3_key: S3 object key

    Returns:
        Page ID or empty string
    """
    filename = s3_key.split("/")[-1]

    # Remove .html extension
    if filename.endswith(".html"):
        filename = filename[:-5]

    # Extract page_id after last underscore
    if "_" in filename:
        return filename.rsplit("_", 1)[-1]

    return ""


def find_orphaned_documents(
    workspace_id: str,
    source: str,
    folder_prefix: str,
    current_ids: Set[str],
) -> List[str]:
    """
    Find S3 keys for documents that no longer exist in source.

    Args:
        workspace_id: Workspace ID
        source: Source type (e.g., "confluence")
        folder_prefix: Folder/space prefix
        current_ids: Set of current item IDs from source

    Returns:
        List of orphaned S3 keys
    """
    s3_prefix = f"workspaces/{workspace_id}/{source}/{folder_prefix}"
    s3_keys = list_s3_keys_with_prefix(s3_prefix)

    orphaned = []
    for key in s3_keys:
        # Extract ID from key based on source type
        if source == "confluence":
            item_id = extract_page_id_from_s3_key(key)
        elif source == "github":
            # GitHub S3 key: workspaces/{ws}/github/{repo}/{file_path}
            # Strip prefix to get relative file path
            item_id = key[len(s3_prefix):] if key.startswith(s3_prefix) else ""
        else:
            item_id = key.split("/")[-1].split("_")[0]

        if item_id and item_id not in current_ids:
            orphaned.append(key)

    return orphaned


# ============================================================================
# Action Handlers
# ============================================================================


def handle_delete_document(message: Dict) -> Dict[str, Any]:
    """
    Delete a single document and its vectors.

    Args:
        message: Message with s3_key

    Returns:
        Result dict
    """
    s3_key = message.get("s3_key")
    if not s3_key:
        return {"status": "error", "error": "Missing s3_key"}

    logger.info(f"Deleting document: {s3_key}")

    # Delete vectors first
    vectors_deleted = delete_vectors_for_s3_key(s3_key)

    # Delete S3 object
    s3_deleted = delete_s3_object(s3_key)

    return {
        "status": "success",
        "action": "delete_document",
        "s3_key": s3_key,
        "vectors_deleted": vectors_deleted,
        "s3_deleted": s3_deleted,
    }


def handle_delete_prefix(message: Dict) -> Dict[str, Any]:
    """
    Delete all documents with a given prefix.

    Used when disconnecting an integration or deleting a space.

    Args:
        message: Message with workspace_id, source, prefix

    Returns:
        Result dict
    """
    workspace_id = message.get("workspace_id")
    source = message.get("source")
    prefix = message.get("prefix", "")

    if not workspace_id or not source:
        return {"status": "error", "error": "Missing workspace_id or source"}

    s3_prefix = f"workspaces/{workspace_id}/{source}/{prefix}"
    logger.info(f"Deleting all documents with prefix: {s3_prefix}")

    # List all keys
    s3_keys = list_s3_keys_with_prefix(s3_prefix)
    logger.info(f"Found {len(s3_keys)} documents to delete")

    # Delete vectors for each S3 key (works for Confluence-style _chunk_ keys)
    vectors_deleted = 0
    for key in s3_keys:
        vectors_deleted += delete_vectors_for_s3_key(key)

    # Also delete vectors by key prefix pattern (works for GitHub-style keys
    # like {workspace_id}#github#{repo}#code-map#... and
    # {workspace_id}#github#{repo}#handler-group-summary#...)
    if workspace_id and source and prefix:
        # Vector keys use # as separator, not /
        safe_prefix = prefix.replace("/", "#")
        vector_key_prefix = f"{workspace_id}#{source}#{safe_prefix}#"
        vectors_deleted += delete_vectors_by_key_prefix(vector_key_prefix)

    # Delete S3 objects
    s3_deleted = delete_s3_objects_batch(s3_keys)

    return {
        "status": "success",
        "action": "delete_prefix",
        "prefix": s3_prefix,
        "documents_deleted": s3_deleted,
        "vectors_deleted": vectors_deleted,
    }


def handle_delete_orphans(message: Dict) -> Dict[str, Any]:
    """
    Delete orphaned documents not present in current source.

    Args:
        message: Message with workspace_id, source, prefix, current_ids

    Returns:
        Result dict
    """
    workspace_id = message.get("workspace_id")
    source = message.get("source")
    prefix = message.get("prefix", "")
    current_ids = set(message.get("current_ids", []))

    if not workspace_id or not source:
        return {"status": "error", "error": "Missing workspace_id or source"}

    if not current_ids:
        logger.warning("No current_ids provided for orphan detection")
        return {"status": "skipped", "reason": "No current_ids provided"}

    logger.info(f"Detecting orphans for {workspace_id}/{source}/{prefix}")

    # Find orphaned documents
    orphaned_keys = find_orphaned_documents(workspace_id, source, prefix, current_ids)
    logger.info(f"Found {len(orphaned_keys)} orphaned documents")

    if not orphaned_keys:
        return {
            "status": "success",
            "action": "delete_orphans",
            "orphans_found": 0,
            "orphans_deleted": 0,
        }

    # Delete vectors for orphaned documents
    vectors_deleted = 0
    for key in orphaned_keys:
        vectors_deleted += delete_vectors_for_s3_key(key)

    # Delete orphaned S3 objects
    s3_deleted = delete_s3_objects_batch(orphaned_keys)

    return {
        "status": "success",
        "action": "delete_orphans",
        "orphans_found": len(orphaned_keys),
        "orphans_deleted": s3_deleted,
        "vectors_deleted": vectors_deleted,
    }


# ============================================================================
# Main Handler
# ============================================================================


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    SQS-triggered handler for delete operations.

    Routes to action-specific handlers based on 'action' field.
    """
    logger.info(f"Delete worker invoked with {len(event.get('Records', []))} records")

    if "Records" not in event:
        logger.error("No SQS records in event")
        return {"statusCode": 400, "body": "Invalid event"}

    results = []

    for record in event["Records"]:
        try:
            message = json.loads(record["body"])
            action = message.get("action")

            logger.info(f"Processing delete action: {action}")

            if action == "delete_document":
                result = handle_delete_document(message)
            elif action == "delete_prefix":
                result = handle_delete_prefix(message)
            elif action == "delete_orphans":
                result = handle_delete_orphans(message)
            else:
                logger.warning(f"Unknown action: {action}")
                result = {"status": "skipped", "reason": f"Unknown action: {action}"}

            results.append(result)

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in SQS message: {e}")
            results.append({"status": "error", "error": "Invalid JSON"})
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            results.append({"status": "error", "error": str(e)})

    successful = sum(1 for r in results if r.get("status") == "success")
    logger.info(f"Delete worker completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Delete processing completed",
            "results": results,
        }),
    }
