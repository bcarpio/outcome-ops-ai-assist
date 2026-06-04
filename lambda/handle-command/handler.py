"""
Lambda handler for processing PR comment commands.

This handler:
1. Parses commands from PR comments (e.g., "outcomeops: fix readme")
2. Validates license (commands are unlimited, tracked for analytics only)
3. Dispatches to appropriate command handlers
4. Posts results as PR comments

Input: {
    "command": "outcomeops: fix readme",
    "pr_number": 42,
    "repo": "owner/repo",
    "commenter": "username",
    "branch": "feature-branch"
}
Output: { "statusCode": 200, "body": { "message": str, "command": str } }

Triggered via GitHub Actions workflow on issue_comment events.
"""

import json
import logging
import os
import re
import sys
import time
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

import boto3
import requests
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import log_audit_entry
from shared.model_client import invoke as invoke_model

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients (initialized once per container)
ssm_client = boto3.client("ssm")
sqs_client = boto3.client("sqs")
# Bedrock LLM completions route via shared.model_client (ADR-035 chunk 11).
# The per-handler bedrock-runtime client was only used for the legacy
# InvokeModel call below and is no longer constructed here.

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
BASIC_MODEL_ID = os.environ.get(
    "BEDROCK_BASIC_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)


class CommandType(str, Enum):
    """Supported command types."""
    FIX_README = "fix readme"
    EXPLAIN_FAILED_TESTS = "explain failed tests"
    FIX_TESTS = "fix tests"
    FIX_ADR = "fix adr"
    HELP = "help"
    UNKNOWN = "unknown"


class HandleCommandRequest(BaseModel):
    """Request schema for handle-command Lambda."""
    command: str = Field(..., min_length=1, description="Full command string from PR comment")
    pr_number: int = Field(..., gt=0, description="Pull Request number")
    repo: str = Field(..., min_length=1, description="Repository in format 'owner/repo'")
    commenter: str = Field(..., min_length=1, description="GitHub username who issued command")
    branch: str = Field(..., min_length=1, description="PR branch name")

    @field_validator("repo")
    @classmethod
    def validate_repo_format(cls, v):
        """Validate repository is in owner/repo format."""
        if "/" not in v or v.count("/") != 1:
            raise ValueError("Repository must be in format 'owner/repo'")
        owner, repo = v.split("/")
        if not owner or not repo:
            raise ValueError("Owner and repo cannot be empty")
        return v


class HandleCommandResponse(BaseModel):
    """Response schema for handle-command Lambda."""
    message: str
    command: str
    command_type: str
    success: bool


def get_github_token() -> str:
    """Get GitHub token from SSM Parameter Store."""
    param_name = f"/{ENVIRONMENT}/{APP_NAME}/github/token"
    try:
        response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
        return response["Parameter"]["Value"]
    except Exception as e:
        logger.error(f"Failed to get GitHub token from SSM: {e}")
        raise


def get_queue_url() -> str:
    """Get SQS queue URL from SSM Parameter Store."""
    param_name = f"/{ENVIRONMENT}/{APP_NAME}/sqs/code-generation-queue-url"
    try:
        response = ssm_client.get_parameter(Name=param_name, WithDecryption=False)
        return response["Parameter"]["Value"]
    except ClientError as e:
        logger.error(f"Failed to get queue URL from SSM: {e}")
        raise


def send_pr_command_message(
    action: str,
    pr_number: int,
    repo_full_name: str,
    branch_name: str,
    commenter: str,
    base_branch: str = "main",
    guidance: str = None
) -> Dict[str, Any]:
    """
    Send PR command message to SQS for async processing.

    Args:
        action: Command action (fix_readme, fix_tests, etc.)
        pr_number: PR number
        repo_full_name: Repository in owner/repo format
        branch_name: PR branch name
        commenter: GitHub username who issued the command
        base_branch: Base branch (default: main)
        guidance: Optional user-provided guidance for the command

    Returns:
        SQS SendMessage response
    """
    queue_url = get_queue_url()

    message_data = {
        "action": action,
        "pr_number": pr_number,
        "repo_full_name": repo_full_name,
        "branch_name": branch_name,
        "commenter": commenter,
        "base_branch": base_branch
    }
    if guidance:
        message_data["guidance"] = guidance

    message_body = json.dumps(message_data)

    try:
        response = sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=message_body,
            MessageGroupId=f"pr-{pr_number}",
            MessageDeduplicationId=str(uuid4())
        )

        logger.info(
            f"Sent {action} command to SQS for PR #{pr_number}. "
            f"MessageId: {response['MessageId']}"
        )
        return response

    except ClientError as e:
        logger.error(f"Failed to send message to SQS: {e}")
        raise


def post_pr_comment(repo: str, pr_number: int, body: str, github_token: str) -> bool:
    """Post a comment to a GitHub PR."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.post(url, headers=headers, json={"body": body}, timeout=30)
        response.raise_for_status()
        logger.info(f"Posted comment to PR #{pr_number} in {repo}")
        return True
    except Exception as e:
        logger.error(f"Failed to post PR comment: {e}")
        return False


def fetch_test_failure_comment(repo: str, pr_number: int, github_token: str) -> Optional[str]:
    """
    Fetch the test failure comment from PR comments.

    Looks for comments with the outcomeops:test-results marker.

    Returns:
        Test output string if found, None otherwise
    """
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        comments = response.json()

        # Find the most recent test results comment
        for comment in reversed(comments):
            body = comment.get("body", "")
            if "<!-- outcomeops:test-results -->" in body:
                # Extract test output from the comment
                # Look for content between ``` markers after "Test Output"
                import re
                match = re.search(r"### Test Output.*?```\n?(.*?)```", body, re.DOTALL)
                if match:
                    return match.group(1).strip()
                # Fallback: return the whole body if no code block found
                return body

        logger.info(f"No test failure comment found for PR #{pr_number}")
        return None

    except Exception as e:
        logger.error(f"Failed to fetch PR comments: {e}")
        return None


def analyze_test_failures(test_output: str) -> str:
    """
    Use Claude to analyze test failures and provide explanations.

    Args:
        test_output: Raw test output from pytest

    Returns:
        Markdown-formatted analysis of the failures
    """
    prompt = f"""Analyze the following test failure output and provide a clear, actionable explanation for each distinct type of failure.

For each failure type:
1. Identify the root cause
2. Explain what's happening technically
3. Suggest how to fix it

Group similar failures together (e.g., all Float/Decimal errors are one group).

Test Output:
```
{test_output}
```

Format your response as markdown with clear headers for each failure type. Be concise but thorough."""

    try:
        bedrock_start = time.time()
        resp = invoke_model(
            model_id=BASIC_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            max_tokens=2000,
        )
        bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

        analysis = resp.text

        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="handle-command",
            model_id=BASIC_MODEL_ID,
            input_text=prompt,
            output_text=analysis,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            duration_ms=bedrock_duration_ms,
        )

        return analysis

    except Exception as e:
        logger.error(f"Failed to analyze test failures with Claude: {e}")
        return f"Failed to analyze test failures: {str(e)}"


def handle_explain_failed_tests_command(
    repo: str,
    pr_number: int,
    github_token: str
) -> str:
    """
    Handle the 'explain failed tests' command.

    Fetches test failure output from PR comments and uses Claude to analyze it.
    """
    # Fetch test failure comment
    test_output = fetch_test_failure_comment(repo, pr_number, github_token)

    if not test_output:
        message = """## OutcomeOps: Explain Failed Tests

No test failure results found for this PR.

This could mean:
- Tests haven't run yet
- Tests passed (no failures to explain)
- The test results comment was deleted

If tests failed, make sure the OutcomeOps run-tests Lambda posted results.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""
        post_pr_comment(repo, pr_number, message, github_token)
        return "No test failures found to explain"

    # Analyze the failures
    analysis = analyze_test_failures(test_output)

    message = f"""<!-- outcomeops:test-analysis -->
## OutcomeOps: Test Failure Analysis

{analysis}

---
**Next Steps:**
1. Review the analysis above
2. Fix the tests:
   - `outcomeops: fix tests` - I'll fix based on my analysis above
   - `outcomeops: fix tests: <your instructions>` - Your instructions take priority, my analysis becomes context
3. If the code itself needs fixing (not the tests): Make changes and push

*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

    post_pr_comment(repo, pr_number, message, github_token)
    return f"Posted test failure analysis for PR #{pr_number}"


def fetch_test_analysis_comment(repo: str, pr_number: int, github_token: str) -> Optional[str]:
    """
    Fetch the most recent test analysis comment from PR comments.

    Looks for comments with the outcomeops:test-analysis marker.

    Returns:
        Analysis content if found, None otherwise
    """
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        comments = response.json()

        # Find the most recent test analysis comment
        for comment in reversed(comments):
            body = comment.get("body", "")
            if "<!-- outcomeops:test-analysis -->" in body:
                # Extract the analysis content (between marker and "---" separator)
                import re
                match = re.search(
                    r"## OutcomeOps: Test Failure Analysis\s*\n(.*?)\n---",
                    body,
                    re.DOTALL
                )
                if match:
                    return match.group(1).strip()
                # Fallback: return body without marker
                return body.replace("<!-- outcomeops:test-analysis -->", "").strip()

        logger.info(f"No test analysis comment found for PR #{pr_number}")
        return None

    except Exception as e:
        logger.error(f"Failed to fetch PR comments: {e}")
        return None


def handle_fix_tests_command(
    repo: str,
    pr_number: int,
    branch: str,
    commenter: str,
    guidance: str,
    github_token: str
) -> str:
    """
    Handle the 'fix tests' command by dispatching to async processor.

    Sends a message to SQS for the generate-code Lambda to process.
    Uses the test analysis from PR comments as context.
    """
    # Fetch the test analysis - this is our primary context
    analysis = fetch_test_analysis_comment(repo, pr_number, github_token)

    if not analysis:
        message = """## OutcomeOps: Fix Tests

No test analysis found for this PR.

Please run `outcomeops: explain failed tests` first to analyze the failures, then run `outcomeops: fix tests`.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""
        post_pr_comment(repo, pr_number, message, github_token)
        return "Fix tests requires analysis first"

    # Send to SQS for async processing
    send_pr_command_message(
        action="fix_tests",
        pr_number=pr_number,
        repo_full_name=repo,
        branch_name=branch,
        commenter=commenter,
        base_branch="main",
        guidance=guidance if guidance else None
    )

    # Build acknowledgment message
    guidance_note = ""
    if guidance:
        guidance_note = f"\n\n**Additional guidance:** {guidance}"

    # Post acknowledgment comment
    message = f"""## OutcomeOps: Fix Tests

Processing test fixes for PR #{pr_number}...

I found the test analysis and will apply fixes based on it.{guidance_note}

This may take a minute. I'll post another comment when complete.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

    post_pr_comment(repo, pr_number, message, github_token)
    return f"Dispatched fix tests command for PR #{pr_number}"


def parse_command(command_text: str) -> tuple[CommandType, str]:
    """
    Parse a command string to extract the command type.

    Args:
        command_text: Full command string (e.g., "outcomeops: fix readme")

    Returns:
        Tuple of (CommandType, cleaned_command_string)
    """
    # Remove the "outcomeops:" prefix and normalize
    match = re.match(r"outcomeops:\s*(.+)", command_text.strip(), re.IGNORECASE)
    if not match:
        return CommandType.UNKNOWN, command_text

    action = match.group(1).strip().lower()

    if action in ("fix readme", "fix-readme", "fixreadme"):
        return CommandType.FIX_README, "fix readme"
    elif action in ("explain failed tests", "explain-failed-tests", "explain tests", "explain-tests"):
        return CommandType.EXPLAIN_FAILED_TESTS, "explain failed tests"
    elif action.startswith("fix tests:") or action.startswith("fix-tests:"):
        # Extract guidance after "fix tests:"
        guidance = action.split(":", 1)[1].strip() if ":" in action else ""
        return CommandType.FIX_TESTS, guidance
    elif action in ("fix tests", "fix-tests", "fixtests"):
        # No guidance provided
        return CommandType.FIX_TESTS, ""
    elif action.startswith("fix adr:") or action.startswith("fix-adr:"):
        # Extract guidance after "fix adr:"
        guidance = action.split(":", 1)[1].strip() if ":" in action else ""
        return CommandType.FIX_ADR, guidance
    elif action in ("fix adr", "fix-adr", "fixadr"):
        # No guidance provided
        return CommandType.FIX_ADR, ""
    elif action in ("help", "?"):
        return CommandType.HELP, "help"
    else:
        return CommandType.UNKNOWN, action


def handle_help_command(repo: str, pr_number: int, github_token: str) -> str:
    """Handle the 'help' command by posting available commands."""
    help_text = """## OutcomeOps Commands

Available commands (comment on a PR):

| Command | Description |
|---------|-------------|
| `outcomeops: help` | Show this help message |
| `outcomeops: fix readme` | Update README based on code changes in this PR |
| `outcomeops: explain failed tests` | Analyze test failures and post analysis |
| `outcomeops: fix tests` | Fix tests using the previous failure analysis |
| `outcomeops: fix tests: <guidance>` | Fix tests with specific guidance |
| `outcomeops: fix adr` | Update ADR documentation based on architectural changes |
| `outcomeops: fix license` | Fix license headers and compliance issues |
| `outcomeops: regenerate` | Regenerate the entire PR (code, tests, docs) |

**Fixing Tests Workflow:**
1. Run `outcomeops: explain failed tests` to get an analysis
2. Run `outcomeops: fix tests` to apply fixes based on that analysis
3. Or run `outcomeops: fix tests: <your guidance>` to provide specific instructions

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

    post_pr_comment(repo, pr_number, help_text, github_token)
    return "Posted help message"


def handle_fix_readme_command(
    repo: str,
    pr_number: int,
    branch: str,
    commenter: str,
    github_token: str
) -> str:
    """
    Handle the 'fix readme' command by dispatching to async processor.

    Sends a message to SQS for the generate-code Lambda to process.
    The async handler will:
    1. Get PR changed files
    2. Read current README
    3. Query KB for README patterns
    4. Call Claude to update README
    5. Commit and push changes
    6. Post result comment
    """
    # Send to SQS for async processing
    send_pr_command_message(
        action="fix_readme",
        pr_number=pr_number,
        repo_full_name=repo,
        branch_name=branch,
        commenter=commenter
    )

    # Post acknowledgment comment
    message = f"""## OutcomeOps: Fix README

Processing README update for PR #{pr_number}...

I'm analyzing the changes in this PR and will update the README to document any new Lambda handlers or infrastructure changes.

This may take a minute. I'll post another comment when the update is complete.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

    post_pr_comment(repo, pr_number, message, github_token)
    return f"Dispatched fix readme command for PR #{pr_number}"


def handle_fix_adr_command(
    repo: str,
    pr_number: int,
    branch: str,
    commenter: str,
    guidance: str,
    github_token: str
) -> str:
    """
    Handle the 'fix adr' command by dispatching to async processor.

    Sends a message to SQS for the generate-code Lambda to process.
    The async handler will:
    1. Analyze architectural changes in the PR
    2. Query KB for ADR patterns and existing ADRs
    3. Use Claude to generate/update ADR documentation
    4. Commit and push changes
    5. Post result comment
    """
    # Send to SQS for async processing
    send_pr_command_message(
        action="fix_adr",
        pr_number=pr_number,
        repo_full_name=repo,
        branch_name=branch,
        commenter=commenter,
        guidance=guidance if guidance else None
    )

    # Build acknowledgment message
    guidance_note = ""
    if guidance:
        guidance_note = f"\n\n**Additional guidance:** {guidance}"

    # Post acknowledgment comment
    message = f"""## OutcomeOps: Fix ADR

Processing ADR update for PR #{pr_number}...

I'm analyzing the architectural changes in this PR and will create or update ADR documentation as needed.{guidance_note}

This may take a minute. I'll post another comment when the update is complete.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

    post_pr_comment(repo, pr_number, message, github_token)
    return f"Dispatched fix adr command for PR #{pr_number}"


def handle_unknown_command(
    repo: str,
    pr_number: int,
    action: str,
    github_token: str
) -> str:
    """Handle unknown commands."""
    message = f"""## OutcomeOps: Unknown Command

I don't recognize the command: `{action}`

Run `outcomeops: help` to see available commands.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
"""

    post_pr_comment(repo, pr_number, message, github_token)
    return f"Unknown command: {action}"


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for processing PR comment commands.

    Args:
        event: Lambda event with command details
        context: Lambda context

    Returns:
        Response with command processing result
    """
    logger.info("Handle command invoked: %s", json.dumps(event))

    # Validate OutcomeOps license before processing
    try:
        from outcomeops_license import validate_license
        claims = validate_license()
        logger.info(
            f"[handle-command] License validated: {claims.get('sub')} "
            f"(tier: {claims.get('license', {}).get('tier')})"
        )
    except ImportError:
        logger.warning("[handle-command] License validator not available - layer not attached")
    except Exception as e:
        logger.error(f"[handle-command] License validation failed: {e}")
        return {
            "statusCode": 403,
            "body": json.dumps({"error": "License validation failed", "message": str(e)})
        }

    # Parse and validate request
    try:
        request = HandleCommandRequest(**event)
    except Exception as e:
        logger.error(f"Invalid request: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Invalid request", "details": str(e)})
        }

    # Get GitHub token
    try:
        github_token = get_github_token()
    except Exception as e:
        logger.error(f"Failed to get GitHub token: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to get GitHub credentials"})
        }

    # Parse command
    command_type, action = parse_command(request.command)
    logger.info(f"Parsed command: type={command_type}, action={action}")

    # Track command execution for analytics (commands are unlimited, no limit check)
    try:
        from outcomeops_license import track_command_executed
        track_command_executed(
            repo=request.repo,
            command=action,
            pr_number=request.pr_number
        )
    except ImportError:
        logger.warning("[handle-command] Usage tracking not available")
    except Exception as e:
        logger.warning(f"[handle-command] Failed to track command: {e}")

    # Dispatch to command handler
    if command_type == CommandType.HELP:
        result_message = handle_help_command(request.repo, request.pr_number, github_token)
        success = True
    elif command_type == CommandType.FIX_README:
        result_message = handle_fix_readme_command(
            request.repo,
            request.pr_number,
            request.branch,
            request.commenter,
            github_token
        )
        success = True
    elif command_type == CommandType.EXPLAIN_FAILED_TESTS:
        result_message = handle_explain_failed_tests_command(
            request.repo,
            request.pr_number,
            github_token
        )
        success = True
    elif command_type == CommandType.FIX_TESTS:
        result_message = handle_fix_tests_command(
            request.repo,
            request.pr_number,
            request.branch,
            request.commenter,
            action,  # This is the guidance extracted by parse_command
            github_token
        )
        success = True
    elif command_type == CommandType.FIX_ADR:
        result_message = handle_fix_adr_command(
            request.repo,
            request.pr_number,
            request.branch,
            request.commenter,
            action,  # This is the guidance extracted by parse_command
            github_token
        )
        success = True
    else:
        result_message = handle_unknown_command(
            request.repo,
            request.pr_number,
            action,
            github_token
        )
        success = False

    response = HandleCommandResponse(
        message=result_message,
        command=request.command,
        command_type=command_type.value,
        success=success
    )

    return {
        "statusCode": 200,
        "body": json.dumps(response.model_dump())
    }
