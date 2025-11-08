"""
Lambda handler for generating code maps from repository structure.

This handler:
1. Fetches repository file tree from GitHub API
2. Uses pluggable backend to discover code units (Lambda handlers, K8s services, etc.)
3. Detects changes since last run using git-based change detection (incremental updates)
4. Generates architectural summary using Claude via Bedrock
5. Stores summaries in DynamoDB with embeddings
6. Sends code unit batches to SQS for detailed async processing
7. Tracks processing state for incremental updates

Backends supported:
- Lambda serverless: Discovers Lambda handlers in lambda/ directory
- Kubernetes: Coming soon
- Monolith: Coming soon

Invocation modes:
- Full regeneration: event = {"repos": ["repo1", "repo2"]} - process specified repos, all handlers
- Incremental: event = {} - process only changed handlers from repos with commits in last 61 minutes

This Lambda is triggered:
- Manually via CLI with repos list (full regeneration)
- Hourly via EventBridge with empty event (incremental)

Documentation: docs/lambda-generate-code-maps.md
"""

import hashlib
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3
from botocore.exceptions import ClientError

# Backend abstraction imports
# Import using sys.path to handle both runtime and testing scenarios
import os
import sys

# Add current directory to path for imports
handler_dir = os.path.dirname(os.path.abspath(__file__))
if handler_dir not in sys.path:
    sys.path.insert(0, handler_dir)

from backends import get_backend, list_backends  # noqa: E402
from state_tracker import StateTracker  # noqa: E402

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# AWS clients (region inferred from environment)
s3_client = boto3.client("s3")
dynamodb_client = boto3.client("dynamodb")
bedrock_client = boto3.client("bedrock-runtime")
sqs_client = boto3.client("sqs")
ssm_client = boto3.client("ssm")

# Configuration (loaded from SSM at container startup)
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
FORCE_FULL_PROCESS = os.environ.get("FORCE_FULL_PROCESS", "false").lower() == "true"
BACKEND_TYPE = os.environ.get("BACKEND_TYPE", "lambda")  # Backend to use (lambda, k8s, monolith)
ENABLE_INCREMENTAL = os.environ.get("ENABLE_INCREMENTAL", "true").lower() == "true"

KB_BUCKET = None
CODE_MAPS_TABLE = None
SQS_QUEUE_URL = None
GITHUB_TOKEN = None
backend = None
state_tracker = None

GITHUB_API_URL = "https://api.github.com"


def load_config():
    """Load configuration from SSM Parameter Store and initialize backend."""
    global KB_BUCKET, CODE_MAPS_TABLE, SQS_QUEUE_URL, GITHUB_TOKEN, backend, state_tracker

    try:
        kb_bucket_param = f"/{ENVIRONMENT}/{APP_NAME}/s3/knowledge-base-bucket"
        KB_BUCKET = ssm_client.get_parameter(Name=kb_bucket_param)["Parameter"]["Value"]

        code_maps_param = f"/{ENVIRONMENT}/{APP_NAME}/dynamodb/code-maps-table"
        CODE_MAPS_TABLE = ssm_client.get_parameter(Name=code_maps_param)["Parameter"]["Value"]

        sqs_queue_param = f"/{ENVIRONMENT}/{APP_NAME}/sqs/code-maps-queue-url"
        SQS_QUEUE_URL = ssm_client.get_parameter(Name=sqs_queue_param)["Parameter"]["Value"]

        github_token_param = f"/{ENVIRONMENT}/{APP_NAME}/github/token"
        GITHUB_TOKEN = ssm_client.get_parameter(
            Name=github_token_param,
            WithDecryption=True
        )["Parameter"]["Value"]

        logger.info(f"Configuration loaded: KB_BUCKET={KB_BUCKET}, TABLE={CODE_MAPS_TABLE}, SQS_QUEUE={SQS_QUEUE_URL}")

        # Initialize backend
        backend_config = {
            "lambda_directory": "lambda",
            "handler_file": "handler.py",
            "include_submodules": True,
            "max_file_size_tokens": 7000,
            "github_token": GITHUB_TOKEN,
            "github_api_url": GITHUB_API_URL,
        }
        backend = get_backend(BACKEND_TYPE, backend_config)
        logger.info(f"Initialized backend: {backend.get_backend_name()}")

        # Initialize state tracker for incremental updates
        state_tracker = StateTracker(dynamodb_client, CODE_MAPS_TABLE)
        logger.info("State tracker initialized for incremental updates")

    except ClientError as e:
        logger.error(f"Failed to load configuration from SSM: {e}")
        raise
    except ValueError as e:
        logger.error(f"Failed to initialize backend: {e}")
        logger.info(f"Available backends: {list_backends()}")
        raise


def github_api_request(endpoint: str, method: str = "GET") -> Any:
    """
    Make a request to the GitHub API with authentication.

    Args:
        endpoint: API endpoint (without base URL)
        method: HTTP method

    Returns:
        JSON response from GitHub API
    """
    url = f"{GITHUB_API_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "outcome-ops-generate-code-maps",
    }

    try:
        request = Request(url, headers=headers, method=method)
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as e:
        logger.error(f"GitHub API error: {e}")
        raise


def has_recent_commits(repo_project: str, minutes_ago: int = 61) -> bool:
    """
    Check if a repository has commits to main branch in the last N minutes.

    This is an optimization for EventBridge hourly runs - skip repos without
    recent commits to avoid unnecessary GitHub API calls.

    Args:
        repo_project: Repository in format "owner/repo"
        minutes_ago: Number of minutes to look back (default: 61)

    Returns:
        True if repo has commits in the last N minutes, False otherwise
    """
    try:
        endpoint = f"/repos/{repo_project}/branches/main"
        response = github_api_request(endpoint)

        commit_data = response.get("commit", {})
        commit_info = commit_data.get("commit", {})
        committer = commit_info.get("committer", {})
        commit_date_str = committer.get("date")

        if not commit_date_str:
            logger.warning(f"No commit date found for {repo_project}")
            return True  # Process anyway if we can't determine

        # Parse ISO 8601 timestamp
        commit_date = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))
        from datetime import timedelta, timezone
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)

        has_recent = commit_date >= cutoff_time

        if has_recent:
            logger.info(f"{repo_project} has commits from {commit_date.isoformat()} (within {minutes_ago}m)")
        else:
            logger.info(f"{repo_project} last commit {commit_date.isoformat()} (older than {minutes_ago}m)")

        return has_recent

    except Exception as e:
        logger.error(f"Failed to check recent commits for {repo_project}: {e}")
        return True  # Process anyway on error


def filter_changed_code_units(code_units: List[Any], changed_files: List[str]) -> List[Any]:
    """
    Filter code units to only those that have files overlapping with changed files.

    This enables incremental code map updates - only regenerate summaries for
    code units that actually changed.

    Args:
        code_units: List of CodeUnit objects
        changed_files: List of changed file paths from git compare

    Returns:
        List of code units that have at least one file in changed_files
    """
    if not changed_files:
        # Empty changed_files means full regeneration
        return code_units

    changed_units = []
    for unit in code_units:
        # Check if any file in this unit appears in changed files
        for file_path in unit.file_paths:
            if file_path in changed_files:
                changed_units.append(unit)
                logger.info(f"Code unit {unit.name} has changes: {file_path}")
                break  # One match is enough, move to next unit

    logger.info(f"Filtered {len(code_units)} code units to {len(changed_units)} changed units")
    return changed_units


def list_repository_files(repo: str, ref: str = "main") -> List[Dict[str, Any]]:
    """
    Fetch all files in a GitHub repository recursively.

    Args:
        repo: Repository in format "owner/repo"
        ref: Branch/tag/commit (default: main)

    Returns:
        List of file objects with path, type, and name
    """
    endpoint = f"/repos/{repo}/git/trees/{ref}?recursive=1"

    try:
        response = github_api_request(endpoint)
        files = response.get("tree", [])

        logger.info(f"Found {len(files)} total items in {repo}")
        return files
    except Exception as e:
        logger.error(f"Failed to list repository files: {e}")
        raise


# Old functions removed - now handled by backend abstraction:
# - identify_key_files() -> backend.discover_code_units()
# - group_files_into_batches() -> backend.discover_code_units()


def retry_bedrock_call(func, max_retries: int = 3):
    """
    Retry helper for Bedrock calls with exponential backoff.

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts

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

            delay_ms = min(1000 * (2 ** (attempt - 1)), 8000)  # Exponential backoff, max 8s
            delay_s = delay_ms / 1000
            logger.warning(f"Bedrock call failed (attempt {attempt}/{max_retries}), retrying in {delay_s}s...")
            time.sleep(delay_s)

    raise last_error


def generate_architectural_summary(repo: str, repo_type: str, files: List[Dict[str, Any]]) -> str:
    """
    Generate architectural summary using Claude via Bedrock.

    Args:
        repo: Repository name
        repo_type: Repository type (application, internal, standards)
        files: List of all files in repository

    Returns:
        Architectural summary text
    """
    # Extract directory structure (unique directories, limited to 50)
    directories = sorted(set(
        os.path.dirname(f["path"])
        for f in files
        if f["type"] == "tree" or "/" in f["path"]
    ))[:50]

    # Count files by extension
    files_by_type = defaultdict(int)
    for file in files:
        if file["type"] == "blob":
            ext = os.path.splitext(file["path"])[1] or "no-extension"
            files_by_type[ext] += 1

    # Sort by count descending, take top 10
    top_file_types = sorted(files_by_type.items(), key=lambda x: x[1], reverse=True)[:10]

    prompt = f"""Analyze this repository structure and provide an architectural summary.

Repository: {repo}
Type: {repo_type}

Directory Structure (sample):
{chr(10).join(directories[:20])}

File Types:
{chr(10).join(f"- {ext}: {count} files" for ext, count in top_file_types)}

Please provide:
1. A brief overview of the repository's purpose (1-2 sentences)
2. Key architectural patterns used (e.g., serverless Lambda, infrastructure as code, etc.)
3. Main components and their relationships
4. Technology stack (Python, Terraform, AWS services, etc.)
5. Notable conventions or standards observed

Keep the summary concise (2-3 paragraphs) and focused on helping developers understand the codebase structure."""

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
                "maxTokens": 1024,
                "temperature": 0.3,
            },
        }

        response = bedrock_client.converse(**payload)

        output_message = response.get("output", {}).get("message", {})
        content = output_message.get("content", [])

        if not content or not content[0].get("text"):
            raise ValueError("No content in Claude response")

        return content[0]["text"]

    try:
        summary = retry_bedrock_call(call_claude)
        logger.info(f"Generated architectural summary for {repo} ({len(summary)} chars)")
        return summary

    except ClientError as e:
        logger.error(f"Failed to generate architectural summary: {e}")
        raise


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

    try:
        return retry_bedrock_call(call_titan)
    except ClientError as e:
        logger.error(f"Failed to generate embedding: {e}")
        raise


def compute_content_hash(content: str) -> str:
    """
    Compute SHA-256 hash of content for change detection.

    Args:
        content: Content to hash

    Returns:
        SHA-256 hash as hex string
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def store_code_map_embedding(
    doc_id: str,
    content: str,
    content_hash: str,
    embedding: List[float],
    repo: str,
    doc_type: str,
    path: str
) -> bool:
    """
    Store code map embedding in DynamoDB.

    Schema matches working pattern:
    - PK: <repo_name>/code-map (architectural summary)
    - SK: METADATA
    - Fields: snake_case (content_hash, timestamp)

    Args:
        doc_id: Document ID (PK) - format: <repo_name>/code-map
        content: Document content
        content_hash: SHA-256 hash of content
        embedding: Embedding vector
        repo: Repository name
        doc_type: Document type (code-map, code-summary)
        path: Path identifier (e.g., "code-map")

    Returns:
        True if successful, False otherwise
    """
    try:
        item = {
            "PK": {"S": doc_id},
            "SK": {"S": "METADATA"},
            "type": {"S": doc_type},
            "content": {"S": content},
            "embedding": {"L": [{"N": str(x)} for x in embedding]},
            "repo": {"S": repo},
            "path": {"S": path},
            "content_hash": {"S": content_hash},
            "timestamp": {"S": datetime.utcnow().isoformat()},
        }

        dynamodb_client.put_item(TableName=CODE_MAPS_TABLE, Item=item)

        logger.info(f"Stored code map in DynamoDB: {doc_id}")
        return True

    except ClientError as e:
        logger.error(f"Failed to store code map in DynamoDB: {e}")
        return False


def send_code_unit_to_sqs(code_unit, repo: str, repo_project: str) -> bool:
    """
    Send code unit batch to SQS for async processing.

    Args:
        code_unit: CodeUnit object from backend
        repo: Repository name
        repo_project: Repository project path (owner/repo)

    Returns:
        True if successful, False otherwise
    """
    try:
        # Generate batch metadata using backend
        batch_metadata = backend.generate_batch_metadata(code_unit, repo)

        message_body = {
            "repo": repo,
            "repo_project": repo_project,
            "batch_type": batch_metadata["batch_type"],
            "group_name": batch_metadata["group_name"],
            "file_paths": code_unit.file_paths,
            "storage_key": batch_metadata["storage_key"],
            "backend_type": batch_metadata.get("backend_type", "lambda"),
        }

        sqs_client.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message_body),
            MessageGroupId=repo,  # FIFO ordering by repo
            MessageDeduplicationId=f"{repo}-{batch_metadata['storage_key']}",  # Deduplication per batch
        )

        logger.info(
            f"Sent {batch_metadata['batch_type']} batch to SQS: {batch_metadata['group_name']} "
            f"({len(code_unit.file_paths)} files)"
        )
        return True

    except ClientError as e:
        logger.error(f"Failed to send code unit to SQS: {e}")
        return False


def handler(event, context):
    """
    Lambda handler for generating code maps.

    Supports two invocation modes:
    - Full regeneration: event = {"repos": ["repo1", "repo2"]} - process specified repos, all handlers
    - Incremental: event = {} - process only changed handlers from repos with commits in last 61 minutes

    Args:
        event: Lambda event
            - {"repos": ["name1", "name2"]}: Full regeneration for specified repos
            - {}: Incremental mode (EventBridge hourly)
        context: Lambda context

    Returns:
        Response with processing results including repos_processed, total_files_analyzed, batches_queued
    """
    logger.info(f"Generate code maps handler invoked: {json.dumps(event)}")

    # Load configuration at container startup
    if KB_BUCKET is None:
        load_config()

    try:
        # Load allowlist from SSM Parameter Store
        allowlist_param = f"/{ENVIRONMENT}/{APP_NAME}/config/repos-allowlist"
        try:
            param_response = ssm_client.get_parameter(Name=allowlist_param)
            allowlist = json.loads(param_response["Parameter"]["Value"])
        except ssm_client.exceptions.ParameterNotFound:
            logger.error(f"Repos allowlist not found in SSM at {allowlist_param}")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Repos allowlist not configured in SSM Parameter Store"}),
            }

        if not allowlist or "repos" not in allowlist:
            logger.error("Invalid allowlist structure in SSM Parameter Store")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Invalid allowlist structure"}),
            }

        # Filter out standards repos (ADRs only, no code to map)
        application_repos = [
            repo for repo in allowlist["repos"]
            if repo.get("type", "internal") != "standards"
        ]

        # Determine invocation mode
        is_incremental_mode = not event.get("repos")  # Empty event = incremental

        # If event.repos is provided, filter to only those repos
        if event.get("repos"):
            application_repos = [
                repo for repo in application_repos
                if repo["name"] in event["repos"]
            ]
            logger.info(f"Full regeneration mode: Processing {len(application_repos)} specific repos: {event['repos']}")
        else:
            logger.info(f"Incremental mode: Checking {len(application_repos)} application/internal repos for recent commits")

        # Process each repository
        repos_processed = []
        total_files_analyzed = 0
        total_batches_queued = 0

        for repo_config in application_repos:
            repo_name = repo_config["name"]
            repo_project = repo_config["project"]
            repo_type = repo_config.get("type", "internal")

            logger.info(f"Generating code map for {repo_name}...")

            # For incremental mode, check if repo has commits in last 61 minutes
            # This is an optimization to skip repos without recent activity
            if is_incremental_mode:
                if not has_recent_commits(repo_project, minutes_ago=61):
                    logger.info(f"Skipping {repo_name} - no commits in last 61 minutes")
                    continue

            # Fetch repository file tree
            all_files = list_repository_files(repo_project)
            logger.info(f"Found {len(all_files)} total files in {repo_name}")

            # Check for changes using backend (git-based incremental updates)
            last_state = None
            if ENABLE_INCREMENTAL and not FORCE_FULL_PROCESS and is_incremental_mode:
                last_state = state_tracker.get_last_state(repo_name)

            change_result = backend.detect_changes(
                repo=repo_name,
                repo_project=repo_project,
                last_state=last_state,
                force_full=FORCE_FULL_PROCESS or not is_incremental_mode
            )

            if not change_result.has_changes and not FORCE_FULL_PROCESS:
                logger.info(f"Skipping {repo_name} - no changes detected since {change_result.last_commit_sha[:7]}")
                continue

            logger.info(
                f"Processing {repo_name}: "
                f"{'full regeneration' if not last_state else f'incremental ({change_result.last_commit_sha[:7]}..{change_result.current_commit_sha[:7]})'}"
            )

            # Discover code units using backend
            all_code_units = backend.discover_code_units(all_files)
            logger.info(f"Discovered {len(all_code_units)} code units for {repo_name}")

            # Filter to only changed code units for incremental mode
            if is_incremental_mode and hasattr(change_result, 'changed_files') and change_result.changed_files:
                code_units = filter_changed_code_units(all_code_units, change_result.changed_files)
                if not code_units:
                    logger.info(f"Skipping {repo_name} - no code units changed (changed files don't overlap with handlers/infrastructure)")
                    continue
            else:
                # Full regeneration: Process all code units
                code_units = all_code_units

            # Generate architectural summary (still uses Claude)
            arch_summary = generate_architectural_summary(repo_name, repo_type, all_files)
            logger.info(f"Generated architectural summary for {repo_name}")

            # Store architectural summary with embedding
            summary_doc_id = f"{repo_name}/code-map"
            summary_hash = compute_content_hash(arch_summary)
            summary_embedding = generate_embedding(arch_summary)

            store_code_map_embedding(
                doc_id=summary_doc_id,
                content=arch_summary,
                content_hash=summary_hash,
                embedding=summary_embedding,
                repo=repo_name,
                doc_type="code-map",
                path="code-map"
            )

            # Upload architectural summary to S3
            s3_client.put_object(
                Bucket=KB_BUCKET,
                Key=f"code-maps/{repo_name}/architectural-summary.txt",
                Body=arch_summary.encode("utf-8"),
            )

            logger.info(f"Stored code map for {repo_name}")

            # Send each code unit to SQS for async processing
            batches_sent = 0
            for code_unit in code_units:
                try:
                    if send_code_unit_to_sqs(code_unit, repo_name, repo_project):
                        batches_sent += 1
                        total_files_analyzed += len(code_unit.file_paths)
                except Exception as e:
                    logger.error(f"Error sending code unit {code_unit.name} to SQS: {e}")
                    continue

            total_batches_queued += batches_sent

            # Save state for incremental updates
            if ENABLE_INCREMENTAL and change_result.current_commit_sha:
                state_tracker.save_state(
                    repo=repo_name,
                    commit_sha=change_result.current_commit_sha,
                    files_processed=total_files_analyzed,
                    batches_sent=batches_sent
                )
                logger.info(f"Saved state for {repo_name}: {change_result.current_commit_sha[:7]}")

            logger.info(
                f"Completed code map generation for {repo_name} "
                f"({batches_sent}/{len(code_units)} code units sent, {total_files_analyzed} files)"
            )
            repos_processed.append(repo_name)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "success": True,
                "repos_processed": repos_processed,
                "total_files_analyzed": total_files_analyzed,
                "batches_queued": total_batches_queued,
                "timestamp": datetime.utcnow().isoformat(),
            }),
        }

    except Exception as e:
        logger.error(f"Error during code map generation: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
