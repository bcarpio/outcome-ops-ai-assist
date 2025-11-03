"""
Lambda handler for generating code maps from repository structure.

This handler:
1. Fetches repository file tree from GitHub API
2. Checks for recent commits (last 61 minutes) to skip unchanged repos
3. Identifies key files (Lambda handlers, Terraform, tests, schemas, etc.)
4. Groups files into logical batches (infrastructure, handler groups, tests, shared)
5. Generates architectural summary using Claude via Bedrock
6. Stores summaries in DynamoDB with embeddings
7. Sends file batches to SQS for detailed async processing

This Lambda is triggered:
- Manually via AWS Lambda invoke
- On-demand after major refactorings
- Optionally via EventBridge schedule

Documentation: docs/lambda-generate-code-maps.md
"""

import hashlib
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3
from botocore.exceptions import ClientError

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

KB_BUCKET = None
CODE_MAPS_TABLE = None
SQS_QUEUE_URL = None
GITHUB_TOKEN = None

GITHUB_API_URL = "https://api.github.com"


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global KB_BUCKET, CODE_MAPS_TABLE, SQS_QUEUE_URL, GITHUB_TOKEN

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
    except ClientError as e:
        logger.error(f"Failed to load configuration from SSM: {e}")
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


def has_recent_commits(repo: str, minutes_ago: int = 61) -> bool:
    """
    Check if repository has commits to main branch in the last N minutes.

    Args:
        repo: Repository in format "owner/repo"
        minutes_ago: Number of minutes to look back

    Returns:
        True if repo has recent commits, False otherwise
    """
    endpoint = f"/repos/{repo}/branches/main"

    try:
        response = github_api_request(endpoint)

        commit_date_str = response["commit"]["commit"]["committer"]["date"]
        last_commit_date = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))
        cutoff_date = datetime.now(last_commit_date.tzinfo) - timedelta(minutes=minutes_ago)

        has_recent = last_commit_date >= cutoff_date
        logger.info(
            f"{repo}: Last commit {last_commit_date.isoformat()}, "
            f"cutoff {cutoff_date.isoformat()}, recent: {has_recent}"
        )

        return has_recent
    except Exception as e:
        logger.warning(f"Error checking commits for {repo}: {e}")
        return True  # Process repo if we can't determine (fail open)


def identify_key_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Identify key files for analysis with prioritization.

    Priority levels for Python projects:
    1. Lambda handlers (lambda/*/handler.py)
    2. Python modules in lambda directories
    3. Terraform infrastructure files (*.tf)
    4. Schema/model files (*_schema.py, models/*.py)
    5. Test files (tests/**/*.py)
    6. Configuration files (requirements.txt, Makefile, etc.)
    7. Shared utilities (src/**/*.py, utils/**/*.py)
    8. Documentation (docs/**/*.md, README.md)

    Args:
        files: List of file objects from GitHub API

    Returns:
        Sorted list of key files with priority
    """
    key_files = []

    for file in files:
        if file["type"] != "blob":
            continue

        path = file["path"]
        name = os.path.basename(path)
        lower_path = path.lower()
        lower_name = name.lower()

        # Skip excluded directories
        if any(excluded in lower_path for excluded in [
            "node_modules", ".git/", "dist/", "build/", "coverage/",
            "__pycache__", ".pytest_cache", ".venv", "venv/"
        ]):
            continue

        priority = 0

        # Priority 1: Lambda handler files
        if lower_name == "handler.py" and "lambda/" in lower_path:
            priority = 1

        # Priority 2: Python modules in lambda directories
        elif "lambda/" in lower_path and lower_name.endswith(".py") and lower_name != "__init__.py":
            priority = 2

        # Priority 3: Terraform infrastructure files
        elif lower_name.endswith(".tf"):
            priority = 3

        # Priority 4: Schema and model files
        elif (lower_name.endswith("_schema.py") or
              lower_name.endswith("_model.py") or
              "models/" in lower_path or
              "schemas/" in lower_path) and lower_name.endswith(".py"):
            priority = 4

        # Priority 5: Test files
        elif ("test_" in lower_name or "_test.py" in lower_name) and lower_name.endswith(".py"):
            priority = 5

        # Priority 6: Configuration files
        elif lower_name in [
            "requirements.txt", "setup.py", "pyproject.toml", "makefile",
            "justfile", ".gitlab-ci.yml", ".github/workflows"
        ]:
            priority = 6

        # Priority 7: Shared utilities and source files
        elif (("src/" in lower_path or "utils/" in lower_path or "common/" in lower_path or "shared/" in lower_path)
              and lower_name.endswith(".py") and lower_name != "__init__.py"):
            priority = 7

        # Priority 8: Documentation files
        elif lower_name.endswith(".md"):
            priority = 8

        if priority > 0:
            key_files.append({
                "file": file,
                "priority": priority,
            })

    # Sort by priority (lower number = higher priority), then by path
    key_files.sort(key=lambda x: (x["priority"], x["file"]["path"]))

    return [kf["file"] for kf in key_files]


def group_files_into_batches(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group files into logical batches for efficient processing.

    Batch types:
    - infrastructure: All .tf files
    - handler-group: Lambda handlers grouped by function directory
    - tests: Test files grouped by type (unit, integration)
    - shared: Shared utilities and common code
    - schemas: Schema and model definitions
    - docs: Documentation files

    Args:
        files: List of file objects

    Returns:
        List of batch objects with metadata
    """
    batches = []

    # Filter out excluded files
    relevant_files = [
        f for f in files
        if f["type"] == "blob" and not any(excluded in f["path"].lower() for excluded in [
            "node_modules", ".git/", "dist/", "build/", "coverage/",
            "__pycache__", ".pytest_cache", ".venv", "venv/",
            "package.json", "package-lock.json", ".gitignore"
        ])
    ]

    # Group 1: Infrastructure (all .tf files)
    infra_files = [f for f in relevant_files if f["path"].endswith(".tf")]
    if infra_files:
        batches.append({
            "batch_type": "infrastructure",
            "group_name": "infrastructure",
            "files": infra_files,
            "storage_key": "summary#infrastructure",
        })

    # Group 2: Lambda handler groups (by function directory)
    handler_files = [f for f in relevant_files if "lambda/" in f["path"] and f["path"].endswith(".py")]
    handler_groups = defaultdict(list)

    for file in handler_files:
        # Extract handler function name: lambda/my-handler/handler.py -> my-handler
        path_parts = file["path"].split("/")
        if len(path_parts) >= 2 and path_parts[0] == "lambda":
            group_key = path_parts[1]
            handler_groups[group_key].append(file)

    for group_key, group_files in handler_groups.items():
        batches.append({
            "batch_type": "handler-group",
            "group_name": group_key,
            "files": group_files,
            "storage_key": f"summary#handler#{group_key}",
        })

    # Group 3: Tests (by type: unit, integration, fixtures)
    test_files = [
        f for f in relevant_files
        if "test" in f["path"].lower() and f["path"].endswith(".py")
    ]
    test_groups = defaultdict(list)

    for file in test_files:
        if "unit" in file["path"].lower():
            test_groups["unit"].append(file)
        elif "integration" in file["path"].lower():
            test_groups["integration"].append(file)
        elif "fixture" in file["path"].lower():
            test_groups["fixtures"].append(file)
        else:
            test_groups["other"].append(file)

    for group_key, group_files in test_groups.items():
        batches.append({
            "batch_type": "tests",
            "group_name": group_key,
            "files": group_files,
            "storage_key": f"summary#tests#{group_key}",
        })

    # Group 4: Shared utilities and common code
    shared_files = [
        f for f in relevant_files
        if any(pattern in f["path"].lower() for pattern in ["src/", "utils/", "common/", "shared/"])
        and f["path"].endswith(".py")
        and "__init__" not in f["path"]
    ]
    if shared_files:
        batches.append({
            "batch_type": "shared",
            "group_name": "shared-utilities",
            "files": shared_files,
            "storage_key": "summary#shared",
        })

    # Group 5: Schemas and models
    schema_files = [
        f for f in relevant_files
        if ("schema" in f["path"].lower() or "model" in f["path"].lower())
        and f["path"].endswith(".py")
    ]
    if schema_files:
        batches.append({
            "batch_type": "schemas",
            "group_name": "schemas-and-models",
            "files": schema_files,
            "storage_key": "summary#schemas",
        })

    # Group 6: Documentation
    doc_files = [f for f in relevant_files if f["path"].endswith(".md")]
    if doc_files:
        batches.append({
            "batch_type": "docs",
            "group_name": "documentation",
            "files": doc_files,
            "storage_key": "summary#docs",
        })

    return batches


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


def send_batch_to_sqs(batch: Dict[str, Any], repo: str, repo_project: str) -> bool:
    """
    Send file batch to SQS for async processing.

    Args:
        batch: Batch object with files and metadata
        repo: Repository name
        repo_project: Repository project path (owner/repo)

    Returns:
        True if successful, False otherwise
    """
    try:
        message_body = {
            "repo": repo,
            "repo_project": repo_project,
            "batch_type": batch["batch_type"],
            "group_name": batch["group_name"],
            "file_paths": [f["path"] for f in batch["files"]],
            "storage_key": batch["storage_key"],
        }

        sqs_client.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message_body),
            MessageGroupId=repo,  # FIFO ordering by repo
            MessageDeduplicationId=f"{repo}-{batch['storage_key']}",  # Deduplication per batch
        )

        logger.info(f"Sent {batch['batch_type']} batch to SQS: {batch['group_name']} ({len(batch['files'])} files)")
        return True

    except ClientError as e:
        logger.error(f"Failed to send batch to SQS: {e}")
        return False


def handler(event, context):
    """
    Lambda handler for generating code maps.

    Args:
        event: Lambda event (optional 'repos' list to process specific repos)
        context: Lambda context

    Returns:
        Response with processing results
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

        # If event.repos is provided, filter to only those repos
        if event.get("repos"):
            application_repos = [
                repo for repo in application_repos
                if repo["name"] in event["repos"]
            ]
            logger.info(f"Processing {len(application_repos)} specific repos from event: {event['repos']}")
        else:
            logger.info(f"Filtered to {len(application_repos)} application/internal repos")

        # Filter repos with recent commits (unless forced or specific repos requested)
        repos_to_process = application_repos

        if not FORCE_FULL_PROCESS and not event.get("repos"):
            logger.info(f"Checking {len(application_repos)} repos for recent commits...")
            filtered = []
            for repo in application_repos:
                has_recent = has_recent_commits(repo["project"])
                if has_recent:
                    filtered.append(repo)
                else:
                    logger.info(f"Skipping {repo['name']} - no recent commits")

            repos_to_process = filtered
            logger.info(f"Processing {len(repos_to_process)} of {len(application_repos)} repos with recent changes")
        elif FORCE_FULL_PROCESS:
            logger.info(f"FORCE_FULL_PROCESS=true: Processing all {len(application_repos)} repos")
        else:
            logger.info(f"Event-driven: Processing {len(application_repos)} specific repos")

        # Process each repository
        repos_processed = []
        total_files_analyzed = 0

        for repo_config in repos_to_process:
            repo_name = repo_config["name"]
            repo_project = repo_config["project"]
            repo_type = repo_config.get("type", "internal")

            logger.info(f"Generating code map for {repo_name}...")

            # Fetch repository file tree
            all_files = list_repository_files(repo_project)
            logger.info(f"Found {len(all_files)} total files in {repo_name}")

            # Generate architectural summary
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

            # Group files into logical batches
            batches = group_files_into_batches(all_files)
            logger.info(f"Grouped into {len(batches)} batches for {repo_name}")

            # Send each batch to SQS for async processing
            batches_sent = 0
            for batch in batches:
                try:
                    if send_batch_to_sqs(batch, repo_name, repo_project):
                        batches_sent += 1
                        total_files_analyzed += len(batch["files"])
                except Exception as e:
                    logger.error(f"Error sending batch {batch['group_name']} to SQS: {e}")
                    continue

            logger.info(
                f"Completed code map generation for {repo_name} "
                f"({batches_sent}/{len(batches)} batches sent, {total_files_analyzed} files)"
            )
            repos_processed.append(repo_name)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "success": True,
                "repos_processed": repos_processed,
                "total_files_analyzed": total_files_analyzed,
                "timestamp": datetime.utcnow().isoformat(),
            }),
        }

    except Exception as e:
        logger.error(f"Error during code map generation: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
