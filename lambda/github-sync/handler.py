"""
GitHub Sync Lambda for OutcomeOps AI Assist.

Processes SQS messages to sync GitHub repos for code-map generation.
This Lambda bridges workspace-scoped GitHub repos to the existing code-maps pipeline.

Flow:
1. Receives sync request from SQS (workspace_id, repo_name, installation_id)
2. Gets installation token via GitHub App JWT
3. Lists repository files via GitHub API
4. Routes based on repo_type:
   - application repos: Discover code units and queue to repo-summaries queue
   - standards repos: Queue files to file-ingestion queue
5. Updates sync status in DynamoDB

Triggered by: SQS github-sync queue (messages from github-integration Lambda or scheduler)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import yaml
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
REPO_SUMMARIES_QUEUE = os.environ.get("REPO_SUMMARIES_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")
KB_BUCKET = os.environ.get("KB_BUCKET")

# Cached clients
_dynamodb = None
_sqs = None
_ssm = None
_s3 = None

# GitHub App config (cached)
_github_config = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# GitHub App Authentication
# ============================================================================


def get_github_config() -> Dict[str, str]:
    """Get GitHub App configuration from SSM."""
    global _github_config
    if _github_config is not None:
        return _github_config

    ssm = _get_ssm()
    param_prefix = f"/{ENV}/{APP_NAME}/github"

    try:
        response = ssm.get_parameters(
            Names=[
                f"{param_prefix}/app-id",
                f"{param_prefix}/private-key",
            ],
            WithDecryption=True,
        )

        params = {p["Name"].split("/")[-1]: p["Value"] for p in response["Parameters"]}

        _github_config = {
            "app_id": params.get("app-id", ""),
            "private_key": params.get("private-key", ""),
        }

        return _github_config

    except ClientError as e:
        logger.error(f"Failed to get GitHub config from SSM: {e}")
        raise


def generate_jwt() -> str:
    """Generate a JWT for GitHub App authentication."""
    import time
    import jwt

    config = get_github_config()
    app_id = config["app_id"]
    private_key = config["private_key"]

    if not app_id or app_id == "PLACEHOLDER_CONFIGURE_ME":
        raise ValueError("GitHub App ID not configured")
    if not private_key or private_key == "PLACEHOLDER_CONFIGURE_ME":
        raise ValueError("GitHub App private key not configured")

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 600,
        "iss": app_id,
    }

    return jwt.encode(payload, private_key, algorithm="RS256")


def get_installation_token(installation_id: str) -> str:
    """Exchange App JWT for an installation access token."""
    import urllib.request

    jwt_token = generate_jwt()
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"

    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data["token"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get installation token: {e.code} - {error_body}")
        raise


# ============================================================================
# GitHub API
# ============================================================================


def get_repo_tree(token: str, owner: str, repo: str, ref: str = "HEAD") -> List[Dict]:
    """
    Get the entire repository tree recursively.

    Returns list of file objects with path, type, size, sha.
    """
    import urllib.request

    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode())
            return data.get("tree", [])
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get repo tree: {e.code} - {error_body}")
        raise


def get_default_branch(token: str, owner: str, repo: str) -> str:
    """Get the default branch of a repository."""
    import urllib.request

    url = f"https://api.github.com/repos/{owner}/{repo}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data.get("default_branch", "main")
    except urllib.error.HTTPError as e:
        logger.warning(f"Failed to get default branch: {e.code}, using 'main'")
        return "main"


def get_latest_commit_sha(token: str, owner: str, repo: str, branch: str) -> str:
    """Get the SHA of the latest commit on a branch."""
    import urllib.request

    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data["sha"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get latest commit: {e.code} - {error_body}")
        raise


def get_file_content(token: str, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
    """Get the content of a file from a repository."""
    import base64
    import urllib.parse
    import urllib.request

    encoded_path = urllib.parse.quote(path, safe="/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}?ref={ref}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            if data.get("type") != "file":
                return None
            content = data.get("content", "")
            return base64.b64decode(content).decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.warning(f"HTTP {e.code} fetching {path}: {e.reason}")
        return None


# ============================================================================
# Repo Config
# ============================================================================


def fetch_ingestion_config(token: str, owner: str, repo_short: str, ref: str) -> List[str]:
    """Fetch standards_paths from .outcomeops.yaml. Returns list of paths."""
    content = get_file_content(token, owner, repo_short, ".outcomeops.yaml", ref)
    if not content:
        return ["docs/adr"]
    try:
        config = yaml.safe_load(content) or {}
        paths = config.get("ingestion", {}).get("standards_paths", ["docs/adr"])
        return paths if isinstance(paths, list) else ["docs/adr"]
    except Exception:
        return ["docs/adr"]


def classify_doc_type(file_path: str, standards_paths: List[str]) -> Optional[str]:
    """Return 'adr' if file is under a standards path, else None."""
    for sp in standards_paths:
        sp = sp.rstrip("/")
        if file_path == sp or file_path.startswith(sp + "/"):
            return "adr"
    return None


# ============================================================================
# DynamoDB Operations
# ============================================================================


def get_repo(workspace_id: str, repo_name: str) -> Optional[Dict]:
    """Get a repo record from DynamoDB."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"}
    )
    return response.get("Item")


def update_sync_status(
    workspace_id: str,
    repo_name: str,
    status: str,
    error: Optional[str] = None,
    commit_sha: Optional[str] = None,
) -> None:
    """Update sync status for a repo."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)

    set_parts = ["last_sync_status = :status", "updated_at = :now"]
    remove_parts = []
    expr_values = {
        ":status": status,
        ":now": _now_iso(),
    }

    if status in ("success", "failed"):
        set_parts.append("last_sync_at = :sync_at")
        expr_values[":sync_at"] = _now_iso()

    if error is not None:
        set_parts.append("last_sync_error = :error")
        expr_values[":error"] = error
    elif status == "success":
        remove_parts.append("last_sync_error")

    if commit_sha is not None:
        set_parts.append("last_commit_sha = :sha")
        expr_values[":sha"] = commit_sha

    update_expr = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expr += " REMOVE " + ", ".join(remove_parts)

    try:
        table.update_item(
            Key={"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(repo_name)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning("Skipping sync status update for deleted repo %s in workspace %s", repo_name, workspace_id)


# ============================================================================
# File Ingestion
# ============================================================================

# File extensions for documentation (standards repos, app repo docs)
DOC_EXTENSIONS = {
    ".md", ".txt", ".html", ".htm", ".yaml", ".yml",
    ".json", ".rst", ".pdf", ".csv", ".xml",
}

# File extensions for source code (code maps)
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt",
    ".go", ".rs", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
    ".tf",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "coverage",
}

# Maximum file size to process (1MB)
MAX_FILE_SIZE = 1024 * 1024


def should_process_code_file(file_item: Dict) -> bool:
    """Check if a file should be processed for code maps."""
    if file_item.get("type") != "blob":
        return False

    path = file_item.get("path", "")

    # Skip directories in path
    path_parts = path.split("/")
    for part in path_parts[:-1]:  # Exclude filename
        if part in SKIP_DIRS:
            return False

    # Check extension
    ext = os.path.splitext(path)[1].lower()
    if ext not in CODE_EXTENSIONS:
        return False

    # Check size
    size = file_item.get("size", 0)
    if size > MAX_FILE_SIZE:
        logger.debug(f"Skipping large file: {path} ({size} bytes)")
        return False

    return True


def should_process_doc_file(file_item: Dict) -> bool:
    """Check if a file should be processed as documentation."""
    if file_item.get("type") != "blob":
        return False

    path = file_item.get("path", "")

    # Skip directories in path
    path_parts = path.split("/")
    for part in path_parts[:-1]:  # Exclude filename
        if part in SKIP_DIRS:
            return False

    # Check extension
    ext = os.path.splitext(path)[1].lower()
    if ext not in DOC_EXTENSIONS:
        return False

    # Check size
    size = file_item.get("size", 0)
    if size > MAX_FILE_SIZE:
        logger.debug(f"Skipping large file: {path} ({size} bytes)")
        return False

    return True


def queue_file_for_ingestion(
    workspace_id: str,
    repo_name: str,
    file_path: str,
    file_sha: str,
    commit_sha: str,
    repo_type: str = "application",
) -> bool:
    """Queue a file for ingestion via file-ingestion queue."""
    if not FILE_INGESTION_QUEUE:
        logger.warning("FILE_INGESTION_QUEUE not configured")
        return False

    try:
        sqs = _get_sqs()
        sqs.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps({
                "source": "github",
                "workspace_id": workspace_id,
                "repo_name": repo_name,
                "repo_type": repo_type,  # application or standards
                "file_path": file_path,
                "file_sha": file_sha,
                "commit_sha": commit_sha,
            }),
        )
        return True
    except ClientError as e:
        logger.error(f"Failed to queue file: {e}")
        return False


def upload_file_to_s3(
    workspace_id: str,
    repo_name: str,
    file_path: str,
    file_sha: str,
    commit_sha: str,
    content: str,
    repo_type: str = "standards",
    doc_type: Optional[str] = None,
) -> Optional[str]:
    """Upload a standards repo file directly to S3 for indexing."""
    if not KB_BUCKET:
        logger.warning("KB_BUCKET not configured")
        return None

    s3_key = f"workspaces/{workspace_id}/github/{repo_name}/{file_path}"

    ext = os.path.splitext(file_path)[1].lower()
    content_types = {
        ".md": "text/markdown",
        ".json": "application/json",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".rst": "text/x-rst",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
        ".csv": "text/csv",
        ".xml": "application/xml",
    }
    content_type = content_types.get(ext, "text/plain")

    metadata = {
        "source": "github",
        "workspace_id": workspace_id,
        "github_repo_name": repo_name,
        "github_file_path": file_path,
        "github_file_sha": file_sha,
        "github_commit_sha": commit_sha,
        "github_repo_type": repo_type,
    }
    if doc_type:
        metadata["github_doc_type"] = doc_type

    try:
        _get_s3().put_object(
            Bucket=KB_BUCKET,
            Key=s3_key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
            Metadata=metadata,
        )
        return s3_key
    except ClientError as e:
        logger.error(f"Failed to upload {file_path} to S3: {e}")
        return None


# ============================================================================
# Orphan Detection
# ============================================================================


def _queue_orphan_detection(
    workspace_id: str,
    repo_name: str,
    current_file_paths: List[str],
) -> None:
    """Queue orphan detection to clean up files removed from the repo."""
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured, skipping orphan detection")
        return

    if not current_file_paths:
        logger.info("No current files, skipping orphan detection")
        return

    message = {
        "action": "delete_orphans",
        "workspace_id": workspace_id,
        "source": "github",
        "prefix": repo_name + "/",
        "current_ids": current_file_paths,
    }

    try:
        _get_sqs().send_message(
            QueueUrl=DELETE_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info(f"Queued orphan detection for {workspace_id}/github/{repo_name}")
    except ClientError as e:
        logger.error(f"Failed to queue orphan detection: {e}")


# ============================================================================
# Code Maps Queue (for application repos)
# ============================================================================


def queue_repo_for_code_maps(
    workspace_id: str,
    repo_name: str,
    repo_type: str,
    installation_id: str,
    all_files: List[Dict],
    last_commit_sha: Optional[str] = None,
    current_commit_sha: Optional[str] = None,
) -> bool:
    """
    Queue repository to repo-summaries queue for code-map generation.

    The process-repo-summary Lambda will handle code unit discovery
    since it has access to the backend abstractions.

    Args:
        workspace_id: Workspace ID
        repo_name: Full repo name (owner/repo)
        repo_type: Repository type
        installation_id: GitHub App installation ID for token generation
        all_files: List of file dicts from GitHub API
        last_commit_sha: Previous commit SHA for incremental processing
        current_commit_sha: Current commit SHA being synced

    Returns:
        True if successful, False otherwise
    """
    if not REPO_SUMMARIES_QUEUE:
        logger.warning("REPO_SUMMARIES_QUEUE not configured")
        return False

    try:
        # Extract just file paths from GitHub API dicts
        file_paths = [f["path"] if isinstance(f, dict) else f for f in all_files]

        # Extract repo short name for message
        repo_short = repo_name.split("/")[-1] if "/" in repo_name else repo_name

        message_body = {
            "repo": repo_short,
            "repo_project": repo_name,
            "repo_type": repo_type,
            "workspace_id": workspace_id,
            "installation_id": installation_id,  # For workspace repos to get tokens
            "all_files": file_paths,
            # code_units will be discovered by process-repo-summary Lambda
            "discover_code_units": True,
        }

        # Include commit SHAs for incremental code map regeneration
        if last_commit_sha:
            message_body["last_commit_sha"] = last_commit_sha
        if current_commit_sha:
            message_body["current_commit_sha"] = current_commit_sha

        sqs = _get_sqs()
        sqs.send_message(
            QueueUrl=REPO_SUMMARIES_QUEUE,
            MessageBody=json.dumps(message_body),
            MessageGroupId=repo_name,  # FIFO ordering by repo
            MessageDeduplicationId=f"{workspace_id}-{repo_name}-{_now_iso()}",
        )

        logger.info(f"Queued {repo_name} to repo-summaries queue ({len(all_files)} files)")
        return True

    except ClientError as e:
        logger.error(f"Failed to queue repo for code maps: {e}")
        return False


# ============================================================================
# Sync Logic
# ============================================================================


def process_sync_request(message: Dict) -> Dict:
    """
    Process a sync request for a GitHub repo.

    Routes based on repo_type:
    - application: Discover code units and queue to repo-summaries queue
    - standards: Queue files to file-ingestion queue

    Args:
        message: SQS message body with workspace_id, repo_name, installation_id

    Returns:
        Result dict with status and counts
    """
    workspace_id = message["workspace_id"]
    repo_name = message["repo_name"]
    installation_id = message["installation_id"]

    logger.info(f"Processing sync for {workspace_id}/{repo_name}")

    # Get repo record
    repo = get_repo(workspace_id, repo_name)
    if not repo:
        return {"status": "error", "error": "Repo not found"}

    if not repo.get("include_code_maps", True):
        return {"status": "skipped", "reason": "Code maps disabled"}

    repo_type = repo.get("repo_type", "application")
    logger.info(f"Repo type: {repo_type}")

    # Update status to in_progress
    update_sync_status(workspace_id, repo_name, "in_progress")

    try:
        # Get installation token
        token = get_installation_token(installation_id)

        # Parse repo name (owner/repo)
        if "/" in repo_name:
            owner, repo_short = repo_name.split("/", 1)
        else:
            logger.error(f"Invalid repo name format: {repo_name}")
            update_sync_status(workspace_id, repo_name, "failed", "Invalid repo name format")
            return {"status": "error", "error": "Invalid repo name format"}

        # Get default branch and latest commit
        default_branch = get_default_branch(token, owner, repo_short)
        current_sha = get_latest_commit_sha(token, owner, repo_short, default_branch)

        # Check if already synced this commit
        last_sha = repo.get("last_commit_sha")
        if last_sha == current_sha:
            logger.info(f"No changes since last sync ({current_sha[:7]})")
            update_sync_status(workspace_id, repo_name, "success", commit_sha=current_sha)
            return {"status": "no_changes", "commit_sha": current_sha}

        # Get repo file tree
        tree = get_repo_tree(token, owner, repo_short, current_sha)
        logger.info(f"Found {len(tree)} items in repo tree")

        # Route based on repo type
        if repo_type == "standards":
            # Standards repos: Upload files directly to S3 for indexing
            return _process_standards_repo(
                workspace_id=workspace_id,
                repo_name=repo_name,
                repo_type=repo_type,
                tree=tree,
                current_sha=current_sha,
                token=token,
                owner=owner,
                repo_short=repo_short,
                default_branch=default_branch,
            )
        else:
            # Application repos: Discover code units and queue to repo-summaries queue
            return _process_application_repo(
                workspace_id=workspace_id,
                repo_name=repo_name,
                repo_type=repo_type,
                installation_id=installation_id,
                owner=owner,
                repo_short=repo_short,
                token=token,
                default_branch=default_branch,
                tree=tree,
                current_sha=current_sha,
                last_commit_sha=last_sha,
            )

    except Exception as e:
        logger.exception(f"Sync failed for {repo_name}")
        update_sync_status(workspace_id, repo_name, "failed", str(e))
        return {"status": "error", "error": str(e)}


def _process_standards_repo(
    workspace_id: str,
    repo_name: str,
    repo_type: str,
    tree: List[Dict],
    current_sha: str,
    token: str,
    owner: str,
    repo_short: str,
    default_branch: str,
) -> Dict:
    """
    Process a standards repo by uploading files directly to S3.

    Standards repos contain ADRs, docs, YAML, JSON, XML that should be
    ingested as documents for the knowledge base. Files are uploaded to S3
    where the process-documents Lambda picks them up for embedding/indexing.
    """
    # Filter to doc files (md, yaml, json, txt, html, pdf, etc.)
    files_to_process = [f for f in tree if should_process_doc_file(f)]
    logger.info(f"Processing {len(files_to_process)} files for standards repo")

    # Fetch and upload files directly to S3
    uploaded = 0
    for file_item in files_to_process:
        file_path = file_item["path"]

        # Fetch file content from GitHub
        content = get_file_content(token, owner, repo_short, file_path, default_branch)
        if content is None:
            logger.warning(f"Could not fetch content for {file_path}, skipping")
            continue

        # Upload to S3 - all standards repo files are ADRs/standards
        s3_key = upload_file_to_s3(
            workspace_id=workspace_id,
            repo_name=repo_name,
            file_path=file_path,
            file_sha=file_item["sha"],
            commit_sha=current_sha,
            content=content,
            repo_type=repo_type,
            doc_type="adr",
        )
        if s3_key:
            uploaded += 1

    # Queue orphan detection to clean up deleted files
    current_file_paths = [f["path"] for f in files_to_process]
    _queue_orphan_detection(workspace_id, repo_name, current_file_paths)

    # Update sync status
    update_sync_status(workspace_id, repo_name, "success", commit_sha=current_sha)

    logger.info(f"Standards sync complete: {uploaded} files uploaded to S3 for {repo_name}")
    return {
        "status": "success",
        "repo_type": repo_type,
        "files_uploaded": uploaded,
        "commit_sha": current_sha,
    }


def _process_application_repo(
    workspace_id: str,
    repo_name: str,
    repo_type: str,
    installation_id: str,
    owner: str,
    repo_short: str,
    token: str,
    default_branch: str,
    tree: List[Dict],
    current_sha: str,
    last_commit_sha: Optional[str] = None,
) -> Dict:
    """
    Process an application repo by uploading doc files to S3 for indexing
    and queueing to repo-summaries queue for code-map generation.

    Application repos contain source code (Python, TypeScript, Java, etc.)
    and infrastructure (Terraform) that should be analyzed for code maps,
    plus documentation (READMEs, YAML configs, etc.) that should be indexed.

    Code unit discovery is handled by the process-repo-summary Lambda
    which has access to the backend abstractions.
    """
    # Fetch ingestion config for standards_paths classification
    standards_paths = fetch_ingestion_config(token, owner, repo_short, default_branch)
    logger.info(f"Standards paths for {repo_name}: {standards_paths}")

    # Upload doc files to S3 for knowledge base indexing
    doc_files = [f for f in tree if should_process_doc_file(f)]
    docs_uploaded = 0
    for file_item in doc_files:
        file_path = file_item["path"]
        content = get_file_content(token, owner, repo_short, file_path, default_branch)
        if content is None:
            logger.warning(f"Could not fetch content for {file_path}, skipping")
            continue
        doc_type = classify_doc_type(file_path, standards_paths)
        s3_key = upload_file_to_s3(
            workspace_id=workspace_id,
            repo_name=repo_name,
            file_path=file_path,
            file_sha=file_item["sha"],
            commit_sha=current_sha,
            content=content,
            repo_type="application",
            doc_type=doc_type,
        )
        if s3_key:
            docs_uploaded += 1

    if docs_uploaded:
        logger.info(f"Uploaded {docs_uploaded} doc files to S3 for {repo_name}")

    # Queue orphan detection to clean up deleted doc files
    current_doc_paths = [f["path"] for f in doc_files]
    _queue_orphan_detection(workspace_id, repo_name, current_doc_paths)

    # Queue repo to repo-summaries queue for code-map generation
    if queue_repo_for_code_maps(
        workspace_id=workspace_id,
        repo_name=repo_name,
        repo_type=repo_type,
        installation_id=installation_id,
        all_files=tree,
        last_commit_sha=last_commit_sha,
        current_commit_sha=current_sha,
    ):
        update_sync_status(workspace_id, repo_name, "success", commit_sha=current_sha)
        logger.info(f"Application sync complete: queued {repo_name} for code-map generation, {docs_uploaded} docs uploaded")
        return {
            "status": "success",
            "repo_type": repo_type,
            "files_queued": len(tree),
            "docs_uploaded": docs_uploaded,
            "commit_sha": current_sha,
        }
    else:
        # Queue failed, mark as error
        update_sync_status(workspace_id, repo_name, "failed", "Failed to queue to repo-summaries")
        return {
            "status": "error",
            "error": "Failed to queue to repo-summaries queue",
        }


# ============================================================================
# Main Handler
# ============================================================================


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    Main Lambda handler for GitHub sync.

    Processes SQS messages containing sync requests.
    """
    logger.info(f"GitHub sync handler invoked with {len(event.get('Records', []))} records")

    results = []
    for record in event.get("Records", []):
        try:
            message = json.loads(record["body"])
            logger.info(f"Processing message: {json.dumps(message)}")

            result = process_sync_request(message)
            results.append({
                "message_id": record.get("messageId"),
                "result": result,
            })

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in message: {e}")
            results.append({
                "message_id": record.get("messageId"),
                "result": {"status": "error", "error": "Invalid JSON"},
            })
        except Exception as e:
            logger.exception(f"Error processing message")
            results.append({
                "message_id": record.get("messageId"),
                "result": {"status": "error", "error": str(e)},
            })

    # Count successes and failures
    successes = sum(1 for r in results if r["result"].get("status") in ("success", "no_changes", "skipped"))
    failures = len(results) - successes

    logger.info(f"Sync complete: {successes} succeeded, {failures} failed")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "processed": len(results),
            "successes": successes,
            "failures": failures,
            "results": results,
        }),
    }
