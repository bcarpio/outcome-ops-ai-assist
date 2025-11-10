"""
Lambda handler that runs repository tests after code generation completes.

Flow:
1. Triggered by EventBridge event emitted by generate-code Lambda
2. Clones the target branch using GitHub PAT from SSM
3. Bootstraps a virtual environment and installs all Lambda/test dependencies
4. Executes `make test` to run the repo's Lambda test suite
5. Uploads logs/artifacts to S3 and emits a follow-up EventBridge event summarizing the run
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
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

# Bedrock client for Claude API (auto-fix)
bedrock_config = Config(
    connect_timeout=60,
    read_timeout=300,  # 5 minutes for auto-fix generation
    retries={'max_attempts': 2}
)
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
CLAUDE_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
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
    planFile: str = Field(..., alias="planFile")
    commitSha: Optional[str] = Field(default=None, alias="commitSha")
    eventVersion: Optional[str] = Field(default=None, alias="eventVersion")
    environment: str = Field(..., alias="environment")
    appName: str = Field(..., alias="appName")

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
        cwd=repo_dir
    )
    command_outputs.append(fetch_result)
    if not fetch_result.succeeded:
        raise RuntimeError(f"git fetch failed: {fetch_result.stderr[:1000]}")

    checkout_result = run_command(
        ["git", "checkout", detail.branchName],
        cwd=repo_dir
    )
    command_outputs.append(checkout_result)
    if not checkout_result.succeeded:
        raise RuntimeError(f"git checkout failed: {checkout_result.stderr[:1000]}")

    # Remove token from remote to avoid leaking credentials in future logs
    remote_update = run_command(
        ["git", "remote", "set-url", "origin", f"https://github.com/{detail.repoFullName}.git"],
        cwd=repo_dir
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
    github_token: str
) -> Dict[str, Any]:
    """
    Create a pull request after tests pass.

    Args:
        detail: Event detail with repo and branch info
        github_token: GitHub PAT

    Returns:
        dict with pr_url and pr_number
    """
    pr_title = f"feat: {detail.issueTitle} (issue #{detail.issueNumber})"
    pr_body = f"""## Summary
This PR implements the code generation for issue #{detail.issueNumber}.

**Issue:** {detail.issueTitle}
**Branch:** `{detail.branchName}`
**Implementation Plan:** See `{detail.planFile}`

## Test Results
All tests passed successfully.

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

    comment_body = f"""## ⚠️ Tests Failed - Human Review Required

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


def invoke_claude_for_fix(error_output: str, failure_type: str) -> Optional[str]:
    """
    Invoke Claude to suggest a fix for the error.

    Args:
        error_output: The test error output
        failure_type: Type of failure (import_error or syntax_error)

    Returns:
        Suggested fix as text, or None if Claude fails
    """
    logger.info(f"[auto-fix] Invoking Claude for {failure_type} fix")

    if failure_type == "import_error":
        system_prompt = """You are helping fix a Python import error.
Analyze the error and provide ONLY the specific fix needed.
For missing modules, respond with: "Add missing import: from module import Class"
Be concise and specific."""
    else:  # syntax_error
        system_prompt = """You are helping fix a Python syntax error.
Analyze the error and provide ONLY the specific fix needed.
Be concise and specific about what to change."""

    prompt = f"""Error output:
{error_output[:2000]}

Provide a concise fix for this error."""

    try:
        response = bedrock_client.converse(
            modelId=CLAUDE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": system_prompt}],
            inferenceConfig={"temperature": 0.3, "maxTokens": 500}
        )

        fix_suggestion = response.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")
        logger.info(f"[auto-fix] Claude suggestion: {fix_suggestion[:200]}")
        return fix_suggestion

    except Exception as e:
        logger.error(f"[auto-fix] Claude invocation failed: {e}")
        return None


def apply_fix_to_file(file_path: str, error_output: str, failure_type: str) -> bool:
    """
    Apply Claude-suggested fix to a file.

    Args:
        file_path: Path to file to fix
        error_output: The error output
        failure_type: Type of error (import_error or syntax_error)

    Returns:
        True if fix was applied successfully
    """
    try:
        # Read the current file content
        with open(file_path, 'r') as f:
            original_content = f.read()

        logger.info(f"[auto-fix] Read {len(original_content)} bytes from {file_path}")

        # Get Claude's fix suggestion with full context
        prompt = f"""Fix this Python {failure_type.replace('_', ' ')}:

File: {file_path}

Error:
{error_output[:1500]}

Current file content:
{original_content[:3000]}

Provide the COMPLETE fixed file content. Output ONLY the Python code, no explanations."""

        system_prompt = f"""You are fixing a Python {failure_type.replace('_', ' ')}.
Analyze the error and file content, then output the COMPLETE corrected file.
Output ONLY valid Python code - no markdown, no explanations."""

        response = bedrock_client.converse(
            modelId=CLAUDE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": system_prompt}],
            inferenceConfig={"temperature": 0.3, "maxTokens": 4000}
        )

        fixed_content = response.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")

        if not fixed_content or len(fixed_content) < 10:
            logger.warning("[auto-fix] Claude returned empty or very short content")
            return False

        # Remove markdown code fences if present
        fixed_content = re.sub(r'^```python\s*\n', '', fixed_content)
        fixed_content = re.sub(r'\n```\s*$', '', fixed_content)
        fixed_content = fixed_content.strip()

        # Write the fixed content
        with open(file_path, 'w') as f:
            f.write(fixed_content)

        logger.info(f"[auto-fix] Applied fix to {file_path}")
        return True

    except Exception as e:
        logger.error(f"[auto-fix] Failed to apply fix: {e}")
        return False


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
    # Format: "  File \"/path/to/file.py\", line X"
    file_match = re.search(r'File "([^"]+\.py)", line (\d+)', error_output)
    if not file_match:
        logger.warning("[auto-fix] Could not parse file path from error")
        return False

    error_file = file_match.group(1)
    logger.info(f"[auto-fix] Error in file: {error_file}")

    # Apply the fix
    if not apply_fix_to_file(error_file, error_output, "import_error"):
        return False

    # Commit the fix
    try:
        # Get relative path for commit message
        relative_path = error_file.replace(f"{repo_dir}/", "")

        commit_result = run_command(
            ["git", "add", error_file],
            cwd=repo_dir
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git add failed: {commit_result.stderr}")
            return False

        commit_msg = f"fix: auto-fix import error in {relative_path}\n\nAutomatically fixed by OutcomeOps run-tests Lambda"
        commit_result = run_command(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_dir
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git commit failed: {commit_result.stderr}")
            return False

        # Temporarily add token back for push
        repo_url_with_token = f"https://x-access-token:{github_token}@github.com/{detail.repoFullName}.git"
        run_command(
            ["git", "remote", "set-url", "origin", repo_url_with_token],
            cwd=repo_dir
        )

        # Push the fix
        push_result = run_command(
            ["git", "push"],
            cwd=repo_dir,
            mask_terms=[github_token]
        )

        # Remove token from remote again
        run_command(
            ["git", "remote", "set-url", "origin", f"https://github.com/{detail.repoFullName}.git"],
            cwd=repo_dir
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
    file_match = re.search(r'File "([^"]+\.py)", line (\d+)', error_output)
    if not file_match:
        logger.warning("[auto-fix] Could not parse file path from error")
        return False

    error_file = file_match.group(1)
    logger.info(f"[auto-fix] Syntax error in file: {error_file}")

    # Apply the fix
    if not apply_fix_to_file(error_file, error_output, "syntax_error"):
        return False

    # Commit the fix (same as import fix)
    try:
        relative_path = error_file.replace(f"{repo_dir}/", "")

        commit_result = run_command(
            ["git", "add", error_file],
            cwd=repo_dir
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git add failed: {commit_result.stderr}")
            return False

        commit_msg = f"fix: auto-fix syntax error in {relative_path}\n\nAutomatically fixed by OutcomeOps run-tests Lambda"
        commit_result = run_command(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_dir
        )
        if not commit_result.succeeded:
            logger.error(f"[auto-fix] git commit failed: {commit_result.stderr}")
            return False

        # Temporarily add token back for push
        repo_url_with_token = f"https://x-access-token:{github_token}@github.com/{detail.repoFullName}.git"
        run_command(
            ["git", "remote", "set-url", "origin", repo_url_with_token],
            cwd=repo_dir
        )

        # Push the fix
        push_result = run_command(
            ["git", "push"],
            cwd=repo_dir,
            mask_terms=[github_token]
        )

        # Remove token from remote again
        run_command(
            ["git", "remote", "set-url", "origin", f"https://github.com/{detail.repoFullName}.git"],
            cwd=repo_dir
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

    start_time = time.monotonic()

    try:
        repo_dir = clone_repository(detail, github_token, workspace, command_outputs)

        setup_results = build_virtualenv(repo_dir)
        command_outputs.extend(setup_results)
        setup_exit_code = max((result.exit_code for result in setup_results), default=0)
        if setup_exit_code != 0:
            failure_reason = "Dependency installation failed"
            raise RuntimeError("Failed to install dependencies")

        # Test execution with auto-fix retry loop
        fix_attempt = 0
        while fix_attempt <= MAX_FIX_ATTEMPTS:
            logger.info(f"[run-tests] Test execution attempt {fix_attempt + 1}/{MAX_FIX_ATTEMPTS + 1}")

            test_result = run_command(
                TEST_COMMAND.split(),
                cwd=repo_dir,
                env=os.environ.copy()
            )
            command_outputs.append(test_result)
            test_exit_code = test_result.exit_code
            tests_passed = test_result.succeeded

            if tests_passed:
                logger.info("[run-tests] Tests passed!")
                break

            # Tests failed - classify and potentially auto-fix
            test_output = test_result.stdout + "\n" + test_result.stderr
            current_failure_type = classify_test_failure(test_output)
            failure_type = current_failure_type  # Store for use outside loop
            failure_reason = f"Tests failed ({failure_type})"
            logger.info(f"[run-tests] Test failure classified as: {failure_type}")

            # If this is the last attempt or it's a logic error, don't try to fix
            if fix_attempt >= MAX_FIX_ATTEMPTS or current_failure_type == "logic_error":
                logger.info(f"[run-tests] No more fix attempts (attempt {fix_attempt + 1}, type: {current_failure_type})")
                break

            # Attempt auto-fix
            fix_applied = False
            if current_failure_type == "import_error":
                fix_applied = attempt_import_fix(test_output, repo_dir, github_token, detail)
            elif current_failure_type == "syntax_error":
                fix_applied = attempt_syntax_fix(test_output, repo_dir, github_token, detail)

            if not fix_applied:
                logger.warning(f"[run-tests] Auto-fix failed for {current_failure_type}")
                break

            logger.info(f"[run-tests] Fix applied, retrying tests (attempt {fix_attempt + 2})")
            fix_attempt += 1

        junit_path = Path(repo_dir) / "lambda" / "junit.xml"
        junit_content = read_file_if_exists(junit_path)

        # Handle PR creation based on test results
        if tests_passed and detail.prUrl is None:
            logger.info("[run-tests] Tests passed - creating pull request")
            pr_result = create_pull_request(detail, github_token)
            detail.prUrl = pr_result["pr_url"]
            detail.prNumber = pr_result["pr_number"]
        elif not tests_passed and failure_type == "logic_error":
            # Logic errors need human review - create PR and post comment
            logger.info("[run-tests] Logic error detected - creating PR for human review")
            if detail.prUrl is None:
                pr_result = create_pull_request(detail, github_token)
                detail.prUrl = pr_result["pr_url"]
                detail.prNumber = pr_result["pr_number"]

            # Post failure comment tagging the configured users
            post_test_failure_comment(
                detail=detail,
                github_token=github_token,
                test_output=test_output,
                failure_reason=failure_reason or "Tests failed",
                github_usernames=github_usernames
            )
        elif not tests_passed:
            logger.warning("[run-tests] Tests failed - skipping PR creation")

    except Exception as exc:  # pragma: no cover - aggregated handling
        logger.exception("Test runner failed: %s", exc)
        if not failure_reason:
            failure_reason = str(exc)
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
            testCommand=TEST_COMMAND,
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
        cleanup_workspace(workspace)

    return {
        "success": tests_passed,
        "issueNumber": detail.issueNumber,
        "branchName": detail.branchName,
        "prNumber": detail.prNumber,
        "status": "passed" if tests_passed else "failed",
        "artifactBucket": TEST_RESULTS_BUCKET,
        "artifactPrefix": artifacts["base_prefix"]
    }
