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


def fake_subprocess_run_factory(tmp_path: Path, fail_tests: bool = False):
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

    response = handler_module.handler(build_event(), None)

    assert response["success"] is False
    assert response["status"] == "failed"
    assert mock_s3.put_object.call_count == 2
    payload = json.loads(
        mock_events.put_events.call_args.kwargs["Entries"][0]["Detail"]
    )
    assert payload["success"] is False
    # After adding error classification, failure reason now includes the error type
    assert payload["failureReason"] == "Tests failed (logic_error)"


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


@patch.object(handler_module, "bedrock_client")
def test_apply_fix_to_file_success(mock_bedrock, tmp_path):
    """Successfully apply a fix to a file."""
    # Arrange
    test_file = tmp_path / "test.py"
    test_file.write_text("def foo():\n  pass\n")

    mock_bedrock.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": "def foo():\n    import os\n    pass\n"}]
            }
        }
    }
    error_output = "ModuleNotFoundError: No module named 'os'"

    # Act
    result = handler_module.apply_fix_to_file(
        str(test_file),
        error_output,
        "import_error"
    )

    # Assert
    assert result is True
    fixed_content = test_file.read_text()
    assert "import os" in fixed_content
    mock_bedrock.converse.assert_called_once()


@patch.object(handler_module, "bedrock_client")
def test_apply_fix_to_file_removes_markdown_fences(mock_bedrock, tmp_path):
    """Remove markdown code fences from Claude's response."""
    # Arrange
    test_file = tmp_path / "test.py"
    test_file.write_text("def foo():\n  pass\n")

    mock_bedrock.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": "```python\ndef foo():\n    import os\n    pass\n```"}]
            }
        }
    }

    # Act
    result = handler_module.apply_fix_to_file(
        str(test_file),
        "error",
        "import_error"
    )

    # Assert
    assert result is True
    fixed_content = test_file.read_text()
    assert "```python" not in fixed_content
    assert "```" not in fixed_content


@patch.object(handler_module, "bedrock_client")
def test_apply_fix_to_file_empty_response(mock_bedrock, tmp_path):
    """Handle empty Claude response."""
    # Arrange
    test_file = tmp_path / "test.py"
    test_file.write_text("original content")

    mock_bedrock.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": ""}]
            }
        }
    }

    # Act
    result = handler_module.apply_fix_to_file(
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
