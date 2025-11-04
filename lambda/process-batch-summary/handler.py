"""
Lambda handler for processing code map batch summaries from SQS.

This handler:
1. Receives batch messages from SQS FIFO queue
2. Fetches file contents from GitHub API
3. Generates batch summaries using Claude via Bedrock
4. Creates embeddings using Bedrock Titan v2
5. Stores summaries in DynamoDB with embeddings
6. Returns successfully processed records or raises errors for retry

Triggered by: SQS event source mapping
Documentation: docs/lambda-process-batch-summary.md
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# AWS clients (region inferred from environment)
dynamodb_client = boto3.client("dynamodb")
bedrock_client = boto3.client("bedrock-runtime")
ssm_client = boto3.client("ssm")

# Configuration (loaded from SSM at container startup)
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

CODE_MAPS_TABLE = None
GITHUB_TOKEN = None

GITHUB_API_URL = "https://api.github.com"


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global CODE_MAPS_TABLE, GITHUB_TOKEN

    try:
        code_maps_param = f"/{ENVIRONMENT}/{APP_NAME}/dynamodb/code-maps-table"
        CODE_MAPS_TABLE = ssm_client.get_parameter(Name=code_maps_param)["Parameter"]["Value"]

        github_token_param = f"/{ENVIRONMENT}/{APP_NAME}/github/token"
        GITHUB_TOKEN = ssm_client.get_parameter(
            Name=github_token_param,
            WithDecryption=True
        )["Parameter"]["Value"]

        logger.info(f"Configuration loaded: TABLE={CODE_MAPS_TABLE}")
    except ClientError as e:
        logger.error(f"Failed to load configuration from SSM: {e}")
        raise


def fetch_file_content(repo: str, file_path: str) -> str:
    """
    Fetch raw file content from GitHub.

    Args:
        repo: Repository in format "owner/repo"
        file_path: Path to file in repository

    Returns:
        Raw file content as string
    """
    url = f"https://raw.githubusercontent.com/{repo}/main/{file_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "outcome-ops-process-batch-summary",
    }

    try:
        request = Request(url, headers=headers)
        with urlopen(request) as response:
            return response.read().decode("utf-8")
    except URLError as e:
        logger.error(f"Failed to fetch {file_path} from GitHub: {e}")
        raise


def retry_bedrock_call(func, max_retries: int = 5):
    """
    Retry helper for Bedrock calls with exponential backoff.

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts (default 5)

    Returns:
        Result from successful function call
    """
    import time
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except ClientError as e:
            last_error = e
            error_code = e.response.get("Error", {}).get("Code", "")

            is_retryable = error_code in [
                "ThrottlingException",
                "ServiceUnavailableException",
                "InternalServerException"
            ]

            if not is_retryable or attempt == max_retries:
                raise

            # VERY aggressive exponential backoff for severe throttling
            # 10s, 20s, 40s, 80s, 120s = up to 2 minutes per retry
            delay_s = min(10 * (2 ** (attempt - 1)), 120)
            logger.warning(f"Bedrock throttled (attempt {attempt}/{max_retries}), waiting {delay_s}s before retry...")
            time.sleep(delay_s)

    raise last_error


def generate_batch_summary(batch: Dict[str, Any]) -> str:
    """
    Generate batch summary using Claude via Bedrock.

    Args:
        batch: Batch message containing repo, batch_type, group_name, file_paths

    Returns:
        Generated summary text
    """
    repo = batch["repo"]
    repo_project = batch["repo_project"]
    batch_type = batch["batch_type"]
    group_name = batch["group_name"]
    file_paths = batch["file_paths"]

    # Fetch all file contents
    file_contents = []

    for file_path in file_paths:
        try:
            content = fetch_file_content(repo_project, file_path)

            # Skip very large files
            if len(content) > 50000:
                logger.info(f"Skipping large file in batch: {file_path} ({len(content)} chars)")
                continue

            # Truncate to 10KB per file
            file_contents.append({
                "path": file_path,
                "content": content[:10000]
            })
        except Exception as e:
            logger.error(f"Error fetching file {file_path}: {e}")
            # Continue with other files

    if not file_contents:
        return "No files available for analysis"

    # Build files section for prompt
    files_section = "\n".join([
        f"File: {fc['path']}\n```\n{fc['content']}\n{'... (truncated)\n' if len(fc['content']) == 10000 else ''}```\n"
        for fc in file_contents
    ])

    # Build prompt based on batch type
    prompt_templates = {
        "infrastructure": f"Summarize the infrastructure for {repo} based on these Terraform files. Describe what resources are created, how they're organized, and the overall architecture.\n\n{files_section}",
        "handler-group": f"""Analyze the {group_name} Lambda handler for {repo} and provide detailed implementation documentation for debugging.

Extract the following for EACH function in this handler:

## Function Signatures
- Function name and parameters with types
- Parameter format specifications (e.g., UUID format, slug format: {{{{name[:90]}}}}-{{{{id[:6]}}}})
- Query string parameters (for API handlers) or event structure (for event-driven handlers)
- Required vs optional parameters

## Query Patterns
- DynamoDB query patterns (PK/SK keys, filters, scans, indexes)
- S3 access patterns (bucket, key patterns)
- External API calls and their purposes

## Returns and Errors
- Success responses with HTTP status codes (e.g., 200, 201)
- Error responses with HTTP status codes (e.g., 400, 404, 500)
- What triggers each error condition
- Response body structure for success and error cases

## Data Contracts
- Data format specifications (slug format, ID format, timestamp format, etc.)
- Where data is stored (DynamoDB table/fields, S3 paths, SSM parameters)
- Data relationships (e.g., slug derived from name + first 6 chars of ID)
- Field names and their types

## Common Pitfalls
- Incorrect parameter usage patterns that cause errors
- What will cause 404s (not found), 400s (bad request), or 500s (server errors)
- Incorrect assumptions about data formats
- Examples:
  - "❌ Passing UUID as 'slug' parameter → 404 not found"
  - "✅ Pass UUID as 'id' parameter OR actual slug as 'slug' parameter"
  - "❌ Assuming slug equals character_id"
  - "✅ Slug is derived from name + first 6 chars of ID"

## Cross-References
- Related functions in other handlers (with file paths)
- Shared utilities and their locations (e.g., jwt_helper.py, utils.py)
- Dependencies on other Lambdas or AWS services
- Where data transformations happen (e.g., slugify() function in character_management)

Format the output as structured documentation that helps developers debug issues and understand implementation details, not just high-level purpose.

{files_section}
""",
        "frontend-pages": f"Summarize the pages/routes for {repo}. Describe what pages exist, their routes, key features, state management patterns, and how they connect to the overall application.\n\n{files_section}",
        "frontend-components": f"Summarize the React components for {repo}. Describe what UI components are provided, their props/interfaces, reusability patterns, and styling approaches.\n\n{files_section}",
        "frontend-tests": f"Summarize the frontend tests for {repo}. Describe what components/features are tested, testing library used (Jest, Vitest, etc.), and testing patterns.\n\n{files_section}",
        "frontend-utils": f"Summarize the frontend utilities and hooks for {repo}. Describe what helper functions, custom hooks, and context providers are available and how they're used.\n\n{files_section}",
        "frontend-types": f"Summarize the TypeScript types and interfaces for {repo}. Describe what data structures, API types, and component prop types are defined.\n\n{files_section}",
        "tests": f"Summarize the {group_name} tests for {repo}. Describe what components are tested, testing patterns used, and coverage approach.\n\n{files_section}",
        "shared": f"Summarize the {group_name} shared code for {repo}. Describe what utilities/helpers are provided and how they're used across the codebase.\n\n{files_section}",
        "schemas": f"Summarize the data schemas for {repo}. Describe what data structures are defined, validation rules, and how they're used.\n\n{files_section}",
        "docs": f"Summarize the documentation for {repo}. Describe what topics are covered and how the documentation is organized.\n\n{files_section}",
    }

    prompt = prompt_templates.get(
        batch_type,
        f"Summarize these {group_name} files for {repo}.\n\n{files_section}"
    )

    def call_claude():
        payload = {
            "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ],
            "inferenceConfig": {
                "maxTokens": 8192,  # Increased for larger summaries
                "temperature": 0.3,
            },
        }

        response = bedrock_client.converse(**payload)

        output_message = response.get("output", {}).get("message", {})
        content = output_message.get("content", [])

        if not content or not content[0].get("text"):
            raise ValueError("No content in Claude response")

        return content[0]["text"]

    summary = retry_bedrock_call(call_claude)
    logger.info(f"Generated batch summary for {group_name} ({len(summary)} chars)")
    return summary


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding using Bedrock Titan Embeddings v2.

    Args:
        text: Text to embed

    Returns:
        1024-dimensional embedding vector
    """
    def call_titan():
        payload = {
            "inputText": text,
            "dimensions": 1024,
            "normalize": True,
        }

        response = bedrock_client.invoke_model(
            modelId="amazon.titan-embed-text-v2:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps(payload),
        )

        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding", [])

        if not embedding:
            logger.warning(f"Empty embedding returned for text: {text[:100]}")
            return []

        logger.info(f"Generated embedding with {len(embedding)} dimensions")
        return embedding

    return retry_bedrock_call(call_titan)


def compute_content_hash(content: str) -> str:
    """
    Compute SHA-256 hash of content for change detection.

    Args:
        content: Content to hash

    Returns:
        SHA-256 hash as hex string
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def store_batch_summary(
    batch: Dict[str, Any],
    summary: str,
    embedding: List[float]
) -> bool:
    """
    Store batch summary in DynamoDB.

    Schema: PK=repo#{repo}, SK={storage_key}

    Args:
        batch: Batch message with metadata
        summary: Generated summary text
        embedding: Embedding vector

    Returns:
        True if successful, False otherwise
    """
    try:
        content_hash = compute_content_hash(summary)

        item = {
            "PK": {"S": f"repo#{batch['repo']}"},
            "SK": {"S": batch["storage_key"]},
            "content": {"S": summary},
            "content_hash": {"S": content_hash},
            "embedding": {"L": [{"N": str(x)} for x in embedding]},
            "repo": {"S": batch["repo"]},
            "type": {"S": f"{batch['batch_type']}-summary"},
            "batch_type": {"S": batch["batch_type"]},
            "group_name": {"S": batch["group_name"]},
            "file_count": {"N": str(len(batch["file_paths"]))},
            "timestamp": {"S": datetime.utcnow().isoformat()}
        }

        dynamodb_client.put_item(TableName=CODE_MAPS_TABLE, Item=item)

        logger.info(f"Stored {batch['batch_type']} summary for {batch['group_name']}")
        return True

    except ClientError as e:
        logger.error(f"Failed to store batch summary in DynamoDB: {e}")
        return False


def process_batch_record(record: Dict[str, Any]) -> None:
    """
    Process a single SQS record.

    Args:
        record: SQS record containing batch message

    Raises:
        Exception: If processing fails (triggers SQS retry)
    """
    import time

    batch = json.loads(record["body"])

    logger.info(
        f"Processing {batch['batch_type']} batch: {batch['group_name']} "
        f"({len(batch['file_paths'])} files)"
    )

    # Generate batch summary
    batch_summary = generate_batch_summary(batch)

    # Generate embedding for batch summary
    batch_embedding = generate_embedding(batch_summary)

    # Store batch summary
    if not store_batch_summary(batch, batch_summary, batch_embedding):
        raise Exception(f"Failed to store batch summary for {batch['group_name']}")

    logger.info(f"Successfully processed {batch['batch_type']} summary for {batch['group_name']}")

    # Add cooldown period to prevent Bedrock throttling on next message
    # This allows rate limits to reset between batches
    cooldown_seconds = 10
    logger.info(f"Waiting {cooldown_seconds}s cooldown before next message...")
    time.sleep(cooldown_seconds)


def handler(event, context):
    """
    Lambda handler for processing batch summaries from SQS.

    Args:
        event: SQS event with Records array
        context: Lambda context

    Returns:
        Success response or raises exception for retry
    """
    logger.info(f"Process batch summary handler invoked with {len(event['Records'])} records")

    # Load configuration at container startup
    if CODE_MAPS_TABLE is None:
        load_config()

    errors = []

    for record in event["Records"]:
        try:
            process_batch_record(record)
        except Exception as e:
            logger.error(f"Error processing batch: {e}", exc_info=True)
            errors.append(e)
            # Continue processing other records

    # If any errors occurred, throw to trigger SQS retry
    if errors:
        raise Exception(
            f"Failed to process {len(errors)} out of {len(event['Records'])} batches"
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Batch processing completed",
            "records_processed": len(event["Records"]),
        }),
    }
