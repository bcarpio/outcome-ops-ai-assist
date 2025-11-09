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
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
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
    prNumber: int = Field(..., alias="prNumber")
    prUrl: str = Field(..., alias="prUrl")
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
    workspace = create_workspace()
    command_outputs: List[CommandResult] = []
    repo_dir = ""

    tests_passed = False
    failure_reason = None
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

        test_result = run_command(
            TEST_COMMAND.split(),
            cwd=repo_dir,
            env=os.environ.copy()
        )
        command_outputs.append(test_result)
        test_exit_code = test_result.exit_code
        tests_passed = test_result.succeeded
        if not tests_passed:
            failure_reason = "Tests failed"

        junit_path = Path(repo_dir) / "lambda" / "junit.xml"
        junit_content = read_file_if_exists(junit_path)

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
            prNumber=detail.prNumber,
            prUrl=detail.prUrl,
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
