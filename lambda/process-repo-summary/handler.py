"""
Lambda handler for processing repository architectural summaries from SQS.

This handler:
1. Receives repo messages from SQS queue
2. Fetches repository file list from GitHub API (or uses provided list)
3. Discovers code units using backend abstraction (for workspace repos)
4. Generates architectural summary using Claude via Bedrock
5. Creates embeddings using Bedrock Titan v2
6. Stores summary in DynamoDB with embeddings
7. Sends code units to code-maps queue for detailed processing
8. Returns successfully processed records or raises errors for retry

Supports both:
- Legacy mode: code_units provided in message (from generate-code-maps)
- Workspace mode: discover_code_units=True, uses installation tokens

Triggered by: SQS event source mapping
Documentation: docs/lambda-process-repo-summary.md
"""

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import ClientError

# Add parent directory to path for shared module imports
_PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

# Add generate-code-maps directory to path for backend imports
GENERATE_CODE_MAPS_DIR = os.path.join(_PARENT_DIR, "generate-code-maps")
if GENERATE_CODE_MAPS_DIR not in sys.path:
    sys.path.insert(0, GENERATE_CODE_MAPS_DIR)

from shared import dual_write
from shared.prompts import CODE_MAP_LITERAL_DISCIPLINE
from shared.audit import COMPREHEND_MODEL_ID, log_audit_entry
from shared.model_client import invoke as invoke_model
from shared.audit_writer import (
    ACTION_DOCUMENT_INGESTED,
    log_admin_action,
    log_license_fail_open,
)
from shared.pii import MODE_STRICT, read_workspace_mode, redact

# License layer imports (ADR-019). The layer is attached at deploy time;
# absence is a deploy bug detected on first invocation and surfaced via
# log_license_fail_open + a batchItemFailures response so SQS retries.
try:
    from outcomeops_license import (
        check_repo_limit,
        track_code_map_updated,
        validate_license,
    )
except ImportError:
    validate_license = None  # type: ignore[assignment]
    check_repo_limit = None  # type: ignore[assignment]
    track_code_map_updated = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Bedrock model IDs (configurable via environment variables)
ADVANCED_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)

# AWS clients (region inferred from environment)

# Configure boto3 with longer timeouts for Bedrock (Claude can take 2-3 minutes)
bedrock_config = Config(
    read_timeout=300,  # 5 minutes read timeout
    connect_timeout=10,
    retries={'max_attempts': 3}
)

dynamodb_client = boto3.client("dynamodb")
dynamodb_resource = boto3.resource("dynamodb")
# ADR-035 chunk 12: bedrock_client is EMBEDDINGS-ONLY (Titan via
# invoke_model). The architectural-summary generation routes through
# shared.model_client.invoke (imported above). The legacy 300s read
# timeout on bedrock_config is no longer load-bearing here.
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
s3_client = boto3.client("s3")
sqs_client = boto3.client("sqs")
ssm_client = boto3.client("ssm")
s3vectors_client = boto3.client("s3vectors")
sts_client = boto3.client("sts")


def _get_workspaces_table():
    """Lazy-init DDB table handle for workspace privacy_mode lookups."""
    name = os.environ.get("WORKSPACES_TABLE", "")
    if not name:
        return None
    return dynamodb_resource.Table(name)


def _emit_ingest_audit(
    workspace_id: str,
    source: str,
    document_id: str,
    redaction,
    mode: str,
) -> None:
    """Emit two audit rows for one ingest: an admin DOCUMENT_INGESTED
    row that surfaces in the audit dashboard, and a Comprehend bedrock
    row that rolls into cost-by-model. Skipped runs (workspace_none /
    org_disabled) emit only the admin row. Non-fatal."""
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return

    log_admin_action(
        audit_table,
        ACTION_DOCUMENT_INGESTED,
        workspace_id=workspace_id,
        user_email="",  # SQS-driven; no user context.
        source_ip="",
        user_agent="",
        details={
            "source": source,
            "document_id": document_id,
            "char_count": redaction.char_count,
            "redaction_summary": redaction.summary,
            "skipped_reason": redaction.skipped_reason,
            "mode": mode,
        },
    )

    if redaction.skipped_reason in ("", "comprehend_error"):
        log_audit_entry(
            audit_table_name=audit_table,
            lambda_name="process-repo-summary",
            model_id=COMPREHEND_MODEL_ID,
            input_text="",
            output_text="",
            char_count=redaction.char_count,
            workspace_id=workspace_id,
        )

# Cached account ID
_account_id = None


def _get_account_id() -> str:
    """Return the AWS account ID (cached after first call)."""
    global _account_id
    if _account_id is None:
        _account_id = sts_client.get_caller_identity()["Account"]
    return _account_id

# Configuration (loaded from SSM at container startup)
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Delete-worker queue URL (env var). Used to enqueue per-sync code-map
# orphan cleanup messages once batch fan-out is complete.
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

CODE_MAPS_TABLE = None
KB_BUCKET = None
CODE_MAPS_QUEUE_URL = None
GITHUB_TOKEN = None
VECTOR_BUCKET = None
VECTOR_INDEX = None

GITHUB_API_URL = "https://api.github.com"


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global CODE_MAPS_TABLE, KB_BUCKET, CODE_MAPS_QUEUE_URL, GITHUB_TOKEN, VECTOR_BUCKET, VECTOR_INDEX

    try:
        code_maps_param = f"/{ENVIRONMENT}/{APP_NAME}/dynamodb/code-maps-table"
        CODE_MAPS_TABLE = ssm_client.get_parameter(Name=code_maps_param)["Parameter"]["Value"]

        kb_bucket_param = f"/{ENVIRONMENT}/{APP_NAME}/s3/knowledge-base-bucket"
        KB_BUCKET = ssm_client.get_parameter(Name=kb_bucket_param)["Parameter"]["Value"]

        queue_url_param = f"/{ENVIRONMENT}/{APP_NAME}/sqs/code-maps-queue-url"
        CODE_MAPS_QUEUE_URL = ssm_client.get_parameter(Name=queue_url_param)["Parameter"]["Value"]

        github_token_param = f"/{ENVIRONMENT}/{APP_NAME}/github/token"
        GITHUB_TOKEN = ssm_client.get_parameter(
            Name=github_token_param,
            WithDecryption=True
        )["Parameter"]["Value"]

        # S3 Vectors configuration
        vector_bucket_param = f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/bucket-name"
        VECTOR_BUCKET = ssm_client.get_parameter(Name=vector_bucket_param)["Parameter"]["Value"]

        vector_index_param = f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/index-name"
        VECTOR_INDEX = ssm_client.get_parameter(Name=vector_index_param)["Parameter"]["Value"]

        logger.info(f"Configuration loaded: BUCKET={KB_BUCKET}, VECTOR_BUCKET={VECTOR_BUCKET}")
    except ClientError as e:
        logger.error(f"Failed to load configuration from SSM: {e}")
        raise


# ============================================================================
# GitHub App Installation Token Support (for workspace repos)
# ============================================================================


def get_github_app_config() -> Dict[str, str]:
    """Get GitHub App configuration from SSM."""
    param_prefix = f"/{ENVIRONMENT}/{APP_NAME}/github"
    try:
        response = ssm_client.get_parameters(
            Names=[
                f"{param_prefix}/app-id",
                f"{param_prefix}/private-key",
            ],
            WithDecryption=True,
        )
        params = {p["Name"].split("/")[-1]: p["Value"] for p in response["Parameters"]}
        return {
            "app_id": params.get("app-id", ""),
            "private_key": params.get("private-key", ""),
        }
    except ClientError as e:
        logger.error(f"Failed to get GitHub App config: {e}")
        raise


def get_installation_token(installation_id: str) -> str:
    """Exchange App JWT for an installation access token."""
    import jwt

    config = get_github_app_config()
    app_id = config["app_id"]
    private_key = config["private_key"]

    if not app_id or app_id == "PLACEHOLDER_CONFIGURE_ME":
        raise ValueError("GitHub App ID not configured")
    if not private_key or private_key == "PLACEHOLDER_CONFIGURE_ME":
        raise ValueError("GitHub App private key not configured")

    # Generate JWT
    now = int(time.time())
    jwt_payload = {
        "iat": now - 60,
        "exp": now + 600,
        "iss": app_id,
    }
    jwt_token = jwt.encode(jwt_payload, private_key, algorithm="RS256")

    # Exchange for installation token
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    req = Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data["token"]
    except URLError as e:
        logger.error(f"Failed to get installation token: {e}")
        raise


# ============================================================================
# Code Unit Discovery (for workspace repos)
# ============================================================================


def fetch_repo_config(token: str, repo_project: str) -> Dict:
    """Fetch .outcomeops.yaml from repository."""
    url = f"https://api.github.com/repos/{repo_project}/contents/.outcomeops.yaml"
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urlopen(req, timeout=30) as response:
            content = response.read().decode("utf-8")
            return yaml.safe_load(content) or {}
    except URLError as e:
        if hasattr(e, 'code') and e.code == 404:
            logger.info(f"No .outcomeops.yaml found in {repo_project}")
            return {}
        logger.warning(f"Failed to fetch .outcomeops.yaml: {e}")
        return {}


def get_backend_for_repo(repo_config: Dict, token: str):
    """Get the appropriate backend for a repository."""
    try:
        from backends import get_backend
    except ImportError:
        logger.warning("Failed to import backends")
        return None

    code_gen_config = repo_config.get("code_generation", {})
    backend_type = code_gen_config.get("backend", "python")

    backend_config = {
        "lambda_directory": "lambda",
        "handler_file": "handler.py",
        "include_submodules": True,
        "max_file_size_tokens": 7000,
        "github_token": token,
        "github_api_url": GITHUB_API_URL,
        "handler_directories": code_gen_config.get("handler_directories", []),
        "handler_patterns": code_gen_config.get("handler_patterns"),
        "source_roots": code_gen_config.get("source_roots"),
    }
    backend_config = {k: v for k, v in backend_config.items() if v is not None}

    try:
        return get_backend(backend_type, backend_config)
    except Exception as e:
        logger.warning(f"Failed to get backend {backend_type}: {e}")
        return get_backend("python", backend_config)


def discover_code_units(
    token: Optional[str],
    repo_project: str,
    repo: str,
    all_files: List[str],
) -> List[Dict[str, Any]]:
    """
    Discover code units from file list using backend abstraction.

    ``token`` may be None for non-github providers (chunk-08a). In
    that case we skip the GitHub .outcomeops.yaml fetch and use an
    empty repo config -- the backend defaults to python with no
    custom handler patterns, which works against any file tree at
    the path-analysis level. Backends that need authenticated change
    detection later won't be exercised because non-github providers
    skip the change-detection path (full-regen on every sync).

    Returns list of code unit dicts with batch_type, group_name, file_paths, storage_key.
    """
    if token:
        repo_config = fetch_repo_config(token, repo_project)
    else:
        repo_config = {}
    backend = get_backend_for_repo(repo_config, token or "")

    if not backend:
        logger.warning(f"No backend available for {repo_project}, returning empty code units")
        return []

    # Convert file paths to format expected by backend
    # Backend expects list of dicts with 'path' and 'type' keys
    file_items = [{"path": f, "type": "blob"} for f in all_files]

    # Discover code units
    code_units = backend.discover_code_units(file_items)
    logger.info(f"Discovered {len(code_units)} code units for {repo_project}")

    # Convert to serializable format
    result = []
    for unit in code_units:
        batch_metadata = backend.generate_batch_metadata(unit, repo)
        result.append({
            "batch_type": batch_metadata["batch_type"],
            "group_name": batch_metadata["group_name"],
            "file_paths": unit.file_paths,
            "storage_key": batch_metadata["storage_key"],
            "backend_type": batch_metadata.get("backend_type", "lambda"),
        })

    return result


def retry_bedrock_call(func, max_retries: int = 5):
    """
    Retry helper for Bedrock calls with exponential backoff.

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts (default 5)

    Returns:
        Result from successful function call
    """
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

            # Aggressive exponential backoff for severe throttling
            # 10s, 20s, 40s, 80s, 120s = up to 2 minutes per retry
            delay_s = min(10 * (2 ** (attempt - 1)), 120)
            logger.warning(f"Bedrock throttled (attempt {attempt}/{max_retries}), waiting {delay_s}s before retry...")
            time.sleep(delay_s)

    raise last_error


def list_repository_files(repo_project: str) -> List[str]:
    """
    List all files in a repository using GitHub API.

    Args:
        repo_project: Repository in format "owner/repo"

    Returns:
        List of file paths
    """
    url = f"{GITHUB_API_URL}/repos/{repo_project}/git/trees/main?recursive=1"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "outcome-ops-process-repo-summary",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        request = Request(url, headers=headers)
        with urlopen(request) as response:
            data = json.loads(response.read().decode("utf-8"))
            tree = data.get("tree", [])
            # Filter to only files (not directories)
            return [item["path"] for item in tree if item["type"] == "blob"]
    except URLError as e:
        logger.error(f"Failed to list files from GitHub: {e}")
        raise


def generate_architectural_summary(repo: str, repo_type: str, all_files: List[str]) -> str:
    """
    Generate architectural summary using Claude via Bedrock.

    Args:
        repo: Repository name
        repo_type: Type of repository (application, standards, etc.)
        all_files: List of all files in the repository

    Returns:
        Generated architectural summary
    """
    # Build file tree representation (limited to first 500 files to avoid token limits)
    # Handle both dict format (legacy) and string format for file paths
    file_paths = [f["path"] if isinstance(f, dict) else f for f in all_files]
    file_tree = "\n".join(sorted(file_paths[:500]))
    if len(all_files) > 500:
        file_tree += f"\n... and {len(all_files) - 500} more files"

    prompt = f"""Analyze the repository '{repo}' and provide a comprehensive architectural summary.

Repository Type: {repo_type}
Total Files: {len(all_files)}

File Structure:
{file_tree}

Provide a structured summary covering:
1. **Primary Purpose**: What is the main function of this repository?
2. **Architecture Pattern**: What architectural pattern is used? (e.g., serverless, microservices, monolith)
3. **Key Components**: What are the main components or modules?
4. **Technology Stack**: What languages, frameworks, and tools are used?
5. **Infrastructure**: What infrastructure resources are defined? (if any)
6. **Dependencies**: What are the key external dependencies?
7. **Testing Strategy**: What testing approach is evident from the file structure?

Keep the summary concise but detailed enough for developers to understand the system quickly.

{CODE_MAP_LITERAL_DISCIPLINE}"""

    def call_model():
        # Low temperature for grounded code-map generation (ADR-027).
        # Routes via shared.model_client.invoke per ADR-035: Converse for
        # anthropic/meta/nova/gpt-oss; Bedrock-Mantle Responses API for
        # openai.gpt-5*/codex. The unified shape lets a tfvars swap of
        # BEDROCK_ADVANCED_MODEL_ID flip backends without code change.
        resp = invoke_model(
            model_id=ADVANCED_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            max_tokens=1024,
            temperature=0.1,
        )
        if not resp.text:
            raise ValueError(
                f"No text content in {ADVANCED_MODEL_ID} response "
                f"(stop_reason={resp.stop_reason})"
            )
        return resp.text, resp.usage

    try:
        bedrock_start = time.time()
        summary, usage = retry_bedrock_call(call_model)
        bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)
        logger.info(f"Generated architectural summary for {repo} ({len(summary)} chars)")

        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="process-repo-summary",
            model_id=ADVANCED_MODEL_ID,
            input_text=prompt,
            output_text=summary,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            duration_ms=bedrock_duration_ms,
        )

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
            "normalize": True
        }

        response = bedrock_client.invoke_model(
            modelId=EMBEDDING_MODEL_ID,
            body=json.dumps(payload)
        )

        response_body = json.loads(response["body"].read())
        return response_body["embedding"]

    try:
        embedding = retry_bedrock_call(call_titan)
        logger.info(f"Generated embedding ({len(embedding)} dimensions)")
        return embedding

    except ClientError as e:
        logger.error(f"Failed to generate embedding: {e}")
        raise


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hash of content for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def store_code_map_embedding(
    doc_id: str,
    content: str,
    content_hash: str,
    embedding: List[float],
    repo: str,
    doc_type: str,
    path: str,
    workspace_id: Optional[str] = None
):
    """
    Store code map with embedding in S3 Vectors.

    Args:
        doc_id: Document ID
        content: Document content
        content_hash: SHA-256 hash of content
        embedding: 1024-dimensional embedding vector
        repo: Repository name
        doc_type: Type of document
        path: File path
        workspace_id: Optional workspace ID for workspace-scoped storage
    """
    try:
        # Construct vector key
        # Workspace repos: {workspace_id}#github#{repo}#{doc_type}#{identifier}
        # Legacy repos: {repo}#{doc_type}#{identifier}
        if workspace_id:
            vector_key = f"{workspace_id}#github#{repo}#{doc_type}#{doc_id.replace(':', '-')}"
        else:
            vector_key = f"{repo}#{doc_type}#{doc_id.replace(':', '-')}"

        metadata = {
            "content": content,
            "content_hash": content_hash,
            "repo": repo,
            "type": doc_type,
            "file_path": path,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if workspace_id:
            metadata["workspace_id"] = workspace_id
            metadata["source"] = "github"

        s3vectors_client.put_vectors(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            vectors=[{
                "key": vector_key,
                "data": {"float32": embedding},
                "metadata": metadata
            }]
        )
        logger.info(f"Stored code map embedding in S3 Vectors: {vector_key}")

    except ClientError as e:
        logger.error(f"Failed to store code map embedding in S3 Vectors: {e}")
        raise


def _expected_code_map_vector_keys(
    code_units: List[Dict[str, Any]],
    repo: str,
    workspace_id: Optional[str],
) -> List[str]:
    """Compute the deterministic vector keys process-batch-summary will write
    for the given code units. Mirrors process-batch-summary/handler.py:929-938
    EXACTLY:

        doc_type = f"{batch['batch_type']}-summary"     # appends -summary
        key      = f"{ws}#github#{repo}#{doc_type}#{group_name}"

    The `-summary` suffix is what differentiates raw batch_types
    (handler-group, csharp-project, source-module, components, ...) as
    returned by the per-backend discover_code_units from the doc_type
    segment that lands in the vector key (handler-group-summary,
    csharp-project-summary, source-module-summary, components-summary).

    Without this suffix, every expected_key would be off-by-`-summary`
    and the orphan cleanup would treat every legitimately-stored vector
    as an orphan and delete it. (This was the bug in the first
    deployment of this code; tests caught the absence of -summary in
    the live prd index.)

    Args:
        code_units: Output of discover_code_units() - each dict has
            batch_type (raw, no -summary suffix) and group_name.
        repo: Short repo name (matches batch["repo"] in the writer).
        workspace_id: Workspace ID, or None for non-workspace repos.

    Returns:
        List of expected vector keys (one per unit).
    """
    keys = []
    for unit in code_units:
        doc_type = f"{unit['batch_type']}-summary"
        if workspace_id:
            keys.append(
                f"{workspace_id}#github#{repo}#{doc_type}#{unit['group_name']}"
            )
        else:
            keys.append(f"{repo}#{doc_type}#{unit['group_name']}")
    return keys


def _queue_orphan_code_map_cleanup(
    repo: str,
    expected_keys: List[str],
    workspace_id: Optional[str],
) -> None:
    """Queue a delete_orphan_code_maps job to delete-worker.

    Non-fatal: if the queue isn't configured or the send fails, log and
    continue. The cleanup is best-effort - missing it leaves stale
    vectors but doesn't break the regen.

    No-op for non-workspace repos (workspace_id == None) since the
    delete-worker action only handles workspace-scoped key shapes today.

    Args:
        repo: Short repo name (matches the writer's batch messages).
        expected_keys: Full set of vector keys to retain.
        workspace_id: Workspace ID. Required - cleanup is skipped if None.
    """
    if not workspace_id:
        return

    if not DELETE_QUEUE:
        logger.warning(
            "[orphan-cleanup] DELETE_QUEUE not configured; skipping "
            f"code-map orphan cleanup for {workspace_id}/{repo}"
        )
        return

    if not expected_keys:
        # Belt+suspenders: delete-worker also defends, but skip the
        # SQS round-trip when there's nothing to compare against.
        logger.warning(
            "[orphan-cleanup] No expected_keys for "
            f"{workspace_id}/{repo}; skipping (would wipe everything)"
        )
        return

    payload = {
        "action": "delete_orphan_code_maps",
        "workspace_id": workspace_id,
        "repo": repo,
        "expected_keys": expected_keys,
    }
    try:
        sqs_client.send_message(
            QueueUrl=DELETE_QUEUE,
            MessageBody=json.dumps(payload),
        )
        logger.info(
            f"[orphan-cleanup] Queued code-map orphan cleanup for "
            f"{workspace_id}/{repo} (expected={len(expected_keys)} keys)"
        )
    except ClientError as e:
        logger.error(
            f"[orphan-cleanup] Failed to queue cleanup for "
            f"{workspace_id}/{repo}: {e}"
        )


def _open_code_map_units_ledger(
    workspace_id: str,
    repo: str,
    commit_sha: str,
    commit_timestamp: int,
    expected_count: int,
) -> None:
    """Open ``PENDING#code_map_units#{sha}`` with the real fan-out
    count once process-repo-summary knows how many code units it
    will dispatch. Closed incrementally by ``process-batch-summary``
    as each code-unit SQS message lands.

    plan-git-truth chunk 02b: this is the second stage of the
    two-stage code-map ledger; the first stage
    (``code_map_dispatch``) was opened by github-sync.
    """
    if expected_count <= 0:
        return
    try:
        from shared.sha_truth import (  # type: ignore
            PendingBatch,
            open_pending_batch,
        )
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[process-repo-summary] sha_truth import failed: %s",
            exc,
        )
        return
    try:
        open_pending_batch(PendingBatch(
            workspace_id=workspace_id,
            repo=repo,
            pipeline="code_map_units",
            target_sha=commit_sha,
            expected_count=expected_count,
            target_commit_timestamp=commit_timestamp,
        ))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[process-repo-summary] code_map_units ledger open failed "
            "for %s@%s: %s",
            repo, commit_sha, exc,
        )


def _close_code_map_dispatch_ledger(
    workspace_id: str,
    repo: str,
    commit_sha: str,
) -> None:
    """Close ``PENDING#code_map_dispatch#{sha}`` once
    process-repo-summary finishes its part of the work. The dispatch
    stage is single-unit (github-sync's repo-summaries message →
    one process-repo-summary invocation), so we mark complete and
    fire try_advance_sha. The advance will only succeed when the
    paired ``code_map_units`` stage is also fully closed.
    """
    try:
        from shared.sha_truth import (  # type: ignore
            mark_unit_complete,
            try_advance_sha,
        )
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[process-repo-summary] sha_truth import failed: %s",
            exc,
        )
        return
    try:
        ledger = mark_unit_complete(
            workspace_id, repo, "code_map_dispatch", commit_sha
        )
        if ledger:
            try_advance_sha(ledger)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[process-repo-summary] code_map_dispatch close failed "
            "for %s@%s: %s",
            repo, commit_sha, exc,
        )


def _record_code_unit_dispatch_failure(
    workspace_id: str,
    repo: str,
    commit_sha: str,
    code_unit: Dict[str, Any],
    failure_detail: str,
) -> None:
    """Write a failed-files row for a code unit that couldn't be
    dispatched. Increments ``failed_count`` on the code_map_units
    ledger (when it exists). Non-fatal if the ledger is missing —
    the per-unit dispatch loop continues either way.
    """
    try:
        from shared.sha_truth import mark_unit_failed  # type: ignore
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[process-repo-summary] sha_truth import failed: %s",
            exc,
        )
        return
    file_path = (
        code_unit.get("storage_key")
        or code_unit.get("group_name")
        or "<unknown-code-unit>"
    )
    try:
        mark_unit_failed(
            workspace_id,
            repo,
            "code_map_units",
            commit_sha,
            file_path=str(file_path),
            failure_reason="sqs_send_failed",
            failure_detail=failure_detail,
            attempt_count=1,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[process-repo-summary] failed-files write failed "
            "for %s@%s/%s: %s",
            repo, commit_sha, file_path, exc,
        )


def send_code_units_to_sqs(
    code_units: List[Dict[str, Any]],
    repo: str,
    repo_project: str,
    workspace_id: Optional[str] = None,
    commit_sha: Optional[str] = None,
    commit_timestamp: Optional[int] = None,
    *,
    provider: str = "github",
    slug: Optional[str] = None,
) -> int:
    """
    Send code units to SQS for detailed batch processing.

    Args:
        code_units: List of code unit dicts (may be filtered to changed
            units in incremental mode - that's fine for processing).
        repo: Repository name
        repo_project: Repository project (owner/repo)
        workspace_id: Optional workspace ID for workspace-scoped storage
        commit_sha: Optional current repo commit SHA. When supplied
            alongside ``workspace_id``, ``process-batch-summary`` runs
            the symbol-graph extractor + atomic-swap pointer protocol.
            When absent, the consumer falls back to the legacy
            summary-only path.
        commit_timestamp: Epoch seconds of the commit being processed.
            Threaded onto each code-unit SQS body so
            process-batch-summary can mark its code_map_units ledger
            with the correct monotonic-advance anchor (plan-git-truth
            chunk 02b).

    Returns:
        Count of units successfully published to SQS. Per-unit
        publish failures are recorded in code-map-failed-files and
        increment ``failed_count`` on the code_map_units ledger row
        (when ledger context is available) — they do NOT abort the
        rest of the batch and do NOT re-raise. This is the
        error-handling shift in plan-git-truth chunk 02b: the
        record's other units still dispatch, the SQS message
        succeeds, and operators see the failed units in the
        failed-files table instead of an opaque DLQ entry.

    Note: orphan cleanup is NOT queued from here. The caller must invoke
    _queue_orphan_code_map_cleanup() separately with the FULL current
    tree's code units (not the incremental subset) so the cleanup's
    expected_keys reflects every handler that should remain - otherwise
    incremental sync would treat all unchanged handlers as orphans.
    """
    sent = 0
    for code_unit in code_units:
        try:
            message = {
                "repo": repo,
                "repo_project": repo_project,
                "batch_type": code_unit["batch_type"],
                "group_name": code_unit["group_name"],
                "file_paths": code_unit["file_paths"],
                "storage_key": code_unit["storage_key"],
            }
            if workspace_id:
                message["workspace_id"] = workspace_id
            if commit_sha:
                # Symbol-graph regen anchors on this commit SHA.
                message["commit_sha"] = commit_sha
            if commit_timestamp is not None:
                # plan-git-truth chunk 02b: monotonic-advance anchor
                # for the per-unit ledger increments downstream.
                message["commit_timestamp"] = commit_timestamp
            # chunk-08a: process-batch-summary's fetch_file_content
            # dispatches on `provider`. For azure_devops it reads from
            # the S3 mirror at workspaces/{ws}/azure_devops/{slug}/.
            # GitHub default "github" keeps the existing
            # raw.githubusercontent.com path; no message-shape change
            # for existing GitHub flows.
            if provider != "github":
                message["provider"] = provider
            if slug:
                message["slug"] = slug

            message_body = json.dumps(message)

            sqs_client.send_message(
                QueueUrl=CODE_MAPS_QUEUE_URL,
                MessageBody=message_body,
            )

            sent += 1
            logger.info(f"Sent code unit to SQS: {code_unit['group_name']}")

        except ClientError as e:
            # plan-git-truth chunk 02b: per-unit dispatch failure does
            # not abort the batch. Record the failure in the SHA-truth
            # failed-files table; the rest of the units still get
            # dispatched.
            logger.error(
                "Failed to send code unit to SQS for %s/%s: %s",
                repo_project,
                code_unit.get("group_name", "<unknown>"),
                e,
            )
            if workspace_id and commit_sha:
                _record_code_unit_dispatch_failure(
                    workspace_id=workspace_id,
                    repo=repo_project,
                    commit_sha=commit_sha,
                    code_unit=code_unit,
                    failure_detail=str(e)[:1024],
                )
    return sent


def handler(event, context):
    """
    Process repository architectural summary requests from SQS.

    SQS Message Format (legacy from generate-code-maps):
    {
        "repo": "repo-name",
        "repo_project": "owner/repo",
        "repo_type": "application",
        "all_files": ["file1.py", "file2.py", ...],
        "code_units": [...]
    }

    SQS Message Format (workspace repos from github-sync):
    {
        "repo": "repo-name",
        "repo_project": "owner/repo",
        "repo_type": "application",
        "workspace_id": "ws-123",
        "installation_id": "12345",
        "all_files": ["file1.py", "file2.py", ...],
        "discover_code_units": true
    }
    """
    # License validation (ADR-019). SQS-driven entry: on validation
    # failure we mark every record in the batch as failed so SQS retries
    # / DLQs them rather than silently dropping.
    records = event.get("Records", [])
    if validate_license is None:
        logger.error(
            "[process-repo-summary] License layer not attached - "
            "deploy bug (ADR-019)"
        )
        log_license_fail_open(
            os.environ.get("AUDIT_TABLE", ""), "process-repo-summary"
        )
        return {
            "batchItemFailures": [
                {"itemIdentifier": r.get("messageId")} for r in records
            ]
        }
    try:
        claims = validate_license()
        logger.info(
            f"[process-repo-summary] License validated: {claims.get('sub')}"
        )
    except Exception as e:
        logger.error(f"[process-repo-summary] License validation failed: {e}")
        return {
            "batchItemFailures": [
                {"itemIdentifier": r.get("messageId")} for r in records
            ]
        }

    load_config()

    logger.info(f"Processing {len(records)} repository summary requests")

    batch_item_failures = []

    for record in records:
        message_id = record.get("messageId")
        receipt_handle = record.get("receiptHandle")

        try:
            # Parse SQS message
            message_body = json.loads(record["body"])
            repo = message_body["repo"]
            repo_project = message_body["repo_project"]
            repo_type = message_body["repo_type"]
            all_files = message_body["all_files"]

            # Handle workspace repos with discover_code_units flag
            workspace_id = message_body.get("workspace_id")
            installation_id = message_body.get("installation_id")
            should_discover = message_body.get("discover_code_units", False)
            # chunk-08a provider routing: github (default) uses the
            # existing installation_token + GitHub API path. ADO uses
            # the S3 mirror; no token needed at this layer (sync
            # worker already authenticated against the customer's
            # tenant during the file upload).
            provider = message_body.get("provider", "github")
            slug = message_body.get("slug")
            connection_id = message_body.get("connection_id")

            # Read commit SHAs for incremental processing
            last_commit_sha = message_body.get("last_commit_sha")
            current_commit_sha = message_body.get("current_commit_sha")
            # plan-git-truth chunk 02b: commit_timestamp is threaded
            # through from github-sync on the repo-summaries body so
            # this dispatcher can open the code_map_units ledger with
            # the correct monotonic-advance anchor.
            current_commit_timestamp = message_body.get("commit_timestamp")

            # Repo branch (used for usage tracking; defaults to "main"
            # when github-sync did not include it in the message body).
            branch = message_body.get("branch", "main")

            # Repo limit check (ADR-019). check_repo_limit returns
            # allowed=True when the repo is already tracked, so this is
            # a no-op for previously-processed repos. allowed=False only
            # when adding a new repo would exceed the tier limit. We
            # skip the message (not added to batch_item_failures)
            # because limit-blocked is not a retryable failure.
            limit_result = check_repo_limit(repo=repo_project)
            if not limit_result.allowed:
                logger.warning(
                    f"[process-repo-summary] Repo limit reached for "
                    f"{repo_project}: {limit_result.reason} "
                    f"(current={limit_result.current}/{limit_result.limit})"
                )
                continue

            # full_code_units captures the COMPLETE current tree (pre-incremental-filter).
            # Used for code-map orphan cleanup so we don't accidentally
            # delete vectors for unchanged handlers.
            full_code_units: List[Dict[str, Any]] = []

            if should_discover and provider == "azure_devops":
                # chunk-08a ADO branch. No installation token needed;
                # the sync worker already wrote files to S3 and
                # downstream process-batch-summary reads them via
                # shared.source_fetch using provider+workspace_id+slug.
                # Backend selection still runs to figure out which
                # code-units the file paths belong to, but with a
                # token=None signal. Change detection is skipped
                # (treat every ADO sync as full-regen for v1 -- the
                # azure-devops-sync skip-unchanged check at the SHA
                # level upstream already gates the work).
                logger.info(
                    f"Workspace ADO repo {repo_project}: discovering code units",
                )
                try:
                    code_units = discover_code_units(
                        token=None, repo_project=repo_project,
                        repo=repo, all_files=all_files,
                    )
                    logger.info(
                        f"Discovered {len(code_units)} code units for {repo_project}",
                    )
                    full_code_units = list(code_units)
                except Exception as e:
                    logger.warning(
                        f"Failed to discover code units for ADO repo "
                        f"{repo_project}: {e}",
                    )
                    code_units = []
                    full_code_units = []
            elif should_discover and installation_id:
                # Workspace repo: Get installation token and discover code units
                logger.info(f"Workspace repo {repo_project}: discovering code units")
                try:
                    token = get_installation_token(installation_id)
                    code_units = discover_code_units(token, repo_project, repo, all_files)
                    logger.info(f"Discovered {len(code_units)} code units for {repo_project}")
                    # Snapshot the full set BEFORE any incremental filter.
                    full_code_units = list(code_units)

                    # Incremental filtering: only process code units with changed files
                    if last_commit_sha and current_commit_sha and code_units:
                        try:
                            backend = get_backend_for_repo(
                                fetch_repo_config(token, repo_project), token
                            )
                            if backend:
                                change_result = backend.detect_changes(
                                    repo, repo_project,
                                    {"commit_sha": last_commit_sha}
                                )
                                if change_result.changed_files:
                                    changed_set = set(change_result.changed_files)
                                    total_units = len(code_units)
                                    code_units = [
                                        u for u in code_units
                                        if any(f in changed_set for f in u["file_paths"])
                                    ]
                                    logger.info(
                                        f"Incremental: {len(code_units)} of {total_units} "
                                        f"code units changed ({len(changed_set)} files changed)"
                                    )
                                else:
                                    logger.info(
                                        f"Full regeneration: no changed_files returned "
                                        f"(first sync or force full)"
                                    )
                        except Exception as e:
                            logger.warning(
                                f"Failed to detect changes for {repo_project}, "
                                f"processing all code units: {e}"
                            )
                except Exception as e:
                    logger.warning(f"Failed to discover code units for {repo_project}: {e}")
                    code_units = []
                    full_code_units = []
            else:
                # Legacy mode: code_units provided in message
                code_units = message_body.get("code_units", [])
                full_code_units = list(code_units)

            logger.info(f"Processing {repo} ({len(all_files)} files, {len(code_units)} code units)")

            # Generate architectural summary
            arch_summary = generate_architectural_summary(repo, repo_type, all_files)

            # PII redaction (plan/ chunk 05). Architectural summaries can
            # echo back inline comments / repo metadata that occasionally
            # carry credentials or customer data. Redact BEFORE both the
            # vector embed and the S3 upload so neither path stores the
            # raw text. Only attempt for workspace-scoped repos (legacy
            # global code-maps run unredacted - no workspace context).
            if workspace_id:
                ws_table = _get_workspaces_table()
                org_enabled = (
                    os.environ.get("PII_REDACTION_ENABLED", "true").lower()
                    == "true"
                )
                mode = (
                    read_workspace_mode(ws_table, workspace_id)
                    if ws_table is not None
                    else MODE_STRICT
                )
                redaction = redact(
                    arch_summary, mode=mode, org_enabled=org_enabled,
                )
                arch_summary = redaction.text
                _emit_ingest_audit(
                    workspace_id=workspace_id,
                    source="github",
                    document_id=f"{repo}/code-map",
                    redaction=redaction,
                    mode=mode,
                )

            # Store architectural summary with embedding
            summary_doc_id = f"{repo}/code-map"
            summary_hash = compute_content_hash(arch_summary)
            summary_embedding = generate_embedding(arch_summary)

            store_code_map_embedding(
                doc_id=summary_doc_id,
                content=arch_summary,
                content_hash=summary_hash,
                embedding=summary_embedding,
                repo=repo_project,
                doc_type="code-map",
                path="code-map",
                workspace_id=workspace_id
            )

            # Upload architectural summary to S3
            # Workspace repos: workspaces/{workspace_id}/github/{repo}/architectural-summary.txt
            # Legacy repos: code-maps/{repo}/architectural-summary.txt
            if workspace_id:
                s3_key = f"workspaces/{workspace_id}/github/{repo}/architectural-summary.txt"
            else:
                s3_key = f"code-maps/{repo}/architectural-summary.txt"

            # Pass through the same S3 object metadata github-sync writes
            # for ordinary repo files (lambda/github-sync/handler.py:543).
            # Without this, the process-documents re-ingest path falls back
            # to folder_path.split("/")[0] and stores the vector with the
            # bare repo name ("<repo>") instead of the org-prefixed
            # identifier ("<owner>/<repo>") that every other ingest path
            # uses.
            # Route through dual_write so the architectural-summary KB doc
            # replicates to the HA secondary (direct s3.put_object would
            # land only in the active region's KB — same standards-repo
            # replication gap fixed in github-sync).
            dual_write.s3_put_object(
                bucket=KB_BUCKET,
                key=s3_key,
                body=arch_summary.encode("utf-8"),
                ExpectedBucketOwner=_get_account_id(),
                Metadata={
                    "source": "github",
                    "workspace_id": workspace_id or "",
                    "github_repo_name": repo_project,
                    "github_repo_type": repo_type,
                },
            )

            logger.info(f"Stored architectural summary for {repo} at {s3_key}")

            # Send code units to SQS for detailed processing
            if code_units:
                # plan-git-truth chunk 02b: open the units-stage ledger
                # BEFORE the dispatch loop so per-unit failures (caught
                # inside send_code_units_to_sqs) can increment
                # failed_count on a row that exists. Expected count is
                # the full unit count; mark_unit_failed inside the loop
                # writes failed-files rows for the units that don't
                # publish.
                if (
                    workspace_id
                    and current_commit_sha
                    and current_commit_timestamp is not None
                ):
                    _open_code_map_units_ledger(
                        workspace_id=workspace_id,
                        repo=repo_project,
                        commit_sha=current_commit_sha,
                        commit_timestamp=int(current_commit_timestamp),
                        expected_count=len(code_units),
                    )

                send_code_units_to_sqs(
                    code_units,
                    repo,
                    repo_project,
                    workspace_id,
                    commit_sha=current_commit_sha,
                    commit_timestamp=(
                        int(current_commit_timestamp)
                        if current_commit_timestamp is not None
                        else None
                    ),
                    # chunk-08a: thread provider+slug so the downstream
                    # process-batch-summary's fetch_file_content can
                    # dispatch to the S3 mirror for non-github providers.
                    provider=provider,
                    slug=slug,
                )
                logger.info(f"Queued {len(code_units)} code units for {repo}")

            # plan-git-truth chunk 02b: close the dispatch-stage ledger
            # AFTER opening the units-stage. The dispatch close fires
            # try_advance_sha, which checks the units stage too — if
            # units is open and incomplete, the advance is deferred to
            # whichever consumer closes the last unit. If there are
            # zero code units (rare; doc-only changes), the units
            # stage never opens and try_advance_sha treats it as
            # "stage not present" and blocks the advance until a
            # subsequent regen reopens both.
            if (
                workspace_id
                and current_commit_sha
                and code_units
                and current_commit_timestamp is not None
            ):
                _close_code_map_dispatch_ledger(
                    workspace_id, repo_project, current_commit_sha
                )

            # Queue code-map orphan cleanup using the FULL tree's code units
            # (pre-incremental-filter). This way the expected_keys reflects
            # every handler that should remain in the index, not just the
            # ones being regenerated this invocation. Skipping when
            # full_code_units is empty defends against wiping a healthy repo
            # if discover_code_units fell over.
            if full_code_units:
                expected_keys = _expected_code_map_vector_keys(
                    full_code_units, repo, workspace_id
                )
                _queue_orphan_code_map_cleanup(repo, expected_keys, workspace_id)

            # Usage tracking (ADR-019). Non-blocking: returns bool, never
            # raises. Liveness tracking moved to connect/disconnect handlers
            # via report_repos_snapshot (license layer v2.0.0); only the
            # code-map-updated usage metric still fires per code-map run.
            track_code_map_updated(repo=repo_project, branch=branch)
            logger.info(
                f"[process-repo-summary] Tracked code map update for "
                f"{repo_project} (branch={branch})"
            )

        except Exception as e:
            logger.error(f"Error processing record {message_id}: {e}")
            batch_item_failures.append({
                "itemIdentifier": message_id
            })

    # Return partial batch response
    # Successfully processed messages are deleted from queue
    # Failed messages remain for retry
    return {
        "batchItemFailures": batch_item_failures
    }
