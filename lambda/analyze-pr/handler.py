"""
Lambda handler for analyzing Pull Requests and queueing architecture checks.

This handler:
1. Fetches PR diff from GitHub API
2. Loads .outcomeops.yaml to determine backend type
3. Parses changed files using backend-specific patterns
4. Queues check jobs to SQS FIFO queue for async processing
5. Posts initial comment to PR with analysis status
6. Checks: ADR Compliance, README Freshness, Test Coverage, Breaking Changes, Architectural Duplication, License Compliance

Input: { "pr_number": int, "repository": "owner/repo" }
Output: { "message": str, "pr_number": int, "checks_queued": int }

This Lambda is triggered manually or via GitHub webhooks.
"""

import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from enum import Enum

import boto3
import requests

# Make shared/ importable when running as a Lambda; the deploy package
# includes lambda/shared/ at zip root via terraform source_path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import ACTION_PR_ANALYZED, log_admin_action
import yaml
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
ssm_client = boto3.client("ssm")
sqs_client = boto3.client("sqs")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")


class CheckType(str, Enum):
    """Types of checks that can be performed on a PR."""
    ADR_COMPLIANCE = "ADR_COMPLIANCE"
    README_FRESHNESS = "README_FRESHNESS"
    TEST_COVERAGE = "TEST_COVERAGE"
    BREAKING_CHANGES = "BREAKING_CHANGES"
    ARCHITECTURAL_DUPLICATION = "ARCHITECTURAL_DUPLICATION"
    LICENSE_COMPLIANCE = "LICENSE_COMPLIANCE"


class AnalyzePrRequest(BaseModel):
    """Request schema for analyze-pr Lambda."""
    pr_number: int = Field(..., gt=0, description="Pull Request number")
    repository: str = Field(..., min_length=1, description="Repository in format 'owner/repo'")

    @field_validator("repository")
    @classmethod
    def validate_repository_format(cls, v):
        """Validate repository is in owner/repo format."""
        if "/" not in v or v.count("/") != 1:
            raise ValueError("Repository must be in format 'owner/repo'")
        owner, repo = v.split("/")
        if not owner or not repo:
            raise ValueError("Owner and repo cannot be empty")
        return v


class AnalyzePrResponse(BaseModel):
    """Response schema for analyze-pr Lambda."""
    message: str
    pr_number: int
    checks_queued: int = Field(..., ge=0)


class CheckJob(BaseModel):
    """Schema for check job sent to SQS."""
    check_type: CheckType
    pr_number: int
    repository: str
    changed_files: List[str]


class GitHubPullRequest(BaseModel):
    """GitHub Pull Request schema (subset of fields we need)."""
    number: int
    title: str
    html_url: str
    base: Dict[str, Any]
    head: Dict[str, Any]


class GitHubFile(BaseModel):
    """GitHub file change schema."""
    filename: str
    status: str  # "added", "modified", "removed", "renamed"
    additions: int
    deletions: int
    changes: int
    # GitHub's /pulls/{n}/files response includes a ``patch`` field with
    # the unified-diff body for each file (omitted for binary diffs or
    # very large patches). The structural-impact pass walks this for
    # symbol resolution. Optional so the existing parse/queue path
    # (which only needs filename + status) stays backwards-compatible.
    patch: Optional[str] = None


def get_github_token() -> str:
    """
    Fetch GitHub personal access token from SSM Parameter Store.

    Returns:
        GitHub PAT string

    Raises:
        Exception: If token not found in SSM
    """
    param_name = f"/{ENVIRONMENT}/{APP_NAME}/github/token"

    try:
        response = ssm_client.get_parameter(
            Name=param_name,
            WithDecryption=True
        )

        if not response.get("Parameter", {}).get("Value"):
            raise Exception(f"GitHub token not found in parameter: {param_name}")

        logger.info(f"Retrieved GitHub token from SSM parameter: {param_name}")
        return response["Parameter"]["Value"]

    except ClientError as e:
        logger.error(f"Failed to retrieve GitHub token from SSM: {e}")
        raise


def fetch_pull_request(repository: str, pr_number: int, token: str) -> GitHubPullRequest:
    """
    Fetch PR details from GitHub API.

    Args:
        repository: Repository in format "owner/repo"
        pr_number: Pull Request number
        token: GitHub personal access token

    Returns:
        GitHubPullRequest object

    Raises:
        Exception: If API request fails
    """
    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        logger.info(f"Fetched PR #{pr_number} from {repository}: {data.get('title', 'Unknown')}")

        return GitHubPullRequest(**data)

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR from GitHub: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def fetch_pr_files(repository: str, pr_number: int, token: str) -> List[GitHubFile]:
    """
    Fetch list of changed files in a PR from GitHub API.

    Args:
        repository: Repository in format "owner/repo"
        pr_number: Pull Request number
        token: GitHub personal access token

    Returns:
        List of GitHubFile objects

    Raises:
        Exception: If API request fails
    """
    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/files"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        files_data = response.json()
        files = [GitHubFile(**file_data) for file_data in files_data]

        logger.info(f"Fetched {len(files)} changed files for PR #{pr_number}")
        return files

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR files from GitHub: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def fetch_file_contents(
    repository: str, file_path: str, ref: str, token: str
) -> Optional[bytes]:
    """Fetch the raw bytes of ``file_path`` at ``ref`` from GitHub.

    Used by the structural-impact pass: the diff symbol resolver
    runs the extractor over the **post-diff** file content so
    canonical symbol IDs match the graph. ``ref`` is the PR's head
    SHA for "what the symbol looks like after the change", or the
    base SHA for staleness queries.

    Returns None for missing files (e.g. removed-in-PR), binary blobs,
    or any error -- callers degrade gracefully.
    """
    url = f"https://api.github.com/repos/{repository}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        # Raw media type so we skip the base64-encoded JSON envelope.
        "Accept": "application/vnd.github.v3.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {"ref": ref}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        logger.warning(
            f"Failed to fetch contents of {file_path} at {ref}: {e}"
        )
        return None


def post_pr_comment(repository: str, pr_number: int, body: str, token: str) -> Dict[str, Any]:
    """
    Post a comment to the PR via GitHub API.

    Args:
        repository: Repository in format "owner/repo"
        pr_number: Pull Request number
        body: Comment text
        token: GitHub personal access token

    Returns:
        API response dict

    Raises:
        Exception: If API request fails
    """
    url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"
    }

    payload = {"body": body}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        logger.info(f"Posted comment to PR #{pr_number} in {repository}")
        return response.json()

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to post comment to GitHub: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


@dataclass
class BackendPatterns:
    """File patterns for a specific backend type."""
    source_dirs: List[str]  # Directories containing source code
    source_extensions: List[str]  # File extensions for source code
    test_dirs: List[str]  # Directories containing tests
    test_patterns: List[str]  # Patterns for test files
    config_files: List[str]  # Build/config files (pom.xml, package.json, etc.)
    handler_patterns: List[str]  # Patterns for main entry points/handlers


# Backend-specific file patterns
BACKEND_PATTERNS: Dict[str, BackendPatterns] = {
    "python": BackendPatterns(
        source_dirs=["lambda/", "src/"],
        source_extensions=[".py"],
        test_dirs=["lambda/tests/", "tests/"],
        test_patterns=["test_*.py", "*_test.py"],
        config_files=["requirements.txt", "pyproject.toml", "setup.py"],
        handler_patterns=["handler.py", "main.py", "app.py"],
    ),
    "java": BackendPatterns(
        source_dirs=["src/main/java/"],
        source_extensions=[".java"],
        test_dirs=["src/test/java/"],
        test_patterns=["*Tests.java", "*Test.java"],
        config_files=["pom.xml", "build.gradle", "build.gradle.kts"],
        handler_patterns=["*Controller.java", "*Application.java", "*Handler.java"],
    ),
    "typescript": BackendPatterns(
        source_dirs=["src/", "lib/"],
        source_extensions=[".ts", ".tsx"],
        test_dirs=["test/", "tests/", "__tests__/"],
        test_patterns=["*.spec.ts", "*.test.ts", "*.spec.tsx", "*.test.tsx"],
        config_files=["package.json", "tsconfig.json"],
        handler_patterns=["handler.ts", "index.ts", "main.ts", "app.ts"],
    ),
}


def fetch_repo_config(repository: str, token: str, ref: str = "main") -> Optional[Dict[str, Any]]:
    """
    Fetch .outcomeops.yaml configuration from the repository.

    Args:
        repository: Repository in format "owner/repo"
        token: GitHub personal access token
        ref: Git ref (branch/tag/commit) to fetch from

    Returns:
        Parsed YAML config dict, or None if not found
    """
    url = f"https://api.github.com/repos/{repository}/contents/.outcomeops.yaml"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    params = {"ref": ref}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 404:
            logger.info(f"No .outcomeops.yaml found in {repository}, using defaults")
            return None

        response.raise_for_status()

        data = response.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        config = yaml.safe_load(content)

        logger.info(f"Loaded .outcomeops.yaml from {repository}: {config}")
        return config

    except requests.exceptions.RequestException as e:
        logger.warning(f"Failed to fetch .outcomeops.yaml from {repository}: {e}")
        return None
    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse .outcomeops.yaml from {repository}: {e}")
        return None


def get_backend_from_config(config: Optional[Dict[str, Any]]) -> str:
    """
    Extract backend type from repo config.

    Args:
        config: Parsed .outcomeops.yaml config dict

    Returns:
        Backend type string ("python", "java", or "typescript"), defaults to "python"
    """
    if not config:
        return "python"

    code_gen_config = config.get("code_generation", {})
    backend = code_gen_config.get("backend", "python")

    # Normalize backend name
    backend = backend.lower().strip()
    if backend not in BACKEND_PATTERNS:
        logger.warning(f"Unknown backend '{backend}', defaulting to python")
        return "python"

    return backend


def _matches_pattern(filepath: str, pattern: str) -> bool:
    """
    Check if a filepath matches a glob-like pattern.

    Supports simple patterns like:
    - "*Controller.java" - ends with Controller.java
    - "test_*.py" - starts with test_ and ends with .py
    - "*.spec.ts" - ends with .spec.ts

    Args:
        filepath: File path to check
        pattern: Glob-like pattern

    Returns:
        True if filepath matches pattern
    """
    import fnmatch
    filename = filepath.split("/")[-1]
    return fnmatch.fnmatch(filename, pattern)


def _is_in_source_dir(filepath: str, patterns: BackendPatterns) -> bool:
    """Check if file is in a source directory (not test directory)."""
    # First check if it's in a test directory
    for test_dir in patterns.test_dirs:
        if filepath.startswith(test_dir):
            return False

    # Check if it's in a source directory
    for source_dir in patterns.source_dirs:
        if filepath.startswith(source_dir):
            return True

    return False


def _is_test_file(filepath: str, patterns: BackendPatterns) -> bool:
    """Check if file is a test file based on directory and pattern."""
    # Check if in test directory
    in_test_dir = any(filepath.startswith(test_dir) for test_dir in patterns.test_dirs)
    if not in_test_dir:
        return False

    # Check if matches test pattern
    return any(_matches_pattern(filepath, pattern) for pattern in patterns.test_patterns)


def _is_handler_file(filepath: str, patterns: BackendPatterns) -> bool:
    """Check if file is a handler/controller/entry point."""
    return any(_matches_pattern(filepath, pattern) for pattern in patterns.handler_patterns)


def parse_changed_files(files: List[GitHubFile], backend: str = "python") -> Dict[str, Any]:
    """
    Parse changed files and determine which checks to run based on backend type.

    Uses backend-specific patterns from BACKEND_PATTERNS to identify:
    - Source files (triggers ADR_COMPLIANCE)
    - Test files (triggers TEST_COVERAGE)
    - Handler/Controller files (triggers BREAKING_CHANGES, ARCHITECTURAL_DUPLICATION)
    - Documentation changes (triggers README_FRESHNESS)
    - All source code (triggers LICENSE_COMPLIANCE)

    Args:
        files: List of GitHubFile objects
        backend: Backend type ("python", "java", or "typescript")

    Returns:
        Dict with "changed_files" list and "checks_to_run" list
    """
    changed_files = [f.filename for f in files]
    checks_to_run: Set[CheckType] = set()

    # Get patterns for this backend
    patterns = BACKEND_PATTERNS.get(backend, BACKEND_PATTERNS["python"])
    logger.info(f"Using {backend} backend patterns for file analysis")

    # Source code extensions for license checking (all supported languages)
    source_extensions = {
        '.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.java',
        '.rb', '.php', '.rs', '.c', '.cpp', '.h', '.hpp',
        '.scala', '.kt', '.swift', '.cs', '.sh', '.bash', '.md'
    }

    for file in files:
        filepath = file.filename
        status = file.status

        # Check file extension matches backend
        has_backend_extension = any(filepath.endswith(ext) for ext in patterns.source_extensions)

        # ADR Compliance: Source files in source directories (not tests)
        if has_backend_extension and _is_in_source_dir(filepath, patterns):
            # Exclude common non-source paths
            exclude_patterns = ["__pycache__", ".pyc", "/node_modules/", "/dist/", "/build/"]
            if not any(excl in filepath for excl in exclude_patterns):
                checks_to_run.add(CheckType.ADR_COMPLIANCE)
                logger.debug(f"ADR_COMPLIANCE triggered by: {filepath}")

        # ADR Compliance: Terraform files (always check)
        if filepath.startswith("terraform/") and filepath.endswith(".tf"):
            checks_to_run.add(CheckType.ADR_COMPLIANCE)

        # README Freshness: Source/Terraform/docs changes
        triggers_readme = (
            any(filepath.startswith(src_dir) for src_dir in patterns.source_dirs) or
            filepath.startswith("terraform/") or
            filepath.startswith("docs/")
        )
        if triggers_readme:
            checks_to_run.add(CheckType.README_FRESHNESS)

        # Test Coverage: Handler/Controller files need tests
        if has_backend_extension and _is_handler_file(filepath, patterns):
            if _is_in_source_dir(filepath, patterns):
                checks_to_run.add(CheckType.TEST_COVERAGE)
                logger.debug(f"TEST_COVERAGE triggered by: {filepath}")

        # Breaking Changes: Handler/Controller modifications
        if has_backend_extension and _is_handler_file(filepath, patterns):
            if status in ["modified", "removed"]:
                checks_to_run.add(CheckType.BREAKING_CHANGES)
                logger.debug(f"BREAKING_CHANGES triggered by: {filepath}")

        # Architectural Duplication: New or modified handlers/controllers
        if has_backend_extension and _is_handler_file(filepath, patterns):
            if status in ["added", "modified"]:
                checks_to_run.add(CheckType.ARCHITECTURAL_DUPLICATION)
                logger.debug(f"ARCHITECTURAL_DUPLICATION triggered by: {filepath}")

        # License Compliance: All source code files
        # Skip generated files and dependencies
        skip_dirs = ['node_modules/', 'vendor/', 'dist/', 'build/', '.git/', 'target/']
        is_skippable = any(skip_dir in filepath for skip_dir in skip_dirs)

        if not is_skippable and any(filepath.endswith(ext) for ext in source_extensions):
            checks_to_run.add(CheckType.LICENSE_COMPLIANCE)

    logger.info(f"Checks to run for {backend} backend: {[c.value for c in checks_to_run]}")

    return {
        "changed_files": changed_files,
        "checks_to_run": [check.value for check in checks_to_run]
    }


def queue_check_jobs(repository: str, pr_number: int, checks: List[str], changed_files: List[str]) -> int:
    """
    Queue check jobs to SQS FIFO queue for async processing.

    Args:
        repository: Repository in format "owner/repo"
        pr_number: Pull Request number
        checks: List of check types to run
        changed_files: List of changed file paths

    Returns:
        Number of jobs queued

    Raises:
        ClientError: If SQS operation fails
    """
    queue_url_param = f"/{ENVIRONMENT}/{APP_NAME}/sqs/pr-checks-queue-url"

    try:
        response = ssm_client.get_parameter(Name=queue_url_param)
        queue_url = response["Parameter"]["Value"]
    except ClientError as e:
        logger.error(f"Failed to retrieve SQS queue URL from SSM: {e}")
        raise

    jobs_queued = 0

    for check_type in checks:
        job = CheckJob(
            check_type=CheckType(check_type),
            pr_number=pr_number,
            repository=repository,
            changed_files=changed_files
        )

        # Message group ensures ordered processing per PR
        message_group_id = f"pr-{repository.replace('/', '-')}-{pr_number}"
        # Deduplication ID prevents duplicate processing
        message_deduplication_id = f"{message_group_id}-{check_type}-{int(datetime.utcnow().timestamp() * 1000)}"

        try:
            sqs_client.send_message(
                QueueUrl=queue_url,
                MessageBody=job.json(),
                MessageGroupId=message_group_id,
                MessageDeduplicationId=message_deduplication_id
            )

            logger.info(f"Queued check job: {check_type} for PR #{pr_number}")
            jobs_queued += 1

        except ClientError as e:
            logger.error(f"Failed to send message to SQS: {e}")
            raise

    return jobs_queued


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for PR analysis orchestration.

    Args:
        event: Lambda event with pr_number and repository
        context: Lambda context (unused)

    Returns:
        Response dict with message, pr_number, and checks_queued

    Raises:
        Exception: For validation or API errors
    """
    start_time = time.time()
    try:
        # Validate request
        request = AnalyzePrRequest(**event)
        logger.info(f"Analyzing PR #{request.pr_number} in {request.repository}")

        # Fetch GitHub token
        token = get_github_token()

        # Fetch PR details
        pr = fetch_pull_request(request.repository, request.pr_number, token)

        # Fetch repo config to determine backend type
        # Use PR's base branch ref for config lookup
        base_ref = pr.base.get("ref", "main")
        repo_config = fetch_repo_config(request.repository, token, ref=base_ref)
        backend = get_backend_from_config(repo_config)
        logger.info(f"Detected backend type: {backend}")

        # Fetch PR file changes
        files = fetch_pr_files(request.repository, request.pr_number, token)

        # Parse changed files and determine checks based on backend
        parse_result = parse_changed_files(files, backend=backend)
        changed_files = parse_result["changed_files"]
        checks_to_run = parse_result["checks_to_run"]

        logger.info(f"Changed files: {len(changed_files)}, Checks to run: {len(checks_to_run)}")

        # Compute the structural-impact block and post it as a PR
        # comment. This is best-effort -- we wrap the whole pass in
        # try/except so a graph failure can't break the existing
        # check-queueing pipeline (structural retrieval is additive
        # context, never blocking).
        try:
            base_sha = (pr.base or {}).get("sha") if isinstance(pr.base, dict) else None
            head_sha = (pr.head or {}).get("sha") if isinstance(pr.head, dict) else None
            # Workspace_id resolution from a repo is plumbed through
            # for forward compatibility (a future repo -> workspace
            # lookup will populate it). For now we honor an explicit
            # env var if set, else skip.
            workspace_id = os.environ.get("PR_WORKSPACE_ID", "") or None
            structural_block = _build_structural_impact_comment(
                repository=request.repository,
                pr=pr,
                files=files,
                token=token,
                workspace_id=workspace_id,
                base_sha=base_sha,
                head_sha=head_sha,
            )
            if structural_block:
                comment_body = (
                    structural_block
                    + "\n\n---\n*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"
                )
                post_pr_comment(
                    request.repository, request.pr_number, comment_body, token
                )
                logger.info(
                    "[analyze-pr.structural] posted structural-impact comment "
                    "for PR #%d", request.pr_number
                )
        except Exception as exc:
            # Non-fatal -- the structural pass is additive context.
            logger.warning(
                "[analyze-pr.structural] failed for PR #%d: %s",
                request.pr_number,
                exc,
            )

        # If no checks needed, post comment and return
        if not checks_to_run:
            comment_body = """**OutcomeOps Analysis:** No checks needed for this PR (no relevant files changed)

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"""
            post_pr_comment(request.repository, request.pr_number, comment_body, token)

            _emit_pr_analyzed(
                repository=request.repository,
                pr_number=request.pr_number,
                pr=pr,
                backend=backend,
                checks_run=[],
                changed_file_count=len(changed_files),
                duration_ms=int((time.time() - start_time) * 1000),
                no_checks_needed=True,
            )

            response = AnalyzePrResponse(
                message="No checks needed for this PR",
                pr_number=request.pr_number,
                checks_queued=0
            )
            return response.dict()

        # Post initial comment to PR
        checks_list = "\n".join([f"- {check.replace('_', ' ')}" for check in checks_to_run])
        comment_body = f"""**OutcomeOps Analysis Started**

Running {len(checks_to_run)} checks:
{checks_list}

Results will be posted as comments when complete.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"""

        post_pr_comment(request.repository, request.pr_number, comment_body, token)

        # Queue check jobs to SQS
        jobs_queued = queue_check_jobs(request.repository, request.pr_number, checks_to_run, changed_files)

        response = AnalyzePrResponse(
            message=f"Analysis started for PR #{request.pr_number}",
            pr_number=request.pr_number,
            checks_queued=jobs_queued
        )

        _emit_pr_analyzed(
            repository=request.repository,
            pr_number=request.pr_number,
            pr=pr,
            backend=backend,
            checks_run=checks_to_run,
            changed_file_count=len(changed_files),
            duration_ms=int((time.time() - start_time) * 1000),
            no_checks_needed=False,
        )

        logger.info(f"Successfully queued {jobs_queued} checks for PR #{request.pr_number}")
        return response.dict()

    except Exception as e:
        logger.error(f"Error analyzing PR: {str(e)}", exc_info=True)
        raise


def _build_structural_impact_comment(
    *,
    repository: str,
    pr: Any,
    files: List[GitHubFile],
    token: str,
    workspace_id: Optional[str],
    base_sha: Optional[str],
    head_sha: Optional[str],
) -> Optional[str]:
    """Compute the structural-impact block for a PR.

    Returns a Markdown string ready to post as a PR comment, or None
    when no block should be emitted (no mutated symbols resolved, or
    no workspace_id, or graph augmentation gated off).

    Graceful degradation: any exception in the symbol-graph reader
    is swallowed and we return ``build_unavailable_block`` so the
    human reviewer still sees that structural retrieval failed --
    the review otherwise proceeds. Structural section omitted;
    review still ships.

    We run the reader against the **base SHA** (the parent of the
    PR) so the pre-change graph anchors caller analysis. The
    reader's pointer aggregation already returns ``graph_sha`` /
    ``staleness_seconds``; the staleness phrase in the rendered
    footer is the disclaimer.
    """

    # Operator kill-switch: GRAPH_RETRIEVAL_ENABLED is the
    # platform-wide gate shared across chat / analyze-pr /
    # generate-code. When disabled, this pass is a no-op so a
    # rollout can be paused without redeploy. The legacy
    # ``GRAPH_AUGMENT_ENABLED`` value is honored as a fallback so
    # older deploys continue to work.
    _kill_switch = os.environ.get(
        "GRAPH_RETRIEVAL_ENABLED",
        os.environ.get("GRAPH_AUGMENT_ENABLED", "true"),
    )
    if _kill_switch.lower() != "true":
        logger.info("[analyze-pr.structural] augmentation disabled via env")
        return None

    symbol_graph_table = os.environ.get("SYMBOL_GRAPH_TABLE", "")
    code_maps_table = os.environ.get("CODE_MAPS_TABLE", "")
    if not symbol_graph_table:
        # IAM + env-var wiring lives in terraform/iam_symbol_graph.tf
        # + terraform/lambda-pr-analysis.tf. If we got here without
        # the env var, the structural pass simply doesn't run.
        logger.info(
            "[analyze-pr.structural] SYMBOL_GRAPH_TABLE not set; skipping"
        )
        return None
    if not workspace_id:
        # Per the symbol-graph reader contract: queries are
        # workspace-scoped. analyze-pr today doesn't know which
        # workspace owns a given repository -- the lookup is plumbed
        # through here for forward compatibility (a future repo ->
        # workspace resolution will fill it in) but we no-op when
        # it's empty.
        logger.info(
            "[analyze-pr.structural] no workspace_id resolved; skipping"
        )
        return None

    # Lazy import so module-load of analyze-pr's handler.py doesn't
    # require the extractor module to be on sys.path (live Lambda zip
    # doesn't include it today; tests + future zip layouts will).
    try:
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
            build_unavailable_block,
        )
    except Exception as exc:  # pragma: no cover - import path
        logger.warning(
            "[analyze-pr.structural] module import failed (%s); skipping",
            exc,
        )
        return None

    if not head_sha:
        # Fall back to "main" if the PR's head SHA isn't available --
        # the extractor doesn't care about ref correctness for the
        # symbol-resolution step, only for picking up the post-diff
        # content. Real PRs always have a head SHA.
        head_sha = "HEAD"

    # 1. Build FilePatch list: filename + patch + post-diff bytes.
    file_patches: List[FilePatch] = []
    for f in files:
        if f.status == "removed":
            file_patches.append(
                FilePatch(
                    file_path=f.filename,
                    status=f.status,
                    patch=f.patch,
                    post_diff_source=None,
                )
            )
            continue
        if not f.patch:
            # Binary diff or omitted patch -- still record so the
            # resolver can log/skip. post_diff_source stays None to
            # short-circuit.
            file_patches.append(
                FilePatch(
                    file_path=f.filename,
                    status=f.status,
                    patch=None,
                    post_diff_source=None,
                )
            )
            continue
        post = fetch_file_contents(repository, f.filename, head_sha, token)
        file_patches.append(
            FilePatch(
                file_path=f.filename,
                status=f.status,
                patch=f.patch,
                post_diff_source=post,
            )
        )

    # 2. Resolve canonical symbol IDs from the diff.
    mutated = resolve_targets(file_patches)
    if not mutated:
        logger.info(
            "[analyze-pr.structural] no mutated symbols resolved; "
            "skipping structural block"
        )
        return None

    logger.info(
        "[analyze-pr.structural] %d mutated symbol(s) resolved", len(mutated)
    )

    # 3. Query the graph. Resilient -- per-symbol errors get surfaced
    # in the block, not raised.
    impacts = analyze_structural_impact(
        workspace_id=workspace_id,
        mutated=mutated,
        symbol_graph_table_name=symbol_graph_table,
        code_maps_table_name=code_maps_table or None,
    )
    if not impacts:
        # Reader raised at module-import time, or the workspace_id
        # / mutated list mismatched. Render an unavailable block so
        # the reviewer knows the structural pass was attempted but
        # couldn't complete.
        return build_unavailable_block(base_sha=base_sha)

    return build_impact_block(impacts, base_sha=base_sha)


def _emit_pr_analyzed(
    repository: str,
    pr_number: int,
    pr: Any,
    backend: str,
    checks_run: List[str],
    changed_file_count: int,
    duration_ms: int,
    no_checks_needed: bool,
) -> None:
    """Emit ACTION_PR_ANALYZED audit row. Non-fatal.

    This is the parent event that ties together the per-check Bedrock
    rows that process-pr-check writes. Carries enough context that an
    auditor can answer "every PR your AI reviewed against this repo
    in Q3" with a single Query.
    """
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return
    try:
        log_admin_action(
            audit_table,
            ACTION_PR_ANALYZED,
            workspace_id="",  # PR analysis is repo-scoped, not workspace-scoped
            user_email="",     # Webhook-driven, no human caller
            source_ip="",
            user_agent="",
            details={
                "repo": repository,
                "pr_number": pr_number,
                "pr_title": getattr(pr, "title", ""),
                "head_sha": (getattr(pr, "head", {}) or {}).get("sha", ""),
                "base_sha": (getattr(pr, "base", {}) or {}).get("sha", ""),
                "backend": backend,
                "checks_run": list(checks_run),
                "checks_queued_count": len(checks_run),
                "changed_file_count": changed_file_count,
                "no_checks_needed": no_checks_needed,
                "duration_ms": duration_ms,
            },
        )
    except Exception as e:
        logger.warning(f"Failed to emit PR_ANALYZED: {e}")
