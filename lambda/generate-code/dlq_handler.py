# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Dead Letter Queue Handler for failed code generation steps.

When a step execution fails and ends up in the DLQ, this handler:
1. Parses the failed message to get repo/branch info
2. Checks if a PR already exists for the branch
3. Creates a draft PR with whatever code was generated before failure
4. Adds a comment to the GitHub issue explaining the failure

This ensures we don't lose partial work.
"""

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import requests

# Make shared/ importable; the deploy package includes lambda/shared/
# at zip root via terraform source_path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import (  # noqa: E402
    ACTION_CODE_GEN_DLQ_SALVAGE_PR_CREATED,
    log_admin_action,
)

from github_api import (
    create_pull_request,
    get_github_token,
    get_branch_head_sha,
    get_headers,
)
from models import StepExecutionMessage


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    so module load order (check_handlers before sys.path fix) is safe."""
    from shared.github_base import github_api_base
    return github_api_base()


GITHUB_API_BASE = f"{_github_api()}"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")


def get_open_pr_for_branch(
    repo_full_name: str,
    branch_name: str,
    github_token: str
) -> Optional[Dict[str, Any]]:
    """
    Check if an open PR already exists for a branch.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        branch_name: Branch name to check
        github_token: GitHub token

    Returns:
        PR data if exists, None otherwise
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls"
    headers = get_headers(github_token)
    params = {
        "head": f"{repo_full_name.split('/')[0]}:{branch_name}",
        "state": "open"
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        prs = response.json()
        return prs[0] if prs else None
    except Exception as e:
        logger.warning(f"[dlq] Failed to check for existing PR: {e}")
        return None


def add_issue_comment(
    repo_full_name: str,
    issue_number: int,
    body: str,
    github_token: str
) -> bool:
    """
    Add a comment to a GitHub issue.

    Args:
        repo_full_name: Repository full name (e.g., "owner/repo")
        issue_number: Issue number
        body: Comment body
        github_token: GitHub token

    Returns:
        True if successful, False otherwise
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/{issue_number}/comments"
    headers = get_headers(github_token)

    try:
        response = requests.post(
            url,
            headers=headers,
            json={"body": body},
            timeout=30
        )
        response.raise_for_status()
        logger.info(f"[dlq] Added comment to issue #{issue_number}")
        return True
    except Exception as e:
        logger.warning(f"[dlq] Failed to add issue comment: {e}")
        return False


def handle_dlq_message(message: StepExecutionMessage) -> Dict[str, Any]:
    """
    Handle a failed code generation message from the DLQ.

    Args:
        message: The failed step execution message

    Returns:
        dict: Result of DLQ processing
    """
    logger.info(
        f"[dlq] Processing failed message for issue #{message.issue_number} "
        f"step {message.current_step}/{message.total_steps}"
    )

    github_token = get_github_token()

    # Check if branch exists (it should if any steps ran)
    branch_sha = get_branch_head_sha(
        repo_full_name=message.repo_full_name,
        branch_name=message.branch_name,
        github_token=github_token
    )

    if not branch_sha:
        logger.warning(f"[dlq] Branch {message.branch_name} does not exist, nothing to salvage")
        return {
            "success": False,
            "reason": "branch_not_found",
            "issue_number": message.issue_number
        }

    # Check if PR already exists
    existing_pr = get_open_pr_for_branch(
        repo_full_name=message.repo_full_name,
        branch_name=message.branch_name,
        github_token=github_token
    )

    if existing_pr:
        logger.info(f"[dlq] PR already exists: {existing_pr.get('html_url')}")

        return {
            "success": True,
            "reason": "pr_already_exists",
            "pr_url": existing_pr.get("html_url"),
            "pr_number": existing_pr.get("number"),
            "issue_number": message.issue_number
        }

    # Create PR with failure note
    pr_title = f"[FAILED] {message.issue_title}"
    pr_body = f"""## Code Generation Failed

This PR was created automatically after code generation failed.

**Issue:** #{message.issue_number}
**Failed at:** Step {message.current_step} of {message.total_steps}

### What happened?
The code generation process encountered an error and could not complete.
This PR contains whatever code was successfully generated before the failure.

### Next steps
1. Review the generated code in this PR
2. Check the plan file in `issues/` for step details
3. Manually complete the remaining steps or retry the generation

---
*This PR was created by the DLQ handler to preserve partial work.*
"""

    try:
        pr_result = create_pull_request(
            repo_full_name=message.repo_full_name,
            title=pr_title,
            body=pr_body,
            head_branch=message.branch_name,
            base_branch=message.base_branch,
            github_token=github_token
        )

        pr_url = pr_result.get("html_url")
        pr_number = pr_result.get("number")

        logger.info(f"[dlq] Created salvage PR: {pr_url}")

        # Emit ACTION_CODE_GEN_DLQ_SALVAGE_PR_CREATED audit row.
        # plan/audit-completeness . Salvage PRs only appear after
        # 4 retries failed; flagging them as a distinct action lets
        # operators monitor codegen reliability.
        try:
            audit_table = os.environ.get("AUDIT_TABLE", "")
            if audit_table:
                log_admin_action(
                    audit_table,
                    ACTION_CODE_GEN_DLQ_SALVAGE_PR_CREATED,
                    workspace_id="",
                    user_email="",
                    source_ip="",
                    user_agent="",
                    details={
                        "repo": message.repo_full_name,
                        "issue_number": message.issue_number,
                        "branch": message.branch_name,
                        "salvage_pr_url": pr_url or "",
                        "salvage_pr_number": pr_number,
                        "failed_step": message.current_step,
                        "total_steps": message.total_steps,
                    },
                )
        except Exception as e:
            logger.warning(f"[dlq] Failed to emit CODE_GEN_DLQ_SALVAGE_PR_CREATED: {e}")

        # Add comment to the issue
        try:
            comment_body = (
                f"Code generation failed at step {message.current_step} of {message.total_steps}.\n\n"
                f"A PR has been created with the partial work: {pr_url}\n\n"
                f"Please review the generated code and complete the remaining steps manually, "
                f"or retry the generation by re-adding the `approved-for-generation` label."
            )
            add_issue_comment(
                repo_full_name=message.repo_full_name,
                issue_number=message.issue_number,
                body=comment_body,
                github_token=github_token
            )
            logger.info(f"[dlq] Added failure comment to issue #{message.issue_number}")
        except Exception as e:
            logger.warning(f"[dlq] Failed to add issue comment: {e}")

        return {
            "success": True,
            "reason": "pr_created",
            "pr_url": pr_url,
            "pr_number": pr_number,
            "issue_number": message.issue_number
        }

    except Exception as e:
        logger.error(f"[dlq] Failed to create salvage PR: {e}")
        return {
            "success": False,
            "reason": "pr_creation_failed",
            "error": str(e),
            "issue_number": message.issue_number
        }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for code generation DLQ.

    Triggered by messages in the code-generation-dlq SQS queue.

    Args:
        event: SQS event with failed messages
        context: Lambda context

    Returns:
        dict: Processing results
    """
    logger.info(f"[dlq] Received {len(event.get('Records', []))} DLQ messages")

    results: List[Dict[str, Any]] = []

    for record in event.get("Records", []):
        try:
            # Parse the message body
            body = json.loads(record.get("body", "{}"))
            message = StepExecutionMessage(**body)

            result = handle_dlq_message(message)
            results.append(result)

        except Exception as e:
            logger.error(f"[dlq] Failed to process DLQ message: {e}")
            results.append({
                "success": False,
                "reason": "processing_error",
                "error": str(e)
            })

    # Summary
    successful = sum(1 for r in results if r.get("success"))
    logger.info(f"[dlq] Processed {len(results)} messages, {successful} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "processed": len(results),
            "successful": successful,
            "results": results
        })
    }
