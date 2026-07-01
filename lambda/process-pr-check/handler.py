"""
Process PR Check Lambda Handler (Worker).

Triggered by SQS messages from pr-checks-queue.
Routes to appropriate check handler based on checkType:
- ADR_COMPLIANCE: Check Lambda handlers and Terraform against ADR standards
- README_FRESHNESS: Check if README needs updating based on changes
- TEST_COVERAGE: Check if new handlers have corresponding tests
- BREAKING_CHANGES: Detect dependencies and potential breaking changes
- ARCHITECTURAL_DUPLICATION: Identify similar functionality across repos

Stores results in DynamoDB and posts comments to PR.
"""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from enum import Enum

import boto3
import requests
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

# shared/ is bundled into the deploy zip at the package root by
# terraform source_path (see lambda-pr-analysis.tf).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import dual_write  # noqa: E402

# Import check handlers
from check_handlers import (
    check_adr_compliance,
    check_architectural_duplication,
    check_breaking_changes,
    check_license_compliance,
    check_readme_freshness,
    check_test_coverage,
    UsageTracker,
)

# chunk 07: Azure DevOps PR-comment posting via the customer's
# connection. The modules are copies (per the sharepoint-sync /
# azure-devops-sync precedent of bundling integration modules into
# the consumer). The handler stays GitHub-default; the ADO branch
# only kicks in when CheckJob carries provider="azure_devops".
try:
    from azure_devops_token_manager import refresh_access_token as _ado_refresh_access_token  # noqa: E402
    from azure_devops_api import (  # noqa: E402
        AzureDevOpsAPIError,
        post_pull_request_thread,
    )
    _ADO_AVAILABLE = True
except ImportError as _e:  # pragma: no cover - deploy-bug path
    logging.warning(f"Azure DevOps support not available: {_e}")
    _ADO_AVAILABLE = False

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
ssm_client = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", f"{ENVIRONMENT}-{APP_NAME}-code-maps")
QUERY_KB_LAMBDA_NAME = os.environ.get("QUERY_KB_LAMBDA_NAME", f"{ENVIRONMENT}-{APP_NAME}-query-kb")

# chunk 07: ADO PR-comment posting needs the customer's per-connection
# OAuth access token. tenant_id + client_id come from tfvars (same as
# the integration Lambda); client secret is read from SSM at request
# time.
AZURE_DEVOPS_TENANT_ID = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
AZURE_DEVOPS_CLIENT_ID = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", ""
)
_ado_client_secret: Optional[str] = None


def _get_ado_client_secret() -> str:
    """One-shot SSM read of the ADO Entra ID app client secret.
    Mirrors the integration Lambda's helper -- operator owns the
    SecureString VALUE; terraform only knows the NAME."""
    global _ado_client_secret
    if _ado_client_secret is not None:
        return _ado_client_secret
    if not AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM:
        raise RuntimeError("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM not configured")
    response = ssm_client.get_parameter(
        Name=AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM, WithDecryption=True,
    )
    _ado_client_secret = response["Parameter"]["Value"]
    return _ado_client_secret


class CheckType(str, Enum):
    """Types of checks that can be performed on a PR."""
    ADR_COMPLIANCE = "ADR_COMPLIANCE"
    README_FRESHNESS = "README_FRESHNESS"
    TEST_COVERAGE = "TEST_COVERAGE"
    BREAKING_CHANGES = "BREAKING_CHANGES"
    ARCHITECTURAL_DUPLICATION = "ARCHITECTURAL_DUPLICATION"
    LICENSE_COMPLIANCE = "LICENSE_COMPLIANCE"


class CheckStatus(str, Enum):
    """Status of a check result."""
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class CheckJob(BaseModel):
    """Check job schema (from SQS message).

    chunk 07: ``provider`` defaults to ``"github"`` so existing
    GitHub-only PR-check messages continue to work unchanged. When
    ``provider == "azure_devops"``, the comment-post path uses ADO
    REST + the customer's connection-id-driven OAuth token. The
    underlying check execution (ADR_COMPLIANCE, README_FRESHNESS,
    etc.) is still GitHub-only in this release; ADO PR-check jobs
    short-circuit to a stub PASS so the customer's pipeline gets
    a successful round-trip + a transparent "not yet implemented
    for ADO" comment instead of a silent failure. Surface checks
    against ADO source require a separate provider-aware refactor
    of the check_handlers/ subpackage.
    """
    check_type: CheckType = Field(..., alias="checkType")
    pr_number: int = Field(..., alias="pr_number", gt=0)
    repository: str
    changed_files: List[str] = Field(..., alias="changedFiles")

    # ADO-specific fields. All optional so GitHub-shape messages
    # continue to validate.
    provider: Literal["github", "azure_devops"] = "github"
    connection_id: Optional[str] = None
    azure_devops_slug: Optional[str] = Field(None, alias="slug")
    azure_devops_org: Optional[str] = Field(None, alias="org")
    azure_devops_project: Optional[str] = Field(None, alias="project")
    azure_devops_repo: Optional[str] = Field(None, alias="repo")

    class Config:
        populate_by_name = True


class CheckResult(BaseModel):
    """Check result schema (stored in DynamoDB).

    PK shape: ``PR#{provider}#{repo}#{pr_number}`` — self-contained per
    (provider, repo, PR). Pre-widening the key was ``PR#{pr_number}``,
    which collided across repos with matching PR numbers; safe with
    display-only reads, actively broken with the update-in-place UX
    that reads the stored comment identifiers.
    """
    PK: str
    SK: str
    check_type: CheckType = Field(..., alias="checkType")
    status: CheckStatus
    message: str
    details: List[str]
    timestamp: str
    comment_url: str | None = Field(None, alias="commentUrl")

    # Provider-specific comment identifiers used by the update-in-place
    # PR-check UX. Populated after the initial POST; read on subsequent
    # check-runs to target a PATCH instead of another POST.
    #
    # - GitHub: only ``provider_comment_id`` (the issue-comment ID from
    #   POST /repos/{r}/issues/{n}/comments -> {"id": ...}).
    # - Azure DevOps: ``provider_thread_id`` (the thread's ID from
    #   POST .../threads -> {"id": ...}) AND
    #   ``provider_thread_comment_id`` (the first comment's ID within
    #   the thread; ADO thread POST returns
    #   ``{"comments": [{"id": ..., ...}], "id": ...}``).
    #
    # All three optional so existing rows (pre-update-in-place) round-
    # trip cleanly and are treated as "no existing comment -> POST" by
    # the upsert path.
    provider_comment_id: int | None = Field(None, alias="providerCommentId")
    provider_thread_id: int | None = Field(None, alias="providerThreadId")
    provider_thread_comment_id: int | None = Field(None, alias="providerThreadCommentId")

    class Config:
        populate_by_name = True


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


def post_pr_comment(repository: str, pr_number: int, body: str, token: str) -> Dict[str, Any]:
    """
    Post a comment to the PR via GitHub API.

    Args:
        repository: Repository in format "owner/repo"
        pr_number: Pull Request number
        body: Comment text
        token: GitHub personal access token

    Returns:
        API response dict with comment metadata

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


def patch_pr_comment(
    repository: str,
    comment_id: int,
    body: str,
    token: str,
) -> Dict[str, Any]:
    """
    Update an existing PR/issue comment via GitHub REST.

    Args:
        repository: Repository in format "owner/repo"
        comment_id: The comment's numeric ID (from a prior POST response's ``id``)
        body: New comment body
        token: GitHub personal access token

    Returns:
        API response dict with the updated comment metadata

    Raises:
        Exception: If API request fails
    """
    url = f"https://api.github.com/repos/{repository}/issues/comments/{comment_id}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"
    }

    payload = {"body": body}

    try:
        response = requests.patch(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        logger.info(f"Updated comment {comment_id} in {repository}")
        return response.json()

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to update comment {comment_id} in {repository}: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def _post_azure_devops_thread(
    org: str,
    project: str,
    repo: str,
    pull_request_id: int,
    body: str,
    connection_id: str,
) -> Dict[str, Any]:
    """Post a comment to an Azure DevOps PR as a top-level thread.

    Uses ``azure_devops_token_manager.refresh_access_token`` to mint
    a fresh access token from the customer's stored OAuth refresh
    token. Then calls ``azure_devops_api.post_pull_request_thread``
    (raw urllib; no SDK -- same as the integration Lambda).

    Returns ADO's thread object on success. Raises on any failure;
    the caller's batch-item-failure path retries via SQS.
    """
    if not _ADO_AVAILABLE:
        raise RuntimeError(
            "Azure DevOps modules not bundled into process-pr-check; "
            "deploy bug"
        )
    if not connection_id:
        raise ValueError("connection_id required for Azure DevOps PR-check")
    if not AZURE_DEVOPS_TENANT_ID or not AZURE_DEVOPS_CLIENT_ID:
        raise RuntimeError(
            "Azure DevOps OAuth app not configured (AZURE_DEVOPS_TENANT_ID / "
            "AZURE_DEVOPS_CLIENT_ID env vars missing)"
        )

    access_token = _ado_refresh_access_token(
        connection_id=connection_id,
        tenant_id=AZURE_DEVOPS_TENANT_ID,
        client_id=AZURE_DEVOPS_CLIENT_ID,
        client_secret=_get_ado_client_secret(),
    )
    if not access_token:
        raise RuntimeError(
            "Failed to refresh Azure DevOps access token; reconnect_required"
        )

    try:
        response = post_pull_request_thread(
            access_token=access_token,
            org=org,
            project=project,
            repo=repo,
            pull_request_id=pull_request_id,
            comment_markdown=body,
        )
        logger.info(
            "Posted ADO PR thread on %s/%s/%s PR #%s",
            org, project, repo, pull_request_id,
        )
        return response
    except AzureDevOpsAPIError as e:
        logger.error(f"Failed to post ADO PR thread: {e}")
        raise


def store_check_result(result: CheckResult) -> None:
    """
    Store check result in DynamoDB.

    Args:
        result: CheckResult object to store

    Raises:
        ClientError: If DynamoDB operation fails
    """
    item = {
        "PK": result.PK,
        "SK": result.SK,
        "checkType": result.check_type.value,
        "status": result.status.value,
        "message": result.message,
        "details": result.details,
        "timestamp": result.timestamp,
    }

    if result.comment_url:
        item["commentUrl"] = result.comment_url
    if result.provider_comment_id is not None:
        item["providerCommentId"] = result.provider_comment_id
    if result.provider_thread_id is not None:
        item["providerThreadId"] = result.provider_thread_id
    if result.provider_thread_comment_id is not None:
        item["providerThreadCommentId"] = result.provider_thread_comment_id

    try:
        dual_write.ddb_put_item(DYNAMODB_TABLE, item)
        logger.info(f"Stored check result: {result.PK} / {result.SK}")

    except ClientError as e:
        logger.error(f"Failed to store check result in DynamoDB: {e}")
        raise


def run_check(job: CheckJob) -> Dict[str, Any]:
    """
    Route to appropriate check handler based on check type.

    Provider-aware: forwards ``provider`` + ADO context fields from the
    CheckJob to each handler. Today every handler still raises
    ``NotImplementedError`` for non-github providers (see the widened
    signatures in ``check_handlers/*.py``), so callers reaching this
    function with an ADO job hit a loud failure. The
    ``process_check_job`` short-circuit above prevents that from
    happening in production until chunk 05 removes it.

    Args:
        job: CheckJob with check type and PR metadata

    Returns:
        Check result dict (without PK/SK/timestamp/commentUrl)

    Raises:
        ValueError: If check type is unknown
    """
    github_token_param = f"/{ENVIRONMENT}/{APP_NAME}/github/token"

    provider_kwargs = {
        "provider": job.provider,
        "connection_id": job.connection_id,
        "ado_org": job.azure_devops_org,
        "ado_project": job.azure_devops_project,
        "ado_repo": job.azure_devops_repo,
    }

    if job.check_type == CheckType.ADR_COMPLIANCE:
        return check_adr_compliance(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            github_token_param=github_token_param,
            **provider_kwargs,
        )

    elif job.check_type == CheckType.README_FRESHNESS:
        return check_readme_freshness(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            github_token_param=github_token_param,
            **provider_kwargs,
        )

    elif job.check_type == CheckType.TEST_COVERAGE:
        return check_test_coverage(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            github_token_param=github_token_param,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            **provider_kwargs,
        )

    elif job.check_type == CheckType.BREAKING_CHANGES:
        return check_breaking_changes(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            **provider_kwargs,
        )

    elif job.check_type == CheckType.ARCHITECTURAL_DUPLICATION:
        return check_architectural_duplication(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            github_token_param=github_token_param,
            **provider_kwargs,
        )

    elif job.check_type == CheckType.LICENSE_COMPLIANCE:
        return check_license_compliance(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            github_token_param=github_token_param,
            **provider_kwargs,
        )

    else:
        raise ValueError(f"Unknown check type: {job.check_type}")


def format_check_comment(result: CheckResult, usage: UsageTracker | None = None) -> str:
    """
    Format check result as GitHub PR comment.

    Args:
        result: CheckResult to format
        usage: Optional UsageTracker with API usage metrics

    Returns:
        Markdown-formatted comment string
    """
    status_emoji = {
        CheckStatus.PASS: ":white_check_mark:",
        CheckStatus.WARN: ":warning:",
        CheckStatus.FAIL: ":x:",
    }[result.status]

    check_name = result.check_type.value.replace("_", " ")

    comment = f"{status_emoji} **{check_name}**: {result.message}\n"

    if result.details:
        comment += "\n**Details:**\n"
        for detail in result.details:
            comment += f"{detail}\n"

    # Add usage info if available
    if usage and usage.total_tokens > 0:
        comment += f"\n**Usage:**\n"
        comment += f"Model: {usage.primary_model}\n"
        comment += f"Tokens: {usage.total_input_tokens:,} input + {usage.total_output_tokens:,} output = {usage.total_tokens:,} total\n"
        comment += f"Cost: ${usage.total_cost:.4f}\n"

    comment += f"\n_Check completed at {result.timestamp}_"
    comment += "\n\n---\n*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"

    return comment


def process_check_job(job: CheckJob) -> CheckResult:
    """
    Main processing logic for a PR check job.

    Args:
        job: CheckJob from SQS message

    Returns:
        CheckResult with full metadata

    Raises:
        Exception: If check execution or storage fails
    """
    logger.info(
        "Processing check job: %s for PR #%s (provider=%s)",
        job.check_type.value, job.pr_number, job.provider,
    )

    # All 6 check handlers accept ``provider=azure_devops`` and dispatch
    # source-fetching accordingly (chunks 03-04 of the AI-PR-Analyzer
    # ADO parity plan). The chunk-07-era short-circuit that stubbed
    # ADO to a PASS with a "not yet implemented" comment was removed
    # in chunk 05; ADO jobs now run the same check pipeline GitHub
    # jobs do.
    check_result = run_check(job)
    usage = check_result.get("usage")

    # Build full result. PK is self-contained per (provider, repo, PR)
    # so rows don't collide across repos with the same PR number.
    pk = f"PR#{job.provider}#{job.repository}#{job.pr_number}"
    sk = f"CHECK#{job.check_type.value.lower()}"
    timestamp = datetime.utcnow().isoformat() + "Z"
    result = CheckResult(
        PK=pk,
        SK=sk,
        checkType=job.check_type,
        status=CheckStatus(check_result["status"]),
        message=check_result["message"],
        details=check_result["details"],
        timestamp=timestamp,
    )

    # Store result in DynamoDB
    store_check_result(result)

    # Post result as a PR comment via the right provider. Extract the
    # provider-side comment identifiers so subsequent runs on the same
    # PR can PATCH-in-place instead of POSTing duplicates (chunk 03).
    comment = format_check_comment(result, usage)
    if job.provider == "azure_devops":
        comment_response = _post_azure_devops_thread(
            org=job.azure_devops_org,
            project=job.azure_devops_project,
            repo=job.azure_devops_repo,
            pull_request_id=job.pr_number,
            body=comment,
            connection_id=job.connection_id,
        )
        # ADO thread response shape:
        #   {"id": <thread_id>, "comments": [{"id": <cid>, ...}], ...}
        thread_id = comment_response.get("id")
        comments = comment_response.get("comments") or []
        thread_comment_id = comments[0].get("id") if comments else None
        result.provider_thread_id = thread_id
        result.provider_thread_comment_id = thread_comment_id
        comment_url = (
            f"https://dev.azure.com/{job.azure_devops_org}/{job.azure_devops_project}"
            f"/_git/{job.azure_devops_repo}/pullrequest/{job.pr_number}"
            f"?_a=overview&discussionId={thread_id}"
            if thread_id else ""
        )
    else:
        token = get_github_token()
        comment_response = post_pr_comment(job.repository, job.pr_number, comment, token)
        comment_url = comment_response.get("html_url", "")
        result.provider_comment_id = comment_response.get("id")

    # Update result with comment URL
    result.comment_url = comment_url

    # Update DynamoDB with comment URL + provider comment identifiers
    store_check_result(result)

    logger.info(f"Check complete: {result.status.value} - {result.message}")
    return result


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Process PR Check Lambda Handler (Worker).

    Triggered by SQS messages from pr-checks-queue.
    Routes to appropriate check handler based on checkType.

    Args:
        event: SQS event with Records
        context: Lambda context (unused)

    Returns:
        Response dict (SQS partial batch failures if any)

    Raises:
        Exception: For processing errors (triggers SQS retry/DLQ)
    """
    logger.info(f"Processing {len(event.get('Records', []))} SQS message(s)")

    batch_item_failures = []

    # Process each message
    for record in event.get("Records", []):
        try:
            # Parse and validate job from SQS message
            message_body = json.loads(record["body"])
            job = CheckJob(**message_body)

            logger.info(f"Processing job: {job.check_type.value} for PR #{job.pr_number}")

            # Process the check job
            process_check_job(job)

            logger.info(f"Successfully processed job: {job.check_type.value} for PR #{job.pr_number}")

        except Exception as error:
            logger.error(f"Error processing SQS record: {error}", exc_info=True)

            # Add to batch failures for SQS to retry
            batch_item_failures.append({
                "itemIdentifier": record["messageId"]
            })

    # Return batch item failures for SQS partial batch failure handling
    if batch_item_failures:
        logger.warning(f"Failed to process {len(batch_item_failures)} message(s)")
        return {
            "batchItemFailures": batch_item_failures
        }

    return {
        "batchItemFailures": []
    }
