# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0

# === Legacy dict -> unified Response adapter (ADR-035 chunk 10) ===
from types import SimpleNamespace
from shared.model_client_types import StopReason

class _LegacyDictResponse:
    def __init__(self, d):
        content = d.get("output", {}).get("message", {}).get("content", [])
        self.text = content[0].get("text", "") if content else ""
        self.content_blocks = [{"text": self.text}] if self.text else []
        usage = d.get("usage", {})
        self.usage = SimpleNamespace(
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )
        self.stop_reason = StopReason.END_TURN
        self.tool_calls = None
        self.raw = d

"""
Unit tests for the run-tests Lambda handler.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure required env vars exist before importing handler
os.environ.setdefault("TEST_RESULTS_BUCKET", "unit-test-bucket")
os.environ.setdefault("ENV", "dev")
os.environ.setdefault("APP_NAME", "outcome-ops-ai-assist")

run_tests_dir = os.path.join(os.path.dirname(__file__), "../../run-tests")
sys.path.insert(0, os.path.abspath(run_tests_dir))

import importlib.util

handler_path = os.path.join(run_tests_dir, "handler.py")
spec = importlib.util.spec_from_file_location("run_tests_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules["run_tests_handler"] = handler_module
spec.loader.exec_module(handler_module)

# Import auto_fix module for patching Claude calls
auto_fix_path = os.path.join(run_tests_dir, "auto_fix.py")
auto_fix_spec = importlib.util.spec_from_file_location("auto_fix", auto_fix_path)
auto_fix_module = importlib.util.module_from_spec(auto_fix_spec)
sys.modules["auto_fix"] = auto_fix_module
auto_fix_spec.loader.exec_module(auto_fix_module)


def build_event(**overrides):
    """Construct a default EventBridge payload."""
    detail = {
        "issueNumber": 6,
        "issueTitle": "Add list-recent-docs handler",
        "repoFullName": "bcarpio/outcome-ops-ai-assist",
        "branchName": "6-lambda-add-list-recent-docs-handler-for-kb-verific",
        "baseBranch": "main",
        "prNumber": 123,
        "prUrl": "https://github.com/bcarpio/outcome-ops-ai-assist/pull/123",
        "planFile": "issues/test-plan.md",
        "eventVersion": "2024-11-09",
        "environment": "dev",
        "appName": "outcome-ops-ai-assist"
    }
    detail.update(overrides)
    return {"detail": detail}


def fake_subprocess_run_factory(tmp_path: Path, fail_tests: bool = False, backend_type: str = "python"):
    """Create a subprocess.run stub that simulates git/make commands."""

    repo_parent = tmp_path / "repo"
    junit_path = repo_parent / "lambda" / "junit.xml"

    def _run(cmd, cwd=None, **_):
        nonlocal junit_path
        args = cmd if isinstance(cmd, list) else cmd.split()
        if args[:2] == ["git", "clone"]:
            repo_parent.mkdir(parents=True, exist_ok=True)
            (repo_parent / "lambda").mkdir(parents=True, exist_ok=True)
            sample_req = repo_parent / "lambda" / "sample" / "requirements.txt"
            sample_req.parent.mkdir(parents=True, exist_ok=True)
            sample_req.write_text("boto3==1.0.0\n")
            # Create .outcomeops.yaml with backend config so tests run
            outcomeops_config = repo_parent / ".outcomeops.yaml"
            outcomeops_config.write_text(f"""
code_generation:
  backend: {backend_type}
testing:
  test_directory: lambda/tests/
""")
        # Handle virtualenv creation
        if "venv" in args and "-m" in args:
            venv_dir = repo_parent / "venv" / "bin"
            venv_dir.mkdir(parents=True, exist_ok=True)
            (venv_dir / "pip").write_text("#!/bin/bash\n")
            (venv_dir / "python3.12").write_text("#!/bin/bash\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="", args=args)
        # Handle pip install commands
        if "pip" in str(args[0]) or (len(args) > 1 and "install" in args):
            return SimpleNamespace(returncode=0, stdout="", stderr="", args=args)
        # Handle pytest execution (new backend-based command)
        if "pytest" in args or (len(args) > 2 and args[2] == "pytest"):
            junit_path.parent.mkdir(parents=True, exist_ok=True)
            junit_path.write_text("<testsuite/>")
            return SimpleNamespace(
                returncode=1 if fail_tests else 0,
                stdout="pytest output",
                stderr="",
                args=args
            )
        # Handle legacy make test command (for backwards compatibility)
        if args[:2] == ["make", "test"]:
            junit_path.parent.mkdir(parents=True, exist_ok=True)
            junit_path.write_text("<testsuite/>")
            return SimpleNamespace(
                returncode=1 if fail_tests else 0,
                stdout="pytest output",
                stderr="",
                args=args
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="", args=args)

    return _run, repo_parent, junit_path


@patch.object(handler_module, "events_client")
@patch.object(handler_module, "s3_client")
@patch.object(handler_module, "ssm_client")
def test_handler_runs_tests_and_publishes_results(mock_ssm, mock_s3, mock_events, tmp_path, monkeypatch):
    """Happy path: tests run and result event indicates success."""
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ghp_test_token"}}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_run, repo_dir, junit_path = fake_subprocess_run_factory(workspace)
    monkeypatch.setattr(handler_module.tempfile, "mkdtemp", lambda prefix: str(workspace))
    original_subprocess = handler_module.subprocess
    monkeypatch.setattr(
        handler_module,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=original_subprocess.TimeoutExpired)
    )
    # Also patch subprocess in the backend base module (test_backend_base)
    import test_backend_base
    monkeypatch.setattr(
        test_backend_base,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=original_subprocess.TimeoutExpired, PIPE=original_subprocess.PIPE)
    )

    response = handler_module.handler(build_event(), None)

    assert response["success"] is True
    assert response["status"] == "passed"
    assert mock_s3.put_object.call_count == 2  # logs + junit
    mock_events.put_events.assert_called_once()
    payload = json.loads(
        mock_events.put_events.call_args.kwargs["Entries"][0]["Detail"]
    )
    assert payload["success"] is True
    assert payload["status"] == "passed"
    assert payload["logObjectKey"].endswith("test-output.log")
    assert payload["junitObjectKey"].endswith("junit.xml")
    assert payload["environment"] == "dev"
    assert not repo_dir.exists()
    assert not workspace.exists()


@patch.object(handler_module, "log_admin_action")
@patch.object(handler_module, "events_client")
@patch.object(handler_module, "s3_client")
@patch.object(handler_module, "ssm_client")
def test_handler_emits_tests_ran_audit_row(
    mock_ssm, mock_s3, mock_events, mock_audit, tmp_path, monkeypatch,
):
    """plan/audit-completeness chunk 08 — handler emits ACTION_TESTS_RAN
    in the finally block, regardless of test pass/fail outcome."""
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ghp_test_token"}}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_run, repo_dir, junit_path = fake_subprocess_run_factory(workspace)
    monkeypatch.setattr(handler_module.tempfile, "mkdtemp", lambda prefix: str(workspace))
    original_subprocess = handler_module.subprocess
    monkeypatch.setattr(
        handler_module,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=original_subprocess.TimeoutExpired),
    )
    import test_backend_base
    monkeypatch.setattr(
        test_backend_base,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=original_subprocess.TimeoutExpired, PIPE=original_subprocess.PIPE),
    )
    monkeypatch.setenv("AUDIT_TABLE", "test-audit")

    handler_module.handler(build_event(), None)

    mock_audit.assert_called_once()
    args, kwargs = mock_audit.call_args
    assert args[1] == "TESTS_RAN"
    d = kwargs["details"]
    # repo comes from build_event() - just confirm it's populated
    assert d["repo"]
    assert d["tests_passed"] is True
    assert "duration_seconds" in d
    assert "test_command" in d


@patch("requests.post")
@patch.object(handler_module, "events_client")
@patch.object(handler_module, "s3_client")
@patch.object(handler_module, "ssm_client")
def test_handler_flags_failed_tests(mock_ssm, mock_s3, mock_events, mock_post, tmp_path, monkeypatch):
    """If make test exits non-zero, handler reports failure and still uploads logs."""
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ghp_test_token"}}
    mock_post.return_value.raise_for_status = MagicMock()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_run, _, _ = fake_subprocess_run_factory(workspace, fail_tests=True)
    monkeypatch.setattr(handler_module.tempfile, "mkdtemp", lambda prefix: str(workspace))
    original_subprocess = handler_module.subprocess
    monkeypatch.setattr(
        handler_module,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=original_subprocess.TimeoutExpired)
    )
    # Also patch subprocess in the backend base module (test_backend_base)
    import test_backend_base
    monkeypatch.setattr(
        test_backend_base,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=original_subprocess.TimeoutExpired, PIPE=original_subprocess.PIPE)
    )

    response = handler_module.handler(build_event(), None)

    assert response["success"] is False
    assert response["status"] == "failed"
    assert mock_s3.put_object.call_count == 2
    payload = json.loads(
        mock_events.put_events.call_args.kwargs["Entries"][0]["Detail"]
    )
    assert payload["success"] is False
    # Backend system uses "Tests failed after N attempts" format
    assert "Tests failed" in payload["failureReason"]


def test_handler_rejects_invalid_event():
    """Missing required fields should raise ValidationError."""
    with pytest.raises(Exception):
        handler_module.handler({"detail": {}}, None)


# ============================================================================
# Auto-fix Function Tests
# ============================================================================


def test_classify_test_failure_import_error():
    """Classify ModuleNotFoundError as import_error."""
    # Arrange
    test_output = """
    Traceback (most recent call last):
      File "test.py", line 5, in <module>
        from missing_module import SomeClass
    ModuleNotFoundError: No module named 'missing_module'
    """

    # Act
    result = handler_module.classify_test_failure(test_output)

    # Assert
    assert result == "import_error"


def test_classify_test_failure_importerror():
    """Classify ImportError as import_error."""
    # Arrange
    test_output = "ImportError: cannot import name 'Foo' from 'bar'"

    # Act
    result = handler_module.classify_test_failure(test_output)

    # Assert
    assert result == "import_error"


def test_classify_test_failure_syntax_error():
    """Classify SyntaxError as syntax_error."""
    # Arrange
    test_output = """
      File "handler.py", line 42
        def foo(
               ^
    SyntaxError: unexpected EOF while parsing
    """

    # Act
    result = handler_module.classify_test_failure(test_output)

    # Assert
    assert result == "syntax_error"


def test_classify_test_failure_logic_error():
    """Classify assertion errors as logic_error."""
    # Arrange
    test_output = """
    FAILED test_handler.py::test_returns_correct_value - AssertionError
    Expected: 42
    Got: 0
    """

    # Act
    result = handler_module.classify_test_failure(test_output)

    # Assert
    assert result == "logic_error"


def test_classify_test_failure_case_insensitive():
    """Classification should be case-insensitive."""
    # Arrange
    test_output = "MODULENOTFOUNDERROR: No module named 'foo'"

    # Act
    result = handler_module.classify_test_failure(test_output)

    # Assert
    assert result == "import_error"


@patch.object(handler_module, "ssm_client")
def test_get_github_usernames_to_tag_success(mock_ssm):
    """Successfully fetch GitHub usernames from SSM."""
    # Arrange
    mock_ssm.get_parameter.return_value = {
        "Parameter": {"Value": "bcarpio,user2"}
    }

    # Act
    result = handler_module.get_github_usernames_to_tag()

    # Assert
    assert result == "bcarpio,user2"
    mock_ssm.get_parameter.assert_called_once_with(
        Name="/dev/outcome-ops-ai-assist/config/github-usernames-to-tag"
    )


@patch.object(handler_module, "ssm_client")
def test_get_github_usernames_to_tag_empty(mock_ssm):
    """Handle empty GitHub usernames parameter."""
    # Arrange
    mock_ssm.get_parameter.return_value = {
        "Parameter": {"Value": ""}
    }

    # Act
    result = handler_module.get_github_usernames_to_tag()

    # Assert
    assert result == ""


@patch.object(handler_module, "ssm_client")
def test_get_github_usernames_to_tag_missing_param(mock_ssm):
    """Handle missing SSM parameter gracefully."""
    # Arrange
    from botocore.exceptions import ClientError
    mock_ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound"}},
        "GetParameter"
    )

    # Act
    result = handler_module.get_github_usernames_to_tag()

    # Assert
    assert result == ""


@patch.object(auto_fix_module, "invoke_model")
def test_apply_fix_to_file_success(mock_invoke, tmp_path):
    """Successfully apply a fix to a file."""
    # Arrange
    test_file = tmp_path / "test.py"
    test_file.write_text("def foo():\n  pass\n")

    mock_invoke.return_value = _LegacyDictResponse({
        "output": {
            "message": {
                "content": [{"text": "def foo():\n    import os\n    pass\n"}]
            }
        },
        "usage": {"inputTokens": 165, "outputTokens": 17}
    })
    error_output = "ModuleNotFoundError: No module named 'os'"

    # Act
    result = auto_fix_module.apply_fix_to_file(
        str(test_file),
        error_output,
        "import_error"
    )

    # Assert
    assert result is True
    fixed_content = test_file.read_text()
    assert "import os" in fixed_content
    mock_invoke.assert_called_once()


@patch.object(auto_fix_module, "invoke_model")
def test_apply_fix_to_file_removes_markdown_fences(mock_invoke, tmp_path):
    """Remove markdown code fences from Claude's response."""
    # Arrange
    test_file = tmp_path / "test.py"
    test_file.write_text("def foo():\n  pass\n")

    mock_invoke.return_value = _LegacyDictResponse({
        "output": {
            "message": {
                "content": [{"text": "```python\ndef foo():\n    import os\n    pass\n```"}]
            }
        },
        "usage": {"inputTokens": 150, "outputTokens": 20}
    })

    # Act
    result = auto_fix_module.apply_fix_to_file(
        str(test_file),
        "error",
        "import_error"
    )

    # Assert
    assert result is True
    fixed_content = test_file.read_text()
    assert "```python" not in fixed_content
    assert "```" not in fixed_content


@patch.object(auto_fix_module, "invoke_model")
def test_apply_fix_to_file_empty_response(mock_invoke, tmp_path):
    """Handle empty Claude response."""
    # Arrange
    test_file = tmp_path / "test.py"
    test_file.write_text("original content")

    mock_invoke.return_value = _LegacyDictResponse({
        "output": {
            "message": {
                "content": [{"text": ""}]
            }
        },
        "usage": {"inputTokens": 151, "outputTokens": 10}
    })

    # Act
    result = auto_fix_module.apply_fix_to_file(
        str(test_file),
        "error",
        "import_error"
    )

    # Assert
    assert result is False
    assert test_file.read_text() == "original content"  # Unchanged


@patch.object(handler_module, "run_command")
@patch.object(handler_module, "apply_fix_to_file")
def test_attempt_import_fix_success(mock_apply_fix, mock_run_cmd):
    """Successfully fix an import error."""
    # Arrange
    error_output = """
      File "/tmp/repo/lambda/handler.py", line 10, in <module>
        from missing import Foo
    ModuleNotFoundError: No module named 'missing'
    """
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )

    mock_apply_fix.return_value = True
    mock_run_cmd.return_value = handler_module.CommandResult(
        command=["git", "add"],
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.1
    )

    # Act
    result = handler_module.attempt_import_fix(
        error_output,
        "/tmp/repo",
        "fake-token",
        detail
    )

    # Assert
    assert result is True
    mock_apply_fix.assert_called_once_with(
        "/tmp/repo/lambda/handler.py",
        error_output,
        "import_error"
    )
    assert mock_run_cmd.call_count >= 3  # add, commit, push


@patch.object(handler_module, "apply_fix_to_file")
def test_attempt_import_fix_no_file_match(mock_apply_fix):
    """Fail gracefully when cannot parse file from error."""
    # Arrange
    error_output = "Some error without file info"
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )

    # Act
    result = handler_module.attempt_import_fix(
        error_output,
        "/tmp/repo",
        "fake-token",
        detail
    )

    # Assert
    assert result is False
    mock_apply_fix.assert_not_called()


@patch.object(handler_module, "run_command")
@patch.object(handler_module, "apply_fix_to_file")
def test_attempt_import_fix_git_push_fails(mock_apply_fix, mock_run_cmd):
    """Handle git push failure."""
    # Arrange
    error_output = 'File "/tmp/repo/test.py", line 1'
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )

    mock_apply_fix.return_value = True

    def run_command_side_effect(cmd, **kwargs):
        if "push" in cmd:
            return handler_module.CommandResult(
                command=cmd,
                exit_code=1,
                stdout="",
                stderr="push failed",
                duration_seconds=0.1
            )
        return handler_module.CommandResult(
            command=cmd,
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.1
        )

    mock_run_cmd.side_effect = run_command_side_effect

    # Act
    result = handler_module.attempt_import_fix(
        error_output,
        "/tmp/repo",
        "fake-token",
        detail
    )

    # Assert
    assert result is False


@patch.object(handler_module, "run_command")
@patch.object(handler_module, "apply_fix_to_file")
def test_attempt_syntax_fix_success(mock_apply_fix, mock_run_cmd):
    """Successfully fix a syntax error."""
    # Arrange
    error_output = """
      File "/tmp/repo/handler.py", line 15
        def bar(
              ^
    SyntaxError: unexpected EOF while parsing
    """
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )

    mock_apply_fix.return_value = True
    mock_run_cmd.return_value = handler_module.CommandResult(
        command=["git", "add"],
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.1
    )

    # Act
    result = handler_module.attempt_syntax_fix(
        error_output,
        "/tmp/repo",
        "fake-token",
        detail
    )

    # Assert
    assert result is True
    mock_apply_fix.assert_called_once_with(
        "/tmp/repo/handler.py",
        error_output,
        "syntax_error"
    )
    assert mock_run_cmd.call_count >= 3  # add, commit, push


@patch.object(handler_module, "apply_fix_to_file")
def test_attempt_syntax_fix_apply_fails(mock_apply_fix):
    """Handle apply_fix failure for syntax error."""
    # Arrange
    error_output = 'File "/tmp/repo/test.py", line 1'
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )

    mock_apply_fix.return_value = False

    # Act
    result = handler_module.attempt_syntax_fix(
        error_output,
        "/tmp/repo",
        "fake-token",
        detail
    )

    # Assert
    assert result is False


@patch("requests.post")
def test_post_test_failure_comment_success(mock_post):
    """Successfully post a PR comment for test failure."""
    # Arrange
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=123,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_post.return_value.raise_for_status = MagicMock()
    test_output = "AssertionError: expected 42, got 0"

    # Act
    handler_module.post_test_failure_comment(
        detail=detail,
        github_token="fake-token",
        test_output=test_output,
        failure_reason="Tests failed (logic_error)",
        github_usernames="bcarpio,user2"
    )

    # Assert
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "user/repo" in call_args[0][0]
    assert "/issues/123/comments" in call_args[0][0]

    comment_body = call_args[1]["json"]["body"]
    assert "@bcarpio @user2" in comment_body
    assert "logic error" in comment_body
    assert test_output in comment_body


@patch("requests.post")
def test_post_test_failure_comment_no_pr_number(mock_post):
    """Handle missing PR number gracefully."""
    # Arrange
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=None,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )

    # Act
    handler_module.post_test_failure_comment(
        detail=detail,
        github_token="fake-token",
        test_output="error",
        failure_reason="failed",
        github_usernames="user"
    )

    # Assert
    mock_post.assert_not_called()


@patch("requests.post")
def test_post_test_failure_comment_empty_usernames(mock_post):
    """Handle empty GitHub usernames."""
    # Arrange
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=123,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_post.return_value.raise_for_status = MagicMock()

    # Act
    handler_module.post_test_failure_comment(
        detail=detail,
        github_token="fake-token",
        test_output="error",
        failure_reason="failed",
        github_usernames=""
    )

    # Assert
    mock_post.assert_called_once()
    comment_body = mock_post.call_args[1]["json"]["body"]
    assert "@" not in comment_body.split("\n")[2]  # No mentions in header


@patch("requests.post")
def test_post_test_failure_comment_truncates_long_output(mock_post):
    """Truncate test output longer than 2000 characters."""
    # Arrange
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=123,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_post.return_value.raise_for_status = MagicMock()
    long_output = "x" * 3000

    # Act
    handler_module.post_test_failure_comment(
        detail=detail,
        github_token="fake-token",
        test_output=long_output,
        failure_reason="failed",
        github_usernames="user"
    )

    # Assert
    comment_body = mock_post.call_args[1]["json"]["body"]
    # The output in the comment should be truncated to last 2000 chars
    assert "x" * 2000 in comment_body
    assert len(long_output) == 3000  # Original unchanged


# ============================================================================
# Additional Handler Tests for Coverage
# ============================================================================


def test_mask_value():
    """Test mask_value replaces secret values with ***."""
    text = "git clone https://ghp_abc123@github.com/repo.git"
    result = handler_module.mask_value(text, ["ghp_abc123"])
    assert "ghp_abc123" not in result
    assert "***" in result


def test_mask_value_empty_secret():
    """Test mask_value handles empty secret strings."""
    text = "some text"
    result = handler_module.mask_value(text, ["", None])
    assert result == "some text"


def test_mask_value_multiple_secrets():
    """Test mask_value replaces multiple secrets."""
    text = "token=abc123 key=xyz789"
    result = handler_module.mask_value(text, ["abc123", "xyz789"])
    assert "abc123" not in result
    assert "xyz789" not in result
    assert result.count("***") == 2


def test_command_result_as_text():
    """Test CommandResult.as_text() formatting."""
    result = handler_module.CommandResult(
        command=["echo", "hello"],
        exit_code=0,
        stdout="hello",
        stderr="",
        duration_seconds=0.5
    )
    text = result.as_text()
    assert "echo hello" in text
    assert "exit 0" in text
    assert "0.50s" in text


def test_read_file_if_exists(tmp_path):
    """Test read_file_if_exists returns file contents."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")
    result = handler_module.read_file_if_exists(test_file)
    assert result == "hello world"


def test_read_file_if_exists_missing(tmp_path):
    """Test read_file_if_exists returns None for missing file."""
    result = handler_module.read_file_if_exists(tmp_path / "missing.txt")
    assert result is None


@patch("requests.post")
def test_create_pull_request_tests_passed(mock_post):
    """Test PR creation with tests passed."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Add handler",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "html_url": "https://github.com/user/repo/pull/42",
        "number": 42
    }
    mock_response.raise_for_status = MagicMock()
    mock_post.return_value = mock_response

    result = handler_module.create_pull_request(detail, "fake-token", tests_passed=True)

    assert result["pr_url"] == "https://github.com/user/repo/pull/42"
    assert result["pr_number"] == 42
    body = mock_post.call_args[1]["json"]["body"]
    assert "All tests passed" in body


@patch("requests.post")
def test_create_pull_request_tests_failed(mock_post):
    """Test PR creation with tests failed."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Add handler",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "html_url": "https://github.com/user/repo/pull/42",
        "number": 42
    }
    mock_response.raise_for_status = MagicMock()
    mock_post.return_value = mock_response

    result = handler_module.create_pull_request(detail, "fake-token", tests_passed=False)

    body = mock_post.call_args[1]["json"]["body"]
    assert "Tests failed" in body


@patch.object(handler_module, "lambda_client")
def test_trigger_pr_analyzer_success(mock_lambda):
    """Test triggering PR analyzer Lambda."""
    mock_lambda.invoke.return_value = {"StatusCode": 202}

    handler_module.trigger_pr_analyzer(42, "user/repo")

    mock_lambda.invoke.assert_called_once()
    call_args = mock_lambda.invoke.call_args
    assert call_args[1]["InvocationType"] == "Event"


@patch.object(handler_module, "lambda_client")
def test_trigger_pr_analyzer_failure(mock_lambda):
    """Test trigger_pr_analyzer handles errors gracefully."""
    mock_lambda.invoke.side_effect = RuntimeError("Lambda error")

    # Should not raise
    handler_module.trigger_pr_analyzer(42, "user/repo")


@patch("requests.post")
def test_post_test_success_comment(mock_post):
    """Test posting success comment on PR."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=123,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_post.return_value.raise_for_status = MagicMock()

    handler_module.post_test_success_comment(detail, "fake-token")

    mock_post.assert_called_once()
    body = mock_post.call_args[1]["json"]["body"]
    assert "Tests Passed" in body


@patch("requests.post")
def test_post_test_success_comment_no_pr(mock_post):
    """Test post_test_success_comment skips when no PR number."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=None,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )

    handler_module.post_test_success_comment(detail, "fake-token")

    mock_post.assert_not_called()


@patch("requests.post")
def test_post_test_success_comment_api_error(mock_post):
    """Test post_test_success_comment handles API errors gracefully."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=123,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_post.side_effect = RuntimeError("API error")

    # Should not raise
    handler_module.post_test_success_comment(detail, "fake-token")


@patch("requests.post")
def test_post_test_failure_comment_with_s3_url(mock_post):
    """Test posting failure comment includes S3 log URL."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=123,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_post.return_value.raise_for_status = MagicMock()

    handler_module.post_test_failure_comment(
        detail=detail,
        github_token="fake-token",
        test_output="error",
        failure_reason="failed",
        github_usernames="user",
        s3_log_url="https://s3.amazonaws.com/bucket/logs.txt"
    )

    body = mock_post.call_args[1]["json"]["body"]
    assert "View full test logs in S3" in body


@patch("requests.post")
def test_post_test_failure_comment_api_error(mock_post):
    """Test post_test_failure_comment handles API errors gracefully."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6,
        issueTitle="Test",
        repoFullName="user/repo",
        branchName="test-branch",
        baseBranch="main",
        prNumber=123,
        planFile="plan.md",
        environment="dev",
        appName="test-app"
    )
    mock_post.side_effect = RuntimeError("API error")

    # Should not raise
    handler_module.post_test_failure_comment(
        detail=detail,
        github_token="fake-token",
        test_output="error",
        failure_reason="failed",
        github_usernames="user"
    )


@patch.object(handler_module, "run_command")
def test_update_plan_with_autofix_cost_no_cost(mock_run_cmd, tmp_path):
    """Test update_plan skips when cost is zero."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6, issueTitle="Test", repoFullName="user/repo",
        branchName="branch", baseBranch="main", planFile="plan.md",
        environment="dev", appName="test-app"
    )
    autofix_cost = {"cost_usd": 0, "input_tokens": 0, "output_tokens": 0}

    result = handler_module.update_plan_with_autofix_cost(
        str(tmp_path), "plan.md", autofix_cost, "token", detail
    )

    assert result is True
    mock_run_cmd.assert_not_called()


@patch.object(handler_module, "run_command")
def test_update_plan_with_autofix_cost_missing_plan(mock_run_cmd, tmp_path):
    """Test update_plan returns False when plan file missing."""
    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6, issueTitle="Test", repoFullName="user/repo",
        branchName="branch", baseBranch="main", planFile="plan.md",
        environment="dev", appName="test-app"
    )
    autofix_cost = {"cost_usd": 0.001, "input_tokens": 100, "output_tokens": 50}

    result = handler_module.update_plan_with_autofix_cost(
        str(tmp_path), "plan.md", autofix_cost, "token", detail
    )

    assert result is False


@patch.object(handler_module, "run_command")
def test_update_plan_with_autofix_cost_grand_total(mock_run_cmd, tmp_path):
    """Test updating plan with existing Grand Total."""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("""## Cost Summary

### Code Generation (Sonnet)

**Cost:** $0.010000

### Grand Total

**Total Cost:** $0.010000
""")

    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6, issueTitle="Test", repoFullName="user/repo",
        branchName="branch", baseBranch="main", planFile="plan.md",
        environment="dev", appName="test-app"
    )
    autofix_cost = {"cost_usd": 0.005000, "input_tokens": 200, "output_tokens": 100}

    mock_run_cmd.return_value = handler_module.CommandResult(
        command=["git"], exit_code=0, stdout="", stderr="", duration_seconds=0.1
    )

    result = handler_module.update_plan_with_autofix_cost(
        str(tmp_path), "plan.md", autofix_cost, "token", detail
    )

    assert result is True
    updated = plan_file.read_text()
    assert "Auto-Fix (Sonnet)" in updated
    assert "$0.015000" in updated


@patch.object(handler_module, "run_command")
def test_update_plan_with_autofix_cost_no_grand_total(mock_run_cmd, tmp_path):
    """Test updating plan with Cost Summary but no Grand Total."""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("""## Cost Summary

### Code Generation (Sonnet)

**Cost:** $0.010000

## Implementation
""")

    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6, issueTitle="Test", repoFullName="user/repo",
        branchName="branch", baseBranch="main", planFile="plan.md",
        environment="dev", appName="test-app"
    )
    autofix_cost = {"cost_usd": 0.005, "input_tokens": 200, "output_tokens": 100}

    mock_run_cmd.return_value = handler_module.CommandResult(
        command=["git"], exit_code=0, stdout="", stderr="", duration_seconds=0.1
    )

    result = handler_module.update_plan_with_autofix_cost(
        str(tmp_path), "plan.md", autofix_cost, "token", detail
    )

    assert result is True
    updated = plan_file.read_text()
    assert "Auto-Fix (Sonnet)" in updated


@patch.object(handler_module, "run_command")
def test_update_plan_with_autofix_cost_no_cost_summary(mock_run_cmd, tmp_path):
    """Test updating plan with no Cost Summary section."""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Implementation\n\nSome content here.")

    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6, issueTitle="Test", repoFullName="user/repo",
        branchName="branch", baseBranch="main", planFile="plan.md",
        environment="dev", appName="test-app"
    )
    autofix_cost = {"cost_usd": 0.005, "input_tokens": 200, "output_tokens": 100}

    mock_run_cmd.return_value = handler_module.CommandResult(
        command=["git"], exit_code=0, stdout="", stderr="", duration_seconds=0.1
    )

    result = handler_module.update_plan_with_autofix_cost(
        str(tmp_path), "plan.md", autofix_cost, "token", detail
    )

    assert result is True
    updated = plan_file.read_text()
    assert "Cost Summary" in updated
    assert "Auto-Fix (Sonnet)" in updated


@patch.object(handler_module, "run_command")
def test_update_plan_with_autofix_cost_push_fails(mock_run_cmd, tmp_path):
    """Test update_plan returns False when push fails."""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Cost Summary\n\n### Grand Total\n\n**Total Cost:** $0.01")

    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6, issueTitle="Test", repoFullName="user/repo",
        branchName="branch", baseBranch="main", planFile="plan.md",
        environment="dev", appName="test-app"
    )
    autofix_cost = {"cost_usd": 0.005, "input_tokens": 200, "output_tokens": 100}

    def run_cmd_side_effect(cmd, **kwargs):
        if "push" in cmd:
            return handler_module.CommandResult(
                command=cmd, exit_code=1, stdout="",
                stderr="push rejected", duration_seconds=0.1
            )
        return handler_module.CommandResult(
            command=cmd, exit_code=0, stdout="", stderr="", duration_seconds=0.1
        )

    mock_run_cmd.side_effect = run_cmd_side_effect

    result = handler_module.update_plan_with_autofix_cost(
        str(tmp_path), "plan.md", autofix_cost, "token", detail
    )

    assert result is False


@patch.object(handler_module, "run_command")
def test_update_plan_with_autofix_cost_exception(mock_run_cmd, tmp_path):
    """Test update_plan handles exceptions gracefully."""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Cost Summary\n\n### Grand Total\n\n**Total Cost:** $0.01")

    detail = handler_module.CodeGenerationCompletedDetail(
        issueNumber=6, issueTitle="Test", repoFullName="user/repo",
        branchName="branch", baseBranch="main", planFile="plan.md",
        environment="dev", appName="test-app"
    )
    autofix_cost = {"cost_usd": 0.005, "input_tokens": 200, "output_tokens": 100}

    mock_run_cmd.side_effect = RuntimeError("Unexpected error")

    result = handler_module.update_plan_with_autofix_cost(
        str(tmp_path), "plan.md", autofix_cost, "token", detail
    )

    assert result is False


def test_cleanup_workspace(tmp_path):
    """Test cleanup_workspace removes directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("test")

    handler_module.cleanup_workspace(str(workspace))

    assert not workspace.exists()
