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
from datetime import datetime
from typing import Any, Dict, List, Literal
from enum import Enum

import boto3
import requests
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

# Import check handlers
from check_handlers import (
    check_adr_compliance,
    check_architectural_duplication,
    check_breaking_changes,
    check_readme_freshness,
    check_test_coverage,
)

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


class CheckType(str, Enum):
    """Types of checks that can be performed on a PR."""
    ADR_COMPLIANCE = "ADR_COMPLIANCE"
    README_FRESHNESS = "README_FRESHNESS"
    TEST_COVERAGE = "TEST_COVERAGE"
    BREAKING_CHANGES = "BREAKING_CHANGES"
    ARCHITECTURAL_DUPLICATION = "ARCHITECTURAL_DUPLICATION"


class CheckStatus(str, Enum):
    """Status of a check result."""
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class CheckJob(BaseModel):
    """Check job schema (from SQS message)."""
    check_type: CheckType = Field(..., alias="checkType")
    pr_number: int = Field(..., alias="pr_number", gt=0)
    repository: str
    changed_files: List[str] = Field(..., alias="changedFiles")

    class Config:
        populate_by_name = True


class CheckResult(BaseModel):
    """Check result schema (stored in DynamoDB)."""
    PK: str
    SK: str
    check_type: CheckType = Field(..., alias="checkType")
    status: CheckStatus
    message: str
    details: List[str]
    timestamp: str
    comment_url: str | None = Field(None, alias="commentUrl")

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


def store_check_result(result: CheckResult) -> None:
    """
    Store check result in DynamoDB.

    Args:
        result: CheckResult object to store

    Raises:
        ClientError: If DynamoDB operation fails
    """
    table = dynamodb.Table(DYNAMODB_TABLE)

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

    try:
        table.put_item(Item=item)
        logger.info(f"Stored check result: {result.PK} / {result.SK}")

    except ClientError as e:
        logger.error(f"Failed to store check result in DynamoDB: {e}")
        raise


def run_check(job: CheckJob) -> Dict[str, Any]:
    """
    Route to appropriate check handler based on check type.

    Args:
        job: CheckJob with check type and PR metadata

    Returns:
        Check result dict (without PK/SK/timestamp/commentUrl)

    Raises:
        ValueError: If check type is unknown
    """
    github_token_param = f"/{ENVIRONMENT}/{APP_NAME}/github/token"

    if job.check_type == CheckType.ADR_COMPLIANCE:
        return check_adr_compliance(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            github_token_param=github_token_param
        )

    elif job.check_type == CheckType.README_FRESHNESS:
        return check_readme_freshness(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            github_token_param=github_token_param
        )

    elif job.check_type == CheckType.TEST_COVERAGE:
        return check_test_coverage(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files
        )

    elif job.check_type == CheckType.BREAKING_CHANGES:
        return check_breaking_changes(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME
        )

    elif job.check_type == CheckType.ARCHITECTURAL_DUPLICATION:
        return check_architectural_duplication(
            check_type=job.check_type.value,
            pr_number=job.pr_number,
            repository=job.repository,
            changed_files=job.changed_files,
            query_kb_lambda_name=QUERY_KB_LAMBDA_NAME,
            github_token_param=github_token_param
        )

    else:
        raise ValueError(f"Unknown check type: {job.check_type}")


def format_check_comment(result: CheckResult) -> str:
    """
    Format check result as GitHub PR comment.

    Args:
        result: CheckResult to format

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

    comment += f"\n_Check completed at {result.timestamp}_"

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
    logger.info(f"Processing check job: {job.check_type.value} for PR #{job.pr_number}")

    # Run the appropriate check
    check_result = run_check(job)

    # Build full result
    timestamp = datetime.utcnow().isoformat() + "Z"
    result = CheckResult(
        PK=f"PR#{job.pr_number}",
        SK=f"CHECK#{job.check_type.value.lower()}",
        checkType=job.check_type,
        status=CheckStatus(check_result["status"]),
        message=check_result["message"],
        details=check_result["details"],
        timestamp=timestamp,
    )

    # Store result in DynamoDB
    store_check_result(result)

    # Post result as PR comment
    token = get_github_token()
    comment = format_check_comment(result)
    comment_response = post_pr_comment(job.repository, job.pr_number, comment, token)

    # Update result with comment URL
    comment_url = comment_response.get("html_url", "")
    result.comment_url = comment_url

    # Update DynamoDB with comment URL
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
