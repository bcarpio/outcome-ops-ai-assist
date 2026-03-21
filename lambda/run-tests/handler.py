"""
Lambda handler that runs repository tests after code generation completes.

Flow:
1. Triggered by EventBridge event emitted by generate-code Lambda
2. Clones the target branch using GitHub PAT from SSM
3. Detects backend type from .outcomeops.yaml and uses appropriate test backend
4. Runs tests using the backend (Python, Java, TypeScript, etc.)
5. If no backend available, creates PR and lets repository CI handle tests
6. Uploads logs/artifacts to S3 and emits a follow-up EventBridge event summarizing the run
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError

# Import test backends using importlib to avoid conflicts with generate-code-maps/backends
import importlib.util

_run_tests_dir = os.path.dirname(os.path.abspath(__file__))


def _import_backends_module():
    """Import run-tests backends avoiding conflicts with other backends packages."""
    backends_init = os.path.join(_run_tests_dir, "backends", "__init__.py")
    spec = importlib.util.spec_from_file_location("run_tests_backends", backends_init)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_tests_backends"] = module
    spec.loader.exec_module(module)
    return module


_backends_module = _import_backends_module()
get_backend = _backends_module.get_backend
TestResult = _backends_module.TestResult

# Import shared auto-fix module for cost tracking and Claude-based fixes
from auto_fix import (
    reset_autofix_cost,
    get_autofix_cost,
    _accumulate_autofix_cost,
    query_knowledge_base,
    query_kb_for_fix,
    apply_fix_to_file,
    parse_error_file_path,
    SONNET_PRICE_PER_MILLION_INPUT_TOKENS,
    SONNET_PRICE_PER_MILLION_OUTPUT_TOKENS,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
TEST_RESULTS_BUCKET = os.environ.get("TEST_RESULTS_BUCKET")
TEST_RESULTS_PREFIX = os.environ.get("TEST_RESULTS_PREFIX", "test-results")
GITHUB_TOKEN_PARAM = os.environ.get(
    "GITHUB_TOKEN_PARAM", f"/{ENV}/{APP_NAME}/github/token"
)
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
TEST_COMMAND = os.environ.get("TEST_COMMAND", "make test")
MAX_COMMAND_SECONDS = int(os.environ.get("MAX_COMMAND_SECONDS", "900"))

if not TEST_RESULTS_BUCKET:
    raise ValueError("TEST_RESULTS_BUCKET environment variable is required")

# AWS clients
ssm_client = boto3.client("ssm")
s3_client = boto3.client("s3")
events_client = boto3.client("events")
lambda_client = boto3.client("lambda")

# Bedrock client for Claude API (auto-fix) - used by handler's direct auto-fix functions
bedrock_config = Config(
    connect_timeout=60,
    read_timeout=300,  # 5 minutes for auto-fix generation
    retries={'max_attempts': 2}
)
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
MAX_FIX_ATTEMPTS = 2  # Maximum auto-fix retry attempts


# ============================================================================
# Data models
# ============================================================================


class CodeGenerationCompletedDetail(BaseModel):
    """EventBridge detail payload emitted by generate-code Lambda."""

    issueNumber: int = Field(..., alias="issueNumber")
    issueTitle: str = Field(..., alias="issueTitle")
    repoFullName: str = Field(..., alias="repoFullName")
    branchName: str = Field(..., alias="branchName")
    baseBranch: str = Field(..., alias="baseBranch")
    prNumber: Optional[int] = Field(default=None, alias="prNumber")  # Created by run-tests if None
    prUrl: Optional[str] = Field(default=None, alias="prUrl")  # Created by run-tests if None
    planFile: Optional[str] = Field(default=None, alias="planFile")  # Optional for PR commands
    commitSha: Optional[str] = Field(default=None, alias="commitSha")
    eventVersion: Optional[str] = Field(default=None, alias="eventVersion")
    environment: str = Field(..., alias="environment")
    appName: str = Field(..., alias="appName")
    skipPlanUpdate: bool = Field(default=False, alias="skipPlanUpdate")  # Skip plan file operations

    class Config:
        populate_by_name = True


class TestRunEventDetail(BaseModel):
    """Result payload sent after test execution."""

    issueNumber: int
    branchName: str
    repoFullName: str
    prNumber: int
    prUrl: str
    status: str
    success: bool
    testCommand: str
    durationSeconds: float
    artifactBucket: str
    artifactPrefix: str
    logObjectKey: str
    junitObjectKey: Optional[str] = None
    failureReason: Optional[str] = None
    setupExitCode: Optional[int] = None
    testExitCode: Optional[int] = None
    logSnippet: Optional[str] = None
    eventVersion: str = "2024-11-09"
    environment: str
    appName: str


@dataclass
class CommandResult:
    """Result of a shell command execution."""

    command: List[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    def as_text(self) -> str:
        masked_command = " ".join(self.command)
        return (
            f"$ {masked_command}\n"
            f"(exit {self.exit_code}, {self.duration_seconds:.2f}s)\n"
            f"{self.stdout}\n{self.stderr}\n"
        )


# ============================================================================
# Helper functions
# ============================================================================


def mask_value(text: str, secrets: List[str]) -> str:
    """Mask sensitive values when logging commands."""
    masked = text
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, "***")
    return masked


def run_command(
    command: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    mask_terms: Optional[List[str]] = None
) -> CommandResult:
    """Execute a shell command and capture stdout/stderr."""
    timeout = timeout or MAX_COMMAND_SECONDS
    mask_terms = mask_terms or []

    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )
        duration = time.monotonic() - start
        masked_cmd = mask_value(" ".join(command), mask_terms)
        logger.info("Command finished (%ss): %s", round(duration, 2), masked_cmd)
        return CommandResult(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=duration
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        masked_cmd = mask_value(" ".join(command), mask_terms)
        logger.error("Command timed out after %ss: %s", round(duration, 2), masked_cmd)
        return CommandResult(
            command=command,
            exit_code=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nCommand timed out after {timeout}s",
            duration_seconds=duration
        )


def get_github_token() -> str:
    """Fetch GitHub PAT from SSM Parameter Store."""
    try:
        response = ssm_client.get_parameter(
            Name=GITHUB_TOKEN_PARAM,
            WithDecryption=True
        )
        token = response.get("Parameter", {}).get("Value")
        if not token:
            raise RuntimeError("GitHub token parameter returned empty value")
        return token
    except ClientError as exc:
        logger.exception("Failed to read GitHub token from SSM: %s", exc)
        raise


def get_github_usernames_to_tag() -> str:
    """Fetch GitHub usernames to tag from SSM Parameter Store."""
    param_name = f"/{ENV}/{APP_NAME}/config/github-usernames-to-tag"
    try:
        response = ssm_client.get_parameter(Name=param_name)
        usernames = response.get("Parameter", {}).get("Value", "")
        logger.info(f"[config] GitHub usernames to tag: {usernames}")
        return usernames
    except ClientError as exc:
        logger.warning(f"[config] Failed to read github-usernames-to-tag from SSM: {exc}")
        return ""


def create_workspace() -> str:
    """Create a temporary workspace under /tmp."""
    path = tempfile.mkdtemp(prefix="tests-workspace-")
    logger.info("Using workspace: %s", path)
    return path


def clone_repository(
    detail: CodeGenerationCompletedDetail,
    github_token: str,
    workspace: str,
    command_outputs: List[CommandResult]
) -> str:
    """Clone repository and checkout generated branch."""
    repo_url = f"https://x-access-token:{github_token}@github.com/{detail.repoFullName}.git"
    repo_dir = os.path.join(workspace, "repo")

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # GIT_EXEC_PATH is set via Lambda environment variables (from Terraform)

    clone_result = run_command(
        ["git", "clone", repo_url, repo_dir],
        env=env,
        mask_terms=[github_token]
    )
    command_outputs.append(clone_result)
    if not clone_result.succeeded:
        raise RuntimeError(f"git clone failed: {clone_result.stderr[:1000]}")

    fetch_result = run_command(
        ["git", "fetch", "origin", detail.branchName],
        cwd=repo_dir,
        env=env
    )
    command_outputs.append(fetch_result)
    if not fetch_result.succeeded:
        raise RuntimeError(f"git fetch failed: {fetch_result.stderr[:1000]}")

    checkout_result = run_command(
        ["git", "checkout", detail.branchName],
        cwd=repo_dir,
        env=env
    )
    command_outputs.append(checkout_result)
    if not checkout_result.succeeded:
        raise RuntimeError(f"git checkout failed: {checkout_result.stderr[:1000]}")

    # Remove token from remote to avoid leaking credentials in future logs
    remote_update = run_command(
        ["git", "remote", "set-url", "origin", f"https://github.com/{detail.repoFullName}.git"],
        cwd=repo_dir,
        env=env
    )
    command_outputs.append(remote_update)
    return repo_dir


def build_virtualenv(repo_dir: str) -> List[CommandResult]:
    """
    Create a Python virtual environment and install dependencies for all Lambdas + tests.

    Returns:
        List of CommandResult for logging.
    """
    results: List[CommandResult] = []
    venv_dir = os.path.join(repo_dir, "venv")

    results.append(
        run_command(
            ["python3.12", "-m", "venv", "venv"],
            cwd=repo_dir
        )
    )

    pip_bin = os.path.join(venv_dir, "bin", "pip")

    # Upgrade pip before installations
    results.append(
        run_command(
            [pip_bin, "install", "--upgrade", "pip"],
            cwd=repo_dir
        )
    )

    # Install all Lambda requirements
    requirements_dir = Path(repo_dir) / "lambda"
    for req_file in sorted(requirements_dir.rglob("requirements.txt")):
        rel = req_file.relative_to(repo_dir)
        logger.info("Installing requirements from %s", rel)
        results.append(
            run_command(
                [pip_bin, "install", "-r", str(req_file)],
                cwd=repo_dir
            )
        )

    # Install shared testing dependencies
    test_packages = [
        "pytest",
        "pytest-cov",
        "pytest-asyncio",
        "boto3",
        "botocore",
        "moto",
        "requests",
        "coverage"
    ]
    results.append(
        run_command(
            [pip_bin, "install", *test_packages],
            cwd=repo_dir
        )
    )

    logger.info("Virtual environment ready: %s", os.path.join(venv_dir, "bin", "python3.12"))
    return results


def read_file_if_exists(path: Path) -> Optional[str]:
    """Return file contents if path exists."""
    if path.exists():
        return path.read_text()
    return None


def create_pull_request(
    detail: CodeGenerationCompletedDetail,
    github_token: str,
    tests_passed: bool = True
) -> Dict[str, Any]:
    """
    Create a pull request after code generation.

    Args:
        detail: Event detail with repo and branch info
        github_token: GitHub PAT
        tests_passed: Whether tests passed (affects PR body)

    Returns:
        dict with pr_url and pr_number
    """
    pr_title = f"feat: {detail.issueTitle} (issue #{detail.issueNumber})"

    if tests_passed:
        test_status = "All tests passed successfully."
    else:
        test_status = "Tests failed - see comments below for details."

    pr_body = f"""## Summary
This PR implements the code generation for issue #{detail.issueNumber}.

**Issue:** {detail.issueTitle}
**Branch:** `{detail.branchName}`
**Implementation Plan:** See `{detail.planFile}`

## Test Results
{test_status}

---
Generated with OutcomeOps AI Assist
Closes #{detail.issueNumber}
"""

    url = f"https://api.github.com/repos/{detail.repoFullName}/pulls"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "title": pr_title,
        "body": pr_body,
        "head": detail.branchName,
        "base": detail.baseBranch
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()

    pr_data = response.json()
    logger.info(f"[run-tests] PR created: {pr_data['html_url']}")

    return {
        "pr_url": pr_data["html_url"],
        "pr_number": pr_data["number"]
    }


def trigger_pr_analyzer(pr_number: int, repository: str) -> None:
    """
    Trigger analyze-pr Lambda to analyze the newly created PR.

    Args:
        pr_number: PR number
        repository: Full repository name (owner/repo)
    """
    logger.info(f"[run-tests] Triggering PR analyzer for PR #{pr_number}")

    analyze_pr_function = f"{ENV}-{APP_NAME}-analyze-pr"
    payload = {
        "pr_number": pr_number,
        "repository": repository
    }

    try:
        response = lambda_client.invoke(
            FunctionName=analyze_pr_function,
            InvocationType="Event",  # Async invocation
            Payload=json.dumps(payload)
        )
        logger.info(f"[run-tests] PR analyzer triggered: StatusCode={response['StatusCode']}")
    except Exception as e:
        logger.error(f"[run-tests] Failed to trigger PR analyzer: {e}", exc_info=True)


def post_test_failure_comment(
    detail: CodeGenerationCompletedDetail,
    github_token: str,
    test_output: str,
    failure_reason: str,
    github_usernames: str,
    s3_log_url: Optional[str] = None
) -> None:
    """
    Post a comment on the PR for logic errors that require human review.

    Args:
        detail: Event detail with PR info
        github_token: GitHub PAT
        test_output: Test output/error details
        failure_reason: Short description of failure
        github_usernames: Comma-separated usernames to tag
        s3_log_url: Optional S3 URL to full logs
    """
    if not detail.prNumber:
        logger.warning("[run-tests] Cannot post comment - no PR number")
        return

    # Build the @mentions
    mentions = ""
    if github_usernames:
        usernames_list = [u.strip() for u in github_usernames.split(",") if u.strip()]
        mentions = " ".join([f"@{u}" for u in usernames_list])

    # Truncate test output for comment
    truncated_output = test_output[-2000:] if len(test_output) > 2000 else test_output

    comment_body = f"""<!-- outcomeops:test-results -->
## ⚠️ Tests Failed - Human Review Required

{mentions}

The automated test run encountered a **logic error** that requires human review.

### Failure Details
- **Reason:** {failure_reason}
- **Branch:** `{detail.branchName}`
- **Issue:** #{detail.issueNumber}

### Test Output (last 2000 chars)
```
{truncated_output}
```
"""

    if s3_log_url:
        comment_body += f"\n[View full test logs in S3]({s3_log_url})\n"

    comment_body += """
---
**Next Steps:**
1. Review the test failure details above
2. Fix the logic error in the branch
3. Push your changes to trigger a new test run

*Generated by OutcomeOps run-tests Lambda*
"""

    url = f"https://api.github.com/repos/{detail.repoFullName}/issues/{detail.prNumber}/comments"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {"body": comment_body}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        logger.info(f"[run-tests] Posted failure comment on PR #{detail.prNumber}")
    except Exception as e:
        logger.error(f"[run-tests] Failed to post PR comment: {e}")


def post_test_success_comment(
    detail: CodeGenerationCompletedDetail,
    github_token: str
) -> None:
    """
    Post a success comment on the PR when tests pass.

    Used for the fix tests flow when PR already exists.
    """
    if not detail.prNumber:
        logger.warning("[run-tests] Cannot post comment - no PR number")
        return

    comment_body = """<!-- outcomeops:test-results -->
## ✅ Tests Passed

All tests are now passing.

---
*Generated by OutcomeOps run-tests Lambda*
"""

    url = f"https://api.github.com/repos/{detail.repoFullName}/issues/{detail.prNumber}/comments"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {"body": comment_body}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        logger.info(f"[run-tests] Posted success comment on PR #{detail.prNumber}")
    except Exception as e:
        logger.error(f"[run-tests] Failed to post PR comment: {e}")


def upload_artifacts(
    command_outputs: List[CommandResult],
    junit_content: Optional[str],
    detail: CodeGenerationCompletedDetail
) -> Dict[str, Optional[str]]:
    """Upload log and junit artifacts to S3."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_prefix = f"{TEST_RESULTS_PREFIX}/issue-{detail.issueNumber}/branch-{detail.branchName}/{timestamp}"
    log_key = f"{base_prefix}/test-output.log"
    junit_key = f"{base_prefix}/junit.xml" if junit_content else None

    log_contents = "\n".join(result.as_text() for result in command_outputs)

    s3_client.put_object(
        Bucket=TEST_RESULTS_BUCKET,
        Key=log_key,
        Body=log_contents.encode("utf-8"),
        ContentType="text/plain"
    )

    if junit_content:
        s3_client.put_object(
            Bucket=TEST_RESULTS_BUCKET,
            Key=junit_key,
            Body=junit_content.encode("utf-8"),
            ContentType="application/xml"
        )

    logger.info(
        "Uploaded test artifacts to s3://%s/%s",
        TEST_RESULTS_BUCKET,
        base_prefix
    )

    return {
        "base_prefix": base_prefix,
        "log_key": log_key,
        "junit_key": junit_key
    }


def emit_result_event(result_detail: TestRunEventDetail) -> None:
    """Publish test result event to EventBridge."""
    events_client.put_events(
        Entries=[
            {
                "Source": "outcomeops.run-tests",
                "DetailType": "OutcomeOps.Tests.Completed",
                "Detail": result_detail.model_dump_json(),
                "EventBusName": EVENT_BUS_NAME
            }
        ]
    )
    logger.info(
        "Published test result event for issue #%s (%s)",
        result_detail.issueNumber,
        result_detail.status
    )


def cleanup_workspace(path: str) -> None:
    """Remove workspace directory."""
    try:
        shutil.rmtree(path, ignore_errors=True)
        logger.info("Workspace cleaned up: %s", path)
    except OSError as exc:
        logger.warning("Failed to clean workspace %s: %s", path, exc)


def update_plan_with_autofix_cost(
    repo_dir: str,
    plan_file: str,
    autofix_cost: dict,
    github_token: str,
    detail: CodeGenerationCompletedDetail
) -> bool:
    """
    Update the plan file with auto-fix cost and commit/push the change.

    Adds an "### Auto-Fix (Sonnet)" section to the Cost Summary and updates
    the Grand Total to include the auto-fix cost.

    Args:
        repo_dir: Path to the cloned repository
        plan_file: Relative path to the plan file (e.g., "issues/plan.md")
        autofix_cost: Dict with input_tokens, output_tokens, cost_usd
        github_token: GitHub PAT for pushing
        detail: Event detail with repo/branch info

    Returns:
        True if plan was updated successfully
    """
    if autofix_cost["cost_usd"] <= 0:
        logger.info("[plan-update] No auto-fix cost to add")
        return True

    plan_path = os.path.join(repo_dir, plan_file)
    if not os.path.exists(plan_path):
        logger.warning(f"[plan-update] Plan file not found: {plan_path}")
        return False

    try:
        # Read the current plan content
        with open(plan_path, 'r') as f:
            content = f.read()

        # Build the auto-fix cost section
        autofix_section = f"""### Auto-Fix (Sonnet)

**Cost:** ${autofix_cost['cost_usd']:.6f}
**Input Tokens:** {autofix_cost['input_tokens']:,}
**Output Tokens:** {autofix_cost['output_tokens']:,}

"""

        # Find where to insert the auto-fix section
        # It should go before "### Grand Total" if it exists
        grand_total_match = re.search(r'(### Grand Total\s*\n\s*\n\*\*Total Cost:\*\* \$)([\d.]+)', content)

        if grand_total_match:
            # Insert auto-fix section before Grand Total and update the total
            old_total = float(grand_total_match.group(2))
            new_total = old_total + autofix_cost['cost_usd']

            # Insert auto-fix section before Grand Total
            insert_pos = grand_total_match.start()
            updated_content = (
                content[:insert_pos] +
                autofix_section +
                f"### Grand Total\n\n**Total Cost:** ${new_total:.6f}"
            )

            # Find the end of the Grand Total section and append the rest
            # The Grand Total line ends after the dollar amount
            end_of_total = grand_total_match.end()
            # Skip to end of line
            next_newline = content.find('\n', end_of_total)
            if next_newline != -1:
                updated_content += content[next_newline:]

        elif "## Cost Summary" in content:
            # No Grand Total yet, add auto-fix section at the end of Cost Summary
            # Find the end of Cost Summary section (next ## or end of file)
            cost_summary_start = content.find("## Cost Summary")
            next_section = content.find("\n## ", cost_summary_start + 1)

            if next_section != -1:
                # Insert before next section
                updated_content = (
                    content[:next_section] +
                    "\n" + autofix_section +
                    content[next_section:]
                )
            else:
                # Append to end
                updated_content = content.rstrip() + "\n\n" + autofix_section

        else:
            # No Cost Summary section, create one
            updated_content = content.rstrip() + f"""

## Cost Summary

{autofix_section}"""

        # Write the updated content
        with open(plan_path, 'w') as f:
            f.write(updated_content)

        logger.info(f"[plan-update] Updated plan file with auto-fix cost: ${autofix_cost['cost_usd']:.6f}")

        # Commit and push the change
        env = os.environ.copy()
        env["GIT_EXEC_PATH"] = "/opt/git/libexec/git-core"

        # Configure git user
        run_command(["git", "config", "user.name", "OutcomeOps Bot"], cwd=repo_dir, env=env)
        run_command(["git", "config", "user.email", "bot@outcomeops.ai"], cwd=repo_dir, env=env)

        # Stage and commit
        run_command(["git", "add", plan_file], cwd=repo_dir, env=env)

        commit_msg = f"chore: add auto-fix cost to plan\n\nAuto-fix cost: ${autofix_cost['cost_usd']:.6f}"
        commit_result = run_command(["git", "commit", "-m", commit_msg], cwd=repo_dir, env=env)

        if not commit_result.succeeded:
            # No changes to commit (plan might have been updated by another process)
            logger.info("[plan-update] No changes to commit")
            return True

        # Push the change
        repo_url_with_token = f"https://x-access-token:{github_token}@github.com/{detail.repoFullName}.git"
        run_command(["git", "remote", "set-url", "origin", repo_url_with_token], cwd=repo_dir, env=env)

        push_result = run_command(
            ["git", "push"],
            cwd=repo_dir,
            env=env,
            mask_terms=[github_token]
        )

        # Remove token from remote
        run_command(
            ["git", "remote", "set-url", "origin", f"https://github.com/{detail.repoFullName}.git"],
            cwd=repo_dir,
            env=env
        )

        if not push_result.succeeded:
            logger.error(f"[plan-update] Failed to push: {push_result.stderr}")
            return False

        logger.info("[plan-update] Successfully committed and pushed plan update")
        return True

    except Exception as e:
        logger.error(f"[plan-update] Failed to update plan: {e}")
        return False


# ============================================================================
# Error Classification and Auto-Fix
# ============================================================================


def classify_test_failure(test_output: str) -> str:
    """
    Classify test failure type to determine if auto-fix should be attempted.

    Args:
        test_output: Combined stdout/stderr from test execution

    Returns:
        "import_error" - Missing module imports (auto-fixable)
        "syntax_error" - Python syntax errors (auto-fixable)
        "logic_error" - Test assertions or business logic (human escalation)
    """
    output_lower = test_output.lower()

    # Import errors - auto-fixable
    if "modulenotfounderror" in output_lower or "importerror" in output_lower:
        logger.info("[classify] Detected import error - auto-fixable")
        return "import_error"

    # Syntax errors - auto-fixable
    if "syntaxerror" in output_lower:
        logger.info("[classify] Detected syntax error - auto-fixable")
        return "syntax_error"

    # Everything else is a logic error - needs human review
    logger.info("[classify] Detected logic error - requires human review")
    return "logic_error"


def attempt_import_fix(error_output: str, repo_dir: str, github_token: str, detail: CodeGenerationCompletedDetail) -> bool:
    """
    Attempt to fix import errors by analyzing error and updating code.

    Args:
        error_output: The pytest error output
        repo_dir: Path to repository
        github_token: GitHub API token
        detail: Event detail with repo/branch info

    Returns:
        True if fix was applied and committed, False otherwise
    """
    logger.info("[auto-fix] Attempting import error fix")

    # Parse error to find the problematic file
    error_file = parse_error_file_path(error_output)
    if not error_file:
        logger.warning("[auto-fix] Could not parse file path from error")
        return False

    logger.info(f"[auto-fix] Error in file: {error_file}")

    # Apply the fix
    if not apply_fix_to_file(error_file, error_output, "import_error"):
        return False

    # Commit the fix
    try:
        # Set up environment for git commands
        env = os.environ.copy()
        env["GIT_EXEC_PATH"] = "/opt/git/libexec/git-core"

        # Configure git user identity for commits
        run_command(
            ["git", "config", "user.name", "OutcomeOps Bot"],
            cwd=repo_dir,
            env=env
        )
        run_command(
            ["git", "config", "user.email", "bot@outcomeops.ai"],
            cwd=repo_dir,
            env=env
        )

        # Get relative path for commit message
        relative_path = error_file.replace(f"{repo_dir}/", "")

        commit_result = run_command(
            ["git", "add", error_file],
            cwd=repo_dir,
            env=env
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git add failed: {commit_result.stderr}")
            return False

        commit_msg = f"fix: auto-fix import error in {relative_path}\n\nAutomatically fixed by OutcomeOps run-tests Lambda"
        commit_result = run_command(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_dir,
            env=env
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git commit failed: {commit_result.stderr}")
            return False

        # Temporarily add token back for push
        repo_url_with_token = f"https://x-access-token:{github_token}@github.com/{detail.repoFullName}.git"
        run_command(
            ["git", "remote", "set-url", "origin", repo_url_with_token],
            cwd=repo_dir,
            env=env
        )

        # Push the fix
        push_result = run_command(
            ["git", "push"],
            cwd=repo_dir,
            env=env,
            mask_terms=[github_token]
        )

        # Remove token from remote again
        run_command(
            ["git", "remote", "set-url", "origin", f"https://github.com/{detail.repoFullName}.git"],
            cwd=repo_dir,
            env=env
        )

        if not push_result.succeeded:
            logger.error(f"[auto-fix] git push failed: {push_result.stderr}")
            return False

        logger.info(f"[auto-fix] Successfully committed and pushed fix for {relative_path}")
        return True

    except Exception as e:
        logger.error(f"[auto-fix] Failed to commit fix: {e}")
        return False


def attempt_syntax_fix(error_output: str, repo_dir: str, github_token: str, detail: CodeGenerationCompletedDetail) -> bool:
    """
    Attempt to fix syntax errors.

    Args:
        error_output: The pytest error output
        repo_dir: Path to repository
        github_token: GitHub API token
        detail: Event detail with repo/branch info

    Returns:
        True if fix was applied and committed, False otherwise
    """
    logger.info("[auto-fix] Attempting syntax error fix")

    # Parse error to find the problematic file
    error_file = parse_error_file_path(error_output)
    if not error_file:
        logger.warning("[auto-fix] Could not parse file path from error")
        return False

    logger.info(f"[auto-fix] Syntax error in file: {error_file}")

    # Apply the fix
    if not apply_fix_to_file(error_file, error_output, "syntax_error"):
        return False

    # Commit the fix (same as import fix)
    try:
        # Set up environment for git commands
        env = os.environ.copy()
        env["GIT_EXEC_PATH"] = "/opt/git/libexec/git-core"

        # Configure git user identity for commits
        run_command(
            ["git", "config", "user.name", "OutcomeOps Bot"],
            cwd=repo_dir,
            env=env
        )
        run_command(
            ["git", "config", "user.email", "bot@outcomeops.ai"],
            cwd=repo_dir,
            env=env
        )

        relative_path = error_file.replace(f"{repo_dir}/", "")

        commit_result = run_command(
            ["git", "add", error_file],
            cwd=repo_dir,
            env=env
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git add failed: {commit_result.stderr}")
            return False

        commit_msg = f"fix: auto-fix syntax error in {relative_path}\n\nAutomatically fixed by OutcomeOps run-tests Lambda"
        commit_result = run_command(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_dir,
            env=env
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git commit failed: {commit_result.stderr}")
            return False

        # Temporarily add token back for push
        repo_url_with_token = f"https://x-access-token:{github_token}@github.com/{detail.repoFullName}.git"
        run_command(
            ["git", "remote", "set-url", "origin", repo_url_with_token],
            cwd=repo_dir,
            env=env
        )

        # Push the fix
        push_result = run_command(
            ["git", "push"],
            cwd=repo_dir,
            env=env,
            mask_terms=[github_token]
        )

        # Remove token from remote again
        run_command(
            ["git", "remote", "set-url", "origin", f"https://github.com/{detail.repoFullName}.git"],
            cwd=repo_dir,
            env=env
        )

        if not push_result.succeeded:
            logger.error(f"[auto-fix] git push failed: {push_result.stderr}")
            return False

        logger.info(f"[auto-fix] Successfully committed and pushed syntax fix for {relative_path}")
        return True

    except Exception as e:
        logger.error(f"[auto-fix] Failed to commit syntax fix: {e}")
        return False


# ============================================================================
# Lambda handler
# ============================================================================


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Entrypoint for the run-tests Lambda."""
    logger.info("Received event: %s", json.dumps(event))

    # Reset auto-fix cost tracker for this invocation
    reset_autofix_cost()

    try:
        detail = CodeGenerationCompletedDetail(**event.get("detail", {}))
    except ValidationError as exc:
        logger.error("Invalid event detail: %s", exc)
        raise

    github_token = get_github_token()
    github_usernames = get_github_usernames_to_tag()
    workspace = create_workspace()
    command_outputs: List[CommandResult] = []
    repo_dir = ""

    tests_passed = False
    failure_reason = None
    failure_type = None
    test_output = ""
    setup_exit_code = None
    test_exit_code = None
    junit_content: Optional[str] = None
    test_command_used = "none"  # Track which test command was used

    start_time = time.monotonic()

    try:
        repo_dir = clone_repository(detail, github_token, workspace, command_outputs)

        # Get the appropriate test backend for this repository
        backend = get_backend(repo_dir)

        if backend is None:
            # No test backend available - skip tests and create PR
            # The repository's CI will handle testing
            logger.info("[run-tests] No test backend available - skipping tests, creating PR for CI")
            tests_passed = True  # Treat as passed, let CI handle actual testing
            test_output = "No test backend configured for this repository type. PR created for CI testing."
            failure_reason = None
            junit_content = None
            test_command_used = "skipped (no backend)"
        else:
            logger.info(f"[run-tests] Using {backend.get_backend_name()} for testing")
            test_command_used = " ".join(backend.get_test_command())

            # Run tests using the backend
            backend_result = backend.run_tests(max_attempts=MAX_FIX_ATTEMPTS + 1)

            # Convert backend results to handler format
            tests_passed = backend_result.passed
            test_exit_code = backend_result.exit_code
            test_output = backend_result.output
            failure_type = backend_result.failure_type
            failure_reason = backend_result.failure_reason
            junit_content = backend_result.junit_content

            # Add backend command results to our output list
            for cmd_result in backend_result.command_results:
                command_outputs.append(CommandResult(
                    command=cmd_result.command,
                    exit_code=cmd_result.exit_code,
                    stdout=cmd_result.stdout,
                    stderr=cmd_result.stderr,
                    duration_seconds=cmd_result.duration_seconds
                ))

        # Handle PR creation based on test results
        if tests_passed and detail.prUrl is None:
            logger.info("[run-tests] Tests passed - creating pull request")
            pr_result = create_pull_request(detail, github_token)
            detail.prUrl = pr_result["pr_url"]
            detail.prNumber = pr_result["pr_number"]

            # Trigger PR analyzer to check the newly created PR
            trigger_pr_analyzer(pr_result["pr_number"], detail.repoFullName)
        elif tests_passed and detail.prUrl is not None:
            # PR exists and tests passed - post success comment (for fix tests flow)
            logger.info("[run-tests] Tests passed on existing PR - posting success comment")
            post_test_success_comment(
                detail=detail,
                github_token=github_token
            )
        elif not tests_passed:
            # All test failures need human review - create PR and post comment
            logger.info(f"[run-tests] Tests failed ({failure_type}) - creating PR for human review")
            if detail.prUrl is None:
                pr_result = create_pull_request(detail, github_token, tests_passed=False)
                detail.prUrl = pr_result["pr_url"]
                detail.prNumber = pr_result["pr_number"]

                # Trigger PR analyzer to check the newly created PR
                trigger_pr_analyzer(pr_result["pr_number"], detail.repoFullName)

            # Post failure comment tagging the configured users
            post_test_failure_comment(
                detail=detail,
                github_token=github_token,
                test_output=test_output,
                failure_reason=failure_reason or "Tests failed",
                github_usernames=github_usernames
            )

    except Exception as exc:  # pragma: no cover - aggregated handling
        logger.exception("Test runner failed: %s", exc)
        if not failure_reason:
            failure_reason = str(exc)
        failure_type = "infrastructure"

        # Even on infrastructure failure, create PR and notify humans
        # The code was generated successfully - let humans review it
        try:
            if detail.prUrl is None:
                logger.info("[run-tests] Infrastructure failure - creating PR for human review")
                pr_result = create_pull_request(detail, github_token, tests_passed=False)
                detail.prUrl = pr_result["pr_url"]
                detail.prNumber = pr_result["pr_number"]

                # Trigger PR analyzer to check the newly created PR
                trigger_pr_analyzer(pr_result["pr_number"], detail.repoFullName)

            # Post failure comment tagging the configured users
            post_test_failure_comment(
                detail=detail,
                github_token=github_token,
                test_output=f"Infrastructure error: {exc}",
                failure_reason=failure_reason or "Infrastructure failure",
                github_usernames=github_usernames
            )
        except Exception as pr_exc:
            logger.exception("Failed to create PR after infrastructure failure: %s", pr_exc)
    finally:
        duration = time.monotonic() - start_time

        artifacts = upload_artifacts(command_outputs, junit_content, detail)

        log_snippet = None
        if command_outputs:
            combined_logs = "\n".join(result.as_text() for result in command_outputs)
            log_snippet = combined_logs[-4000:]

        result_detail = TestRunEventDetail(
            issueNumber=detail.issueNumber,
            branchName=detail.branchName,
            repoFullName=detail.repoFullName,
            prNumber=detail.prNumber or 0,  # Default to 0 if no PR created
            prUrl=detail.prUrl or "",  # Default to empty string if no PR created
            status="passed" if tests_passed else "failed",
            success=tests_passed,
            testCommand=test_command_used,
            durationSeconds=round(duration, 2),
            artifactBucket=TEST_RESULTS_BUCKET,
            artifactPrefix=artifacts["base_prefix"],
            logObjectKey=artifacts["log_key"],
            junitObjectKey=artifacts["junit_key"],
            failureReason=failure_reason,
            setupExitCode=setup_exit_code,
            testExitCode=test_exit_code,
            logSnippet=log_snippet,
            environment=detail.environment,
            appName=detail.appName
        )

        emit_result_event(result_detail)

        # Get accumulated auto-fix cost and update plan file before cleanup
        autofix_cost = get_autofix_cost()
        if autofix_cost["cost_usd"] > 0:
            logger.info(
                f"[run-tests] Auto-fix cost (Sonnet): ${autofix_cost['cost_usd']:.6f} "
                f"({autofix_cost['input_tokens']} in, {autofix_cost['output_tokens']} out)"
            )
            # Update the plan file with auto-fix cost (skip for PR commands)
            if repo_dir and detail.planFile and not detail.skipPlanUpdate:
                update_plan_with_autofix_cost(
                    repo_dir=repo_dir,
                    plan_file=detail.planFile,
                    autofix_cost=autofix_cost,
                    github_token=github_token,
                    detail=detail
                )

        cleanup_workspace(workspace)

    # Build response (autofix_cost was retrieved in finally block above)
    response = {
        "success": tests_passed,
        "issueNumber": detail.issueNumber,
        "branchName": detail.branchName,
        "prNumber": detail.prNumber,
        "status": "passed" if tests_passed else "failed",
        "artifactBucket": TEST_RESULTS_BUCKET,
        "artifactPrefix": artifacts["base_prefix"]
    }

    # Include auto-fix cost if any Claude calls were made
    # Re-fetch since autofix_cost was in finally block scope
    final_autofix_cost = get_autofix_cost()
    if final_autofix_cost["cost_usd"] > 0:
        response["autofixCost"] = {
            "inputTokens": final_autofix_cost["input_tokens"],
            "outputTokens": final_autofix_cost["output_tokens"],
            "costUsd": final_autofix_cost["cost_usd"]
        }

    return response
