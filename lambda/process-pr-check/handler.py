# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Set, Tuple
from enum import Enum

import boto3
import requests
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

# shared/ is bundled into the deploy zip at the package root by
# terraform source_path (see lambda-pr-analysis.tf).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import dual_write  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from shared.pr_diff import ChangeBlock, FileDiffInfo  # noqa: F401

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

# Azure DevOps PR-comment posting via the customer's
# connection. The modules are copies (per the sharepoint-sync /
# azure-devops-sync precedent of bundling integration modules into
# the consumer). The handler stays GitHub-default; the ADO branch
# only kicks in when CheckJob carries provider="azure_devops".
try:
    from shared.oauth.azure_devops import refresh_access_token as _ado_refresh_access_token  # noqa: E402
    from shared.azure_devops_api import (  # noqa: E402
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

# ADR-057 deferred-ADO-summary coordination. analyze-pr persists the rendered
# summary under SK=SUMMARY_DEFERRED (ADO orders threads by creation, so an
# up-front summary sinks below the per-check threads); the completion fan-in
# here posts it as the newest thread once every check finishes, so it lands on
# top. Both constants MUST match analyze-pr/handler.py.
DEFERRED_SUMMARY_SK = "SUMMARY_DEFERRED"
COMMENT_ROLE_SUMMARY = "summary"

# ADO PR-comment posting needs the customer's per-connection
# OAuth access token. tenant_id + client_id come from tfvars (same as
# the integration Lambda); client secret is read from SSM at request
# time.
AZURE_DEVOPS_TENANT_ID = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
AZURE_DEVOPS_CLIENT_ID = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", ""
)
_ado_client_secret: Optional[str] = None


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    keeps module load order safe."""
    from shared.github_base import github_api_base
    return github_api_base()


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

    ``provider`` defaults to ``"github"`` so existing
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

    # Provider context. All optional so GitHub-shape messages continue
    # to validate. GitLab needs only ``connection_id`` -- ``repository``
    # IS the full project path (variable-depth slug).
    provider: Literal["github", "azure_devops", "gitlab"] = "github"
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
    url = f"{_github_api()}/repos/{repository}/issues/{pr_number}/comments"

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
    url = f"{_github_api()}/repos/{repository}/issues/comments/{comment_id}"

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


def post_pr_review_comment(
    repository: str,
    pr_number: int,
    body: str,
    token: str,
    *,
    commit_id: str,
    path: str,
    line: int,
    side: str = "RIGHT",
) -> Dict[str, Any]:
    """
    Post a line-level review comment on a PR diff (GitHub pulls API).

    Unlike post_pr_comment (issue-level), this anchors the comment to a specific
    file+line via the pull-request review-comments endpoint. Callers store the
    returned ``id`` to target patch_pr_review_comment on later runs (the inline
    upsert bookkeeping; see plan ).

    Args:
        commit_id: PR head SHA the line is anchored to (from pr_metadata).
        path: Repo-relative file path.
        line: 1-based line number on the post-change (RIGHT) side.
        side: "RIGHT" (added/changed) or "LEFT" (removed); default RIGHT.
    """
    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    payload = {
        "body": body,
        "commit_id": commit_id,
        "path": path,
        "line": line,
        "side": side,
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        logger.info(
            f"Posted review comment on {path}:{line} in PR #{pr_number} ({repository})"
        )
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to post review comment to GitHub: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def patch_pr_review_comment(
    repository: str,
    comment_id: int,
    body: str,
    token: str,
) -> Dict[str, Any]:
    """Update an existing line-level PR review comment in place (upsert UX).

    Endpoint differs from patch_pr_comment: review comments live under
    ``/pulls/comments/{id}``, not ``/issues/comments/{id}``.
    """
    url = f"{_github_api()}/repos/{repository}/pulls/comments/{comment_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    payload = {"body": body}
    try:
        response = requests.patch(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        logger.info(f"Updated review comment {comment_id} in {repository}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(
            f"Failed to update review comment {comment_id} in {repository}: {e}"
        )
        raise Exception(f"GitHub API error: {str(e)}")


def delete_pr_review_comment(
    repository: str,
    comment_id: int,
    token: str,
) -> None:
    """Delete a line-level PR review comment (stale inline-finding cleanup).

    Used when a prior inline finding no longer applies (line removed or issue
    fixed) so a fixed line doesn't keep an orphaned comment.
    """
    url = f"{_github_api()}/repos/{repository}/pulls/comments/{comment_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.delete(url, headers=headers, timeout=30)
        response.raise_for_status()
        logger.info(f"Deleted review comment {comment_id} in {repository}")
    except requests.exceptions.RequestException as e:
        logger.error(
            f"Failed to delete review comment {comment_id} in {repository}: {e}"
        )
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


def _get_existing_check_result_row(pk: str, sk: str) -> Optional[Dict[str, Any]]:
    """Read the existing DDB row for this (PR × check_type), or return None.

    Used by ``process_check_job``'s upsert path: if we've posted a
    comment on a previous run, its provider comment identifiers live
    on this row. The upsert path uses them to PATCH instead of POST.
    """
    try:
        table = dynamodb.Table(DYNAMODB_TABLE)
        response = table.get_item(Key={"PK": pk, "SK": sk})
        return response.get("Item")
    except ClientError as e:
        # Non-fatal -- if the read fails we just POST a new comment,
        # matching the pre-upsert behavior.
        logger.warning(
            f"Failed to read existing check result at {pk}/{sk}: {e}. "
            f"Falling back to POST."
        )
        return None


def _try_patch_github_comment(
    repository: str,
    existing_row: Dict[str, Any],
    comment_body: str,
) -> Optional[str]:
    """Try to PATCH the existing GitHub comment. Returns the comment's
    html_url on success; ``None`` if there's no stored comment ID or
    the PATCH failed (caller falls back to POST)."""
    comment_id = existing_row.get("providerCommentId")
    if comment_id is None:
        return None
    try:
        token = get_github_token()
        response = patch_pr_comment(repository, int(comment_id), comment_body, token)
        return response.get("html_url", "")
    except Exception as e:
        logger.warning(
            f"[upsert] PATCH failed for GitHub comment {comment_id} in "
            f"{repository}: {e}. Falling back to POST."
        )
        return None


def _try_patch_ado_comment(
    *,
    org: str,
    project: str,
    repo: str,
    pull_request_id: int,
    connection_id: str,
    existing_row: Dict[str, Any],
    comment_body: str,
) -> Optional[str]:
    """Try to PATCH the existing ADO thread comment. Returns the
    human-followable URL on success; ``None`` if there's no stored
    thread/comment ID or the PATCH failed."""
    if not _ADO_AVAILABLE:
        return None
    thread_id = existing_row.get("providerThreadId")
    thread_comment_id = existing_row.get("providerThreadCommentId")
    if thread_id is None or thread_comment_id is None:
        return None
    try:
        from shared.azure_devops_api import update_pull_request_thread_comment
        access_token = _ado_refresh_access_token(
            connection_id=connection_id,
            tenant_id=AZURE_DEVOPS_TENANT_ID,
            client_id=AZURE_DEVOPS_CLIENT_ID,
            client_secret=_get_ado_client_secret(),
        )
        if not access_token:
            return None
        update_pull_request_thread_comment(
            access_token=access_token,
            org=org, project=project, repo=repo,
            pull_request_id=pull_request_id,
            thread_id=int(thread_id),
            comment_id=int(thread_comment_id),
            comment_markdown=comment_body,
        )
        return (
            f"https://dev.azure.com/{org}/{project}/_git/{repo}"
            f"/pullrequest/{pull_request_id}"
            f"?_a=overview&discussionId={thread_id}"
        )
    except Exception as e:
        logger.warning(
            f"[upsert] ADO PATCH failed for thread {thread_id}/comment "
            f"{thread_comment_id} in {org}/{project}/{repo} PR "
            f"#{pull_request_id}: {e}. Falling back to POST."
        )
        return None


def _try_delete_ado_comment(
    *,
    org: str,
    project: str,
    repo: str,
    pull_request_id: int,
    connection_id: str,
    existing_row: Dict[str, Any],
) -> bool:
    """Best-effort delete of a previously posted ADO thread comment.

    Deleting a thread's sole comment removes the thread. Returns ``True`` on a
    successful delete, ``False`` when there is nothing to delete or the call
    fails -- the caller POSTs a fresh thread regardless, since a transient
    duplicate is a cosmetic nuisance while a missing summary is worse.
    """
    if not _ADO_AVAILABLE:
        return False
    thread_id = existing_row.get("providerThreadId")
    thread_comment_id = existing_row.get("providerThreadCommentId")
    if thread_id is None or thread_comment_id is None:
        return False
    try:
        from shared.azure_devops_api import delete_pull_request_thread_comment
        access_token = _ado_refresh_access_token(
            connection_id=connection_id,
            tenant_id=AZURE_DEVOPS_TENANT_ID,
            client_id=AZURE_DEVOPS_CLIENT_ID,
            client_secret=_get_ado_client_secret(),
        )
        if not access_token:
            return False
        delete_pull_request_thread_comment(
            access_token=access_token,
            org=org, project=project, repo=repo,
            pull_request_id=pull_request_id,
            thread_id=int(thread_id),
            comment_id=int(thread_comment_id),
        )
        return True
    except Exception as e:
        logger.warning(
            f"[pr-summary.ado] delete of prior summary thread {thread_id}/"
            f"comment {thread_comment_id} in {org}/{project}/{repo} PR "
            f"#{pull_request_id} failed: {e}. Posting a fresh thread anyway."
        )
        return False


def _ado_mark_done_and_maybe_post_summary(job: CheckJob) -> None:
    """Mark this ADO check complete; post the deferred summary if it was last.

    analyze-pr persists the ADO summary under SK=SUMMARY_DEFERRED instead of
    posting it up front (ADO orders threads by creation, so an up-front summary
    sinks below the per-check threads). Each check idempotently adds itself to
    ``completedChecks`` (a String Set -- safe under SQS at-least-once); the check
    that completes the expected set claims the post exactly once (a conditional
    write) and posts the summary as the newest thread, so it lands on top
    (ADR-057). A no-op when no summary was deferred for this PR.
    """
    pk = f"PR#{job.provider}#{job.repository}#{job.pr_number}"
    deferred = _get_existing_check_result_row(pk, DEFERRED_SUMMARY_SK)
    if not deferred or not deferred.get("summaryBody"):
        return  # summary not deferred for this PR (nothing to do)
    expected = set(deferred.get("expectedChecks") or [])
    if not expected:
        return

    table = dynamodb.Table(DYNAMODB_TABLE)
    # Idempotent: ADDing an element already in the set is a no-op, so an SQS
    # redelivery of the same check cannot over- or under-count completion.
    updated = table.update_item(
        Key={"PK": pk, "SK": DEFERRED_SUMMARY_SK},
        UpdateExpression="ADD completedChecks :c",
        ExpressionAttributeValues={":c": {job.check_type.value}},
        ReturnValues="ALL_NEW",
    ).get("Attributes", {})
    completed = set(updated.get("completedChecks") or [])
    if not expected.issubset(completed):
        return  # more checks still pending
    if updated.get("summaryPosted"):
        return  # already posted on a prior completion

    # Claim the post exactly once -- guards the race where the last two checks
    # complete concurrently and both observe the full set.
    try:
        table.update_item(
            Key={"PK": pk, "SK": DEFERRED_SUMMARY_SK},
            UpdateExpression="SET summaryPosted = :t",
            ConditionExpression="attribute_not_exists(summaryPosted)",
            ExpressionAttributeValues={":t": True},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return  # another worker won the claim
        raise

    _ado_upsert_summary(job, deferred["summaryBody"])
    logger.info(
        "[pr-summary.ado] posted deferred summary as newest thread for PR #%s",
        job.pr_number,
    )


def _ado_upsert_summary(job: CheckJob, body: str) -> None:
    """Post the ADO summary as a fresh thread, deleting any prior one.

    ADO orders PR Overview threads by creation time (``publishedDate``), newest
    at the top, and a PATCH does *not* change a thread's ``publishedDate``. So
    editing a summary thread in place leaves it wherever it was first created --
    which, after the per-check threads have posted, is at the bottom. To keep the
    summary on top on every run (first analysis and every re-push), delete the
    previously posted summary comment (its sole comment removes the thread) and
    POST a brand-new thread, whose fresh ``publishedDate`` is the newest at
    completion time (ADR-057).
    """
    pk = f"PR#{job.provider}#{job.repository}#{job.pr_number}"
    sk = f"COMMENT#{COMMENT_ROLE_SUMMARY}"
    org, project, repo = (
        job.azure_devops_org, job.azure_devops_project, job.azure_devops_repo,
    )
    existing = _get_existing_check_result_row(pk, sk)
    if existing:
        _try_delete_ado_comment(
            org=org, project=project, repo=repo,
            pull_request_id=job.pr_number, connection_id=job.connection_id,
            existing_row=existing,
        )
    resp = _post_azure_devops_thread(
        org, project, repo, job.pr_number, body, job.connection_id,
    )
    thread_id = resp.get("id")
    comments = resp.get("comments") or []
    comment_id = comments[0].get("id") if comments else None
    item: Dict[str, Any] = {
        "PK": pk,
        "SK": sk,
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if thread_id is not None:
        item["providerThreadId"] = int(thread_id)
    if comment_id is not None:
        item["providerThreadCommentId"] = int(comment_id)
    dual_write.ddb_put_item(DYNAMODB_TABLE, item)


def _gitlab_note_url(repository: str, pr_number: int, note_id,
                     base_url: str) -> str:
    """Human-followable URL for an MR note on the connection's instance."""
    return (
        f"{base_url}/{repository}/-/merge_requests/"
        f"{pr_number}#note_{note_id}"
    )


def _post_gitlab_note(
    repository: str,
    pr_number: int,
    body: str,
    connection_id: str,
) -> Dict[str, Any]:
    """Post a comment (note) on a GitLab MR.

    Token comes from the cached single-flight path (ADR-050 v1.1) --
    GitLab rotates refresh tokens on every refresh, so a parallel raw
    refresh here would race the sync workers. Raises on failure; the
    caller's batch-item-failure path retries via SQS.
    """
    if not connection_id:
        raise ValueError("connection_id required for GitLab PR-check")
    from check_handlers.gitlab_context import (
        get_gitlab_token, get_gitlab_base_url)
    from shared import gitlab_api

    access_token = get_gitlab_token(connection_id)
    response = gitlab_api.post_merge_request_note(
        access_token, repository, pr_number, body,
        base_url=get_gitlab_base_url(connection_id),
    )
    logger.info(
        "Posted GitLab MR note on %s !%s", repository, pr_number,
    )
    return response


def _try_patch_gitlab_comment(
    *,
    repository: str,
    pr_number: int,
    connection_id: str,
    existing_row: Dict[str, Any],
    comment_body: str,
) -> Optional[str]:
    """Try to update the existing GitLab MR note in place. Returns the
    note URL on success; ``None`` if there's no stored note ID or the
    update failed (caller falls back to POST)."""
    note_id = existing_row.get("providerCommentId")
    if note_id is None:
        return None
    try:
        from check_handlers.gitlab_context import (
            get_gitlab_token, get_gitlab_base_url)
        from shared import gitlab_api

        base = get_gitlab_base_url(connection_id)
        access_token = get_gitlab_token(connection_id)
        gitlab_api.update_merge_request_note(
            access_token, repository, pr_number, int(note_id), comment_body,
            base_url=base,
        )
        return _gitlab_note_url(repository, pr_number, note_id, base)
    except Exception as e:
        logger.warning(
            f"[upsert] GitLab note update failed for note {note_id} on "
            f"{repository} !{pr_number}: {e}. Falling back to POST."
        )
        return None


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


def run_check(
    job: CheckJob,
    anchors: Optional[Dict[str, List[int]]] = None,
    *,
    change_blocks: Optional[Dict[str, List["ChangeBlock"]]] = None,
    head_texts: Optional[Dict[str, str]] = None,
    new_files: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Route to appropriate check handler based on check type.

    Provider-aware: forwards ``provider`` + ADO context fields from the
    CheckJob to each handler. Today every handler still raises
    ``NotImplementedError`` for non-github providers (see the widened
    signatures in ``check_handlers/*.py``), so callers reaching this
    function with an ADO job hit a loud failure. The
    ``process_check_job`` short-circuit above prevents that from
    happening in production until removes it.

    Args:
        job: CheckJob with check type and PR metadata
        anchors: Added lines per file for checks that post inline findings
        change_blocks: Per-file ``pr_diff.ChangeBlock`` lists for
            BREAKING_CHANGES; ``None`` means the diff could not be computed
        head_texts: Decoded head content per changed file for BREAKING_CHANGES
            signature compare; ``None`` means the diff could not be computed
        new_files: Changed files added in this PR, for README_FRESHNESS;
            ``None`` means newness is unknown

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
            new_files=new_files,
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
            anchors=anchors,
            **provider_kwargs,
        )

    elif job.check_type == CheckType.BREAKING_CHANGES:
        return check_breaking_changes(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            anchors=anchors,
            change_blocks=change_blocks,
            head_texts=head_texts,
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
            anchors=anchors,
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
    Format check result as a PR comment.

    The comment is wrapped in an HTML ``<details>`` block so reviewers
    see just a one-line summary (status glyph + check name + message)
    per check, and expand only when they want the full detail. Both
    GitHub and Azure DevOps render ``<details>`` / ``<summary>`` in
    their PR-comment markdown. The blank line after ``</summary>`` is
    required so markdown inside the block renders (a well-known
    GitHub-flavored markdown quirk that ADO shares).

    Args:
        result: CheckResult to format
        usage: Optional UsageTracker with API usage metrics

    Returns:
        Markdown-formatted comment string
    """
    # Raw Unicode emoji instead of GitHub markdown shortcodes
    # (:white_check_mark: / :warning: / :x:). GitHub renders the
    # shortcodes; Azure DevOps does NOT and shows them as literal
    # text. Unicode renders identically on both providers.
    status_emoji = {
        CheckStatus.PASS: "✅",       # ✅
        CheckStatus.WARN: "⚠️", # ⚠️ (warning sign + emoji variation)
        CheckStatus.FAIL: "❌",       # ❌
    }[result.status]

    check_name = result.check_type.value.replace("_", " ")

    # Summary line stays scannable; everything else lives inside the
    # collapsible body.
    summary_line = f"{status_emoji} <b>{check_name}</b>: {result.message}"

    body_parts: List[str] = []
    if result.details:
        body_parts.append("**Details:**")
        for detail in result.details:
            body_parts.append(str(detail))

    if usage and usage.total_tokens > 0:
        body_parts.append("**Usage:**")
        body_parts.append(f"Model: {usage.primary_model}")
        body_parts.append(
            f"Tokens: {usage.total_input_tokens:,} input + "
            f"{usage.total_output_tokens:,} output = "
            f"{usage.total_tokens:,} total"
        )
        body_parts.append(f"Cost: ${usage.total_cost:.4f}")

    body_parts.append(f"_Check completed at {result.timestamp}_")
    body_parts.append(
        "---\n*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"
    )
    # Blank line separator between body sections so markdown paragraph
    # breaks render correctly inside the <details> block.
    body = "\n\n".join(body_parts)

    return (
        f"<details>\n"
        f"<summary>{summary_line}</summary>\n"
        f"\n"
        f"{body}\n"
        f"</details>"
    )


# =============================================================================
# Inline (line-level) findings sync
#
# A check may return, alongside status/message/details, a ``findings`` list of
# shared.pr_findings.Finding items (as dicts) anchored to a file+line. Each is
# posted as a line-level review comment (GitHub review comment / GitLab
# discussion with a diff position / ADO thread with threadContext), upserted and
# stale-resolved per (check, file, line) using INLINE# bookkeeping rows:
#     PK = PR#{provider}#{repo}#{pr_number}
#     SK = INLINE#{check}#{path}#{line}
# This is additive: the per-check <details> summary comment is unchanged, so a
# check with no line-anchorable findings behaves exactly as before.
# =============================================================================


def _compute_diff_info(job: CheckJob, files: List[str]) -> Dict[str, "FileDiffInfo"]:
    """Per-file diff info (added lines, change blocks, newness) for ``files``.

    Provider-aware (github/gitlab/azure_devops): resolves the provider token,
    fetches the PR base/head SHAs, then calls ``pr_diff.diff_info_by_file``.

    RAISES on any failure. Callers decide the fallback: the anchor path
    degrades to ``{}``, BREAKING_CHANGES passes ``change_blocks=None`` so the
    check fails closed (ADR-054), README_FRESHNESS passes ``new_files=None``.
    """
    from shared import pr_diff
    from shared.pr_metadata import fetch_pr_metadata

    if job.provider == "gitlab":
        from check_handlers.gitlab_context import (
            get_gitlab_base_url, get_gitlab_token)

        token = get_gitlab_token(job.connection_id)
        base = get_gitlab_base_url(job.connection_id)
        meta = fetch_pr_metadata(
            "gitlab", job.pr_number, gitlab_project_path=job.repository,
            gitlab_access_token=token, gitlab_base_url=base)
        return pr_diff.diff_info_by_file(
            "gitlab", job.repository, meta.base_sha, meta.head_sha,
            files, gitlab_access_token=token, gitlab_base_url=base)
    if job.provider == "azure_devops":
        access_token = _ado_refresh_access_token(
            connection_id=job.connection_id,
            tenant_id=AZURE_DEVOPS_TENANT_ID,
            client_id=AZURE_DEVOPS_CLIENT_ID,
            client_secret=_get_ado_client_secret(),
        )
        meta = fetch_pr_metadata(
            "azure_devops", job.pr_number, ado_org=job.azure_devops_org,
            ado_project=job.azure_devops_project, ado_repo=job.azure_devops_repo,
            ado_access_token=access_token)
        return pr_diff.diff_info_by_file(
            "azure_devops", job.repository, meta.base_sha, meta.head_sha,
            files, ado_org=job.azure_devops_org,
            ado_project=job.azure_devops_project, ado_repo=job.azure_devops_repo,
            ado_access_token=access_token)
    # github
    token = get_github_token()
    meta = fetch_pr_metadata(
        "github", job.pr_number, repository=job.repository, github_token=token)
    return pr_diff.diff_info_by_file(
        "github", job.repository, meta.base_sha, meta.head_sha,
        files, github_token=token)


def _anchors_from_info(info: Dict[str, "FileDiffInfo"]) -> Dict[str, List[int]]:
    """Added-line anchors per file, omitting files with no added lines."""
    return {p: i.added_lines for p, i in info.items() if i.added_lines}


def _compute_anchors(job: CheckJob) -> Dict[str, List[int]]:
    """All added (RIGHT-side) lines per changed file, for file-level checks that
    anchor inline findings (TEST_COVERAGE, ARCHITECTURAL_DUPLICATION).

    Returns the FULL added-line list per file (not just the first) so a check
    can anchor multiple findings in one file on their own distinct lines. A
    single-anchor consumer takes ``lines[0]``.

    Best-effort: returns ``{}`` on any failure so the check still runs and
    simply posts no inline findings (it keeps its per-check <details> summary
    either way).
    """
    try:
        return _anchors_from_info(_compute_diff_info(job, job.changed_files))
    except Exception as e:
        logger.warning(
            "[inline] anchor computation failed for %s (checks fall back to "
            "summary-only): %s", job.check_type.value, e)
        return {}


def _compute_breaking_changes_inputs(
    job: CheckJob,
) -> Tuple[
    Dict[str, List[int]],
    Optional[Dict[str, List["ChangeBlock"]]],
    Optional[Dict[str, str]],
]:
    """``(anchors, change_blocks, head_texts)`` for BREAKING_CHANGES from ONE
    diff fetch.

    ``head_texts`` maps path -> decoded head content for every file that has
    one (deleted and binary files are omitted); the check re-parses it to
    compare a changed symbol's head signature with the graph's base one.

    On failure returns ``({}, None, None)``: anchors degrade like every other
    inline check, and ``change_blocks=None`` tells the check it could not
    determine what changed so it fails closed (ADR-054). ``{}`` is never
    used to mean "failed".
    """
    try:
        info = _compute_diff_info(job, job.changed_files)
    except Exception as e:
        logger.warning(
            "[inline] diff computation failed for %s (anchors fall back to "
            "summary-only, change blocks unknown): %s", job.check_type.value, e)
        return {}, None, None
    head_texts = {
        p: i.head_text for p, i in info.items()
        if getattr(i, "head_text", None) is not None
    }
    return (
        _anchors_from_info(info),
        {p: i.blocks for p, i in info.items()},
        head_texts,
    )


def _is_readme_handler_file(path: str) -> bool:
    """Same filter README_FRESHNESS applies: ``lambda/*/handler.py`` outside tests."""
    return (path.startswith("lambda/") and path.endswith("/handler.py")
            and "/tests/" not in path)


def _compute_new_handler_files(job: CheckJob) -> Optional[Set[str]]:
    """Changed handler files that are absent at the PR base (added in this PR).

    Fetches diff info for ONLY the files README_FRESHNESS inspects, so other
    changed files are not fetched. Returns ``set()`` without any fetch when
    no handler file changed, and ``None`` on failure (newness unknown; the
    check falls back to its pre-newness behavior).
    """
    handler_files = [f for f in job.changed_files if _is_readme_handler_file(f)]
    if not handler_files:
        return set()
    try:
        info = _compute_diff_info(job, handler_files)
    except Exception as e:
        logger.warning(
            "[inline] new-file detection failed for %s (newness unknown): %s",
            job.check_type.value, e)
        return None
    return {p for p, i in info.items() if i.is_new}


def _github_pr_head_sha(repository: str, pr_number: int, token: str) -> str:
    """Head commit SHA of a GitHub PR (the ``commit_id`` review comments anchor to)."""
    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()["head"]["sha"]


def _query_inline_rows(pk: str, sk_prefix: str) -> Dict[str, Dict[str, Any]]:
    """All INLINE# bookkeeping rows for this (PR, check), keyed by SK."""
    from boto3.dynamodb.conditions import Key

    table = dynamodb.Table(DYNAMODB_TABLE)
    rows: Dict[str, Dict[str, Any]] = {}
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with(sk_prefix)
    }
    while True:
        resp = table.query(**kwargs)
        for item in resp.get("Items", []) or []:
            rows[item["SK"]] = item
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return rows


def _format_inline_comment(check_name: str, fd: "Finding") -> str:
    """Render one Finding as an inline comment body (no aggregate counts)."""
    glyph = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(fd.severity, "⚠️")
    where = f" in `{fd.enclosing_symbol}`" if fd.enclosing_symbol else ""
    return (
        f"{glyph} <b>{check_name}</b>{where}: {fd.message}\n\n"
        "---\n*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"
    )


def _format_inline_body(check_name: str, findings: List["Finding"]) -> str:
    """Render ONE inline comment body for all findings on a single (path, line).

    A single finding keeps the original one-line format (unchanged). Two or more
    findings that legitimately anchor to the same line are MERGED into one
    bulleted comment rather than posted as separate overlapping comments (and
    rather than silently dropping all but one, as the old per-(path,line) dict
    did). No aggregate counts (ADR-032): each concrete finding is enumerated.
    """
    if len(findings) == 1:
        return _format_inline_comment(check_name, findings[0])
    lines = [f"<b>{check_name}</b>:", ""]
    for fd in findings:
        glyph = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(fd.severity, "⚠️")
        where = f" in `{fd.enclosing_symbol}`" if fd.enclosing_symbol else ""
        lines.append(f"- {glyph}{where} {fd.message}")
    lines.append("")
    lines.append("---\n*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*")
    return "\n".join(lines)


def _inline_provider_context(job: CheckJob) -> Dict[str, Any]:
    """Resolve the token + diff anchors an inline post needs, once per job."""
    if job.provider == "gitlab":
        from check_handlers.gitlab_context import (
            get_gitlab_base_url, get_gitlab_token)
        from shared import gitlab_api

        token = get_gitlab_token(job.connection_id)
        base = get_gitlab_base_url(job.connection_id)
        mr = gitlab_api.get_merge_request(
            token, job.repository, job.pr_number, base_url=base)
        return {"token": token, "base_url": base,
                "diff_refs": mr.get("diff_refs", {}) or {}}
    if job.provider == "azure_devops":
        access_token = _ado_refresh_access_token(
            connection_id=job.connection_id,
            tenant_id=AZURE_DEVOPS_TENANT_ID,
            client_id=AZURE_DEVOPS_CLIENT_ID,
            client_secret=_get_ado_client_secret(),
        )
        if not access_token:
            raise RuntimeError(
                "Failed to refresh Azure DevOps access token; reconnect_required")
        return {"access_token": access_token, "org": job.azure_devops_org,
                "project": job.azure_devops_project, "repo": job.azure_devops_repo}
    # github
    token = get_github_token()
    return {"token": token,
            "head_sha": _github_pr_head_sha(job.repository, job.pr_number, token)}


def _post_inline(job: CheckJob, ctx: Dict[str, Any], body: str,
                 fd: "Finding") -> Dict[str, Any]:
    """POST a new inline comment; return the provider id(s) to persist."""
    if job.provider == "gitlab":
        from shared import gitlab_api

        dr = ctx["diff_refs"]
        position: Dict[str, Any] = {
            "position_type": "text",
            "base_sha": dr.get("base_sha"),
            "start_sha": dr.get("start_sha"),
            "head_sha": dr.get("head_sha"),
            "new_path": fd.path,
        }
        if fd.side == "LEFT":
            position["old_path"] = fd.path
            position["old_line"] = fd.line
        else:
            position["new_line"] = fd.line
        resp = gitlab_api.post_merge_request_discussion(
            ctx["token"], job.repository, job.pr_number, body, position,
            base_url=ctx["base_url"])
        notes = resp.get("notes") or []
        return {"providerDiscussionId": resp.get("id"),
                "providerCommentId": notes[0].get("id") if notes else None}
    if job.provider == "azure_devops":
        from shared.azure_devops_api import post_pull_request_inline_thread

        resp = post_pull_request_inline_thread(
            ctx["access_token"], ctx["org"], ctx["project"], ctx["repo"],
            job.pr_number, body, file_path=fd.path, line=fd.line)
        comments = resp.get("comments") or []
        return {"providerThreadId": resp.get("id"),
                "providerThreadCommentId": comments[0].get("id") if comments else None}
    # github
    resp = post_pr_review_comment(
        job.repository, job.pr_number, body, ctx["token"],
        commit_id=ctx["head_sha"], path=fd.path, line=fd.line, side=fd.side)
    return {"providerCommentId": resp.get("id")}


def _patch_inline(job: CheckJob, ctx: Dict[str, Any], body: str,
                  row: Dict[str, Any]) -> None:
    """PATCH an existing inline comment in place."""
    if job.provider == "gitlab":
        from shared import gitlab_api

        gitlab_api.update_merge_request_discussion_note(
            ctx["token"], job.repository, job.pr_number,
            row["providerDiscussionId"], int(row["providerCommentId"]), body,
            base_url=ctx["base_url"])
        return
    if job.provider == "azure_devops":
        from shared.azure_devops_api import update_pull_request_thread_comment

        update_pull_request_thread_comment(
            ctx["access_token"], ctx["org"], ctx["project"], ctx["repo"],
            job.pr_number, int(row["providerThreadId"]),
            int(row["providerThreadCommentId"]), body)
        return
    patch_pr_review_comment(
        job.repository, int(row["providerCommentId"]), body, ctx["token"])


def _delete_inline(job: CheckJob, ctx: Dict[str, Any], row: Dict[str, Any]) -> None:
    """Resolve a stale inline comment (finding no longer applies)."""
    if job.provider == "gitlab":
        from shared import gitlab_api

        gitlab_api.delete_merge_request_discussion_note(
            ctx["token"], job.repository, job.pr_number,
            row["providerDiscussionId"], int(row["providerCommentId"]),
            base_url=ctx["base_url"])
        return
    if job.provider == "azure_devops":
        from shared.azure_devops_api import delete_pull_request_thread_comment

        delete_pull_request_thread_comment(
            ctx["access_token"], ctx["org"], ctx["project"], ctx["repo"],
            job.pr_number, int(row["providerThreadId"]),
            int(row["providerThreadCommentId"]))
        return
    delete_pr_review_comment(job.repository, int(row["providerCommentId"]), ctx["token"])


def _store_inline_row(pk: str, sk: str, job: CheckJob, fd: "Finding",
                      ids: Dict[str, Any]) -> None:
    item: Dict[str, Any] = {
        "PK": pk,
        "SK": sk,
        "checkType": job.check_type.value,
        "path": fd.path,
        "line": fd.line,
        "side": fd.side,
        "severity": fd.severity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for k, v in ids.items():
        if v is not None:
            item[k] = v
    dual_write.ddb_put_item(DYNAMODB_TABLE, item)


def _delete_inline_row(pk: str, sk: str) -> None:
    dynamodb.Table(DYNAMODB_TABLE).delete_item(Key={"PK": pk, "SK": sk})


def _sync_inline_findings(job: CheckJob, findings_raw: List[Dict[str, Any]]) -> None:
    """Reconcile this check's line-anchored findings with inline PR comments.

    Best-effort and non-fatal: the per-check summary comment has already been
    posted by the caller, so a failure to post/patch/resolve a single inline
    finding is logged and swallowed rather than failing the check (which would
    trigger an SQS retry of the whole check).
    """
    from shared.pr_findings import Finding

    pk = f"PR#{job.provider}#{job.repository}#{job.pr_number}"
    check = job.check_type.value.lower()
    sk_prefix = f"INLINE#{check}#"
    check_name = job.check_type.value.replace("_", " ")

    findings: List[Finding] = []
    for raw in findings_raw or []:
        try:
            findings.append(Finding(**raw))
        except Exception as e:  # malformed finding -> skip, don't fail the check
            logger.warning("[inline] dropping malformed finding %r: %s", raw, e)

    # Group findings by (path, line): all findings on one line share a single
    # inline comment (and a single INLINE# bookkeeping row). This both MERGES
    # co-located findings into one comment and stops the old silent last-wins
    # drop that a per-(path,line) dict caused.
    desired: Dict[tuple, List[Finding]] = {}
    for fd in findings:
        desired.setdefault((fd.path, fd.line), []).append(fd)

    existing = _query_inline_rows(pk, sk_prefix)
    existing_by_key: Dict[tuple, tuple] = {}
    for sk, row in existing.items():
        try:
            existing_by_key[(row.get("path"), int(row.get("line", 0)))] = (sk, row)
        except (TypeError, ValueError):
            continue

    if not desired and not existing:
        return

    ctx: Optional[Dict[str, Any]] = None

    def _get_ctx() -> Dict[str, Any]:
        nonlocal ctx
        if ctx is None:
            ctx = _inline_provider_context(job)
        return ctx

    # Upsert desired findings (one comment per (path, line) group).
    for key, group in desired.items():
        rep = group[0]  # path/line/side identical across the group
        body = _format_inline_body(check_name, group)
        prior = existing_by_key.get(key)
        try:
            if prior:
                _patch_inline(job, _get_ctx(), body, prior[1])
            else:
                ids = _post_inline(job, _get_ctx(), body, rep)
                _store_inline_row(pk, f"{sk_prefix}{rep.path}#{rep.line}", job, rep, ids)
        except Exception as e:
            logger.warning(
                "[inline] upsert failed for %s %s:%s: %s", check, rep.path, rep.line, e)

    # Resolve stale rows (posted before, not among this run's findings).
    for key, (sk, row) in existing_by_key.items():
        if key in desired:
            continue
        try:
            _delete_inline(job, _get_ctx(), row)
        except Exception as e:
            logger.warning("[inline] stale-resolve failed for %s: %s", sk, e)
        try:
            _delete_inline_row(pk, sk)
        except Exception as e:
            logger.warning("[inline] stale-row delete failed for %s: %s", sk, e)


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
    # source-fetching accordingly. The -era short-circuit that stubbed
    # ADO to a PASS with a "not yet implemented" comment was removed
    # in ; ADO jobs now run the same check pipeline GitHub
    # jobs do.
    # File-level checks can anchor inline findings only if given an
    # added-line map; compute it provider-aware for just those checks (other
    # checks don't need it and skip the fetch). BREAKING_CHANGES also needs
    # the per-file change blocks, derived from the same single diff fetch.
    # README_FRESHNESS needs to know which handler files are new in the PR.
    anchors: Optional[Dict[str, List[int]]] = None
    change_blocks: Optional[Dict[str, List["ChangeBlock"]]] = None
    head_texts: Optional[Dict[str, str]] = None
    new_files: Optional[Set[str]] = None
    if job.check_type == CheckType.BREAKING_CHANGES:
        anchors, change_blocks, head_texts = _compute_breaking_changes_inputs(job)
    elif job.check_type in (
        CheckType.TEST_COVERAGE,
        CheckType.ARCHITECTURAL_DUPLICATION,
    ):
        anchors = _compute_anchors(job)
    elif job.check_type == CheckType.README_FRESHNESS:
        new_files = _compute_new_handler_files(job)
    check_result = run_check(
        job, anchors, change_blocks=change_blocks, head_texts=head_texts,
        new_files=new_files)
    usage = check_result.get("usage")

    # Build full result. PK is self-contained per (provider, repo, PR)
    # so rows don't collide across repos with the same PR number.
    pk = f"PR#{job.provider}#{job.repository}#{job.pr_number}"
    sk = f"CHECK#{job.check_type.value.lower()}"
    timestamp = datetime.now(timezone.utc).isoformat()
    result = CheckResult(
        PK=pk,
        SK=sk,
        checkType=job.check_type,
        status=CheckStatus(check_result["status"]),
        message=check_result["message"],
        details=check_result["details"],
        timestamp=timestamp,
    )

    # Upsert prep: read the existing row BEFORE we overwrite it in the
    # intermediate store, so we can find provider comment identifiers
    # from a previous run and target a PATCH.
    existing_row = _get_existing_check_result_row(pk, sk)

    # Store intermediate row (matches pre-upsert semantics: check-result
    # data persists even if the comment post/patch below fails).
    store_check_result(result)

    # Post OR update the PR comment. On repeat runs (subsequent pushes
    # to the same PR) we PATCH the existing comment instead of POSTing
    # a fresh one; the customer sees one comment per (PR × check_type)
    # that stays current instead of a fresh one every push.
    comment = format_check_comment(result, usage)
    patched_url: Optional[str] = None
    if existing_row:
        if job.provider == "azure_devops":
            patched_url = _try_patch_ado_comment(
                org=job.azure_devops_org,
                project=job.azure_devops_project,
                repo=job.azure_devops_repo,
                pull_request_id=job.pr_number,
                connection_id=job.connection_id,
                existing_row=existing_row,
                comment_body=comment,
            )
        elif job.provider == "gitlab":
            patched_url = _try_patch_gitlab_comment(
                repository=job.repository,
                pr_number=job.pr_number,
                connection_id=job.connection_id,
                existing_row=existing_row,
                comment_body=comment,
            )
        else:  # github
            patched_url = _try_patch_github_comment(
                repository=job.repository,
                existing_row=existing_row,
                comment_body=comment,
            )

    if patched_url is not None:
        # PATCH succeeded: preserve the existing comment identifiers on
        # the new row so future runs continue to find and update the
        # same comment.
        result.provider_comment_id = existing_row.get("providerCommentId")
        result.provider_thread_id = existing_row.get("providerThreadId")
        result.provider_thread_comment_id = existing_row.get("providerThreadCommentId")
        comment_url = patched_url
        logger.info(
            f"[upsert] Updated existing comment for {job.check_type.value} "
            f"on {job.provider} PR #{job.pr_number}"
        )
    elif job.provider == "gitlab":
        # POST new MR note. Store the note ID so the next run can PUT
        # (same single-id model as GitHub; reuses providerCommentId).
        comment_response = _post_gitlab_note(
            job.repository, job.pr_number, comment, job.connection_id,
        )
        note_id = comment_response.get("id")
        result.provider_comment_id = note_id
        if note_id:
            from check_handlers.gitlab_context import get_gitlab_base_url
            comment_url = _gitlab_note_url(
                job.repository, job.pr_number, note_id,
                get_gitlab_base_url(job.connection_id))
        else:
            comment_url = ""
    elif job.provider == "azure_devops":
        # POST new ADO thread. Extract thread ID + first comment ID so
        # the next run can PATCH.
        comment_response = _post_azure_devops_thread(
            org=job.azure_devops_org,
            project=job.azure_devops_project,
            repo=job.azure_devops_repo,
            pull_request_id=job.pr_number,
            body=comment,
            connection_id=job.connection_id,
        )
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
        # POST new GitHub comment. Extract comment ID so the next run
        # can PATCH.
        token = get_github_token()
        comment_response = post_pr_comment(job.repository, job.pr_number, comment, token)
        comment_url = comment_response.get("html_url", "")
        result.provider_comment_id = comment_response.get("id")

    # Update result with comment URL
    result.comment_url = comment_url

    # Update DynamoDB with comment URL + provider comment identifiers
    store_check_result(result)

    # Post/patch/resolve any line-anchored findings as inline comments. Additive
    # and non-fatal: the summary comment above already landed, so an inline
    # failure must not fail the check (which would trigger an SQS retry).
    try:
        _sync_inline_findings(job, check_result.get("findings") or [])
    except Exception as e:
        logger.warning("[inline] finding sync failed (non-fatal): %s", e)

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

            # ADR-057: on ADO the PR summary is deferred to the completion
            # fan-in so it lands as the newest (top) thread. Marking this check
            # done posts the summary if it was the last one. Non-fatal: a
            # failure here must not retry an already-successful check.
            if job.provider == "azure_devops":
                try:
                    _ado_mark_done_and_maybe_post_summary(job)
                except Exception as hook_error:
                    logger.warning(
                        "[pr-summary.ado] completion hook failed for PR #%s "
                        "(%s): %s",
                        job.pr_number, job.check_type.value, hook_error,
                    )

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
