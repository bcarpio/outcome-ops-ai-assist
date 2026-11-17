# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Set
from enum import Enum

import boto3
import requests

# Make shared/ importable when running as a Lambda; the deploy package
# includes lambda/shared/ at zip root via terraform source_path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import ACTION_PR_ANALYZED, log_admin_action
from shared import dual_write
import yaml
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator, model_validator


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    so module load order (check_handlers before sys.path fix) is safe."""
    from shared.github_base import github_api_base
    return github_api_base()


# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
ssm_client = boto3.client("ssm")
sqs_client = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
DYNAMODB_TABLE = os.environ.get(
    "DYNAMODB_TABLE",
    os.environ.get("CODE_MAPS_TABLE", f"{ENVIRONMENT}-{APP_NAME}-code-maps"),
)


class CheckType(str, Enum):
    """Types of checks that can be performed on a PR."""
    ADR_COMPLIANCE = "ADR_COMPLIANCE"
    README_FRESHNESS = "README_FRESHNESS"
    TEST_COVERAGE = "TEST_COVERAGE"
    BREAKING_CHANGES = "BREAKING_CHANGES"
    ARCHITECTURAL_DUPLICATION = "ARCHITECTURAL_DUPLICATION"
    LICENSE_COMPLIANCE = "LICENSE_COMPLIANCE"


class AnalyzePrRequest(BaseModel):
    """Request schema for analyze-pr Lambda.

    ``provider`` defaults to ``"github"`` so existing GitHub-only
    invocations continue to validate unchanged. When
    ``provider="azure_devops"``, the caller MUST provide
    ``connection_id`` + ``ado_org`` + ``ado_project`` + ``ado_repo``;
    ``repository`` in that shape is a 3-segment slug
    (``{org}/{project}/{repo}``) matching the CheckJob wire format
    downstream. When ``provider="gitlab"``, the caller MUST provide
    ``connection_id``; ``repository`` is the FULL project path
    (``group/{...subgroups}/project`` — variable depth, so validation
    checks segments, never slash-count) and ``pr_number`` is the MR
    ``iid``.
    """
    pr_number: int = Field(..., gt=0, description="Pull Request number")

    # Provider MUST be declared before ``repository`` so pydantic's
    # in-order field validation populates ``info.data['provider']``
    # before ``validate_repository_format`` runs.
    provider: Literal["github", "azure_devops", "gitlab"] = "github"
    connection_id: Optional[str] = None
    ado_org: Optional[str] = None
    ado_project: Optional[str] = None
    ado_repo: Optional[str] = None

    repository: str = Field(
        ...,
        min_length=1,
        description="Repository slug (owner/repo for GitHub, org/project/repo for Azure DevOps)",
    )

    @field_validator("repository")
    @classmethod
    def validate_repository_format(cls, v, info):
        """Validate repository format by provider.

        GitHub: ``owner/repo`` (exactly 1 slash).
        Azure DevOps: ``org/project/repo`` (exactly 2 slashes).
        GitLab: ``group/{...subgroups}/project`` — at least 1 slash,
        variable depth (nested subgroups), every segment non-empty.
        """
        provider = (info.data or {}).get("provider", "github")
        if provider == "gitlab":
            segments = v.split("/")
            if len(segments) < 2 or not all(segments):
                raise ValueError(
                    "GitLab repository must be a full project path "
                    "'group/.../project' with non-empty segments"
                )
            return v
        if provider == "azure_devops":
            if v.count("/") != 2:
                raise ValueError(
                    "Azure DevOps repository must be in format 'org/project/repo'"
                )
            org, project, repo = v.split("/")
            if not (org and project and repo):
                raise ValueError("Org, project, and repo cannot be empty")
            return v
        # github
        if "/" not in v or v.count("/") != 1:
            raise ValueError("Repository must be in format 'owner/repo'")
        owner, repo = v.split("/")
        if not owner or not repo:
            raise ValueError("Owner and repo cannot be empty")
        return v

    @model_validator(mode="after")
    def _require_ado_context_for_azure_devops(self):
        """Cross-field: ADO provider requires connection_id + org + project + repo.
        GitLab requires only connection_id (repository IS the project path)."""
        if self.provider == "gitlab":
            if not self.connection_id:
                raise ValueError("provider='gitlab' requires: connection_id")
            return self
        if self.provider != "azure_devops":
            return self
        missing = []
        if not self.connection_id:
            missing.append("connection_id")
        if not self.ado_org:
            missing.append("ado_org")
        if not self.ado_project:
            missing.append("ado_project")
        if not self.ado_repo:
            missing.append("ado_repo")
        if missing:
            raise ValueError(
                f"provider='azure_devops' requires: {', '.join(missing)}"
            )
        return self


class AnalyzePrResponse(BaseModel):
    """Response schema for analyze-pr Lambda."""
    message: str
    pr_number: int
    checks_queued: int = Field(..., ge=0)


class CheckJob(BaseModel):
    """Schema for check job sent to SQS.

    Field-for-field wire-compatible with ``process-pr-check.CheckJob``
    (aliases + optional ADO fields identical) so the SQS round-trip
    preserves every field the downstream consumer needs. Any drift
    between the two schemas silently breaks ADO PR-checks; a shape-
    parity regression test in ``test_analyze_pr.py`` pins it.
    """
    check_type: CheckType = Field(..., alias="checkType")
    pr_number: int = Field(..., alias="pr_number", gt=0)
    repository: str
    changed_files: List[str] = Field(..., alias="changedFiles")

    # Provider context. All optional so GitHub-shape messages continue
    # to validate. GitLab needs only ``connection_id``.
    provider: Literal["github", "azure_devops", "gitlab"] = "github"
    connection_id: Optional[str] = None
    azure_devops_slug: Optional[str] = Field(None, alias="slug")
    azure_devops_org: Optional[str] = Field(None, alias="org")
    azure_devops_project: Optional[str] = Field(None, alias="project")
    azure_devops_repo: Optional[str] = Field(None, alias="repo")

    class Config:
        populate_by_name = True


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
    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}"

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
    # fable-scale chunk 06: paginate GitHub's /pulls/{n}/files. The prior
    # single unpaginated GET returned only the first 30 files (GitHub's
    # default page size); monorepo PRs above that count got silently
    # partial analysis. Also route through shared.github_backoff so
    # rate-limit 403s and 429s back off rather than hard-fail.
    from shared import github_backoff

    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}/files"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        files: List[GitHubFile] = []
        page = 1
        per_page = 100  # GitHub's max page size for this endpoint
        # 3000-file safety cap prevents runaway on catastrophically-large PRs
        # (GitHub itself starts truncating past 3000 anyway).
        MAX_PAGES = 30
        while page <= MAX_PAGES:
            response = github_backoff.github_request(
                "GET", url, headers=headers, timeout=30,
                params={"per_page": per_page, "page": page},
            )
            response.raise_for_status()
            batch = response.json() or []
            files.extend(GitHubFile(**fd) for fd in batch)
            if len(batch) < per_page:
                break
            page += 1
        if page > MAX_PAGES:
            logger.warning(
                "PR #%s has >%d files; analysis truncated at page cap",
                pr_number, MAX_PAGES * per_page,
            )

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
    url = f"{_github_api()}/repos/{repository}/contents/{file_path}"
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


# =============================================================================
# Upsert helpers: PATCH the same comment on subsequent runs instead of
# POSTing a new one. Companion DDB row is keyed by (PR × comment-role).
# Mirrors the process-pr-check side, but adapted to analyze-pr's three
# self-emitted comments (analysis_started / structural_impact /
# no_checks_needed) which don't fit the CheckResult schema.
# =============================================================================

COMMENT_ROLE_ANALYSIS_STARTED = "analysis_started"
COMMENT_ROLE_STRUCTURAL_IMPACT = "structural_impact"
COMMENT_ROLE_NO_CHECKS_NEEDED = "no_checks_needed"
COMMENT_ROLE_SUMMARY = "summary"

# SK for the deferred-ADO-summary coordination item (ADR-057). On Azure DevOps
# the PR summary is NOT posted up front: ADO orders PR threads by creation, so
# an up-front summary sinks below the per-check threads. analyze-pr persists the
# rendered body + expected check set here; process-pr-check's completion fan-in
# reads it and posts the summary as the newest thread once every check finishes,
# so it lands at the top. MUST match the identically named constant in
# process-pr-check/handler.py.
DEFERRED_SUMMARY_SK = "SUMMARY_DEFERRED"


def _build_analysis_started_comment(checks_to_run: List[str]) -> str:
    """Compose the "OutcomeOps Analysis Started" comment body.

    Wrapped in an HTML ``<details>`` block for the same reason
    ``format_check_comment`` is (see process-pr-check handler): each
    PR-Analyzer run drops multiple comments; a reviewer scanning the
    PR wants the one-line status ("Analysis started, N checks
    running") without the full check list unfolding by default.

    Both GitHub and Azure DevOps render <details>/<summary> in PR
    comment markdown. The blank line after </summary> is required for
    the body's markdown to render inside the block.

    Provider-agnostic: both dispatch paths call this helper so the
    template stays in a single place.
    """
    checks_list = "\n".join(
        [f"- {check.replace('_', ' ')}" for check in checks_to_run]
    )
    summary_line = (
        f"<b>OutcomeOps Analysis Started</b>: "
        f"Running {len(checks_to_run)} check(s)"
    )
    body = (
        "**Checks queued:**\n\n"
        f"{checks_list}\n\n"
        "Results will be posted as comments when complete.\n\n"
        "---\n"
        "*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"
    )
    return (
        f"<details>\n"
        f"<summary>{summary_line}</summary>\n"
        f"\n"
        f"{body}\n"
        f"</details>"
    )


def _build_comment_pk(provider: str, repository: str, pr_number: int) -> str:
    """Match the process-pr-check chunk-02 shape: PR#{provider}#{repo}#{n}."""
    return f"PR#{provider}#{repository}#{pr_number}"


def _get_existing_comment_row(pk: str, sk: str) -> Optional[Dict[str, Any]]:
    """Read the existing DDB row for (PR × comment-role), or None.
    Non-fatal on read failure -- caller POSTs a new comment."""
    try:
        table = dynamodb.Table(DYNAMODB_TABLE)
        response = table.get_item(Key={"PK": pk, "SK": sk})
        return response.get("Item")
    except ClientError as e:
        logger.warning(
            f"[analyze-pr.upsert] Failed to read existing comment row "
            f"{pk}/{sk}: {e}. Falling back to POST."
        )
        return None


def _store_comment_row(
    *,
    pk: str,
    sk: str,
    provider_comment_id: Optional[int] = None,
    provider_thread_id: Optional[int] = None,
    provider_thread_comment_id: Optional[int] = None,
    comment_url: Optional[str] = None,
) -> None:
    """Write the companion DDB row so the next run can PATCH instead of POST.
    Non-fatal on write failure -- the comment is already live in the PR."""
    item: Dict[str, Any] = {
        "PK": pk,
        "SK": sk,
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if provider_comment_id is not None:
        item["providerCommentId"] = int(provider_comment_id)
    if provider_thread_id is not None:
        item["providerThreadId"] = int(provider_thread_id)
    if provider_thread_comment_id is not None:
        item["providerThreadCommentId"] = int(provider_thread_comment_id)
    if comment_url:
        item["commentUrl"] = comment_url

    try:
        dual_write.ddb_put_item(DYNAMODB_TABLE, item)
    except ClientError as e:
        logger.warning(
            f"[analyze-pr.upsert] Failed to store comment row {pk}/{sk}: {e}"
        )


def _store_deferred_ado_summary(
    *, pk: str, body: str, expected_checks: List[str]
) -> None:
    """Persist the ADO summary so the check-completion fan-in posts it last.

    ADO orders PR threads by creation; a summary posted up front sinks below the
    per-check threads. Deferring the ADO post to process-pr-check's completion
    fan-in makes the summary the newest thread -> top of the Overview (ADR-057).
    Stores the fully rendered body and the expected check-type set (a String Set
    the fan-in matches ``completedChecks`` against) under SK=SUMMARY_DEFERRED.

    Written via the resource client rather than ``dual_write`` because the value
    is a DynamoDB String Set and this is transient, single-cycle coordination
    state (not a durable artifact that needs cross-region replication).
    """
    dynamodb.Table(DYNAMODB_TABLE).put_item(
        Item={
            "PK": pk,
            "SK": DEFERRED_SUMMARY_SK,
            "summaryBody": body,
            "expectedChecks": set(expected_checks),
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )


def _upsert_github_comment(
    *,
    repository: str,
    pr_number: int,
    role: str,
    body: str,
    token: str,
) -> None:
    """PATCH the existing GitHub comment for this (PR × role) if we have one;
    otherwise POST a new one. Stores the resulting comment ID for next run."""
    pk = _build_comment_pk("github", repository, pr_number)
    sk = f"COMMENT#{role}"

    existing_row = _get_existing_comment_row(pk, sk)
    existing_id = (existing_row or {}).get("providerCommentId")

    if existing_id is not None:
        try:
            response = patch_pr_comment(repository, int(existing_id), body, token)
            _store_comment_row(
                pk=pk, sk=sk,
                provider_comment_id=int(existing_id),
                comment_url=response.get("html_url"),
            )
            return
        except Exception as e:
            logger.warning(
                f"[analyze-pr.upsert] GitHub PATCH failed for comment "
                f"{existing_id} in {repository} PR #{pr_number} "
                f"({role}): {e}. Falling back to POST."
            )

    try:
        response = post_pr_comment(repository, pr_number, body, token)
    except Exception as e:
        logger.error(
            f"[analyze-pr.upsert] GitHub POST failed for {repository} PR "
            f"#{pr_number} ({role}): {e}"
        )
        return
    _store_comment_row(
        pk=pk, sk=sk,
        provider_comment_id=response.get("id"),
        comment_url=response.get("html_url"),
    )


def _upsert_ado_comment(
    *,
    repository: str,
    ado_org: str,
    ado_project: str,
    ado_repo: str,
    pr_number: int,
    access_token: str,
    role: str,
    body: str,
) -> None:
    """PATCH the existing ADO thread comment for this (PR × role) if we have
    one; otherwise POST a new thread. Stores the resulting thread + comment
    IDs for next run."""
    pk = _build_comment_pk("azure_devops", repository, pr_number)
    sk = f"COMMENT#{role}"

    existing_row = _get_existing_comment_row(pk, sk)
    existing_thread = (existing_row or {}).get("providerThreadId")
    existing_comment = (existing_row or {}).get("providerThreadCommentId")

    if existing_thread is not None and existing_comment is not None:
        try:
            from shared.azure_devops_api import update_pull_request_thread_comment
            update_pull_request_thread_comment(
                access_token=access_token,
                org=ado_org, project=ado_project, repo=ado_repo,
                pull_request_id=pr_number,
                thread_id=int(existing_thread),
                comment_id=int(existing_comment),
                comment_markdown=body,
            )
            _store_comment_row(
                pk=pk, sk=sk,
                provider_thread_id=int(existing_thread),
                provider_thread_comment_id=int(existing_comment),
            )
            return
        except Exception as e:
            logger.warning(
                f"[analyze-pr.upsert] ADO PATCH failed for thread "
                f"{existing_thread}/comment {existing_comment} in "
                f"{ado_org}/{ado_project}/{ado_repo} PR #{pr_number} "
                f"({role}): {e}. Falling back to POST."
            )

    try:
        from shared.azure_devops_api import (
            AzureDevOpsAPIError, post_pull_request_thread,
        )
        response = post_pull_request_thread(
            access_token=access_token,
            org=ado_org, project=ado_project, repo=ado_repo,
            pull_request_id=pr_number,
            comment_markdown=body,
        )
    except Exception as e:
        logger.error(
            f"[analyze-pr.upsert] ADO POST failed for "
            f"{ado_org}/{ado_project}/{ado_repo} PR #{pr_number} "
            f"({role}): {e}"
        )
        return

    thread_id = response.get("id") if isinstance(response, dict) else None
    comments = response.get("comments") or [] if isinstance(response, dict) else []
    comment_id = comments[0].get("id") if comments else None
    _store_comment_row(
        pk=pk, sk=sk,
        provider_thread_id=thread_id,
        provider_thread_comment_id=comment_id,
    )


def _upsert_gitlab_comment(
    *,
    repository: str,
    pr_number: int,
    access_token: str,
    base_url: str,
    role: str,
    body: str,
) -> None:
    """PUT the existing GitLab MR note for this (MR × role) if we have one;
    otherwise POST a new note. Stores the note ID (providerCommentId --
    same single-id model as GitHub) for the next run."""
    from shared import gitlab_api

    pk = _build_comment_pk("gitlab", repository, pr_number)
    sk = f"COMMENT#{role}"

    existing_row = _get_existing_comment_row(pk, sk)
    existing_id = (existing_row or {}).get("providerCommentId")

    if existing_id is not None:
        try:
            gitlab_api.update_merge_request_note(
                access_token, repository, pr_number, int(existing_id), body,
                base_url=base_url,
            )
            _store_comment_row(
                pk=pk, sk=sk,
                provider_comment_id=int(existing_id),
            )
            return
        except Exception as e:
            logger.warning(
                f"[analyze-pr.upsert] GitLab note update failed for note "
                f"{existing_id} on {repository} !{pr_number} ({role}): {e}. "
                f"Falling back to POST."
            )

    try:
        response = gitlab_api.post_merge_request_note(
            access_token, repository, pr_number, body,
            base_url=base_url,
        )
    except Exception as e:
        logger.error(
            f"[analyze-pr.upsert] GitLab note POST failed for {repository} "
            f"!{pr_number} ({role}): {e}"
        )
        return
    _store_comment_row(
        pk=pk, sk=sk,
        provider_comment_id=response.get("id"),
    )


# =============================================================================
# PR/MR summary comment (SK=COMMENT#summary). Builds a plain-English
# walkthrough + per-file table + queued-checks line and upserts it as the top
# comment, PATCHing in place on re-push. Additive reviewer context -- every
# call site wraps this in try/except so a summary failure never blocks the
# check-queueing pipeline (ADR-004 non-fatal; mirrors the structural pass).
# =============================================================================

# Bound the number of files we fetch diffs for on ADO/GitLab (GitHub ships
# inline patches for free). Keeps PR-open latency + model cost bounded; files
# beyond the cap are reported as truncated in the summary (ADR-032).
_SUMMARY_MAX_DIFF_FILES = int(os.environ.get("PR_SUMMARY_MAX_DIFF_FILES", "12"))


def _render_summary_footer(usage, tier: str) -> str:
    """Collapsed token-usage footer for the summary comment (ADR-016).

    ``tier`` is reported as-is (the builder owns the tier choice); it is never
    hardcoded here so a tfvar model swap stays accurately labelled. Returns an
    empty string when the model call failed (usage is None) so the summary still
    posts without a misleading zero-cost footer.
    """
    if usage is None:
        return ""
    return (
        "\n\n<details>\n<summary>Usage</summary>\n\n"
        f"- model tier: {tier} (pr-analyzer workload)\n"
        f"- input tokens: {usage.input_tokens}\n"
        f"- output tokens: {usage.output_tokens}\n"
        "</details>"
    )


def _build_summary_body(
    *,
    metadata,
    diffs_by_file: Dict[str, str],
    checks_to_run: List[str],
    truncated_files: Optional[List[str]] = None,
) -> str:
    """Build the full summary comment body (prose walkthrough + checks line + footer)."""
    from pr_summary import build_pr_summary, SUMMARY_MODEL_TIER

    body, usage = build_pr_summary(
        metadata=metadata,
        diffs_by_file=diffs_by_file,
        checks_to_run=checks_to_run,
        truncated_files=truncated_files,
    )
    return (
        body
        + _render_summary_footer(usage, SUMMARY_MODEL_TIER)
        + "\n\n---\n*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"
    )


def _fetch_diffs_via_sha(
    *,
    provider: str,
    changed_files: List[str],
    change_types: Dict[str, str],
    base_sha: str,
    head_sha: str,
    max_files: int,
    **fetch_kwargs: Any,
) -> tuple:
    """Fetch per-file unified diffs for up to ``max_files`` files (ADO/GitLab).

    GitHub supplies patches inline, so this is only for providers that don't.
    Each file needs its base + head bytes; an added file has no base, a removed
    file has no head. Non-fatal per file: a fetch error or a file beyond the cap
    is reported in ``truncated_files`` so the summary signals the omission
    (ADR-032) rather than silently dropping it.

    Returns ``(diffs_by_file, truncated_files)``.
    """
    from shared.pr_diff import compute_file_diff
    from shared.source_fetch import fetch_file_at_sha, SourceFetchError

    diffs: Dict[str, str] = {}
    truncated: List[str] = []
    for idx, path in enumerate(changed_files):
        if idx >= max_files:
            truncated.append(path)
            continue
        status = change_types.get(path, "modified")
        try:
            base_bytes = (
                None if status == "added"
                else fetch_file_at_sha(provider, path, base_sha, **fetch_kwargs)
            )
            head_bytes = (
                None if status == "removed"
                else fetch_file_at_sha(provider, path, head_sha, **fetch_kwargs)
            )
            diff = compute_file_diff(base_bytes, head_bytes, path)
        except SourceFetchError:
            truncated.append(path)
            continue
        except Exception:
            logger.warning(
                "[analyze-pr.summary] diff build failed for %s; omitting",
                path, exc_info=True,
            )
            truncated.append(path)
            continue
        if diff:
            diffs[path] = diff
    return diffs, truncated


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
    "csharp": BackendPatterns(
        # eShop and most .NET repos: services under src/<Project>/, tests under
        # tests/<Project>.UnitTests/. `_matches_pattern` globs the basename.
        source_dirs=["src/", "source/"],
        source_extensions=[".cs"],
        test_dirs=["tests/", "test/"],
        test_patterns=["*Tests.cs", "*Test.cs"],
        config_files=["*.csproj", "*.sln", "*.slnx", "Directory.Build.props"],
        handler_patterns=["*Controller.cs", "*Api.cs", "Program.cs", "*Handler.cs"],
    ),
    "go": BackendPatterns(
        # Go packages live anywhere in the tree (including the repo root), so the
        # "" source-dir prefix matches every non-test path. Tests are *_test.go
        # beside source (no test directory), handled via test_patterns rather
        # than test_dirs.
        source_dirs=[""],
        source_extensions=[".go"],
        test_dirs=[],
        test_patterns=["*_test.go"],
        config_files=["go.mod", "go.sum"],
        handler_patterns=["main.go"],
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
    url = f"{_github_api()}/repos/{repository}/contents/.outcomeops.yaml"

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


def get_source_roots_from_config(config: Optional[Dict[str, Any]]) -> List[str]:
    """Extract ``code_generation.source_roots`` from the repo config (or [])."""
    if not config:
        return []
    roots = config.get("code_generation", {}).get("source_roots")
    return [str(r) for r in roots] if isinstance(roots, list) else []


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


def parse_changed_files(
    files: List[GitHubFile],
    backend: str = "python",
    source_roots: Optional[List[str]] = None,
) -> Dict[str, Any]:
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
        source_roots: Optional .outcomeops.yaml source_roots override. A repo not
            laid out like the backend's defaults (e.g. a package at the repo root)
            declares its roots; they are added to the backend's source_dirs so
            file-scoping (and thus BREAKING_CHANGES dispatch) recognizes them --
            the same override process-repo-summary honors for code-unit discovery.

    Returns:
        Dict with "changed_files" list and "checks_to_run" list
    """
    changed_files = [f.filename for f in files]
    checks_to_run: Set[CheckType] = set()

    # Get patterns for this backend
    patterns = BACKEND_PATTERNS.get(backend, BACKEND_PATTERNS["python"])
    if source_roots:
        extra_dirs = [r if r.endswith("/") else r + "/" for r in source_roots if r]
        patterns = replace(
            patterns, source_dirs=list(patterns.source_dirs) + extra_dirs
        )
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

        # Breaking Changes: any source-dir code modification.
        # Plane-1 (ADR-054/ADR-055) resolves cross-module/cross-file symbol
        # consumers via the symbol graph, so a breaking edit can live in any
        # source module (e.g. a shared/ helper), not only a handler entrypoint.
        # Dispatch therefore mirrors the plane-1 file scope -- any modified or
        # removed source-dir file of this backend, excluding tests -- rather
        # than the legacy handler-only gate. Non-handler PRs incur only the
        # cheap symbol-graph (DDB) path; the Bedrock heuristic keys off the
        # handler-scoped relevant_files set and stays dormant for them.
        if has_backend_extension and _is_in_source_dir(filepath, patterns):
            # Exclude tests both by path (`/tests/`) and by the backend's test
            # filename patterns -- covers languages like Go whose tests
            # (`*_test.go`) sit beside source with no test directory.
            is_test_named = any(
                _matches_pattern(filepath, tp) for tp in patterns.test_patterns
            )
            if (
                status in ["modified", "removed"]
                and "/tests/" not in filepath
                and not is_test_named
            ):
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


def queue_check_jobs(
    repository: str,
    pr_number: int,
    checks: List[str],
    changed_files: List[str],
    *,
    provider: str = "github",
    connection_id: Optional[str] = None,
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
) -> int:
    """
    Queue check jobs to SQS FIFO queue for async processing.

    Args:
        repository: Repository slug — ``owner/repo`` for GitHub,
            ``org/project/repo`` for Azure DevOps.
        pr_number: Pull Request number
        checks: List of check types to run
        changed_files: List of changed file paths
        provider: ``"github"`` (default) or ``"azure_devops"``.
        connection_id: ADO connection ID; required for ADO.
        ado_org / ado_project / ado_repo: ADO context; required for ADO.

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
        job_kwargs = dict(
            check_type=CheckType(check_type),
            pr_number=pr_number,
            repository=repository,
            changed_files=changed_files,
        )
        if provider == "azure_devops":
            # Embed ADO context so process-pr-check.CheckJob's
            # provider-aware branch fires downstream.
            job_kwargs.update(
                provider="azure_devops",
                connection_id=connection_id,
                azure_devops_slug=repository,
                azure_devops_org=ado_org,
                azure_devops_project=ado_project,
                azure_devops_repo=ado_repo,
            )
        elif provider == "gitlab":
            # GitLab needs only the connection -- ``repository`` IS the
            # full project path the downstream consumer uses.
            job_kwargs.update(
                provider="gitlab",
                connection_id=connection_id,
            )
        job = CheckJob(**job_kwargs)

        # Message group ensures ordered processing per PR
        message_group_id = f"pr-{repository.replace('/', '-')}-{pr_number}"
        # fable-scale chunk 06: MessageDeduplicationId is a stable content
        # hash — same (repo, pr, head_sha, check_type) tuple always maps to
        # the same dedup ID, so GitHub Actions re-runs of the same commit
        # naturally coalesce within the SQS 5-min dedup window. Prior
        # timestamp-based ID produced a new dedup ID per re-run, so a user
        # clicking "re-run failed checks" queued duplicate jobs.
        head_sha = getattr(job, "head_sha", "") or getattr(job, "commit_sha", "")
        dedup_input = f"{repository}|{pr_number}|{head_sha}|{check_type}"
        message_deduplication_id = hashlib.sha256(dedup_input.encode("utf-8")).hexdigest()

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

    Dispatches on ``provider``. GitHub-shape requests use the
    existing api.github.com path unchanged. Azure DevOps-shape
    requests (``provider="azure_devops"``) refresh the customer's
    OAuth token, fetch PR metadata + repo config through the
    shared primitives, post an initial "analysis started" comment
    via the ADO PR-thread API, and queue CheckJobs carrying the
    ADO context so ``process-pr-check`` can consume them.

    Args:
        event: Lambda event with pr_number + repository, plus
            provider + connection_id + ado_org/project/repo when
            provider is azure_devops.
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
        logger.info(
            f"Analyzing PR #{request.pr_number} in {request.repository} "
            f"(provider={request.provider})"
        )

        if request.provider == "azure_devops":
            return _handle_azure_devops(request, start_time)

        if request.provider == "gitlab":
            return _handle_gitlab(request, start_time)

        # ==== GitHub flow (unchanged from pre-widening) ====
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
        parse_result = parse_changed_files(
        files, backend=backend,
        source_roots=get_source_roots_from_config(repo_config),
    )
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
                _upsert_github_comment(
                    repository=request.repository,
                    pr_number=request.pr_number,
                    role=COMMENT_ROLE_STRUCTURAL_IMPACT,
                    body=comment_body,
                    token=token,
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
            _upsert_github_comment(
                repository=request.repository,
                pr_number=request.pr_number,
                role=COMMENT_ROLE_NO_CHECKS_NEEDED,
                body=comment_body,
                token=token,
            )

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

        # Post the PR summary comment (walkthrough + per-file table + queued
        # checks) just above the analysis-started status. Non-fatal -- additive
        # reviewer context; only posted when checks will run.
        try:
            from shared.pr_metadata import PRMetadata

            gh_base = pr.base if isinstance(pr.base, dict) else {}
            gh_head = pr.head if isinstance(pr.head, dict) else {}
            summary_meta = PRMetadata(
                head_sha=gh_head.get("sha", ""),
                base_sha=gh_base.get("sha", ""),
                base_branch=base_ref,
                changed_files=[f.filename for f in files],
                author="",
                title=pr.title or "",
                file_change_types={f.filename: f.status for f in files},
            )
            summary_diffs = {f.filename: f.patch for f in files if f.patch}
            summary_truncated = [f.filename for f in files if not f.patch]
            _upsert_github_comment(
                repository=request.repository,
                pr_number=request.pr_number,
                role=COMMENT_ROLE_SUMMARY,
                body=_build_summary_body(
                    metadata=summary_meta,
                    diffs_by_file=summary_diffs,
                    checks_to_run=checks_to_run,
                    truncated_files=summary_truncated,
                ),
                token=token,
            )
        except Exception as exc:
            logger.warning(
                "[analyze-pr.summary] GitHub summary failed for PR #%d: %s",
                request.pr_number, exc,
            )

        # Post initial comment to PR (collapsed <details> template).
        _upsert_github_comment(
            repository=request.repository,
            pr_number=request.pr_number,
            role=COMMENT_ROLE_ANALYSIS_STARTED,
            body=_build_analysis_started_comment(checks_to_run),
            token=token,
        )

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


# =============================================================================
# Azure DevOps dispatch path
# =============================================================================


def _refresh_ado_access_token(connection_id: str) -> str:
    """Refresh the ADO OAuth access token for the given connection.

    Mirrors the ``refresh_ado_token`` helper in
    ``lambda/process-pr-check/check_handlers/ado_context.py`` --
    reads AZURE_DEVOPS_TENANT_ID / AZURE_DEVOPS_CLIENT_ID env vars
    plus the client-secret from SSM at AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM,
    then calls azure_devops_token_manager.refresh_access_token.

    Raises RuntimeError on any missing prereq or refresh failure --
    callers surface a "reconnect_required" hint on that.
    """
    if not connection_id:
        raise ValueError("connection_id required for ADO token refresh")

    tenant_id = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
    client_id = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
    if not (tenant_id and client_id):
        raise RuntimeError(
            "Azure DevOps OAuth app not configured "
            "(AZURE_DEVOPS_TENANT_ID / AZURE_DEVOPS_CLIENT_ID env vars missing)"
        )

    param_name = os.environ.get("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", "")
    if not param_name:
        raise RuntimeError("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM not configured")
    ssm_response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
    client_secret = ssm_response["Parameter"]["Value"]

    from shared.oauth.azure_devops import refresh_access_token as _refresh  # noqa: E402

    access_token = _refresh(
        connection_id=connection_id,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    if not access_token:
        raise RuntimeError(
            "Failed to refresh Azure DevOps access token; reconnect_required"
        )
    return access_token


def _post_ado_initial_comment(
    *,
    ado_org: str,
    ado_project: str,
    ado_repo: str,
    pr_number: int,
    access_token: str,
    body: str,
) -> None:
    """Post the initial "analysis started" comment on an ADO PR thread.

    Uses the same primitive process-pr-check uses for check-result
    comment posting (``azure_devops_api.post_pull_request_thread``).
    Errors are logged and swallowed -- the check-execution pipeline
    still runs even if the initial-comment post fails.
    """
    from shared.azure_devops_api import (  # noqa: E402
        AzureDevOpsAPIError,
        post_pull_request_thread,
    )

    try:
        post_pull_request_thread(
            access_token=access_token,
            org=ado_org,
            project=ado_project,
            repo=ado_repo,
            pull_request_id=pr_number,
            comment_markdown=body,
        )
        logger.info(
            "[analyze-pr.ado] posted initial comment on %s/%s/%s PR #%s",
            ado_org, ado_project, ado_repo, pr_number,
        )
    except AzureDevOpsAPIError as e:
        # Don't fail the whole analysis if the initial comment doesn't
        # post -- the check-execution pipeline still runs and its
        # comments still land.
        logger.warning(
            "[analyze-pr.ado] initial comment post failed on %s/%s/%s PR #%s: %s",
            ado_org, ado_project, ado_repo, pr_number, e,
        )


def _handle_azure_devops(
    request: AnalyzePrRequest, start_time: float,
) -> Dict[str, Any]:
    """Handle an Azure DevOps analyze-pr request.

    Mirrors the GitHub flow through the shared/ primitives:
    - Refresh ADO OAuth token.
    - Fetch PR metadata (head SHA, base SHA, base branch, changed
      files) via shared.pr_metadata.fetch_pr_metadata.
    - Fetch .outcomeops.yaml at the base branch tip via
      shared.source_fetch.fetch_file_at_sha (branch-mode).
    - Parse changed files against backend patterns to determine
      which checks to run.
    - Post initial "analysis started" comment via ADO PR-thread API.
    - Queue CheckJobs carrying ADO context so process-pr-check
      dispatches on provider="azure_devops" downstream.

    Structural-impact block is NOT posted for ADO in v1 -- it depends
    on GitHub-specific data plumbing (a follow-up cleans that up).
    """
    from shared.pr_metadata import fetch_pr_metadata
    from shared.source_fetch import fetch_file_at_sha, SourceFetchError

    ado_org = request.ado_org
    ado_project = request.ado_project
    ado_repo = request.ado_repo
    connection_id = request.connection_id

    # Refresh the customer's ADO access token.
    access_token = _refresh_ado_access_token(connection_id)

    # Fetch PR metadata (head/base SHAs + base branch + changed files
    # + title + author) in one call.
    pr_meta = fetch_pr_metadata(
        "azure_devops",
        request.pr_number,
        ado_org=ado_org,
        ado_project=ado_project,
        ado_repo=ado_repo,
        ado_access_token=access_token,
    )
    base_branch = pr_meta.base_branch

    # Fetch .outcomeops.yaml at the base branch tip to determine backend.
    repo_config: Optional[Dict[str, Any]] = None
    try:
        yaml_bytes = fetch_file_at_sha(
            "azure_devops",
            ".outcomeops.yaml",
            base_branch,
            ado_org=ado_org,
            ado_project=ado_project,
            ado_repo=ado_repo,
            ado_access_token=access_token,
            ado_version_type="branch",
        )
        repo_config = yaml.safe_load(yaml_bytes.decode("utf-8", errors="replace"))
    except SourceFetchError:
        logger.info(
            "[analyze-pr.ado] no .outcomeops.yaml at base branch in %s/%s/%s",
            ado_org, ado_project, ado_repo,
        )

    backend = get_backend_from_config(repo_config)
    logger.info(f"[analyze-pr.ado] Detected backend type: {backend}")

    # ADO's fetch_pr_metadata returns a plain list of changed-file
    # paths + a per-path status map (translated from ADO's changeType
    # to GitHub's status vocab in the shared shim). Wrap them in the
    # GitHubFile shape that parse_changed_files expects; a missing key
    # defaults to "modified" for parity with the pre-fix behavior.
    files = [
        GitHubFile(
            filename=path,
            status=pr_meta.file_change_types.get(path, "modified"),
            additions=0,
            deletions=0,
            changes=0,
        )
        for path in pr_meta.changed_files
    ]

    parse_result = parse_changed_files(
        files, backend=backend,
        source_roots=get_source_roots_from_config(repo_config),
    )
    changed_files = parse_result["changed_files"]
    checks_to_run = parse_result["checks_to_run"]

    logger.info(
        f"[analyze-pr.ado] Changed files: {len(changed_files)}, "
        f"Checks to run: {len(checks_to_run)}"
    )

    # Post initial "analysis started" comment (or a "no checks" comment
    # when nothing to run).
    if not checks_to_run:
        comment_body = (
            "**OutcomeOps Analysis:** No checks needed for this PR "
            "(no relevant files changed)\n\n---\n"
            "*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"
        )
        _upsert_ado_comment(
            repository=request.repository,
            ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
            pr_number=request.pr_number, access_token=access_token,
            role=COMMENT_ROLE_NO_CHECKS_NEEDED,
            body=comment_body,
        )
        return AnalyzePrResponse(
            message="No checks needed for this PR",
            pr_number=request.pr_number,
            checks_queued=0,
        ).dict()

    # Defer the PR summary on ADO. ADO orders PR threads by creation, so a
    # summary posted here (before the checks) sinks below the later per-check
    # threads. Persist the rendered body + expected check set instead; the
    # process-pr-check completion fan-in posts it as the newest thread once
    # every check finishes, so it lands on top (ADR-057). Non-fatal.
    try:
        summary_diffs, summary_truncated = _fetch_diffs_via_sha(
            provider="azure_devops",
            changed_files=pr_meta.changed_files,
            change_types=pr_meta.file_change_types,
            base_sha=pr_meta.base_sha,
            head_sha=pr_meta.head_sha,
            max_files=_SUMMARY_MAX_DIFF_FILES,
            ado_org=ado_org,
            ado_project=ado_project,
            ado_repo=ado_repo,
            ado_access_token=access_token,
        )
        _store_deferred_ado_summary(
            pk=_build_comment_pk("azure_devops", request.repository, request.pr_number),
            body=_build_summary_body(
                metadata=pr_meta,
                diffs_by_file=summary_diffs,
                checks_to_run=checks_to_run,
                truncated_files=summary_truncated,
            ),
            expected_checks=checks_to_run,
        )
    except Exception as exc:
        logger.warning(
            "[analyze-pr.summary] ADO summary defer failed for PR #%d: %s",
            request.pr_number, exc,
        )

    _upsert_ado_comment(
        repository=request.repository,
        ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
        pr_number=request.pr_number, access_token=access_token,
        role=COMMENT_ROLE_ANALYSIS_STARTED,
        body=_build_analysis_started_comment(checks_to_run),
    )

    # Queue check jobs to SQS with ADO context so process-pr-check
    # dispatches on provider="azure_devops".
    jobs_queued = queue_check_jobs(
        request.repository,
        request.pr_number,
        checks_to_run,
        changed_files,
        provider="azure_devops",
        connection_id=connection_id,
        ado_org=ado_org,
        ado_project=ado_project,
        ado_repo=ado_repo,
    )

    logger.info(
        f"[analyze-pr.ado] Successfully queued {jobs_queued} checks "
        f"for PR #{request.pr_number} in {request.repository}"
    )
    return AnalyzePrResponse(
        message=f"Analysis started for PR #{request.pr_number}",
        pr_number=request.pr_number,
        checks_queued=jobs_queued,
    ).dict()


def _handle_gitlab(
    request: AnalyzePrRequest, start_time: float,
) -> Dict[str, Any]:
    """Handle a GitLab analyze-pr (MR) request.

    Mirrors ``_handle_azure_devops`` through the shared primitives:
    - Obtain the customer's access token via the cached single-flight
      path (ADR-050 v1.1 -- GitLab rotates refresh tokens on every
      refresh; a parallel raw refresh here would race the sync workers).
    - Fetch MR metadata (head/base SHAs, target branch, changed files
      with explicit change-type booleans) via shared.pr_metadata.
    - Fetch .outcomeops.yaml at the target-branch tip (GitLab's ref
      slot takes a branch name or SHA uniformly).
    - Parse changed files -> checks to run.
    - Upsert the "analysis started" note (collapsed <details>; PUT the
      previous run's note in place on re-runs).
    - Queue CheckJobs with provider="gitlab" + connection_id.

    Structural-impact block is NOT posted for GitLab in v1 (same as
    ADO -- it depends on GitHub-specific data plumbing).
    """
    from shared.oauth.gitlab import (
        get_valid_access_token, instance_for_connection)
    from shared.pr_metadata import fetch_pr_metadata
    from shared.source_fetch import fetch_file_at_sha, SourceFetchError

    repository = request.repository  # full project path
    connection_id = request.connection_id

    workspaces_table = os.environ.get("WORKSPACES_TABLE", "")
    kms_key_arn = os.environ.get("OAUTH_KMS_KEY_ARN", "")
    if not (workspaces_table and kms_key_arn):
        raise RuntimeError(
            "GitLab OAuth not configured "
            "(WORKSPACES_TABLE / OAUTH_KMS_KEY_ARN env vars missing)"
        )
    # Which GitLab instance this connection targets (multi-instance, ADR-050
    # v2.0). base_url flows into every API + note call so a self-managed
    # connection is analyzed against the right host.
    gl_instance = instance_for_connection(workspaces_table, connection_id)
    gl_base = gl_instance.base_url
    access_token = get_valid_access_token(
        workspaces_table, kms_key_arn, connection_id,
        instance=gl_instance,
    )
    if not access_token:
        raise RuntimeError(
            "Failed to obtain GitLab access token; reconnect_required"
        )

    pr_meta = fetch_pr_metadata(
        "gitlab",
        request.pr_number,
        gitlab_project_path=repository,
        gitlab_access_token=access_token,
        gitlab_base_url=gl_base,
    )
    base_branch = pr_meta.base_branch

    repo_config: Optional[Dict[str, Any]] = None
    try:
        yaml_bytes = fetch_file_at_sha(
            "gitlab",
            ".outcomeops.yaml",
            base_branch,
            gitlab_project_path=repository,
            gitlab_access_token=access_token,
            gitlab_base_url=gl_base,
        )
        repo_config = yaml.safe_load(yaml_bytes.decode("utf-8", errors="replace"))
    except SourceFetchError:
        logger.info(
            "[analyze-pr.gitlab] no .outcomeops.yaml at %s in %s",
            base_branch, repository,
        )

    backend = get_backend_from_config(repo_config)
    logger.info(f"[analyze-pr.gitlab] Detected backend type: {backend}")

    # GitLab's diffs endpoint carries explicit change-type booleans,
    # already translated to GitHub status vocab by the shared shim.
    files = [
        GitHubFile(
            filename=path,
            status=pr_meta.file_change_types.get(path, "modified"),
            additions=0,
            deletions=0,
            changes=0,
        )
        for path in pr_meta.changed_files
    ]

    parse_result = parse_changed_files(
        files, backend=backend,
        source_roots=get_source_roots_from_config(repo_config),
    )
    changed_files = parse_result["changed_files"]
    checks_to_run = parse_result["checks_to_run"]

    logger.info(
        f"[analyze-pr.gitlab] Changed files: {len(changed_files)}, "
        f"Checks to run: {len(checks_to_run)}"
    )

    if not checks_to_run:
        comment_body = (
            "**OutcomeOps Analysis:** No checks needed for this MR "
            "(no relevant files changed)\n\n---\n"
            "*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*"
        )
        _upsert_gitlab_comment(
            repository=repository,
            pr_number=request.pr_number,
            access_token=access_token,
            base_url=gl_base,
            role=COMMENT_ROLE_NO_CHECKS_NEEDED,
            body=comment_body,
        )
        return AnalyzePrResponse(
            message="No checks needed for this MR",
            pr_number=request.pr_number,
            checks_queued=0,
        ).dict()

    # Post the PR summary comment just above the analysis-started status.
    # Non-fatal -- additive reviewer context; only posted when checks will run.
    try:
        summary_diffs, summary_truncated = _fetch_diffs_via_sha(
            provider="gitlab",
            changed_files=pr_meta.changed_files,
            change_types=pr_meta.file_change_types,
            base_sha=pr_meta.base_sha,
            head_sha=pr_meta.head_sha,
            max_files=_SUMMARY_MAX_DIFF_FILES,
            gitlab_project_path=repository,
            gitlab_access_token=access_token,
            gitlab_base_url=gl_base,
        )
        _upsert_gitlab_comment(
            repository=repository,
            pr_number=request.pr_number,
            access_token=access_token,
            base_url=gl_base,
            role=COMMENT_ROLE_SUMMARY,
            body=_build_summary_body(
                metadata=pr_meta,
                diffs_by_file=summary_diffs,
                checks_to_run=checks_to_run,
                truncated_files=summary_truncated,
            ),
        )
    except Exception as exc:
        logger.warning(
            "[analyze-pr.summary] GitLab summary failed for MR !%d: %s",
            request.pr_number, exc,
        )

    _upsert_gitlab_comment(
        repository=repository,
        pr_number=request.pr_number,
        access_token=access_token,
        base_url=gl_base,
        role=COMMENT_ROLE_ANALYSIS_STARTED,
        body=_build_analysis_started_comment(checks_to_run),
    )

    jobs_queued = queue_check_jobs(
        repository,
        request.pr_number,
        checks_to_run,
        changed_files,
        provider="gitlab",
        connection_id=connection_id,
    )

    logger.info(
        f"[analyze-pr.gitlab] Successfully queued {jobs_queued} checks "
        f"for MR !{request.pr_number} in {repository}"
    )
    return AnalyzePrResponse(
        message=f"Analysis started for MR !{request.pr_number}",
        pr_number=request.pr_number,
        checks_queued=jobs_queued,
    ).dict()


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
