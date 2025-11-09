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


@patch.object(handler_module, "events_client")
@patch.object(handler_module, "s3_client")
@patch.object(handler_module, "ssm_client")
def test_handler_flags_failed_tests(mock_ssm, mock_s3, mock_events, tmp_path, monkeypatch):
    """If make test exits non-zero, handler reports failure and still uploads logs."""
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "ghp_test_token"}}

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
    assert payload["failureReason"] == "Tests failed"


def test_handler_rejects_invalid_event():
    """Missing required fields should raise ValidationError."""
    with pytest.raises(Exception):
        handler_module.handler({"detail": {}}, None)
