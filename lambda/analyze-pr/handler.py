"""
Lambda handler for analyzing Pull Requests and queueing architecture checks.

This handler:
1. Fetches PR diff from GitHub API
2. Parses changed files to determine which checks to run
3. Queues check jobs to SQS FIFO queue for async processing
4. Posts initial comment to PR with analysis status
5. Checks: ADR Compliance, README Freshness, Test Coverage, Breaking Changes, Architectural Duplication

Input: { "pr_number": int, "repository": "owner/repo" }
Output: { "message": str, "pr_number": int, "checks_queued": int }

This Lambda is triggered manually or via GitHub webhooks.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Set
from enum import Enum

import boto3
import requests
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


def parse_changed_files(files: List[GitHubFile]) -> Dict[str, Any]:
    """
    Parse changed files and determine which checks to run.

    Checks are triggered based on file paths:
    - ADR_COMPLIANCE: Lambda handlers (lambda/**/*.py) or Terraform (terraform/**/*.tf)
    - README_FRESHNESS: Lambda/Terraform/docs changes
    - TEST_COVERAGE: New Lambda handlers
    - BREAKING_CHANGES: Handler or schema modifications
    - ARCHITECTURAL_DUPLICATION: New or modified Lambda handlers

    Args:
        files: List of GitHubFile objects

    Returns:
        Dict with "changed_files" list and "checks_to_run" list
    """
    changed_files = [f.filename for f in files]
    checks_to_run: Set[CheckType] = set()

    for file in files:
        filepath = file.filename
        status = file.status

        # ADR Compliance: Lambda handlers or Terraform files
        if filepath.startswith("lambda/") and filepath.endswith(".py"):
            if not any(x in filepath for x in ["/tests/", "__pycache__", ".pyc"]):
                checks_to_run.add(CheckType.ADR_COMPLIANCE)

        if filepath.startswith("terraform/") and filepath.endswith(".tf"):
            checks_to_run.add(CheckType.ADR_COMPLIANCE)

        # README Freshness: Lambda/Terraform/docs changes
        if filepath.startswith(("lambda/", "terraform/", "docs/")):
            checks_to_run.add(CheckType.README_FRESHNESS)

        # Test Coverage: New Lambda handlers
        if filepath.startswith("lambda/") and filepath.endswith("/handler.py"):
            checks_to_run.add(CheckType.TEST_COVERAGE)

        # Breaking Changes: Handler modifications
        if filepath.startswith("lambda/") and filepath.endswith(".py"):
            if status in ["modified", "removed"] and "/handler.py" in filepath:
                checks_to_run.add(CheckType.BREAKING_CHANGES)

        # Architectural Duplication: New or modified handlers
        if filepath.startswith("lambda/") and filepath.endswith("/handler.py"):
            if status in ["added", "modified"]:
                checks_to_run.add(CheckType.ARCHITECTURAL_DUPLICATION)

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
    try:
        # Validate request
        request = AnalyzePrRequest(**event)
        logger.info(f"Analyzing PR #{request.pr_number} in {request.repository}")

        # Fetch GitHub token
        token = get_github_token()

        # Fetch PR details
        pr = fetch_pull_request(request.repository, request.pr_number, token)

        # Fetch PR file changes
        files = fetch_pr_files(request.repository, request.pr_number, token)

        # Parse changed files and determine checks
        parse_result = parse_changed_files(files)
        changed_files = parse_result["changed_files"]
        checks_to_run = parse_result["checks_to_run"]

        logger.info(f"Changed files: {len(changed_files)}, Checks to run: {len(checks_to_run)}")

        # If no checks needed, post comment and return
        if not checks_to_run:
            comment_body = "**OutcomeOps Analysis:** No checks needed for this PR (no relevant files changed)"
            post_pr_comment(request.repository, request.pr_number, comment_body, token)

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

Results will be posted as comments when complete."""

        post_pr_comment(request.repository, request.pr_number, comment_body, token)

        # Queue check jobs to SQS
        jobs_queued = queue_check_jobs(request.repository, request.pr_number, checks_to_run, changed_files)

        response = AnalyzePrResponse(
            message=f"Analysis started for PR #{request.pr_number}",
            pr_number=request.pr_number,
            checks_queued=jobs_queued
        )

        logger.info(f"Successfully queued {jobs_queued} checks for PR #{request.pr_number}")
        return response.dict()

    except Exception as e:
        logger.error(f"Error analyzing PR: {str(e)}", exc_info=True)
        raise
