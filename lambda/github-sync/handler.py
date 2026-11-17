# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import yaml
from botocore.exceptions import ClientError

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import log_source_sync  # noqa: E402

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


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    keeps module load order safe."""
    from shared.github_base import github_api_base
    return github_api_base()


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


def _mint_installation_token(installation_id: str) -> str:
    """Uncached mint: JWT + POST /app/installations/{id}/access_tokens.

    fable-scale chunk 06: split off from the cached wrapper below so
    `shared.github_auth.get_installation_token` can invoke it on cache miss.
    """
    import urllib.request

    jwt_token = generate_jwt()
    url = f"{_github_api()}/app/installations/{installation_id}/access_tokens"

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


def get_installation_token(installation_id: str) -> str:
    """Return an installation access token, SSM-cached for ~55 minutes.

    fable-scale chunk 06: prior behavior minted a fresh token on every
    call (~200ms + 2 API-quota units). At enterprise sync rates the mint
    itself is a meaningful share of the GitHub App's 5000 req/hr ceiling.
    Cache hits skip the mint entirely.

    Env `GITHUB_TOKEN_CACHE_DISABLED=true` reverts to always-mint for
    rollback / test isolation.
    """
    from shared.github_auth import get_installation_token as cached_get
    return cached_get(installation_id, _mint_installation_token)


# ============================================================================
# GitHub API
# ============================================================================


def get_repo_tree(token: str, owner: str, repo: str, ref: str = "HEAD") -> List[Dict]:
    """
    Get the entire repository tree recursively.

    Returns list of file objects with path, type, size, sha.

    fable-scale chunk 06: routes through shared.github_backoff so a
    rate-limit 403 or 429 sleeps + retries instead of hard-failing the
    sync. A single sync = many get_repo_tree calls at enterprise scale
    (thousands of repos); previously a burst could exhaust the App's
    5000 req/hr ceiling and silently leave code-map gaps.
    """
    import urllib.request
    from shared import github_backoff

    url = f"{_github_api()}/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with github_backoff.github_urllib_request(req, timeout=60) as response:
            data = json.loads(response.read().decode())
            tree = data.get("tree", [])
            return tree
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Failed to get repo tree: {e.code} - {error_body}")
        raise


def get_default_branch(token: str, owner: str, repo: str) -> str:
    """Get the default branch of a repository."""
    import urllib.request

    url = f"{_github_api()}/repos/{owner}/{repo}"

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
    """Get the SHA of the latest commit on a branch.

    Kept for back-compat with the few legacy call sites that only need
    the SHA. New code should call ``get_latest_commit`` which also
    returns the commit timestamp needed for the SHA-truth ledger's
    monotonic-advance guard.
    """
    sha, _ts = get_latest_commit(token, owner, repo, branch)
    return sha


def get_latest_commit(
    token: str, owner: str, repo: str, branch: str
) -> tuple[str, int]:
    """Get the SHA + commit timestamp (epoch seconds) of the latest
    commit on a branch.

    Returns ``(sha, commit_timestamp)``. The timestamp comes from
    ``commit.committer.date`` on the GitHub API response, parsed to
    epoch seconds. It drives the monotonic-advance check in
    ``shared.sha_truth.try_advance_sha`` so an older SHA finishing
    after a newer one (DLQ-replay, retry-after-failover) does not
    clobber the newer SHA on the workspaces row.
    """
    import urllib.request
    from datetime import datetime, timezone

    url = f"{_github_api()}/repos/{owner}/{repo}/commits/{branch}"

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
            sha = data["sha"]
            # GitHub returns ``commit.committer.date`` as an ISO-8601
            # string with a Z suffix. Parse to epoch seconds. Falls
            # back to wall-clock time if the field is missing for any
            # reason — gracefully degrades the monotonic guard rather
            # than failing the sync.
            date_str = (
                data.get("commit", {}).get("committer", {}).get("date")
            )
            if date_str:
                ts = int(
                    datetime.fromisoformat(
                        date_str.replace("Z", "+00:00")
                    )
                    .astimezone(timezone.utc)
                    .timestamp()
                )
            else:
                ts = int(datetime.now(timezone.utc).timestamp())
            return sha, ts
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
    url = f"{_github_api()}/repos/{owner}/{repo}/contents/{encoded_path}?ref={ref}"

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


def fetch_changed_files(
    token: str, owner: str, repo_short: str, base_sha: str, head_sha: str
) -> Optional[set[str]]:
    """Return the set of file paths added/modified between two SHAs.

    Calls GitHub's compare API and returns the union of added,
    modified, renamed, and copied file paths (the docs that may need
    re-uploading). Removed files are NOT included — the existing
    ``_queue_orphan_detection`` path cleans up deletions separately.

    Returns ``None`` on API failure so the caller can fall back to a
    full re-upload (safe degradation: same as today's behavior). An
    empty set is a successful "no files changed" result, which is the
    happy path for docs-static / code-only commits.

    plan-doc-incremental: mirrors the ``detect_changes`` semantic that
    process-repo-summary already uses for code-unit filtering, so the
    docs pipeline matches the code-maps pipeline's incremental
    behavior. Without this, every commit-changes tick re-uploads and
    re-vectorizes every doc in the repo even when only Lambda code
    changed — pure Bedrock waste.
    """
    import urllib.parse
    import urllib.request

    url = (
        f"{_github_api()}/repos/{owner}/{repo_short}/compare/"
        f"{base_sha}...{head_sha}"
    )

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
    except urllib.error.HTTPError as e:
        # 404 happens when the base SHA is unreachable (rebased away,
        # force-pushed, or first-sync edge case). Treat as "unknown",
        # caller falls back to full upload.
        if e.code == 404:
            logger.info(
                "[github-sync] compare %s...%s returned 404; "
                "falling back to full doc upload",
                base_sha[:7], head_sha[:7],
            )
            return None
        logger.warning(
            "[github-sync] compare %s...%s failed (%d); "
            "falling back to full doc upload",
            base_sha[:7], head_sha[:7], e.code,
        )
        return None
    except Exception as e:
        logger.warning(
            "[github-sync] compare %s...%s failed (%s); "
            "falling back to full doc upload",
            base_sha[:7], head_sha[:7], e,
        )
        return None

    # GitHub's compare API paginates large diffs (>300 files by
    # default). For typical commits the full list fits in one page;
    # for unusually large diffs we accept truncation -- the worst case
    # is "we re-upload some unchanged docs," same as today's behavior.
    changed: set[str] = set()
    for f in data.get("files", []):
        status = f.get("status", "")
        if status in ("added", "modified", "renamed", "copied"):
            path = f.get("filename")
            if path:
                changed.add(path)
            # Renamed files also carry previous_filename — that's a
            # delete handled by orphan detection, not a re-upload.
    return changed


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


def _backfill_default_branch(
    workspace_id: str, repo_name: str, default_branch: str,
) -> None:
    """Write ``default_branch`` back to the GITHUB_REPO# row so the
    next sync can read it without a live GitHub API call.

    Used only by the lazy-backfill path in ``resolve_sync_branch``:
    existing rows added before chunk 01 lack the field, and rows
    added by pre-fix installs of this Lambda too. Best-effort --
    a write failure just means the next sync re-resolves (extra
    quota, correct behavior). Goes through dual_write so the HA
    secondary picks it up on the same tick.
    """
    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"},
            UpdateExpression="SET default_branch = :b",
            ExpressionAttributeValues={":b": default_branch},
            ConditionExpression="attribute_exists(repo_name)",
        )
    except Exception as e:
        logger.warning(
            f"[github-sync] Could not backfill default_branch for "
            f"{repo_name}: {e}. Next sync will re-resolve."
        )


def resolve_sync_branch(
    repo_row: Dict,
    token: str,
    owner: str,
    repo_short: str,
    workspace_id: str,
    repo_name: str,
) -> str:
    """Return the branch this sync should walk.

    Priority:
    1. The ``default_branch`` field on the repo's DDB row -- this is
       what the customer picked in the add-repo picker (chunk 03) or
       what the backend resolved on add (chunk 01).
    2. Live ``GET /repos/{owner}/{repo}`` lookup, with the result
       written back to the row so the next sync doesn't burn quota.

    Case (2) fires only for rows added before chunk 01 shipped
    (they lack the field). After the first sync backfills it, every
    subsequent sync takes case (1).
    """
    stored = repo_row.get("default_branch")
    if stored:
        logger.info(
            f"[github-sync] Using stored default_branch={stored} "
            f"for {repo_name}"
        )
        return stored
    logger.info(
        f"[github-sync] No stored default_branch for {repo_name}; "
        f"resolving live and backfilling."
    )
    resolved = get_default_branch(token, owner, repo_short)
    _backfill_default_branch(workspace_id, repo_name, resolved)
    return resolved


def update_sync_status(
    workspace_id: str,
    repo_name: str,
    status: str,
    error: Optional[str] = None,
    commit_sha: Optional[str] = None,
    last_attempted_sha: Optional[str] = None,
    last_dispatched_sha: Optional[str] = None,
    last_failed_sha: Optional[str] = None,
    last_failure_reason: Optional[str] = None,
) -> None:
    """Update sync status for a repo.

    plan-git-truth chunk 02 adds the diagnostic columns
    ``last_attempted_sha`` / ``last_dispatched_sha`` /
    ``last_failed_sha`` / ``last_failed_at`` / ``last_failure_reason``
    so operators can see what github-sync tried, what it dispatched,
    and what failed -- separate from the per-pipeline columns
    (``last_code_map_sha`` / ``last_docs_sha`` / ``last_gkg_sha``)
    which only advance on actual downstream completion.

    The legacy ``commit_sha`` parameter still writes ``last_commit_sha``
    for back-compat with the UI; chunk-04 (future, separate plan)
    removes both the parameter and the column.
    """
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

    if last_attempted_sha is not None:
        set_parts.append("last_attempted_sha = :att_sha")
        expr_values[":att_sha"] = last_attempted_sha

    if last_dispatched_sha is not None:
        set_parts.append("last_dispatched_sha = :disp_sha")
        expr_values[":disp_sha"] = last_dispatched_sha

    if last_failed_sha is not None:
        set_parts.append("last_failed_sha = :fail_sha")
        set_parts.append("last_failed_at = :fail_at")
        expr_values[":fail_sha"] = last_failed_sha
        expr_values[":fail_at"] = int(time.time())

    if last_failure_reason is not None:
        set_parts.append("last_failure_reason = :fail_reason")
        expr_values[":fail_reason"] = last_failure_reason

    update_expr = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expr += " REMOVE " + ", ".join(remove_parts)

    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"},
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
    commit_timestamp: Optional[int] = None,
) -> bool:
    """Queue a file for ingestion via file-ingestion queue.

    ``commit_timestamp`` (epoch seconds) rides on the message body so
    the file-ingestion consumer can mark its docs-pipeline ledger row
    complete with the correct monotonic-advance anchor. Optional for
    back-compat during the chunk-02b rolling deploy — file-ingestion
    tolerates the absence and skips the ledger call for one release.
    """
    if not FILE_INGESTION_QUEUE:
        logger.warning("FILE_INGESTION_QUEUE not configured")
        return False

    try:
        sqs = _get_sqs()
        body: Dict[str, Any] = {
            "source": "github",
            "workspace_id": workspace_id,
            "repo_name": repo_name,
            "repo_type": repo_type,  # application or standards
            "file_path": file_path,
            "file_sha": file_sha,
            "commit_sha": commit_sha,
        }
        if commit_timestamp is not None:
            body["commit_timestamp"] = commit_timestamp
        sqs.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(body),
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
    commit_timestamp: Optional[int] = None,
) -> Optional[str]:
    """Upload a standards repo file directly to S3 for indexing.

    ``commit_timestamp`` (epoch seconds) lands as an S3 object
    metadata header so process-documents can use it as the
    monotonic-advance anchor when it closes the docs ledger after
    vectorization. Required for the active/passive HA failover
    contract -- a failover mid-vectorization must see the SHA
    still at the previous commit until every chunk lands.
    """
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
    if commit_timestamp is not None:
        # S3 object metadata values are strings.
        metadata["github_commit_timestamp"] = str(commit_timestamp)

    try:
        # Must go through dual_write: standards-repo docs are uploaded to
        # the KB bucket directly here (not via file-ingestion). A direct
        # s3.put_object bypasses the EventBridge replication emit, so
        # standards-repo doc changes never reach the HA secondary — the
        # passive region only ever has what ha-bootstrap snapshot-copied
        # at bringup. dual_write.s3_put_object emits a replication event;
        # the applier does a cross-region GetObject to materialize the
        # object in the other region. (No-op emit in single-region or
        # passive-region per shared.dual_write.)
        dual_write.s3_put_object(
            bucket=KB_BUCKET,
            key=s3_key,
            body=content.encode("utf-8"),
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
    commit_timestamp: Optional[int] = None,
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
        # plan-git-truth chunk 02: commit_timestamp on the SQS body so
        # process-repo-summary can open the code_map_units ledger row
        # with the correct monotonic-advance anchor, and the gkg
        # writer downstream can do the same after commit_swap.
        if commit_timestamp is not None:
            message_body["commit_timestamp"] = commit_timestamp

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
# SHA-truth ledger helpers (plan-git-truth chunk 02)
#
# These are thin wrappers around shared.sha_truth that absorb the
# import side-effect cost and the ledger-failure swallowing pattern.
# Ledger writes must not break the sync path -- the workspaces row is
# a mirror for the audit story; the SHA truth is in the per-pipeline
# ledger tables themselves.
# ============================================================================


def _open_code_map_dispatch_ledger(
    workspace_id: str,
    repo_name: str,
    current_sha: str,
    current_commit_timestamp: int,
) -> None:
    """Open ``PENDING#code_map_dispatch#{sha}`` before publishing the
    repo-summaries SQS message. process-repo-summary closes it when
    it finishes consuming the message."""
    try:
        from shared.sha_truth import (  # type: ignore
            PendingBatch,
            open_pending_batch,
        )
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[github-sync] sha_truth import failed: %s; skipping ledger open",
            exc,
        )
        return
    try:
        open_pending_batch(PendingBatch(
            workspace_id=workspace_id,
            repo=repo_name,
            pipeline="code_map_dispatch",
            target_sha=current_sha,
            expected_count=1,
            target_commit_timestamp=current_commit_timestamp,
        ))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[github-sync] code_map_dispatch ledger open failed for %s@%s: %s",
            repo_name, current_sha, exc,
        )


def _mark_code_map_dispatch_failed(
    workspace_id: str,
    repo_name: str,
    current_sha: str,
    failure_reason: str,
    failure_detail: str,
) -> None:
    """Close the code_map_dispatch ledger as failed when SQS publish
    fails. Without this the SHA would wedge with the dispatch row
    open until TTL — operators wouldn't see the failure on the
    pending ledger until they investigated."""
    try:
        from shared.sha_truth import mark_unit_failed  # type: ignore
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[github-sync] sha_truth import failed: %s; skipping ledger close",
            exc,
        )
        return
    try:
        mark_unit_failed(
            workspace_id,
            repo_name,
            "code_map_dispatch",
            current_sha,
            file_path="<repo-summaries-message>",
            failure_reason=failure_reason,
            failure_detail=failure_detail,
            attempt_count=1,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[github-sync] code_map_dispatch ledger close failed for %s@%s: %s",
            repo_name, current_sha, exc,
        )


def _filter_docs_to_changed(
    token: str,
    owner: str,
    repo_short: str,
    all_doc_files: List[Dict],
    last_commit_sha: Optional[str],
    current_sha: str,
) -> List[Dict]:
    """Narrow a doc-file list to only files changed between
    ``last_commit_sha`` and ``current_sha``.

    On first-sync (no last_commit_sha) or compare-API failure, returns
    the full list — safe fall-back to the pre-incremental behavior.
    """
    if not last_commit_sha or not all_doc_files:
        return all_doc_files
    changed_paths = fetch_changed_files(
        token, owner, repo_short, last_commit_sha, current_sha,
    )
    if changed_paths is None:
        return all_doc_files
    return [f for f in all_doc_files if f.get("path") in changed_paths]


def _advance_docs_sha_no_changes(
    workspace_id: str,
    repo_name: str,
    current_sha: str,
    current_commit_timestamp: int,
) -> None:
    """Advance ``last_docs_sha`` directly when zero docs changed at
    this SHA. No ledger row is opened (there's nothing to wait on);
    the workspaces row gets a single conditional UpdateItem with the
    monotonic guard so a stale concurrent run can't roll it back.

    Audit-trail invariant: if no ``PENDING#docs#{sha}`` row exists
    but ``last_docs_sha=={sha}``, the docs pipeline did zero work for
    that commit (no doc files changed) — distinct from "pipeline ran,
    143 docs vectorized" which leaves the closed ledger row visible
    until TTL.
    """
    import time as _time

    try:
        # Must go through dual_write: this is the "commit changed zero doc
        # files" shortcut that advances last_docs_sha without running the
        # docs pipeline. A direct table.update_item here bypasses the
        # EventBridge emit, so the secondary's last_docs_sha never advances
        # for no-doc-change commits — it drifts behind permanently until a
        # commit DOES touch docs (which replicates via the normal
        # process-documents -> sha_truth -> dual_write path). The applier
        # replays the same conditional update on the secondary.
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"GITHUB_REPO#{repo_name}",
            },
            UpdateExpression=(
                "SET last_docs_sha = :sha, last_docs_sha_ts = :ts"
            ),
            ConditionExpression=(
                "attribute_not_exists(last_docs_sha_ts) "
                "OR last_docs_sha_ts < :ts"
            ),
            ExpressionAttributeValues={
                ":sha": current_sha,
                ":ts": current_commit_timestamp,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Newer SHA already advanced past us — harmless.
            return
        logger.warning(
            "[github-sync] no-change docs advance failed for %s@%s: %s",
            repo_name, current_sha, exc,
        )


def _mark_docs_unit_fetch_failed(
    workspace_id: str,
    repo_name: str,
    current_sha: str,
    file_path: str,
) -> None:
    """Mark a docs ledger unit as failed when github-sync can't fetch
    the file content from the GitHub API or the S3 PutObject fails.

    Required for plan-git-truth chunk 02b/4 (race fix): the ledger is
    opened BEFORE the upload loop with expected_count = len(doc_files),
    so any file that fails to upload still needs its unit closed —
    otherwise expected_count > completed_count + failed_count forever
    and try_advance_sha stays at "not_complete."
    """
    try:
        from shared.sha_truth import mark_unit_failed  # type: ignore
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[github-sync] sha_truth import failed: %s",
            exc,
        )
        return
    try:
        mark_unit_failed(
            workspace_id,
            repo_name,
            "docs",
            current_sha,
            file_path=file_path,
            failure_reason="github_fetch_or_s3_put_failed",
            failure_detail="",
            attempt_count=1,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[github-sync] docs unit-failed mark failed for %s@%s/%s: %s",
            repo_name, current_sha, file_path, exc,
        )


def _open_docs_pending_batch(
    workspace_id: str,
    repo_name: str,
    current_sha: str,
    current_commit_timestamp: int,
    docs_uploaded: int,
) -> None:
    """Open ``PENDING#docs#{sha}`` with ``expected_count = docs_uploaded``.

    Closed incrementally by process-documents as each S3 object is
    vectorized. Only the last close advances ``last_docs_sha`` on the
    workspaces row, so the SHA on the row means "all docs are in the
    vector index" — NOT "all docs landed in S3."

    The stricter contract matters for active/passive HA failover: if
    the primary region dies mid-vectorization, the secondary region's
    new primary needs to see ``last_docs_sha`` still at the previous
    SHA so it can re-trigger or wait. Advancing on S3 upload alone
    would lose the in-flight vectorization work silently.

    Failure to open is logged but non-fatal -- the S3 uploads are
    durable regardless; the ledger row is operator observability.
    """
    if docs_uploaded <= 0:
        return
    try:
        from shared.sha_truth import (  # type: ignore
            PendingBatch,
            open_pending_batch,
        )
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[github-sync] sha_truth import failed: %s; skipping docs open",
            exc,
        )
        return
    try:
        open_pending_batch(PendingBatch(
            workspace_id=workspace_id,
            repo=repo_name,
            pipeline="docs",
            target_sha=current_sha,
            expected_count=docs_uploaded,
            target_commit_timestamp=current_commit_timestamp,
        ))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[github-sync] docs ledger open failed for %s@%s: %s",
            repo_name, current_sha, exc,
        )


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
    sync_start = time.time()
    trigger = message.get("trigger", "scheduled")
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

        # Get default branch and latest commit (plan-git-truth chunk 02:
        # commit_timestamp drives the monotonic-advance check downstream).
        # ``resolve_sync_branch`` reads the customer's stored/picked
        # branch from the DDB row first (honors the chunk-03 override,
        # avoids a live GitHub call), lazy-backfilling on the first
        # sync of a pre-chunk-01 row.
        default_branch = resolve_sync_branch(
            repo, token, owner, repo_short, workspace_id, repo_name,
        )
        current_sha, current_commit_timestamp = get_latest_commit(
            token, owner, repo_short, default_branch
        )

        # plan-git-truth chunk 02: record what we attempted before any
        # SQS dispatch so a sync that dies mid-flight is observable.
        # last_attempted_sha is a diagnostic — distinct from
        # last_{pipeline}_sha which only advances on real completion.
        update_sync_status(
            workspace_id,
            repo_name,
            "in_progress",
            last_attempted_sha=current_sha,
        )

        # Check if already synced this commit
        last_sha = repo.get("last_commit_sha")
        if last_sha == current_sha:
            logger.info(f"No changes since last sync ({current_sha[:7]})")
            # No SQS dispatch happens, so no ledger row is opened. We
            # still keep last_commit_sha in sync as the legacy mirror,
            # but skip the per-pipeline columns since this path didn't
            # actually re-run any pipeline.
            update_sync_status(
                workspace_id, repo_name, "success", commit_sha=current_sha
            )
            log_source_sync(
                audit_table_name=os.environ.get("AUDIT_TABLE", ""),
                source="github",
                workspace_id=workspace_id,
                target=repo_name,
                trigger=trigger,
                files_synced=0,
                skipped_no_change=True,
                duration_ms=int((time.time() - sync_start) * 1000),
                extra_details={
                    "commit_sha": current_sha,
                    "repo_type": repo_type,
                },
            )
            return {"status": "no_changes", "commit_sha": current_sha}

        # Get repo file tree
        tree = get_repo_tree(token, owner, repo_short, current_sha)
        logger.info(f"Found {len(tree)} items in repo tree")

        # Route based on repo type
        if repo_type == "standards":
            # Standards repos: Upload files directly to S3 for indexing
            result = _process_standards_repo(
                workspace_id=workspace_id,
                repo_name=repo_name,
                repo_type=repo_type,
                tree=tree,
                current_sha=current_sha,
                current_commit_timestamp=current_commit_timestamp,
                token=token,
                owner=owner,
                repo_short=repo_short,
                default_branch=default_branch,
                last_commit_sha=last_sha,
            )
        else:
            # Application repos: Discover code units and queue to repo-summaries queue
            result = _process_application_repo(
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
                current_commit_timestamp=current_commit_timestamp,
                last_commit_sha=last_sha,
            )

        # Emit REPO_SYNCED parent audit row covering both repo types.
        # The per-doc DOCUMENT_INGESTED rows (process-documents +
        # process-batch-summary) are still emitted downstream; this row
        # is the parent that ties them to the sync trigger.
        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="github",
            workspace_id=workspace_id,
            target=repo_name,
            trigger=trigger,
            files_synced=int(
                result.get("files_uploaded")
                or result.get("files_queued")
                or result.get("docs_uploaded")
                or 0
            ),
            duration_ms=int((time.time() - sync_start) * 1000),
            success=(result.get("status") == "success"),
            extra_details={
                "commit_sha": current_sha,
                "repo_type": repo_type,
            },
        )
        return result

    except Exception as e:
        logger.exception(f"Sync failed for {repo_name}")
        update_sync_status(workspace_id, repo_name, "failed", str(e))
        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="github",
            workspace_id=workspace_id,
            target=repo_name,
            trigger=trigger,
            files_synced=0,
            duration_ms=int((time.time() - sync_start) * 1000),
            success=False,
            error=str(e),
            extra_details={"repo_type": repo_type},
        )
        return {"status": "error", "error": str(e)}


def _process_standards_repo(
    workspace_id: str,
    repo_name: str,
    repo_type: str,
    tree: List[Dict],
    current_sha: str,
    current_commit_timestamp: int,
    token: str,
    owner: str,
    repo_short: str,
    default_branch: str,
    last_commit_sha: Optional[str] = None,
) -> Dict:
    """
    Process a standards repo by uploading files directly to S3.

    Standards repos contain ADRs, docs, YAML, JSON, XML that should be
    ingested as documents for the knowledge base. Files are uploaded to S3
    where the process-documents Lambda picks them up for embedding/indexing.

    plan-git-truth chunk 02: Standards repos write directly to S3 (no
    file-ingestion SQS dispatch), so there's no docs-pipeline ledger to
    open here. The S3 PutObject is synchronous; success means the doc
    is in the knowledge base immediately. ``last_docs_sha`` is advanced
    inline on success since the dispatch and completion are the same
    step.
    """
    # Filter to doc files (md, yaml, json, txt, html, pdf, etc.)
    all_doc_files = [f for f in tree if should_process_doc_file(f)]

    # plan-doc-incremental: when we have a base SHA, narrow the doc
    # set to only files that changed in the (base, head) range. Same
    # semantic process-repo-summary already uses for code units via
    # backend.detect_changes — without this, every commit-changes
    # tick re-uploads + re-vectorizes every doc in the repo.
    files_to_process = _filter_docs_to_changed(
        token, owner, repo_short,
        all_doc_files, last_commit_sha, current_sha,
    )
    logger.info(
        f"Processing {len(files_to_process)} of {len(all_doc_files)} "
        f"doc files for standards repo"
    )

    # plan-git-truth chunk 02b/4 (race fix 2026-05-17): open the docs
    # ledger BEFORE the upload loop. S3 PUTs trigger process-documents
    # asynchronously and the first few invocations can finish
    # vectorizing + call mark_unit_complete before github-sync's loop
    # has even finished, let alone opened the ledger row. Opening
    # before the loop guarantees the row exists when the first
    # process-documents fires.
    #
    # When no docs changed (filtered list is empty), skip the ledger
    # entirely and advance last_docs_sha directly. The pipeline did
    # zero work for docs at this SHA — recording that as
    # "expected=0/completed=0" wouldn't fit the invariant; a direct
    # advance does.
    if files_to_process:
        _open_docs_pending_batch(
            workspace_id, repo_name, current_sha,
            current_commit_timestamp, len(files_to_process),
        )
    else:
        _advance_docs_sha_no_changes(
            workspace_id, repo_name, current_sha,
            current_commit_timestamp,
        )

    # Fetch and upload files directly to S3
    uploaded = 0
    for file_item in files_to_process:
        file_path = file_item["path"]

        # Fetch file content from GitHub
        content = get_file_content(token, owner, repo_short, file_path, default_branch)
        if content is None:
            logger.warning(f"Could not fetch content for {file_path}, skipping")
            _mark_docs_unit_fetch_failed(
                workspace_id, repo_name, current_sha, file_path,
            )
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
            commit_timestamp=current_commit_timestamp,
        )
        if s3_key:
            uploaded += 1
        else:
            _mark_docs_unit_fetch_failed(
                workspace_id, repo_name, current_sha, file_path,
            )

    # Queue orphan detection to clean up files removed from the repo.
    # CRITICAL: the "keep" set must be ALL docs currently in the repo
    # (all_doc_files), NOT the incrementally-changed subset
    # (files_to_process). Orphan detection deletes every KB object under
    # the repo prefix that is NOT in current_ids; passing the changed
    # subset made the delete-worker treat every UNCHANGED doc as an
    # orphan and delete it — silent data loss on any incremental sync
    # where only some docs changed.
    current_file_paths = [f["path"] for f in all_doc_files]
    _queue_orphan_detection(workspace_id, repo_name, current_file_paths)

    update_sync_status(
        workspace_id,
        repo_name,
        "success",
        commit_sha=current_sha,
        last_dispatched_sha=current_sha,
    )

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
    current_commit_timestamp: int,
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
    all_doc_files = [f for f in tree if should_process_doc_file(f)]

    # plan-doc-incremental: filter to changed docs only. See the
    # _process_standards_repo branch above for the rationale.
    doc_files = _filter_docs_to_changed(
        token, owner, repo_short,
        all_doc_files, last_commit_sha, current_sha,
    )
    if len(doc_files) < len(all_doc_files):
        logger.info(
            f"Incremental doc upload: {len(doc_files)} of "
            f"{len(all_doc_files)} doc files changed since {last_commit_sha[:7] if last_commit_sha else '<none>'}"
        )

    # plan-git-truth chunk 02b/4 (race fix 2026-05-17): open the docs
    # ledger BEFORE the upload loop with expected_count = len(doc_files).
    # S3 PUTs trigger process-documents asynchronously and the first
    # invocations can finish vectorizing + call mark_unit_complete
    # before this loop has even uploaded all the files, let alone
    # opened the ledger row. Pre-opening guarantees the row exists
    # when the first process-documents fires.
    if doc_files:
        _open_docs_pending_batch(
            workspace_id, repo_name, current_sha,
            current_commit_timestamp, len(doc_files),
        )
    else:
        # Zero docs changed at this SHA. Skip the ledger entirely
        # and advance last_docs_sha directly — see standards-repo
        # branch above.
        _advance_docs_sha_no_changes(
            workspace_id, repo_name, current_sha,
            current_commit_timestamp,
        )

    docs_uploaded = 0
    for file_item in doc_files:
        file_path = file_item["path"]
        content = get_file_content(token, owner, repo_short, file_path, default_branch)
        if content is None:
            logger.warning(f"Could not fetch content for {file_path}, skipping")
            _mark_docs_unit_fetch_failed(
                workspace_id, repo_name, current_sha, file_path,
            )
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
            commit_timestamp=current_commit_timestamp,
        )
        if s3_key:
            docs_uploaded += 1
        else:
            _mark_docs_unit_fetch_failed(
                workspace_id, repo_name, current_sha, file_path,
            )

    if docs_uploaded:
        logger.info(f"Uploaded {docs_uploaded} doc files to S3 for {repo_name}")

    # Queue orphan detection with the FULL current doc set (all_doc_files),
    # NOT the incremental `doc_files` subset — passing the changed subset
    # would orphan-delete every unchanged doc (data loss). See the
    # matching note in the standards-repo path.
    current_doc_paths = [f["path"] for f in all_doc_files]
    _queue_orphan_detection(workspace_id, repo_name, current_doc_paths)

    # plan-git-truth chunk 02: open the code_map_dispatch ledger row
    # BEFORE publishing the repo-summaries SQS message. If the
    # send_message fails, mark_unit_failed below closes it as failed
    # so try_advance_sha never trips on a half-open dispatch.
    _open_code_map_dispatch_ledger(
        workspace_id, repo_name, current_sha, current_commit_timestamp
    )

    if queue_repo_for_code_maps(
        workspace_id=workspace_id,
        repo_name=repo_name,
        repo_type=repo_type,
        installation_id=installation_id,
        all_files=tree,
        last_commit_sha=last_commit_sha,
        current_commit_sha=current_sha,
        commit_timestamp=current_commit_timestamp,
    ):
        update_sync_status(
            workspace_id,
            repo_name,
            "success",
            commit_sha=current_sha,
            last_dispatched_sha=current_sha,
        )
        logger.info(f"Application sync complete: queued {repo_name} for code-map generation, {docs_uploaded} docs uploaded")
        return {
            "status": "success",
            "repo_type": repo_type,
            "files_queued": len(tree),
            "docs_uploaded": docs_uploaded,
            "commit_sha": current_sha,
        }
    else:
        # SQS publish failed. Close the dispatch ledger row as failed
        # so the SHA never advances — better to wedge visibly than
        # silently advance past a SHA whose work never happened.
        _mark_code_map_dispatch_failed(
            workspace_id,
            repo_name,
            current_sha,
            failure_reason="sqs_send_failed",
            failure_detail="queue_repo_for_code_maps returned False",
        )
        update_sync_status(
            workspace_id,
            repo_name,
            "failed",
            "Failed to queue to repo-summaries",
            last_failed_sha=current_sha,
            last_failure_reason="sqs_send_failed",
        )
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

    # Chunk 02: partial-batch-failure semantics. Previously per-record
    # exceptions were caught and the batch returned 200, so SQS
    # deleted every message even ones that failed. Failed records
    # never retried and never reached the DLQ -- silent KB data loss
    # under transient upstream 5xx. Now failed records land in
    # batchItemFailures so SQS re-delivers them for retry (bounded by
    # the queue's maxReceiveCount before landing in the DLQ).
    #
    # Idempotency: sync writes are S3 put + DDB upsert, both idempotent
    # under re-delivery. No counter/append side effects to guard.
    batch_item_failures: List[Dict[str, str]] = []
    successes = 0
    failures = 0
    for record in event.get("Records", []):
        try:
            message = json.loads(record["body"])
            logger.info(f"Processing message: {json.dumps(message)}")

            result = process_sync_request(message)
            if result.get("status") in ("success", "no_changes", "skipped"):
                successes += 1
            else:
                # Business-logic failure surface -- treat as retry too
                # so the DLQ builds an inspectable inventory.
                logger.error(
                    f"[github-sync] non-success result for {record.get('messageId')}: {result}"
                )
                batch_item_failures.append({"itemIdentifier": record["messageId"]})
                failures += 1

        except Exception as e:
            logger.exception(f"Error processing message {record.get('messageId')}: {e}")
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
            failures += 1

    logger.info(
        f"Sync complete: {successes} succeeded, {failures} failed, "
        f"{len(batch_item_failures)} re-delivered"
    )

    return {"batchItemFailures": batch_item_failures}
