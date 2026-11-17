# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for process-pr-check Lambda handler.

Tests cover:
- Schema validation (Pydantic models)
- SQS message processing
- Check job routing
- GitHub comment posting
- DynamoDB result storage
- End-to-end orchestration
"""

import json
import os
import sys
from datetime import datetime
from unittest.mock import Mock, patch
import importlib.util
from types import ModuleType

import pytest
from pydantic import ValidationError

# Set up the check_handlers package with all modules
# This is needed because handler.py imports from check_handlers
check_handlers_dir = os.path.join(os.path.dirname(__file__), '../../process-pr-check/check_handlers')

# First, load bedrock_utils
bedrock_utils_path = os.path.join(check_handlers_dir, 'bedrock_utils.py')
bedrock_spec = importlib.util.spec_from_file_location("bedrock_utils", bedrock_utils_path)
bedrock_module = importlib.util.module_from_spec(bedrock_spec)
bedrock_spec.loader.exec_module(bedrock_module)

# Create check_handlers package
check_handlers_package = ModuleType("check_handlers")
check_handlers_package.__path__ = [check_handlers_dir]
check_handlers_package.bedrock_utils = bedrock_module
check_handlers_package.UsageTracker = bedrock_module.UsageTracker
sys.modules['check_handlers'] = check_handlers_package
sys.modules['check_handlers.bedrock_utils'] = bedrock_module

# Helper to load a check handler module
def load_check_handler(name):
    path = os.path.join(check_handlers_dir, f'{name}.py')
    spec = importlib.util.spec_from_file_location(
        f"check_handlers.{name}",
        path,
        submodule_search_locations=[check_handlers_dir]
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "check_handlers"
    sys.modules[f'check_handlers.{name}'] = module
    spec.loader.exec_module(module)
    return module

# Load all check handler modules
adr_compliance_module = load_check_handler('adr_compliance')
architectural_duplication_module = load_check_handler('architectural_duplication')
breaking_changes_module = load_check_handler('breaking_changes')
license_compliance_module = load_check_handler('license_compliance')
readme_freshness_module = load_check_handler('readme_freshness')
test_coverage_module = load_check_handler('test_coverage')

# Set up exports in check_handlers package
check_handlers_package.check_adr_compliance = adr_compliance_module.check_adr_compliance
check_handlers_package.check_architectural_duplication = architectural_duplication_module.check_architectural_duplication
check_handlers_package.check_breaking_changes = breaking_changes_module.check_breaking_changes
check_handlers_package.check_license_compliance = license_compliance_module.check_license_compliance
check_handlers_package.check_readme_freshness = readme_freshness_module.check_readme_freshness
check_handlers_package.check_test_coverage = test_coverage_module.check_test_coverage

# Add process-pr-check directory to Python path for handler.py
process_pr_check_dir = os.path.join(os.path.dirname(__file__), '../../process-pr-check')
sys.path.insert(0, process_pr_check_dir)

# Load the process-pr-check handler module with unique name
handler_path = os.path.join(process_pr_check_dir, 'handler.py')
spec = importlib.util.spec_from_file_location("process_pr_check_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['process_pr_check_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Clean up sys.path after import
sys.path.remove(process_pr_check_dir)

# Import from the loaded module
handler = handler_module.handler
CheckType = handler_module.CheckType
CheckStatus = handler_module.CheckStatus
CheckJob = handler_module.CheckJob
CheckResult = handler_module.CheckResult
process_check_job = handler_module.process_check_job
format_check_comment = handler_module.format_check_comment
get_github_token = handler_module.get_github_token
post_pr_comment = handler_module.post_pr_comment
store_check_result = handler_module.store_check_result
run_check = handler_module.run_check


class TestSchemas:
    """Test Pydantic schema validation."""

    def test_check_job_valid(self):
        """Test valid CheckJob creation."""
        # Arrange & Act
        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["lambda/hello/handler.py"]
        )

        # Assert
        assert job.check_type == CheckType.ADR_COMPLIANCE
        assert job.pr_number == 123
        assert job.repository == "owner/repo"
        assert job.changed_files == ["lambda/hello/handler.py"]

    def test_check_job_invalid_pr_number(self):
        """Test CheckJob with invalid PR number."""
        # Arrange & Act & Assert
        with pytest.raises(ValidationError):
            CheckJob(
                checkType="ADR_COMPLIANCE",
                pr_number=-1,  # Invalid: must be > 0
                repository="owner/repo",
                changedFiles=[]
            )

    def test_check_result_valid(self):
        """Test valid CheckResult creation."""
        # Arrange & Act
        result = CheckResult(
            PK="PR#123",
            SK="CHECK#adr_compliance",
            checkType="ADR_COMPLIANCE",
            status="PASS",
            message="All files follow ADR standards",
            details=["Handler uses Pydantic schemas"],
            timestamp="2025-01-15T10:00:00Z",
            commentUrl="https://github.com/owner/repo/pull/123#issuecomment-456"
        )

        # Assert
        assert result.PK == "PR#123"
        assert result.SK == "CHECK#adr_compliance"
        assert result.check_type == CheckType.ADR_COMPLIANCE
        assert result.status == CheckStatus.PASS
        assert result.comment_url == "https://github.com/owner/repo/pull/123#issuecomment-456"


class TestFormatCheckComment:
    """Test formatting check results as GitHub comments."""

    def test_format_pass_comment(self):
        """Test formatting a PASS result."""
        # Arrange
        result = CheckResult(
            PK="PR#123",
            SK="CHECK#test_coverage",
            checkType="TEST_COVERAGE",
            status="PASS",
            message="All handlers have test coverage",
            details=["lambda/hello/handler.py: Has test_hello.py"],
            timestamp="2025-01-15T10:00:00Z"
        )

        # Act
        comment = format_check_comment(result)

        # Assert -- raw Unicode emoji so ADO renders the same as GH.
        assert "✅" in comment
        assert "TEST COVERAGE" in comment
        assert "All handlers have test coverage" in comment
        assert "lambda/hello/handler.py" in comment
        assert "2025-01-15T10:00:00Z" in comment

    def test_format_warn_comment(self):
        """Test formatting a WARN result."""
        # Arrange
        result = CheckResult(
            PK="PR#123",
            SK="CHECK#readme_freshness",
            checkType="README_FRESHNESS",
            status="WARN",
            message="README.md not updated",
            details=["Infrastructure files changed but README not updated"],
            timestamp="2025-01-15T10:00:00Z"
        )

        # Act
        comment = format_check_comment(result)

        # Assert -- raw Unicode emoji so ADO renders the same as GH.
        assert "⚠️" in comment
        assert "README FRESHNESS" in comment
        assert "README.md not updated" in comment

    def test_format_comment_is_wrapped_in_collapsible_details(self):
        """Comment must open with <details><summary>... so reviewers see
        a one-line row and expand only when they want the body. Both
        GitHub and ADO markdown render this natively."""
        result = CheckResult(
            PK="PR#github#owner/repo#1",
            SK="CHECK#breaking_changes",
            checkType="BREAKING_CHANGES",
            status="PASS",
            message="No dependencies found - safe to modify",
            details=["Checked 3 handler(s) for external callers"],
            timestamp="2025-01-15T10:00:00Z",
        )
        comment = format_check_comment(result)

        assert comment.startswith("<details>"), (
            "collapsible: comment must open with <details>"
        )
        assert comment.rstrip().endswith("</details>"), (
            "collapsible: comment must close with </details>"
        )
        assert "<summary>" in comment and "</summary>" in comment

        # The summary line carries the scannable bits (glyph + name + message).
        summary_start = comment.index("<summary>") + len("<summary>")
        summary_end = comment.index("</summary>")
        summary_line = comment[summary_start:summary_end]
        assert "✅" in summary_line
        assert "BREAKING CHANGES" in summary_line
        assert "No dependencies found - safe to modify" in summary_line

        # Details + timestamp live INSIDE the block so they're hidden
        # by default.
        body_start = summary_end
        body = comment[body_start:]
        assert "Checked 3 handler(s) for external callers" in body
        assert "2025-01-15T10:00:00Z" in body

    def test_format_comment_has_blank_line_after_summary(self):
        """Markdown-inside-HTML requires a blank line after </summary>
        or GH/ADO won't render bold/lists/etc. inside the details block."""
        result = CheckResult(
            PK="PR#github#owner/repo#1",
            SK="CHECK#adr_compliance",
            checkType="ADR_COMPLIANCE",
            status="WARN",
            message="Found compliance issues",
            details=["- Handler missing schema validation"],
            timestamp="2025-01-15T10:00:00Z",
        )
        comment = format_check_comment(result)
        assert "</summary>\n\n" in comment, (
            "markdown-in-html requires blank line after </summary>"
        )


class TestGetGitHubToken:
    """Test fetching GitHub token from SSM."""

    @patch('process_pr_check_handler.ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test successful token retrieval."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "test-token-123"}
        }

        # Act
        token = get_github_token()

        # Assert
        assert token == "test-token-123"
        mock_ssm.get_parameter.assert_called_once()

    @patch('process_pr_check_handler.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test token not found in SSM."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": ""}
        }

        # Act & Assert
        with pytest.raises(Exception, match="GitHub token not found"):
            get_github_token()


class TestPostPrComment:
    """Test posting comments to GitHub PR."""

    @patch('process_pr_check_handler.requests.post')
    def test_post_pr_comment_success(self, mock_post):
        """Test successful comment posting."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": 456,
            "html_url": "https://github.com/owner/repo/pull/123#issuecomment-456",
            "body": "Test comment"
        }
        mock_post.return_value = mock_response

        # Act
        result = post_pr_comment("owner/repo", 123, "Test comment", "test-token")

        # Assert
        assert result["id"] == 456
        assert "issuecomment-456" in result["html_url"]
        mock_post.assert_called_once()

    @patch('process_pr_check_handler.requests.post')
    def test_post_pr_comment_api_error(self, mock_post):
        """Test comment posting with API error."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.raise_for_status.side_effect = Exception("Forbidden")
        mock_post.return_value = mock_response

        # Act & Assert
        with pytest.raises(Exception):
            post_pr_comment("owner/repo", 123, "Test comment", "test-token")


class TestPatchPrComment:
    """Test updating existing PR comments -- chunk 01 of the update-in-place plan."""

    @patch('process_pr_check_handler.requests.patch')
    def test_patch_pr_comment_success(self, mock_patch_req):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": 456,
            "html_url": "https://github.com/owner/repo/pull/123#issuecomment-456",
            "body": "Updated body",
        }
        mock_patch_req.return_value = mock_response

        result = handler_module.patch_pr_comment(
            "owner/repo", 456, "Updated body", "ghp_xxx",
        )
        assert result["id"] == 456
        assert result["body"] == "Updated body"

        # URL shape: /repos/{r}/issues/comments/{cid}
        call_args = mock_patch_req.call_args
        assert call_args.args[0] == (
            "https://api.github.com/repos/owner/repo/issues/comments/456"
        )
        # Header + payload shape
        headers = call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer ghp_xxx"
        assert headers["Accept"] == "application/vnd.github.v3+json"
        assert call_args.kwargs["json"] == {"body": "Updated body"}

    @patch('process_pr_check_handler.requests.patch')
    def test_patch_pr_comment_404_raises(self, mock_patch_req):
        # Comment was deleted between POST and next PATCH attempt --
        # let the caller catch this and fall back to POST (chunk 03
        # logic).
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = (
            handler_module.requests.exceptions.HTTPError("404 Not Found")
        )
        mock_patch_req.return_value = mock_response

        with pytest.raises(Exception, match="GitHub API error"):
            handler_module.patch_pr_comment(
                "owner/repo", 999, "body", "ghp_xxx",
            )


class TestStoreCheckResult:
    """Test storing check results in DynamoDB."""

    @patch('process_pr_check_handler.dynamodb')
    def test_store_check_result_success(self, mock_dynamodb):
        """Test successful result storage."""
        # Arrange
        mock_table = Mock()
        mock_dynamodb.Table.return_value = mock_table

        result = CheckResult(
            PK="PR#123",
            SK="CHECK#adr_compliance",
            checkType="ADR_COMPLIANCE",
            status="PASS",
            message="All files follow ADR standards",
            details=["Handler uses Pydantic schemas"],
            timestamp="2025-01-15T10:00:00Z",
            commentUrl="https://github.com/owner/repo/pull/123#issuecomment-456"
        )

        # Act
        store_check_result(result)

        # Assert
        mock_table.put_item.assert_called_once()
        call_args = mock_table.put_item.call_args[1]
        assert call_args["Item"]["PK"] == "PR#123"
        assert call_args["Item"]["SK"] == "CHECK#adr_compliance"
        assert call_args["Item"]["status"] == "PASS"


class TestRunCheck:
    """Test routing to check handlers."""

    @patch('process_pr_check_handler.check_test_coverage')
    def test_run_check_test_coverage(self, mock_check):
        """Test routing to test coverage check."""
        # Arrange
        job = CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["lambda/hello/handler.py"]
        )

        mock_check.return_value = {
            "checkType": "TEST_COVERAGE",
            "status": "PASS",
            "message": "All handlers have test coverage",
            "details": []
        }

        # Act
        result = run_check(job)

        # Assert
        assert result["status"] == "PASS"
        mock_check.assert_called_once()

    def test_run_check_unknown_type(self):
        """Test routing with unknown check type (should not happen with enum)."""
        # This test verifies that the enum prevents invalid check types
        # If we somehow bypass the enum, we should get an error

        # Arrange - Create job with bypassed enum
        job_dict = {
            "checkType": "INVALID_CHECK",
            "pr_number": 123,
            "repository": "owner/repo",
            "changedFiles": []
        }

        # Act & Assert
        with pytest.raises(ValidationError):
            CheckJob(**job_dict)


class TestProcessCheckJob:
    """Test end-to-end check job processing."""

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler.post_pr_comment')
    @patch('process_pr_check_handler.get_github_token')
    @patch('process_pr_check_handler.run_check')
    def test_process_check_job_success(
        self,
        mock_run_check,
        mock_get_token,
        mock_post_comment,
        mock_store
    ):
        """Test successful check job processing."""
        # Arrange
        job = CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["lambda/hello/handler.py"]
        )

        mock_run_check.return_value = {
            "checkType": "TEST_COVERAGE",
            "status": "PASS",
            "message": "All handlers have test coverage",
            "details": ["lambda/hello/handler.py: Has test_hello.py"]
        }

        mock_get_token.return_value = "test-token"

        mock_post_comment.return_value = {
            "id": 456,
            "html_url": "https://github.com/owner/repo/pull/123#issuecomment-456"
        }

        # Act -- isolate from the inline-findings subsystem: anchor computation
        # runs for TEST_COVERAGE/BREAKING_CHANGES and resolves its own token,
        # which is orthogonal to the comment-post flow under test here.
        with patch('process_pr_check_handler._compute_anchors', return_value={}):
            result = process_check_job(job)

        # Assert -- PK is self-contained per (provider, repo, PR)
        # per chunk 02 of the update-in-place plan.
        assert result.PK == "PR#github#owner/repo#123"
        assert result.SK == "CHECK#test_coverage"
        assert result.status == CheckStatus.PASS
        assert result.comment_url == "https://github.com/owner/repo/pull/123#issuecomment-456"

        mock_run_check.assert_called_once()
        mock_get_token.assert_called_once()
        mock_post_comment.assert_called_once()
        assert mock_store.call_count == 2  # Once before comment, once after
        # Provider-comment-id is extracted from POST response and lands on
        # the CheckResult so chunk 03's PATCH can target it.
        assert result.provider_comment_id == 456
        assert result.provider_thread_id is None
        assert result.provider_thread_comment_id is None


class TestCheckResultUpdateInPlaceFields:
    """Chunk 02: CheckResult schema carries provider comment identifiers +
    the widened PK shape so subsequent runs can PATCH the existing
    comment instead of POSTing a new one."""

    def test_defaults_all_provider_ids_to_none(self):
        # Backward compat: existing serialized rows had none of these
        # fields; a CheckResult with just the pre-widening fields
        # continues to construct cleanly and reports None for all three.
        r = CheckResult(
            PK="PR#github#owner/repo#42",
            SK="CHECK#adr_compliance",
            checkType=CheckType.ADR_COMPLIANCE,
            status=CheckStatus.PASS,
            message="ok", details=[], timestamp="2026-07-01T00:00:00Z",
        )
        assert r.provider_comment_id is None
        assert r.provider_thread_id is None
        assert r.provider_thread_comment_id is None

    def test_store_check_result_writes_new_fields_when_set(self):
        result = CheckResult(
            PK="PR#github#owner/repo#42",
            SK="CHECK#adr_compliance",
            checkType=CheckType.ADR_COMPLIANCE,
            status=CheckStatus.PASS,
            message="ok", details=[], timestamp="2026-07-01T00:00:00Z",
            provider_comment_id=12345,
        )
        with patch("process_pr_check_handler.dual_write.ddb_put_item") as mock_put:
            handler_module.store_check_result(result)
        item = mock_put.call_args.args[1]
        assert item["providerCommentId"] == 12345
        assert "providerThreadId" not in item      # None -> omitted
        assert "providerThreadCommentId" not in item

    def test_store_check_result_writes_ado_thread_fields(self):
        result = CheckResult(
            PK="PR#azure_devops#org/proj/repo#7",
            SK="CHECK#test_coverage",
            checkType=CheckType.TEST_COVERAGE,
            status=CheckStatus.WARN,
            message="warn", details=[], timestamp="2026-07-01T00:00:00Z",
            provider_thread_id=99,
            provider_thread_comment_id=1,
        )
        with patch("process_pr_check_handler.dual_write.ddb_put_item") as mock_put:
            handler_module.store_check_result(result)
        item = mock_put.call_args.args[1]
        assert item["providerThreadId"] == 99
        assert item["providerThreadCommentId"] == 1
        assert "providerCommentId" not in item

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler._post_azure_devops_thread')
    @patch('process_pr_check_handler.run_check')
    def test_ado_process_check_job_extracts_thread_and_comment_ids(
        self, mock_run, mock_post_ado, mock_store,
    ):
        # ADO POST response shape carries both {"id": thread_id} and
        # {"comments": [{"id": comment_id, ...}], ...}. Both surface on
        # the returned CheckResult.
        mock_run.return_value = {
            "status": "PASS", "message": "ok", "details": [],
        }
        mock_post_ado.return_value = {"id": 77, "comments": [{"id": 1, "content": "x"}]}

        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changedFiles=[],
            provider="azure_devops",
            connection_id="conn-xyz",
            org="example-org",
            project="example-project",
            repo="example-repo",
        )
        result = process_check_job(job)
        assert result.PK == "PR#azure_devops#example-org/example-project/example-repo#42"
        assert result.provider_thread_id == 77
        assert result.provider_thread_comment_id == 1
        assert result.provider_comment_id is None


class TestUpsertBehavior:
    """Chunk 03 of the update-in-place plan: process_check_job
    PATCHes an existing comment instead of POSTing on subsequent runs."""

    def _fresh_job_github(self):
        return CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["src/x.py"],
        )

    def _fresh_job_ado(self):
        return CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changedFiles=["src/x.py"],
            provider="azure_devops",
            connection_id="conn-xyz",
            org="example-org",
            project="example-project",
            repo="example-repo",
        )

    def _default_check_result(self):
        return {"status": "PASS", "message": "ok", "details": []}

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler.post_pr_comment')
    @patch('process_pr_check_handler.patch_pr_comment')
    @patch('process_pr_check_handler.get_github_token', return_value="ghp")
    @patch('process_pr_check_handler._get_existing_check_result_row', return_value=None)
    @patch('process_pr_check_handler.run_check')
    def test_first_run_posts_new_comment(
        self, mock_run, _mock_exist, _mock_tok, mock_patch, mock_post, mock_store,
    ):
        # No existing row -> POST path fires, PATCH doesn't.
        mock_run.return_value = self._default_check_result()
        mock_post.return_value = {
            "id": 111,
            "html_url": "https://github.com/o/r/pull/1#issuecomment-111",
        }
        result = process_check_job(self._fresh_job_github())
        mock_post.assert_called_once()
        mock_patch.assert_not_called()
        assert result.provider_comment_id == 111

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler.post_pr_comment')
    @patch('process_pr_check_handler.patch_pr_comment')
    @patch('process_pr_check_handler.get_github_token', return_value="ghp")
    @patch('process_pr_check_handler._get_existing_check_result_row')
    @patch('process_pr_check_handler.run_check')
    def test_second_run_with_github_id_patches_instead_of_posting(
        self, mock_run, mock_exist, _mock_tok, mock_patch, mock_post, mock_store,
    ):
        mock_run.return_value = self._default_check_result()
        mock_exist.return_value = {
            "PK": "PR#github#owner/repo#123",
            "SK": "CHECK#test_coverage",
            "providerCommentId": 111,
            "commentUrl": "https://github.com/owner/repo/pull/123#issuecomment-111",
        }
        mock_patch.return_value = {
            "id": 111,
            "html_url": "https://github.com/owner/repo/pull/123#issuecomment-111",
        }
        result = process_check_job(self._fresh_job_github())
        mock_patch.assert_called_once()
        assert mock_patch.call_args.args[1] == 111  # comment_id
        mock_post.assert_not_called()
        # ID preserved on the new row.
        assert result.provider_comment_id == 111

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler.post_pr_comment')
    @patch('process_pr_check_handler.patch_pr_comment',
           side_effect=Exception("GitHub API error: 404"))
    @patch('process_pr_check_handler.get_github_token', return_value="ghp")
    @patch('process_pr_check_handler._get_existing_check_result_row')
    @patch('process_pr_check_handler.run_check')
    def test_patch_failure_falls_back_to_post(
        self, mock_run, mock_exist, _mock_tok, mock_patch, mock_post, mock_store,
    ):
        # PATCH raises (comment deleted, auth error, etc.) -> POST fires.
        mock_run.return_value = self._default_check_result()
        mock_exist.return_value = {
            "PK": "PR#github#owner/repo#123",
            "SK": "CHECK#test_coverage",
            "providerCommentId": 999,  # stale (deleted comment)
        }
        mock_post.return_value = {
            "id": 222,  # brand new comment
            "html_url": "https://github.com/owner/repo/pull/123#issuecomment-222",
        }
        result = process_check_job(self._fresh_job_github())
        mock_patch.assert_called_once()
        mock_post.assert_called_once()
        assert result.provider_comment_id == 222  # new POST'd id, not stale

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler._post_azure_devops_thread')
    @patch('process_pr_check_handler._try_patch_ado_comment')
    @patch('process_pr_check_handler._get_existing_check_result_row')
    @patch('process_pr_check_handler.run_check')
    def test_second_run_with_ado_thread_id_patches(
        self, mock_run, mock_exist, mock_patch_ado, mock_post_ado, mock_store,
    ):
        mock_run.return_value = self._default_check_result()
        mock_exist.return_value = {
            "PK": "PR#azure_devops#example-org/example-project/example-repo#42",
            "SK": "CHECK#test_coverage",
            "providerThreadId": 77,
            "providerThreadCommentId": 1,
        }
        mock_patch_ado.return_value = (
            "https://dev.azure.com/example-org/example-project/_git/example-repo"
            "/pullrequest/42?_a=overview&discussionId=77"
        )
        result = process_check_job(self._fresh_job_ado())
        mock_patch_ado.assert_called_once()
        mock_post_ado.assert_not_called()
        assert result.provider_thread_id == 77
        assert result.provider_thread_comment_id == 1

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler._post_azure_devops_thread')
    @patch('process_pr_check_handler._try_patch_ado_comment', return_value=None)
    @patch('process_pr_check_handler._get_existing_check_result_row')
    @patch('process_pr_check_handler.run_check')
    def test_ado_patch_returning_none_falls_back_to_post(
        self, mock_run, mock_exist, _mock_patch, mock_post_ado, mock_store,
    ):
        # _try_patch_ado_comment returning None means "couldn't PATCH,
        # fall back to POST". Row has no IDs (never posted before),
        # simulated by a row without them.
        mock_run.return_value = self._default_check_result()
        mock_exist.return_value = {
            "PK": "PR#azure_devops#example-org/example-project/example-repo#42",
            "SK": "CHECK#test_coverage",
            # no providerThreadId / providerThreadCommentId
        }
        mock_post_ado.return_value = {
            "id": 88, "comments": [{"id": 1, "content": "x"}],
        }
        result = process_check_job(self._fresh_job_ado())
        mock_post_ado.assert_called_once()
        assert result.provider_thread_id == 88
        assert result.provider_thread_comment_id == 1


class TestLicenseComplianceHelpers:
    """Test license compliance helper functions."""

    def test_detect_license_patterns_prohibited(self):
        """Test detecting prohibited license patterns."""
        text = "This code is under the GNU General Public License v3"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "GPL" in findings["prohibited"]

    def test_detect_license_patterns_agpl(self):
        """Test detecting AGPL license."""
        text = "Licensed under the GNU Affero General Public License"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "AGPL" in findings["prohibited"]

    def test_detect_license_patterns_sspl(self):
        """Test detecting SSPL license."""
        text = "Server Side Public License"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "SSPL" in findings["prohibited"]

    def test_detect_license_patterns_allowed(self):
        """Test detecting allowed license patterns."""
        text = "MIT License\n\nPermission is hereby granted..."
        findings = license_compliance_module.detect_license_patterns(text)
        assert "MIT" in findings["allowed"]
        assert not findings["prohibited"]

    def test_detect_license_patterns_apache(self):
        """Test detecting Apache 2.0 license."""
        text = "Licensed under the Apache License, Version 2.0"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "Apache-2.0" in findings["allowed"]

    def test_detect_license_patterns_bsd(self):
        """Test detecting BSD license."""
        text = "BSD 3-Clause License"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "BSD" in findings["allowed"]

    def test_detect_license_patterns_isc(self):
        """Test detecting ISC license."""
        text = "ISC License"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "ISC" in findings["allowed"]

    def test_detect_license_patterns_review_required(self):
        """Test detecting review-required license patterns."""
        text = "GNU Lesser General Public License"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "LGPL" in findings["review_required"]

    def test_detect_license_patterns_mpl(self):
        """Test detecting MPL license."""
        text = "Mozilla Public License v2.0"
        findings = license_compliance_module.detect_license_patterns(text)
        assert "MPL" in findings["review_required"]

    def test_detect_license_patterns_no_match(self):
        """Test no findings when no license text present."""
        text = "def hello():\n    return 'world'"
        findings = license_compliance_module.detect_license_patterns(text)
        assert not findings["prohibited"]
        assert not findings["allowed"]
        assert not findings["review_required"]

    def test_detect_license_patterns_no_duplicates(self):
        """Test no duplicate entries when pattern matches multiple times."""
        text = "GPL-2.0 and also GPL-3.0 license"
        findings = license_compliance_module.detect_license_patterns(text)
        assert findings["prohibited"].count("GPL") == 1

    def test_check_copyright_header_present(self):
        """Test detecting copyright header in diff."""
        diff = "+# Copyright 2024 OutcomeOps\n+# All rights reserved"
        result = license_compliance_module.check_copyright_header(diff)
        assert result is True

    def test_check_copyright_header_spdx(self):
        """Test detecting SPDX identifier in diff."""
        diff = "+# SPDX-License-Identifier: Apache-2.0\n+code here"
        result = license_compliance_module.check_copyright_header(diff)
        assert result is True

    def test_check_copyright_header_missing(self):
        """Test when no copyright header present."""
        diff = "+def foo():\n+    return 42"
        result = license_compliance_module.check_copyright_header(diff)
        assert result is False

    def test_check_copyright_header_ignores_removed_lines(self):
        """Test that removed lines are not counted."""
        diff = "-# Copyright 2024 Company\n+def foo():\n+    return 42"
        result = license_compliance_module.check_copyright_header(diff)
        assert result is False

    def test_is_source_file_python(self):
        """Test python file recognized as source."""
        assert license_compliance_module.is_source_file("lambda/handler.py") is True

    def test_is_source_file_typescript(self):
        """Test typescript file recognized as source."""
        assert license_compliance_module.is_source_file("src/app.ts") is True

    def test_is_source_file_javascript(self):
        """Test javascript file recognized as source."""
        assert license_compliance_module.is_source_file("src/utils.js") is True

    def test_is_source_file_java(self):
        """Test java file recognized as source."""
        assert license_compliance_module.is_source_file("src/main/App.java") is True

    def test_is_source_file_markdown(self):
        """Test markdown file is recognized (for code block scanning)."""
        assert license_compliance_module.is_source_file("docs/guide.md") is True

    def test_is_source_file_node_modules(self):
        """Test node_modules files excluded."""
        assert license_compliance_module.is_source_file("node_modules/foo/index.js") is False

    def test_is_source_file_vendor(self):
        """Test vendor files excluded."""
        assert license_compliance_module.is_source_file("vendor/lib/file.go") is False

    def test_is_source_file_test_directory(self):
        """Test test directory files excluded."""
        assert license_compliance_module.is_source_file("src/tests/test_foo.py") is False

    def test_is_source_file_test_prefix(self):
        """Test test_ prefixed files excluded."""
        assert license_compliance_module.is_source_file("lambda/test_handler.py") is False

    def test_is_source_file_dist(self):
        """Test dist directory files excluded."""
        assert license_compliance_module.is_source_file("dist/bundle.js") is False

    def test_is_source_file_build(self):
        """Test build directory files excluded."""
        assert license_compliance_module.is_source_file("build/output.js") is False

    def test_is_source_file_unsupported_extension(self):
        """Test unsupported extension not recognized."""
        assert license_compliance_module.is_source_file("data.csv") is False

    def test_is_new_file(self):
        """Test new file detection in diff."""
        diff = "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null"
        assert license_compliance_module.is_new_file(diff) is True

    def test_is_not_new_file(self):
        """Test modified file detection in diff."""
        diff = "diff --git a/existing.py b/existing.py\n--- a/existing.py"
        assert license_compliance_module.is_new_file(diff) is False

    def test_normalize_code_removes_comments(self):
        """Test code normalization removes comments."""
        code = "# comment\ndef foo():  # inline\n    pass"
        result = license_compliance_module.normalize_code(code)
        assert "# comment" not in result
        assert "# inline" not in result
        assert "def foo():" in result

    def test_normalize_code_removes_multiline_comments(self):
        """Test code normalization removes multi-line comments."""
        code = "/* multi\nline\ncomment */\nvar x = 1;"
        result = license_compliance_module.normalize_code(code)
        assert "multi" not in result
        assert "var x = 1;" in result

    def test_normalize_code_removes_js_comments(self):
        """Test code normalization removes // comments."""
        code = "const x = 1; // comment\nconst y = 2;"
        result = license_compliance_module.normalize_code(code)
        assert "// comment" not in result

    def test_normalize_code_strips_whitespace(self):
        """Test code normalization strips whitespace."""
        code = "  def foo():  \n    pass  \n\n\n"
        result = license_compliance_module.normalize_code(code)
        lines = result.split("\n")
        for line in lines:
            assert line == line.strip()

    def test_quick_pattern_check_no_match(self):
        """Test quick pattern check returns None for clean code."""
        code = "def hello():\n    return 'world'"
        result = license_compliance_module.quick_pattern_check(code)
        assert result is None

    def test_quick_pattern_check_gpl_pattern(self):
        """Test quick pattern check detects GPL algorithm patterns."""
        code = "def quicksort(arr):\n    pivot = arr[0]\n    partition(arr, pivot)"
        result = license_compliance_module.quick_pattern_check(code)
        assert result is not None
        assert result["match_type"] == "pattern"

    def test_extract_file_diff_from_full_diff(self):
        """Test extracting a specific file's diff from full PR diff."""
        full_diff = (
            "diff --git a/foo.py b/foo.py\n"
            "+added line\n"
            "diff --git a/bar.py b/bar.py\n"
            "+another line\n"
        )
        result = license_compliance_module.extract_file_diff_from_full_diff(full_diff, "foo.py")
        assert "added line" in result
        assert "another line" not in result

    def test_extract_file_diff_not_found(self):
        """Test extracting diff for file not in full diff."""
        full_diff = "diff --git a/foo.py b/foo.py\n+line\n"
        result = license_compliance_module.extract_file_diff_from_full_diff(full_diff, "missing.py")
        assert result == ""

    def test_is_ai_generated_commit_claude_code(self):
        """Test detecting Claude Code generated commit."""
        commit = {"message": "feat: add handler\n\nGenerated with Claude Code"}
        assert license_compliance_module.is_ai_generated_commit(commit) is True

    def test_is_ai_generated_commit_co_author(self):
        """Test detecting Claude co-authored commit."""
        commit = {"message": "feat: add handler\n\nCo-Authored-By: Claude <noreply@anthropic.com>"}
        assert license_compliance_module.is_ai_generated_commit(commit) is True

    def test_is_not_ai_generated_commit(self):
        """Test human commit not flagged as AI."""
        commit = {"message": "fix: manual bug fix"}
        assert license_compliance_module.is_ai_generated_commit(commit) is False

    def test_is_ai_generated_commit_empty_message(self):
        """Test commit with empty message not flagged."""
        commit = {"message": ""}
        assert license_compliance_module.is_ai_generated_commit(commit) is False

    def test_is_ai_generated_commit_missing_message(self):
        """Test commit with missing message key."""
        commit = {}
        assert license_compliance_module.is_ai_generated_commit(commit) is False


class TestLicenseComplianceAnalyzeWithClaude:
    """Test Claude-based license analysis."""

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_with_claude_pass(self, mock_bedrock):
        """Test Claude analysis returning PASS."""
        mock_bedrock.return_value = (
            '{"status": "PASS", "explanation": "No issues", "suggestions": []}',
            {"inputTokens": 100, "outputTokens": 50}
        )
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_with_claude(
            "handler.py", {"prohibited": [], "allowed": ["MIT"], "review_required": []},
            True, False, "Use allowed licenses", tracker
        )

        assert result["status"] == "PASS"

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_with_claude_fail_prohibited(self, mock_bedrock):
        """Test Claude analysis with prohibited license."""
        mock_bedrock.return_value = (
            '{"status": "FAIL", "explanation": "GPL detected", "suggestions": ["Remove GPL code"]}',
            {"inputTokens": 100, "outputTokens": 50}
        )
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_with_claude(
            "lib.py", {"prohibited": ["GPL"], "allowed": [], "review_required": []},
            False, False, "No GPL", tracker
        )

        assert result["status"] == "FAIL"

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_with_claude_markdown_response(self, mock_bedrock):
        """Test Claude response wrapped in markdown code blocks."""
        mock_bedrock.return_value = (
            '```json\n{"status": "PASS", "explanation": "Clean", "suggestions": []}\n```',
            {"inputTokens": 100, "outputTokens": 50}
        )
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_with_claude(
            "test.py", {"prohibited": [], "allowed": [], "review_required": []},
            True, True, "policy", tracker
        )

        assert result["status"] == "PASS"

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_with_claude_exception_fallback_prohibited(self, mock_bedrock):
        """Test fallback when Claude fails with prohibited licenses."""
        mock_bedrock.side_effect = RuntimeError("API error")
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_with_claude(
            "test.py", {"prohibited": ["GPL"], "allowed": [], "review_required": []},
            False, False, "policy", tracker
        )

        assert result["status"] == "FAIL"

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_with_claude_exception_fallback_review(self, mock_bedrock):
        """Test fallback when Claude fails with review-required licenses."""
        mock_bedrock.side_effect = RuntimeError("API error")
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_with_claude(
            "test.py", {"prohibited": [], "allowed": [], "review_required": ["LGPL"]},
            False, False, "policy", tracker
        )

        assert result["status"] == "WARN"

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_with_claude_exception_fallback_new_no_copyright(self, mock_bedrock):
        """Test fallback when Claude fails for new file without copyright."""
        mock_bedrock.side_effect = RuntimeError("API error")
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_with_claude(
            "test.py", {"prohibited": [], "allowed": [], "review_required": []},
            False, True, "policy", tracker
        )

        assert result["status"] == "WARN"

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_with_claude_exception_fallback_pass(self, mock_bedrock):
        """Test fallback when Claude fails with no issues."""
        mock_bedrock.side_effect = RuntimeError("API error")
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_with_claude(
            "test.py", {"prohibited": [], "allowed": [], "review_required": []},
            True, False, "policy", tracker
        )

        assert result["status"] == "PASS"


class TestLicenseComplianceCodeSimilarity:
    """Test code similarity analysis."""

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_code_similarity_success(self, mock_bedrock):
        """Test successful Claude code similarity analysis."""
        mock_bedrock.return_value = (
            json.dumps({
                "problem_description": "JWT validation",
                "similar_projects": [
                    {"name": "jwt-lib", "license": "MIT", "similarity_percentage": 30,
                     "confidence": "LOW", "matching_patterns": ["common pattern"]}
                ]
            }),
            {"inputTokens": 200, "outputTokens": 100}
        )
        tracker = bedrock_module.UsageTracker()
        pattern_match = {"description": "JWT validator"}

        result = license_compliance_module.analyze_code_similarity_with_claude(
            "code", "file.py", pattern_match, tracker
        )

        assert result["analysis_successful"] is True
        assert len(result["similar_projects"]) == 1

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_code_similarity_failure_fallback(self, mock_bedrock):
        """Test fallback when Claude similarity analysis fails."""
        mock_bedrock.side_effect = RuntimeError("API error")
        tracker = bedrock_module.UsageTracker()
        pattern_match = {"description": "Sorting algorithm"}

        result = license_compliance_module.analyze_code_similarity_with_claude(
            "code", "sort.py", pattern_match, tracker
        )

        assert result["analysis_successful"] is False
        assert len(result["similar_projects"]) == 1
        assert result["similar_projects"][0]["confidence"] == "MEDIUM"

    @patch.object(license_compliance_module, 'call_bedrock')
    def test_analyze_code_similarity_markdown_response(self, mock_bedrock):
        """Test Claude similarity response wrapped in markdown."""
        mock_bedrock.return_value = (
            '```json\n{"problem_description": "test", "similar_projects": []}\n```',
            {"inputTokens": 200, "outputTokens": 100}
        )
        tracker = bedrock_module.UsageTracker()
        pattern_match = {"description": "test"}

        result = license_compliance_module.analyze_code_similarity_with_claude(
            "code", "file.py", pattern_match, tracker
        )

        assert result["analysis_successful"] is True


class TestLicenseComplianceGitHub:
    """Test GitHub API interactions for license compliance."""

    @patch.object(license_compliance_module, 'ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test successful token retrieval from SSM."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ghp_test123"}
        }

        token = license_compliance_module.get_github_token("/dev/token")
        assert token == "ghp_test123"

    @patch.object(license_compliance_module, 'ssm_client')
    def test_get_github_token_empty(self, mock_ssm):
        """Test empty token raises exception."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": ""}
        }

        with pytest.raises(Exception, match="not found"):
            license_compliance_module.get_github_token("/dev/token")

    @patch.object(license_compliance_module, 'ssm_client')
    def test_get_github_token_client_error(self, mock_ssm):
        """Test SSM client error propagates."""
        from botocore.exceptions import ClientError
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )

        with pytest.raises(ClientError):
            license_compliance_module.get_github_token("/dev/token")

    @patch('check_handlers.license_compliance.requests.get')
    def test_fetch_pr_file_diff_success(self, mock_get):
        """Test fetching PR file diff."""
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = (
            "diff --git a/foo.py b/foo.py\n"
            "+added line\n"
        )
        mock_resp.raise_for_status = Mock()
        mock_get.return_value = mock_resp

        result = license_compliance_module.fetch_pr_file_diff(
            "owner/repo", 1, "foo.py", "token"
        )
        assert "added line" in result

    @patch('check_handlers.license_compliance.requests.get')
    def test_fetch_pr_file_diff_not_found(self, mock_get):
        """Test fallback when file not found in diff."""
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = "diff --git a/other.py b/other.py\n+line\n"
        mock_resp.raise_for_status = Mock()
        mock_get.return_value = mock_resp

        result = license_compliance_module.fetch_pr_file_diff(
            "owner/repo", 1, "missing.py", "token"
        )
        assert "No diff available" in result

    @patch('check_handlers.license_compliance.requests.get')
    def test_fetch_pr_file_diff_api_error(self, mock_get):
        """Test GitHub API error raises exception."""
        import requests as req
        mock_get.side_effect = req.exceptions.RequestException("API error")

        with pytest.raises(Exception, match="GitHub API error"):
            license_compliance_module.fetch_pr_file_diff(
                "owner/repo", 1, "foo.py", "token"
            )

    @patch('check_handlers.license_compliance.requests.get')
    def test_fetch_pr_commits_success(self, mock_get):
        """Test fetching PR commits."""
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"sha": "abc", "commit": {"message": "feat: test", "author": {"name": "Dev"}}}
        ]
        mock_resp.raise_for_status = Mock()
        mock_get.return_value = mock_resp

        result = license_compliance_module.fetch_pr_commits("owner/repo", 1, "token")
        assert len(result) == 1
        assert result[0]["sha"] == "abc"

    @patch('check_handlers.license_compliance.requests.get')
    def test_fetch_pr_commits_api_error(self, mock_get):
        """Test GitHub API error when fetching commits."""
        import requests as req
        mock_get.side_effect = req.exceptions.RequestException("Network error")

        with pytest.raises(Exception, match="GitHub API error"):
            license_compliance_module.fetch_pr_commits("owner/repo", 1, "token")

    @patch.object(license_compliance_module, 'fetch_pr_commits')
    def test_get_ai_generated_files_no_ai_commits(self, mock_commits):
        """Test no AI-generated files when no AI commits found."""
        mock_commits.return_value = [
            {"sha": "abc", "message": "manual fix", "author": "Dev"}
        ]

        result = license_compliance_module.get_ai_generated_files(
            "owner/repo", 1, ["foo.py", "bar.py"], "token"
        )
        assert result == []

    @patch.object(license_compliance_module, 'fetch_pr_commits')
    def test_get_ai_generated_files_with_ai_commits(self, mock_commits):
        """Test all files returned when AI commits found."""
        mock_commits.return_value = [
            {"sha": "abc", "message": "feat: add handler\n\nGenerated with Claude Code", "author": "Bot"}
        ]

        files = ["foo.py", "bar.py"]
        result = license_compliance_module.get_ai_generated_files(
            "owner/repo", 1, files, "token"
        )
        assert result == files

    @patch.object(license_compliance_module, 'fetch_pr_commits')
    def test_get_ai_generated_files_exception_returns_empty(self, mock_commits):
        """Test exception returns empty list (fallback)."""
        mock_commits.side_effect = RuntimeError("API error")

        result = license_compliance_module.get_ai_generated_files(
            "owner/repo", 1, ["foo.py"], "token"
        )
        assert result == []

    @patch.object(license_compliance_module, 'lambda_client')
    def test_query_knowledge_base_success(self, mock_lambda):
        """Test querying knowledge base via Lambda."""
        mock_lambda.invoke.return_value = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "body": json.dumps({"answer": "Use MIT", "sources": []})
            }).encode()))
        }

        result = license_compliance_module.query_knowledge_base("kb-lambda", "license policy")
        assert result["answer"] == "Use MIT"


class TestLicenseComplianceAnalyzeFileComprehensive:
    """Test comprehensive file analysis (Phase 1 + Phase 2)."""

    @patch.object(license_compliance_module, 'analyze_with_claude')
    def test_phase1_only_non_ai(self, mock_analyze):
        """Test only Phase 1 runs for non-AI-generated code."""
        mock_analyze.return_value = {
            "status": "PASS", "explanation": "Clean", "suggestions": []
        }
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_file_comprehensive(
            "handler.py", "+def foo(): pass", "policy", False, tracker
        )

        assert result["status"] == "PASS"
        assert result["phase2_triggered"] is False

    @patch.object(license_compliance_module, 'analyze_code_similarity_with_claude')
    @patch.object(license_compliance_module, 'quick_pattern_check')
    @patch.object(license_compliance_module, 'analyze_with_claude')
    def test_phase2_triggered_with_gpl_match(self, mock_analyze, mock_pattern, mock_similarity):
        """Test Phase 2 triggered when pattern match found with GPL similarity."""
        mock_analyze.return_value = {
            "status": "PASS", "explanation": "Clean", "suggestions": []
        }
        mock_pattern.return_value = {"match_type": "pattern", "description": "GPL sort"}
        mock_similarity.return_value = {
            "similar_projects": [
                {"name": "gpl-lib", "license": "GPL-3.0", "similarity_percentage": 80,
                 "confidence": "HIGH", "matching_patterns": ["algorithm"]}
            ],
            "analysis_successful": True
        }
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_file_comprehensive(
            "sort.py", "+def quicksort(): pass", "policy", True, tracker
        )

        assert result["status"] == "WARN"
        assert result["phase2_triggered"] is True

    @patch.object(license_compliance_module, 'quick_pattern_check')
    @patch.object(license_compliance_module, 'analyze_with_claude')
    def test_phase2_no_pattern_match(self, mock_analyze, mock_pattern):
        """Test Phase 2 skipped when no pattern match."""
        mock_analyze.return_value = {
            "status": "PASS", "explanation": "Clean", "suggestions": []
        }
        mock_pattern.return_value = None
        tracker = bedrock_module.UsageTracker()

        result = license_compliance_module.analyze_file_comprehensive(
            "handler.py", "+def foo(): pass", "policy", True, tracker
        )

        assert result["status"] == "PASS"
        assert result["phase2_triggered"] is False


class TestCheckLicenseComplianceOrchestrator:
    """Test the main check_license_compliance orchestrator function."""

    @patch.object(license_compliance_module, 'analyze_file_comprehensive')
    @patch.object(license_compliance_module, 'fetch_pr_file_diff')
    @patch.object(license_compliance_module, 'get_ai_generated_files')
    @patch.object(license_compliance_module, 'query_knowledge_base')
    @patch.object(license_compliance_module, 'get_github_token')
    def test_check_license_compliance_no_source_files(
        self, mock_token, mock_kb, mock_ai, mock_diff, mock_analyze
    ):
        """Test early return when no source files in changed files."""
        mock_token.return_value = "token"
        mock_kb.return_value = {"answer": "policy"}

        result = license_compliance_module.check_license_compliance(
            "LICENSE_COMPLIANCE", 1, "owner/repo",
            ["README.md", "data.csv", "image.png"],  # no source files (md is source but others aren't)
            "kb-lambda", "/dev/token"
        )

        # README.md IS a source file, so it should be analyzed
        # but data.csv and image.png are not
        if result["status"] == "PASS" and result["message"] == "No source files to check":
            # Only non-source files were passed
            pass
        # Just verify the function ran without error
        assert result["checkType"] == "LICENSE_COMPLIANCE"

    @patch.object(license_compliance_module, 'analyze_file_comprehensive')
    @patch.object(license_compliance_module, 'fetch_pr_file_diff')
    @patch.object(license_compliance_module, 'get_ai_generated_files')
    @patch.object(license_compliance_module, 'query_knowledge_base')
    @patch.object(license_compliance_module, 'get_github_token')
    def test_check_license_compliance_pass(
        self, mock_token, mock_kb, mock_ai, mock_diff, mock_analyze
    ):
        """Test license compliance check that passes."""
        mock_token.return_value = "token"
        mock_kb.return_value = {"answer": "Use MIT or Apache"}
        mock_ai.return_value = []
        mock_diff.return_value = "+def foo(): pass"
        mock_analyze.return_value = {
            "status": "PASS", "explanation": "No issues", "phase2_triggered": False
        }

        result = license_compliance_module.check_license_compliance(
            "LICENSE_COMPLIANCE", 1, "owner/repo",
            ["lambda/handler.py"],
            "kb-lambda", "/dev/token"
        )

        assert result["status"] == "PASS"
        assert "passed" in result["message"]

    @patch.object(license_compliance_module, 'analyze_file_comprehensive')
    @patch.object(license_compliance_module, 'fetch_pr_file_diff')
    @patch.object(license_compliance_module, 'get_ai_generated_files')
    @patch.object(license_compliance_module, 'query_knowledge_base')
    @patch.object(license_compliance_module, 'get_github_token')
    def test_check_license_compliance_fail(
        self, mock_token, mock_kb, mock_ai, mock_diff, mock_analyze
    ):
        """Test license compliance check that fails."""
        mock_token.return_value = "token"
        mock_kb.return_value = {"answer": "No GPL"}
        mock_ai.return_value = []
        mock_diff.return_value = "+GNU General Public License"
        mock_analyze.return_value = {
            "status": "FAIL", "explanation": "GPL prohibited license detected",
            "phase2_triggered": False
        }

        result = license_compliance_module.check_license_compliance(
            "LICENSE_COMPLIANCE", 1, "owner/repo",
            ["src/lib.py"],
            "kb-lambda", "/dev/token"
        )

        assert result["status"] == "FAIL"
        assert "Prohibited" in result["message"]

    @patch.object(license_compliance_module, 'analyze_file_comprehensive')
    @patch.object(license_compliance_module, 'fetch_pr_file_diff')
    @patch.object(license_compliance_module, 'get_ai_generated_files')
    @patch.object(license_compliance_module, 'query_knowledge_base')
    @patch.object(license_compliance_module, 'get_github_token')
    def test_check_license_compliance_warn(
        self, mock_token, mock_kb, mock_ai, mock_diff, mock_analyze
    ):
        """Test license compliance check with warning."""
        mock_token.return_value = "token"
        mock_kb.return_value = {"answer": "Review LGPL"}
        mock_ai.return_value = []
        mock_diff.return_value = "+LGPL licensed code"
        mock_analyze.return_value = {
            "status": "WARN", "explanation": "LGPL requires review",
            "phase2_triggered": False
        }

        result = license_compliance_module.check_license_compliance(
            "LICENSE_COMPLIANCE", 1, "owner/repo",
            ["src/utils.py"],
            "kb-lambda", "/dev/token"
        )

        assert result["status"] == "WARN"
        assert "review required" in result["message"]

    @patch.object(license_compliance_module, 'analyze_file_comprehensive')
    @patch.object(license_compliance_module, 'fetch_pr_file_diff')
    @patch.object(license_compliance_module, 'get_ai_generated_files')
    @patch.object(license_compliance_module, 'query_knowledge_base')
    @patch.object(license_compliance_module, 'get_github_token')
    def test_check_license_compliance_file_error(
        self, mock_token, mock_kb, mock_ai, mock_diff, mock_analyze
    ):
        """Test handling of per-file errors."""
        mock_token.return_value = "token"
        mock_kb.return_value = {"answer": "policy"}
        mock_ai.return_value = []
        mock_diff.side_effect = RuntimeError("API error")

        result = license_compliance_module.check_license_compliance(
            "LICENSE_COMPLIANCE", 1, "owner/repo",
            ["src/app.py"],
            "kb-lambda", "/dev/token"
        )

        # Should still return a result, with error in details
        assert result["checkType"] == "LICENSE_COMPLIANCE"
        assert any("Error" in d for d in result["details"])

    @patch.object(license_compliance_module, 'analyze_file_comprehensive')
    @patch.object(license_compliance_module, 'fetch_pr_file_diff')
    @patch.object(license_compliance_module, 'get_ai_generated_files')
    @patch.object(license_compliance_module, 'query_knowledge_base')
    @patch.object(license_compliance_module, 'get_github_token')
    def test_check_license_compliance_only_non_source_files(
        self, mock_token, mock_kb, mock_ai, mock_diff, mock_analyze
    ):
        """Test early return when only non-source files changed."""
        mock_token.return_value = "token"
        mock_kb.return_value = {"answer": "policy"}

        result = license_compliance_module.check_license_compliance(
            "LICENSE_COMPLIANCE", 1, "owner/repo",
            ["data.csv", "image.png"],
            "kb-lambda", "/dev/token"
        )

        assert result["status"] == "PASS"
        assert result["message"] == "No source files to check"

    @patch.object(license_compliance_module, 'analyze_file_comprehensive')
    @patch.object(license_compliance_module, 'fetch_pr_file_diff')
    @patch.object(license_compliance_module, 'get_ai_generated_files')
    @patch.object(license_compliance_module, 'query_knowledge_base')
    @patch.object(license_compliance_module, 'get_github_token')
    def test_check_license_compliance_phase2_warn_count(
        self, mock_token, mock_kb, mock_ai, mock_diff, mock_analyze
    ):
        """Test phase2 count is included in WARN message."""
        mock_token.return_value = "token"
        mock_kb.return_value = {"answer": "policy"}
        mock_ai.return_value = ["src/sort.py"]
        mock_diff.return_value = "+code"
        mock_analyze.return_value = {
            "status": "WARN", "explanation": "Code similarity detected",
            "phase2_triggered": True
        }

        result = license_compliance_module.check_license_compliance(
            "LICENSE_COMPLIANCE", 1, "owner/repo",
            ["src/sort.py"],
            "kb-lambda", "/dev/token"
        )

        assert result["status"] == "WARN"
        assert "code similarity" in result["message"]


class TestHandler:
    """Test main Lambda handler with SQS events."""

    @patch('process_pr_check_handler.process_check_job')
    def test_handler_success(self, mock_process):
        """Test successful SQS message processing."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-123",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": 123,
                        "repository": "owner/repo",
                        "changedFiles": ["lambda/hello/handler.py"]
                    })
                }
            ]
        }

        mock_process.return_value = Mock(spec=CheckResult)

        # Act
        result = handler(event, None)

        # Assert
        assert result["batchItemFailures"] == []
        mock_process.assert_called_once()

    @patch('process_pr_check_handler.process_check_job')
    def test_handler_with_failure(self, mock_process):
        """Test handler with processing failure."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-123",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": 123,
                        "repository": "owner/repo",
                        "changedFiles": ["lambda/hello/handler.py"]
                    })
                }
            ]
        }

        mock_process.side_effect = Exception("Processing failed")

        # Act
        result = handler(event, None)

        # Assert
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-123"

    def test_handler_invalid_message(self):
        """Test handler with invalid SQS message."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-123",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": -1,  # Invalid
                        "repository": "owner/repo",
                        "changedFiles": []
                    })
                }
            ]
        }

        # Act
        result = handler(event, None)

        # Assert - Should be in batch failures
        assert len(result["batchItemFailures"]) == 1

    @patch('process_pr_check_handler.process_check_job')
    def test_handler_multiple_messages(self, mock_process):
        """Test handler with multiple SQS messages."""
        # Arrange
        event = {
            "Records": [
                {
                    "messageId": "msg-1",
                    "body": json.dumps({
                        "checkType": "TEST_COVERAGE",
                        "pr_number": 123,
                        "repository": "owner/repo",
                        "changedFiles": []
                    })
                },
                {
                    "messageId": "msg-2",
                    "body": json.dumps({
                        "checkType": "ADR_COMPLIANCE",
                        "pr_number": 124,
                        "repository": "owner/repo",
                        "changedFiles": []
                    })
                }
            ]
        }

        mock_process.return_value = Mock(spec=CheckResult)

        # Act
        result = handler(event, None)

        # Assert
        assert result["batchItemFailures"] == []
        assert mock_process.call_count == 2


# ---------------------------------------------------------------------------
# chunk 07: Azure DevOps PR-comment posting
# ---------------------------------------------------------------------------


class TestCheckJobAdoFields:
    """The CheckJob model gains optional ADO fields. Default
    ``provider="github"`` so existing messages still validate."""

    def test_default_provider_is_github(self):
        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["a.py"],
        )
        assert job.provider == "github"
        assert job.connection_id is None
        assert job.azure_devops_slug is None

    def test_azure_devops_job_validates_with_ado_fields(self):
        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changedFiles=["src/main.py"],
            provider="azure_devops",
            connection_id="conn-xyz",
            slug="example-org/example-project/example-repo",
            org="example-org",
            project="example-project",
            repo="example-repo",
        )
        assert job.provider == "azure_devops"
        assert job.connection_id == "conn-xyz"
        assert job.azure_devops_slug == "example-org/example-project/example-repo"
        assert job.azure_devops_org == "example-org"
        assert job.azure_devops_project == "example-project"
        assert job.azure_devops_repo == "example-repo"

    def test_invalid_provider_rejected(self):
        with pytest.raises(ValidationError):
            CheckJob(
                checkType="ADR_COMPLIANCE",
                pr_number=1,
                repository="x/y",
                changedFiles=[],
                provider="bitbucket",  # Not in the Literal set
            )


class TestProcessCheckJobAdoBranch:
    """process_check_job dispatches the comment-post step on
    job.provider. Chunk 05 removed the stub-PASS short-circuit
    that used to intercept ADO jobs; they now run the full
    check pipeline the same as GitHub jobs."""

    @patch.object(handler_module, "store_check_result")
    @patch.object(handler_module, "_post_azure_devops_thread")
    @patch.object(handler_module, "run_check")
    def test_ado_job_runs_real_check_and_posts_via_ado_path(
        self, mock_run_check, mock_post_ado, mock_store,
    ):
        # Chunk 05: run_check IS called for ADO jobs now (real check
        # execution). Chunk 03/04 wired ADO in every check_handler.
        mock_run_check.return_value = {
            "status": "PASS",
            "message": "All ADR standards met on ADO PR",
            "details": [],
        }
        mock_post_ado.return_value = {"id": 99}
        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changedFiles=[],
            provider="azure_devops",
            connection_id="conn-xyz",
            org="example-org",
            project="example-project",
            repo="example-repo",
        )
        result = process_check_job(job)

        mock_run_check.assert_called_once()
        # Comment posted via ADO; not via the GitHub path.
        mock_post_ado.assert_called_once()
        kwargs = mock_post_ado.call_args.kwargs
        assert kwargs["org"] == "example-org"
        assert kwargs["project"] == "example-project"
        assert kwargs["repo"] == "example-repo"
        assert kwargs["pull_request_id"] == 42
        assert kwargs["connection_id"] == "conn-xyz"
        assert result.status == CheckStatus.PASS

    @patch.object(handler_module, "store_check_result")
    @patch.object(handler_module, "_post_azure_devops_thread")
    @patch.object(handler_module, "run_check")
    def test_ado_job_comment_url_points_at_dev_azure_thread(
        self, mock_run_check, mock_post_ado, mock_store,
    ):
        mock_run_check.return_value = {
            "status": "PASS", "message": "ok", "details": [],
        }
        mock_post_ado.return_value = {"id": 77}
        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changedFiles=[],
            provider="azure_devops",
            connection_id="conn-xyz",
            org="example-org",
            project="example-project",
            repo="example-repo",
        )
        result = process_check_job(job)
        assert "dev.azure.com/example-org/example-project" in result.comment_url
        assert "discussionId=77" in result.comment_url
        assert "pullrequest/42" in result.comment_url

    @patch.object(handler_module, "post_pr_comment", return_value={"html_url": "http://example/c"})
    @patch.object(handler_module, "get_github_token", return_value="ghp_xxx")
    @patch.object(handler_module, "store_check_result")
    @patch.object(handler_module, "run_check")
    def test_github_job_unchanged_by_ado_addition(
        self, mock_run_check, mock_store, mock_token, mock_post_github,
    ):
        # Regression guard: the existing GitHub flow is untouched by
        # the chunk-07 additive changes.
        mock_run_check.return_value = {
            "status": "PASS", "message": "all good", "details": [], "usage": None,
        }
        job = CheckJob(
            checkType="ADR_COMPLIANCE",
            pr_number=123,
            repository="owner/repo",
            changedFiles=["a.py"],
        )
        result = process_check_job(job)
        # GitHub path: run_check runs, GitHub PAT pulled, github comment posted.
        assert mock_run_check.called
        assert mock_token.called
        assert mock_post_github.called
        assert result.comment_url == "http://example/c"


class TestPostAzureDevOpsThread:

    @patch.object(handler_module, "post_pull_request_thread")
    @patch.object(handler_module, "_ado_refresh_access_token", return_value="ado-access-tok")
    @patch.object(handler_module, "_get_ado_client_secret", return_value="ado-secret")
    @patch.object(handler_module, "AZURE_DEVOPS_TENANT_ID", "contoso-tenant")
    @patch.object(handler_module, "AZURE_DEVOPS_CLIENT_ID", "client-uuid")
    @patch.object(handler_module, "_ADO_AVAILABLE", True)
    def test_posts_via_token_refresh_then_ado_api(
        self, mock_secret, mock_refresh, mock_post,
    ):
        mock_post.return_value = {"id": 99}
        result = handler_module._post_azure_devops_thread(
            org="example-org",
            project="example-project",
            repo="example-repo",
            pull_request_id=42,
            body="hello from check",
            connection_id="conn-xyz",
        )
        assert result == {"id": 99}
        # Token refresh used the per-customer tenant_id (NOT "common").
        refresh_kwargs = mock_refresh.call_args.kwargs
        assert refresh_kwargs["tenant_id"] == "contoso-tenant"
        assert refresh_kwargs["tenant_id"] != "common"
        assert refresh_kwargs["connection_id"] == "conn-xyz"
        # The ADO API was called with the fresh access token + the
        # right body.
        post_kwargs = mock_post.call_args.kwargs
        assert post_kwargs["access_token"] == "ado-access-tok"
        assert post_kwargs["org"] == "example-org"
        assert post_kwargs["pull_request_id"] == 42
        assert post_kwargs["comment_markdown"] == "hello from check"

    @patch.object(handler_module, "_ADO_AVAILABLE", True)
    def test_missing_connection_id_raises(self):
        with pytest.raises(ValueError):
            handler_module._post_azure_devops_thread(
                org="o", project="p", repo="r",
                pull_request_id=1, body="x", connection_id="",
            )

    @patch.object(handler_module, "_ado_refresh_access_token", return_value=None)
    @patch.object(handler_module, "_get_ado_client_secret", return_value="s")
    @patch.object(handler_module, "AZURE_DEVOPS_TENANT_ID", "contoso-tenant")
    @patch.object(handler_module, "AZURE_DEVOPS_CLIENT_ID", "client-uuid")
    @patch.object(handler_module, "_ADO_AVAILABLE", True)
    def test_failed_token_refresh_raises_with_reconnect_required(
        self, mock_secret, mock_refresh_none,
    ):
        with pytest.raises(RuntimeError) as ctx:
            handler_module._post_azure_devops_thread(
                org="o", project="p", repo="r",
                pull_request_id=1, body="x", connection_id="conn",
            )
        # The UI uses "reconnect_required" as a sentinel to surface
        # the reconnect-Azure-DevOps banner -- pin the wording.
        assert "reconnect_required" in str(ctx.value)


# ---------------------------------------------------------------------------
# Chunk 02 of AI-PR-Analyzer ADO parity plan: widened check_* signatures
# and provider-context forwarding through run_check().
# ---------------------------------------------------------------------------


class TestCheckHandlerSignatureWidening:
    """Every check_* accepts the provider-context kwargs (default github)
    and raises NotImplementedError for other providers. Behavior on the
    GitHub path is preserved by the defaults."""

    def _minimal_kwargs(self):
        return dict(
            check_type="ADR_COMPLIANCE",
            pr_number=1,
            repository="o/r",
            changed_files=[],
            query_kb_lambda_name="qkb",
            github_token_param="/env/app/github/token",
        )

    def test_check_adr_compliance_defaults_to_github(self):
        # Passing no provider should not raise NotImplementedError.
        # We patch the internals it calls so no real network happens.
        with patch.object(adr_compliance_module, "get_github_token", return_value="tok"), \
             patch.object(adr_compliance_module, "analyze_lambda_handlers", return_value={"status": "PASS", "details": []}), \
             patch.object(adr_compliance_module, "analyze_terraform_files", return_value={"status": "PASS", "details": []}):
            result = adr_compliance_module.check_adr_compliance(**self._minimal_kwargs())
            assert result["status"] == "PASS"

    # Chunks 03 + 04 wired ADO for ALL six check handlers, and the
    # GitLab chunk (05) wired gitlab, so the NotImplementedError guards
    # were removed for both. What remains is the "unknown provider"
    # guard: any provider string other than github / azure_devops /
    # gitlab should still raise loudly.

    def test_unknown_provider_still_raises(self):
        with pytest.raises(NotImplementedError):
            license_compliance_module.check_license_compliance(
                check_type="LICENSE_COMPLIANCE",
                pr_number=1, repository="o/r", changed_files=[],
                query_kb_lambda_name="qkb", github_token_param="/x",
                provider="bitbucket",
            )
        with pytest.raises(NotImplementedError):
            readme_freshness_module.check_readme_freshness(
                check_type="README_FRESHNESS",
                pr_number=1, repository="o/r", changed_files=[],
                github_token_param="/x",
                provider="bitbucket",
            )
        with pytest.raises(NotImplementedError):
            test_coverage_module.check_test_coverage(
                check_type="TEST_COVERAGE",
                pr_number=1, repository="o/r", changed_files=[],
                github_token_param="/x", query_kb_lambda_name="qkb",
                provider="bitbucket",
            )


class TestRunCheckProviderForwarding:
    """run_check() extracts provider context from CheckJob and forwards
    it to each handler. On a GitHub job (default provider) the handler
    behaves as before. On an ADO job, the handler receives the ADO context
    and raises NotImplementedError (until chunks 03-04)."""

    def _ado_job(self, check_type="ADR_COMPLIANCE"):
        return CheckJob(
            checkType=check_type,
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changedFiles=["src/foo.py"],
            provider="azure_devops",
            connection_id="conn-xyz",
            slug="example-org/example-project/example-repo",
            org="example-org",
            project="example-project",
            repo="example-repo",
        )

    def _github_job(self, check_type="ADR_COMPLIANCE"):
        return CheckJob(
            checkType=check_type,
            pr_number=42,
            repository="o/r",
            changedFiles=["src/foo.py"],
        )

    @patch.object(handler_module, "check_adr_compliance")
    def test_forwards_github_defaults_when_provider_not_set(self, mock_check):
        mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
        run_check(self._github_job("ADR_COMPLIANCE"))
        # The handler received provider="github" and no ADO context.
        _, kwargs = mock_check.call_args
        assert kwargs["provider"] == "github"
        assert kwargs["connection_id"] is None
        assert kwargs["ado_org"] is None
        assert kwargs["ado_project"] is None
        assert kwargs["ado_repo"] is None

    @patch.object(handler_module, "check_adr_compliance")
    def test_forwards_ado_context_to_handler(self, mock_check):
        # Even though the handler's NotImplementedError would fire in
        # reality, we mock it here to inspect the kwargs run_check
        # actually passes.
        mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
        run_check(self._ado_job("ADR_COMPLIANCE"))
        _, kwargs = mock_check.call_args
        assert kwargs["provider"] == "azure_devops"
        assert kwargs["connection_id"] == "conn-xyz"
        assert kwargs["ado_org"] == "example-org"
        assert kwargs["ado_project"] == "example-project"
        assert kwargs["ado_repo"] == "example-repo"

    @patch.object(handler_module, "check_readme_freshness")
    def test_forwards_ado_context_across_check_types(self, mock_check):
        mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
        run_check(self._ado_job("README_FRESHNESS"))
        _, kwargs = mock_check.call_args
        assert kwargs["provider"] == "azure_devops"
        assert kwargs["ado_org"] == "example-org"

    @patch.object(handler_module, "check_breaking_changes")
    def test_forwards_ado_context_to_breaking_changes(self, mock_check):
        # breaking_changes has a slightly different signature (no
        # github_token_param) -- confirm run_check still forwards the
        # provider kwargs.
        mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
        run_check(self._ado_job("BREAKING_CHANGES"))
        _, kwargs = mock_check.call_args
        assert kwargs["provider"] == "azure_devops"
        assert kwargs["ado_project"] == "example-project"

    @patch.object(handler_module, "check_test_coverage")
    def test_forwards_ado_context_to_test_coverage(self, mock_check):
        mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
        run_check(self._ado_job("TEST_COVERAGE"))
        _, kwargs = mock_check.call_args
        assert kwargs["provider"] == "azure_devops"
        assert kwargs["ado_repo"] == "example-repo"

    @patch.object(handler_module, "check_license_compliance")
    def test_forwards_ado_context_to_license_compliance(self, mock_check):
        mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
        run_check(self._ado_job("LICENSE_COMPLIANCE"))
        _, kwargs = mock_check.call_args
        assert kwargs["provider"] == "azure_devops"

    @patch.object(handler_module, "check_architectural_duplication")
    def test_forwards_ado_context_to_arch_duplication(self, mock_check):
        mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
        run_check(self._ado_job("ARCHITECTURAL_DUPLICATION"))
        _, kwargs = mock_check.call_args
        assert kwargs["provider"] == "azure_devops"

    def test_run_check_with_ado_job_reaches_handler_not_guard(self):
        # Chunk 04 wired all 6 handlers so run_check no longer
        # hits a NotImplementedError guard on ADO jobs. Mock the
        # handler and confirm run_check forwards the ADO context.
        with patch.object(handler_module, "check_license_compliance") as mock_check:
            mock_check.return_value = {"status": "PASS", "message": "ok", "details": []}
            run_check(self._ado_job("LICENSE_COMPLIANCE"))
            _, kwargs = mock_check.call_args
            assert kwargs["provider"] == "azure_devops"


# ---------------------------------------------------------------------------
# Chunk 03 of AI-PR-Analyzer ADO parity plan: real ADO execution wired
# into BREAKING_CHANGES, ADR_COMPLIANCE, ARCHITECTURAL_DUPLICATION.
# ---------------------------------------------------------------------------


class TestBreakingChangesADO:
    """BREAKING_CHANGES is provider-agnostic — no fetch, no auth."""

    def test_ado_job_runs_end_to_end_without_touching_ado_apis(self):
        # No mocks needed for ADO plumbing because BREAKING_CHANGES
        # only reads changed_files (already in the CheckJob) and
        # KB. The provider-context kwargs are accepted but unused.
        # Empty changed_files short-circuits before any KB call.
        result = breaking_changes_module.check_breaking_changes(
            check_type="BREAKING_CHANGES",
            pr_number=1,
            repository="example-org/example-project/example-repo",
            changed_files=[],
            query_kb_lambda_name="qkb",
            provider="azure_devops",
            connection_id="conn-xyz",
            ado_org="example-org", ado_project="example-project",
            ado_repo="example-repo",
        )
        assert result["status"] in ("PASS", "WARN", "FAIL")


class TestADRComplianceADO:
    """ADR_COMPLIANCE end-to-end on ADO: refreshes token, fetches PR
    metadata + base/head file bytes, computes diff client-side."""

    @patch.object(adr_compliance_module, "refresh_ado_token", return_value="ado-tok")
    def test_ado_branch_refreshes_token_and_fetches_pr_metadata(self, mock_refresh):
        # Build a fake PRMetadata NamedTuple that fetch_pr_metadata
        # would return; we mock it at the source_fetch/pr_metadata
        # boundary so no actual HTTP happens.
        from shared.pr_metadata import PRMetadata

        fake_pr = PRMetadata(
            head_sha="head-sha",
            base_sha="base-sha",
            base_branch="main",
            changed_files=["lambda/hello/handler.py"],
            author="user@example.com",
            title="Add hello",
        )

        with patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   side_effect=[b"before content\n", b"after content\n"]), \
             patch.object(adr_compliance_module, "query_knowledge_base",
                          return_value={"answer": "std", "sources": []}), \
             patch.object(adr_compliance_module, "analyze_code_with_claude",
                          return_value={"compliant": True, "explanation": "ok", "suggestions": []}):
            result = adr_compliance_module.check_adr_compliance(
                check_type="ADR_COMPLIANCE",
                pr_number=42,
                repository="example-org/example-project/example-repo",
                changed_files=["lambda/hello/handler.py"],
                query_kb_lambda_name="qkb",
                github_token_param="/x",
                provider="azure_devops",
                connection_id="conn-xyz",
                ado_org="example-org",
                ado_project="example-project",
                ado_repo="example-repo",
            )

        assert result["status"] == "PASS"
        mock_refresh.assert_called_once_with("conn-xyz")

    @patch.object(adr_compliance_module, "refresh_ado_token", return_value="ado-tok")
    def test_ado_branch_handles_added_file_no_base_content(self, _mock_refresh):
        # File exists in head but not in base -- fetch_file_at_sha for
        # base_sha raises SourceFetchError, and pr_diff.compute_file_diff
        # emits a valid diff with /dev/null on the base side.
        from shared.pr_metadata import PRMetadata
        from shared.source_fetch import SourceFetchError

        fake_pr = PRMetadata(
            head_sha="head-sha",
            base_sha="base-sha",
            base_branch="main",
            changed_files=["lambda/added/handler.py"],
            author="u",
            title="t",
        )

        def _fetch(*args, **kwargs):
            if kwargs.get("sha") == "base-sha" or (len(args) >= 3 and args[2] == "base-sha"):
                raise SourceFetchError("404 in base")
            return b"new file content\n"

        with patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha", side_effect=_fetch), \
             patch.object(adr_compliance_module, "query_knowledge_base",
                          return_value={"answer": "std", "sources": []}), \
             patch.object(adr_compliance_module, "analyze_code_with_claude",
                          return_value={"compliant": True, "explanation": "ok", "suggestions": []}):
            result = adr_compliance_module.check_adr_compliance(
                check_type="ADR_COMPLIANCE",
                pr_number=1,
                repository="example-org/example-project/example-repo",
                changed_files=["lambda/added/handler.py"],
                query_kb_lambda_name="qkb",
                github_token_param="/x",
                provider="azure_devops",
                connection_id="conn",
                ado_org="example-org",
                ado_project="example-project",
                ado_repo="example-repo",
            )
        assert result["status"] == "PASS"

    def test_github_path_unchanged_by_ado_addition(self):
        # Regression: passing no provider kwarg still runs the GitHub
        # path with a get_github_token + fetch_pr_file_diff flow.
        with patch.object(adr_compliance_module, "get_github_token", return_value="ghp"), \
             patch.object(adr_compliance_module, "analyze_lambda_handlers",
                          return_value={"status": "PASS", "message": "ok", "details": []}), \
             patch.object(adr_compliance_module, "analyze_terraform_files",
                          return_value={"status": "PASS", "message": "ok", "details": []}):
            result = adr_compliance_module.check_adr_compliance(
                check_type="ADR_COMPLIANCE",
                pr_number=1, repository="o/r", changed_files=[],
                query_kb_lambda_name="qkb", github_token_param="/x",
            )
        assert result["status"] == "PASS"


class TestArchitecturalDuplicationADO:
    """ARCHITECTURAL_DUPLICATION end-to-end on ADO: mirrors the
    ADR_COMPLIANCE ADO pattern (token refresh + PR metadata +
    per-file diff via base/head fetch + client-side diff)."""

    def test_ado_branch_ends_up_with_diffs_computed_client_side(self):
        # Patch the module-local refresh_ado_token helper directly so
        # we don't need the ado_context module to be importable.
        from shared.pr_metadata import PRMetadata

        fake_pr = PRMetadata(
            head_sha="head-sha", base_sha="base-sha", base_branch="main",
            changed_files=["lambda/foo/handler.py"],
            author="u", title="t",
        )

        with patch.object(architectural_duplication_module, "refresh_ado_token",
                          return_value="ado-tok", create=True), \
             patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   side_effect=[b"base\n", b"head\n"]), \
             patch.object(architectural_duplication_module, "summarize_pr_with_claude",
                          return_value="summary"), \
             patch.object(architectural_duplication_module, "extract_functional_keywords",
                          return_value=["q1"]), \
             patch.object(architectural_duplication_module, "query_knowledge_base",
                          return_value={"answer": "", "sources": []}), \
             patch.object(architectural_duplication_module, "analyze_similarity_with_claude",
                          return_value={"has_related_functionality": False, "matches": []}):
            result = architectural_duplication_module.check_architectural_duplication(
                check_type="ARCHITECTURAL_DUPLICATION",
                pr_number=42,
                repository="example-org/example-project/example-repo",
                changed_files=["lambda/foo/handler.py"],
                query_kb_lambda_name="qkb",
                github_token_param="/x",
                provider="azure_devops",
                connection_id="conn-xyz",
                ado_org="example-org",
                ado_project="example-project",
                ado_repo="example-repo",
            )

        assert result["status"] in ("PASS", "WARN")

    def test_github_path_unchanged_by_ado_addition(self):
        # GitHub regression: no provider kwarg still runs the GitHub
        # path with get_github_token + fetch_pr_file_diff flow.
        with patch.object(architectural_duplication_module, "get_github_token", return_value="ghp"), \
             patch.object(architectural_duplication_module, "fetch_pr_file_diff", return_value="diff"), \
             patch.object(architectural_duplication_module, "summarize_pr_with_claude",
                          return_value="summary"), \
             patch.object(architectural_duplication_module, "extract_functional_keywords",
                          return_value=["q1"]), \
             patch.object(architectural_duplication_module, "query_knowledge_base",
                          return_value={"answer": "", "sources": []}), \
             patch.object(architectural_duplication_module, "analyze_similarity_with_claude",
                          return_value={"has_related_functionality": False, "matches": []}):
            result = architectural_duplication_module.check_architectural_duplication(
                check_type="ARCHITECTURAL_DUPLICATION",
                pr_number=1,
                repository="o/r",
                changed_files=["lambda/foo/handler.py"],
                query_kb_lambda_name="qkb",
                github_token_param="/x",
            )
        assert result["status"] in ("PASS", "WARN")


# ---------------------------------------------------------------------------
# Chunk 04 of AI-PR-Analyzer ADO parity plan: introspection-heavy
# handlers (README_FRESHNESS, LICENSE_COMPLIANCE, TEST_COVERAGE)
# wired for Azure DevOps.
# ---------------------------------------------------------------------------


class TestReadmeFreshnessADO:

    def test_ado_branch_readme_updated_runs_end_to_end(self):
        from shared.pr_metadata import PRMetadata

        fake_pr = PRMetadata(
            head_sha="head", base_sha="base", base_branch="main",
            changed_files=["README.md"], author="u", title="t",
        )
        with patch.object(readme_freshness_module, "refresh_ado_token",
                          return_value="ado-tok", create=True), \
             patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   side_effect=[b"before readme\n", b"after readme\n"]), \
             patch.object(readme_freshness_module, "analyze_readme_with_claude",
                          return_value={"adequate": True, "explanation": "documented"}):
            result = readme_freshness_module.check_readme_freshness(
                check_type="README_FRESHNESS",
                pr_number=42,
                repository="example-org/example-project/example-repo",
                changed_files=["lambda/foo/handler.py", "README.md"],
                github_token_param="/x",
                provider="azure_devops",
                connection_id="conn-xyz",
                ado_org="example-org",
                ado_project="example-project",
                ado_repo="example-repo",
            )
        assert result["status"] == "PASS"

    def test_ado_branch_readme_not_updated_returns_warn_without_ado_fetch(self):
        # If README wasn't in changed_files, no fetch/refresh needed —
        # handler returns WARN immediately. Confirms the ADO code path
        # short-circuits on the same input GitHub short-circuits on.
        result = readme_freshness_module.check_readme_freshness(
            check_type="README_FRESHNESS",
            pr_number=1,
            repository="o/r",
            changed_files=["terraform/x.tf"],
            github_token_param="/x",
            provider="azure_devops",
            connection_id="conn",
            ado_org="o", ado_project="p", ado_repo="r",
        )
        assert result["status"] == "WARN"


class TestLicenseComplianceADO:

    def test_ado_branch_scans_files_end_to_end(self):
        from shared.pr_metadata import PRMetadata

        fake_pr = PRMetadata(
            head_sha="head", base_sha="base", base_branch="main",
            changed_files=["src/x.py"], author="u", title="t",
        )
        with patch.object(license_compliance_module, "refresh_ado_token",
                          return_value="ado-tok", create=True), \
             patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   side_effect=[b"before src\n", b"after src\n"]), \
             patch.object(license_compliance_module, "query_knowledge_base",
                          return_value={"answer": "policy text", "sources": []}), \
             patch.object(license_compliance_module, "get_ai_generated_files",
                          return_value=[]), \
             patch.object(license_compliance_module, "analyze_file_comprehensive",
                          return_value={"status": "PASS", "issues": [], "phase2_triggered": False}):
            result = license_compliance_module.check_license_compliance(
                check_type="LICENSE_COMPLIANCE",
                pr_number=1,
                repository="example-org/example-project/example-repo",
                changed_files=["src/x.py"],
                query_kb_lambda_name="qkb",
                github_token_param="/x",
                provider="azure_devops",
                connection_id="conn",
                ado_org="example-org",
                ado_project="example-project",
                ado_repo="example-repo",
            )
        # No more skip-note; result details are just the per-file
        # analysis output.
        assert result["status"] == "PASS"
        assert not any(
            "commit-list inspection" in d for d in result["details"]
        )

    def test_ado_branch_runs_ai_generated_detection_via_shared_primitive(self):
        # get_ai_generated_files IS called on ADO now (Phase 2 wired via
        # shared.pr_commits.fetch_pr_commits with ADO context).
        from shared.pr_metadata import PRMetadata

        fake_pr = PRMetadata(
            head_sha="head", base_sha="base", base_branch="main",
            changed_files=["src/x.py"], author="u", title="t",
        )
        with patch.object(license_compliance_module, "refresh_ado_token",
                          return_value="ado-tok", create=True), \
             patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   side_effect=[b"a\n", b"b\n"]), \
             patch.object(license_compliance_module, "query_knowledge_base",
                          return_value={"answer": "", "sources": []}), \
             patch.object(license_compliance_module, "analyze_file_comprehensive",
                          return_value={"status": "PASS", "issues": [], "phase2_triggered": False}), \
             patch.object(license_compliance_module, "get_ai_generated_files",
                          return_value=[]) as mock_ai:
            license_compliance_module.check_license_compliance(
                check_type="LICENSE_COMPLIANCE",
                pr_number=1, repository="o/p/r", changed_files=["src/x.py"],
                query_kb_lambda_name="qkb", github_token_param="/x",
                provider="azure_devops", connection_id="c",
                ado_org="o", ado_project="p", ado_repo="r",
            )
        # Called with provider + ADO context (not skipped).
        mock_ai.assert_called_once()
        _, kwargs = mock_ai.call_args
        assert kwargs["provider"] == "azure_devops"
        assert kwargs["ado_org"] == "o"


class TestTestCoverageADO:

    def test_ado_branch_uses_repo_metadata_for_tree_and_config(self):
        from shared.repo_metadata import RepoMetadata

        fake_repo = RepoMetadata(
            default_branch="main",
            file_paths=[
                "lambda/foo/handler.py",
                "lambda/foo/tests/unit/test_handler.py",
                "lambda/bar/handler.py",
            ],
        )
        with patch.object(test_coverage_module, "refresh_ado_token",
                          return_value="ado-tok", create=True), \
             patch("shared.repo_metadata.fetch_repo_metadata", return_value=fake_repo), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   return_value=b"testing:\n  require_tests: []\n"), \
             patch.object(test_coverage_module, "query_testing_standards",
                          return_value=("std", "ADR-XYZ", {"input_tokens": 0, "output_tokens": 0})):
            result = test_coverage_module.check_test_coverage(
                check_type="TEST_COVERAGE",
                pr_number=1,
                repository="example-org/example-project/example-repo",
                changed_files=["lambda/foo/handler.py"],
                github_token_param="/x",
                query_kb_lambda_name="qkb",
                provider="azure_devops",
                connection_id="conn",
                ado_org="example-org",
                ado_project="example-project",
                ado_repo="example-repo",
            )
        # Handler ran without touching GitHub.
        assert result["status"] in ("PASS", "WARN", "FAIL")

    def test_ado_config_yaml_missing_still_runs(self):
        # If .outcomeops.yaml doesn't exist in the ADO repo,
        # fetch_file_at_sha raises SourceFetchError; the handler
        # should fall through to backend defaults, not crash.
        from shared.repo_metadata import RepoMetadata
        from shared.source_fetch import SourceFetchError

        fake_repo = RepoMetadata(default_branch="main", file_paths=[])
        with patch.object(test_coverage_module, "refresh_ado_token",
                          return_value="ado-tok", create=True), \
             patch("shared.repo_metadata.fetch_repo_metadata", return_value=fake_repo), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   side_effect=SourceFetchError("404")), \
             patch.object(test_coverage_module, "query_testing_standards",
                          return_value=("", None, None)):
            result = test_coverage_module.check_test_coverage(
                check_type="TEST_COVERAGE",
                pr_number=1, repository="o/p/r",
                changed_files=[],
                github_token_param="/x", query_kb_lambda_name="qkb",
                provider="azure_devops", connection_id="c",
                ado_org="o", ado_project="p", ado_repo="r",
            )
        assert result["status"] in ("PASS", "WARN", "FAIL")


class TestGitLabCheckJob:
    """Chunk 05: gitlab provider on the worker side."""

    def _fresh_job_gitlab(self):
        return CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=7,
            repository="group/subgroup/project",
            changedFiles=["src/x.py"],
            provider="gitlab",
            connection_id="conn-1",
        )

    def test_gitlab_job_validates_with_connection_only(self):
        job = self._fresh_job_gitlab()
        assert job.provider == "gitlab"
        assert job.connection_id == "conn-1"
        assert job.azure_devops_org is None

    @patch('check_handlers.gitlab_context.get_gitlab_base_url',
           return_value="https://gitlab.com")
    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler._post_gitlab_note')
    @patch('process_pr_check_handler._get_existing_check_result_row', return_value=None)
    @patch('process_pr_check_handler.run_check')
    def test_gitlab_first_run_posts_note_and_stores_id(
        self, mock_run, _mock_exist, mock_post_note, mock_store, _mock_base,
    ):
        mock_run.return_value = {"status": "PASS", "message": "ok", "details": []}
        mock_post_note.return_value = {"id": 314}
        result = process_check_job(self._fresh_job_gitlab())
        mock_post_note.assert_called_once()
        assert result.PK == "PR#gitlab#group/subgroup/project#7"
        assert result.provider_comment_id == 314
        assert result.provider_thread_id is None
        assert "#note_314" in result.comment_url

    @patch('process_pr_check_handler.store_check_result')
    @patch('process_pr_check_handler._post_gitlab_note')
    @patch('process_pr_check_handler._try_patch_gitlab_comment')
    @patch('process_pr_check_handler._get_existing_check_result_row')
    @patch('process_pr_check_handler.run_check')
    def test_gitlab_second_run_updates_note_in_place(
        self, mock_run, mock_exist, mock_patch_note, mock_post_note, mock_store,
    ):
        mock_run.return_value = {"status": "WARN", "message": "w", "details": []}
        mock_exist.return_value = {"providerCommentId": 314}
        mock_patch_note.return_value = (
            "https://gitlab.com/group/subgroup/project/-/merge_requests/7#note_314"
        )
        result = process_check_job(self._fresh_job_gitlab())
        mock_patch_note.assert_called_once()
        mock_post_note.assert_not_called()
        # Existing note id preserved so the NEXT run also PATCHes.
        assert result.provider_comment_id == 314

    def test_try_patch_gitlab_no_stored_id_returns_none(self):
        assert handler_module._try_patch_gitlab_comment(
            repository="g/p", pr_number=7, connection_id="c",
            existing_row={}, comment_body="b",
        ) is None


# ===========================================================================
# Inline (line-level) review comment machinery (commit 24d1790)
# ===========================================================================

from types import SimpleNamespace  # noqa: E402
from shared.pr_findings import Finding  # noqa: E402


class TestFinding:
    """shared.pr_findings.Finding contract (ADR-031/032)."""

    def test_line_must_be_positive(self):
        with pytest.raises(ValidationError):
            Finding(path="a.py", line=0, message="m")
        with pytest.raises(ValidationError):
            Finding(path="a.py", line=-3, message="m")

    def test_defaults_side_right_and_severity_warning(self):
        f = Finding(path="a.py", line=1, message="m")
        assert f.side == "RIGHT"
        assert f.severity == "warning"
        assert f.enclosing_symbol is None

    def test_accepts_explicit_fields(self):
        f = Finding(path="a.py", line=5, side="LEFT", severity="error",
                    message="bad", enclosing_symbol="my_func")
        assert f.side == "LEFT"
        assert f.severity == "error"
        assert f.enclosing_symbol == "my_func"


class TestGithubReviewCommentHelpers:
    """GitHub line-level (pulls) review-comment POST/PATCH/DELETE helpers."""

    @patch('process_pr_check_handler.requests.post')
    def test_post_pr_review_comment(self, mock_post):
        resp = Mock()
        resp.json.return_value = {"id": 42}
        resp.raise_for_status = Mock()
        mock_post.return_value = resp

        out = handler_module.post_pr_review_comment(
            "owner/repo", 7, "body text", "tok",
            commit_id="sha1", path="src/a.py", line=12, side="RIGHT",
        )
        assert out == {"id": 42}
        call = mock_post.call_args
        assert call.args[0] == (
            "https://api.github.com/repos/owner/repo/pulls/7/comments"
        )
        assert call.kwargs["json"] == {
            "body": "body text", "commit_id": "sha1",
            "path": "src/a.py", "line": 12, "side": "RIGHT",
        }
        assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    @patch('process_pr_check_handler.requests.post')
    def test_post_pr_review_comment_raises_on_error(self, mock_post):
        resp = Mock()
        resp.raise_for_status.side_effect = (
            handler_module.requests.exceptions.HTTPError("422")
        )
        mock_post.return_value = resp
        with pytest.raises(Exception, match="GitHub API error"):
            handler_module.post_pr_review_comment(
                "owner/repo", 7, "b", "tok",
                commit_id="s", path="a.py", line=1,
            )

    @patch('process_pr_check_handler.requests.patch')
    def test_patch_pr_review_comment(self, mock_patch_req):
        resp = Mock()
        resp.json.return_value = {"id": 42, "body": "new"}
        resp.raise_for_status = Mock()
        mock_patch_req.return_value = resp

        out = handler_module.patch_pr_review_comment(
            "owner/repo", 42, "new", "tok",
        )
        assert out["id"] == 42
        call = mock_patch_req.call_args
        # review comments live under /pulls/comments/{id}, NOT /issues/comments/{id}
        assert call.args[0] == (
            "https://api.github.com/repos/owner/repo/pulls/comments/42"
        )
        assert call.kwargs["json"] == {"body": "new"}

    @patch('process_pr_check_handler.requests.patch')
    def test_patch_pr_review_comment_raises_on_error(self, mock_patch_req):
        resp = Mock()
        resp.raise_for_status.side_effect = (
            handler_module.requests.exceptions.HTTPError("404")
        )
        mock_patch_req.return_value = resp
        with pytest.raises(Exception, match="GitHub API error"):
            handler_module.patch_pr_review_comment("owner/repo", 42, "b", "tok")

    @patch('process_pr_check_handler.requests.delete')
    def test_delete_pr_review_comment(self, mock_delete):
        resp = Mock()
        resp.raise_for_status = Mock()
        mock_delete.return_value = resp

        out = handler_module.delete_pr_review_comment("owner/repo", 42, "tok")
        assert out is None
        call = mock_delete.call_args
        assert call.args[0] == (
            "https://api.github.com/repos/owner/repo/pulls/comments/42"
        )

    @patch('process_pr_check_handler.requests.delete')
    def test_delete_pr_review_comment_raises_on_error(self, mock_delete):
        resp = Mock()
        resp.raise_for_status.side_effect = (
            handler_module.requests.exceptions.HTTPError("404")
        )
        mock_delete.return_value = resp
        with pytest.raises(Exception, match="GitHub API error"):
            handler_module.delete_pr_review_comment("owner/repo", 42, "tok")


class TestFormatInlineComment:
    """_format_inline_comment renders one Finding; no aggregate counts."""

    def test_error_glyph_and_message(self):
        fd = Finding(path="a.py", line=5, severity="error", message="boom here")
        body = handler_module._format_inline_comment("TEST COVERAGE", fd)
        assert "❌" in body
        assert "TEST COVERAGE" in body
        assert "boom here" in body
        # No enclosing symbol -> no "in `...`" clause.
        assert "in `" not in body

    def test_warning_glyph_default(self):
        fd = Finding(path="a.py", line=5, message="watch out")
        body = handler_module._format_inline_comment("BREAKING CHANGES", fd)
        assert "⚠️" in body

    def test_info_glyph(self):
        fd = Finding(path="a.py", line=5, severity="info", message="fyi")
        body = handler_module._format_inline_comment("X", fd)
        assert "ℹ️" in body

    def test_includes_enclosing_symbol_when_present(self):
        fd = Finding(path="a.py", line=5, message="m", enclosing_symbol="do_thing")
        body = handler_module._format_inline_comment("CHECK", fd)
        assert "in `do_thing`" in body

    def test_no_aggregate_count(self):
        # ADR-031/032: one concrete finding, no "N issues found" rollup.
        fd = Finding(path="a.py", line=5, message="a single concrete problem")
        body = handler_module._format_inline_comment("CHECK", fd)
        assert body.count("a single concrete problem") == 1
        lowered = body.lower()
        assert "issues found" not in lowered
        assert "findings" not in lowered


class TestFormatInlineBody:
    """_format_inline_body: single keeps old format; multiple merge, no counts."""

    def test_single_finding_matches_single_formatter(self):
        fd = Finding(path="a.py", line=5, message="solo")
        assert (handler_module._format_inline_body("CHECK", [fd])
                == handler_module._format_inline_comment("CHECK", fd))

    def test_multiple_findings_enumerated_no_counts(self):
        fds = [Finding(path="a.py", line=5, severity="error", message="one"),
               Finding(path="a.py", line=5, message="two")]
        body = handler_module._format_inline_body("BREAKING CHANGES", fds)
        assert "one" in body and "two" in body
        assert "- " in body  # bulleted merge
        low = body.lower()
        assert "2 findings" not in low and "issues found" not in low


class _InlineJobMixin:
    def _github_job(self):
        return CheckJob(
            checkType="TEST_COVERAGE",
            pr_number=7,
            repository="owner/repo",
            changedFiles=["src/x.py"],
        )


class TestSyncInlineFindings(_InlineJobMixin):
    """_sync_inline_findings: upsert + stale-resolution per (path, line)."""

    def _patches(self):
        """Patch all provider-facing leaf helpers + the provider context."""
        return {
            "_inline_provider_context": patch(
                "process_pr_check_handler._inline_provider_context",
                return_value={"token": "t", "head_sha": "h"}),
            "_post_inline": patch("process_pr_check_handler._post_inline"),
            "_patch_inline": patch("process_pr_check_handler._patch_inline"),
            "_delete_inline": patch("process_pr_check_handler._delete_inline"),
        }

    def test_new_finding_posts_and_stores_row(self):
        job = self._github_job()
        findings = [{"path": "src/x.py", "line": 10, "message": "bad"}]
        with patch("process_pr_check_handler._query_inline_rows", return_value={}), \
             patch("process_pr_check_handler._inline_provider_context",
                   return_value={"token": "t", "head_sha": "h"}), \
             patch("process_pr_check_handler._post_inline",
                   return_value={"providerCommentId": 55}) as mock_post, \
             patch("process_pr_check_handler._patch_inline") as mock_patch, \
             patch("process_pr_check_handler.dual_write.ddb_put_item") as mock_put:
            handler_module._sync_inline_findings(job, findings)

        mock_post.assert_called_once()
        mock_patch.assert_not_called()
        # INLINE#{check}#{path}#{line} row stored via dual_write.
        item = mock_put.call_args.args[1]
        assert item["SK"] == "INLINE#test_coverage#src/x.py#10"
        assert item["PK"] == "PR#github#owner/repo#7"
        assert item["path"] == "src/x.py"
        assert item["line"] == 10
        assert item["providerCommentId"] == 55

    def test_two_findings_same_line_merge_into_one_comment(self):
        """Two findings on the same (path, line) -> ONE comment containing both,
        one bookkeeping row. Neither is silently dropped (old dict last-wins)."""
        job = self._github_job()
        findings = [
            {"path": "src/x.py", "line": 10, "message": "first problem"},
            {"path": "src/x.py", "line": 10, "message": "second problem"},
        ]
        captured = {}

        def fake_post(job, ctx, body, fd):
            captured["body"] = body
            return {"providerCommentId": 55}

        with patch("process_pr_check_handler._query_inline_rows", return_value={}), \
             patch("process_pr_check_handler._inline_provider_context",
                   return_value={"token": "t", "head_sha": "h"}), \
             patch("process_pr_check_handler._post_inline",
                   side_effect=fake_post) as mock_post, \
             patch("process_pr_check_handler.dual_write.ddb_put_item") as mock_put:
            handler_module._sync_inline_findings(job, findings)

        mock_post.assert_called_once()  # ONE comment for the line, not two
        mock_put.assert_called_once()   # ONE bookkeeping row for the line
        assert "first problem" in captured["body"]
        assert "second problem" in captured["body"]

    def test_existing_matching_row_patches_not_posts(self):
        job = self._github_job()
        sk = "INLINE#test_coverage#src/x.py#10"
        existing = {sk: {"PK": "PR#github#owner/repo#7", "SK": sk,
                         "path": "src/x.py", "line": 10, "providerCommentId": 55}}
        findings = [{"path": "src/x.py", "line": 10, "message": "still bad"}]
        with patch("process_pr_check_handler._query_inline_rows", return_value=existing), \
             patch("process_pr_check_handler._inline_provider_context",
                   return_value={"token": "t", "head_sha": "h"}), \
             patch("process_pr_check_handler._post_inline") as mock_post, \
             patch("process_pr_check_handler._patch_inline") as mock_patch, \
             patch("process_pr_check_handler.dual_write.ddb_put_item") as mock_put:
            handler_module._sync_inline_findings(job, findings)

        mock_patch.assert_called_once()
        mock_post.assert_not_called()
        mock_put.assert_not_called()  # no new row on a patch

    def test_stale_row_deleted_when_not_in_findings(self):
        job = self._github_job()
        sk = "INLINE#test_coverage#src/old.py#3"
        existing = {sk: {"PK": "PR#github#owner/repo#7", "SK": sk,
                         "path": "src/old.py", "line": 3, "providerCommentId": 99}}
        with patch("process_pr_check_handler._query_inline_rows", return_value=existing), \
             patch("process_pr_check_handler._inline_provider_context",
                   return_value={"token": "t", "head_sha": "h"}), \
             patch("process_pr_check_handler._delete_inline") as mock_del, \
             patch("process_pr_check_handler.dynamodb") as mock_ddb:
            mock_table = Mock()
            mock_ddb.Table.return_value = mock_table
            handler_module._sync_inline_findings(job, [])  # no current findings

        mock_del.assert_called_once()
        # Bookkeeping row removed too.
        mock_table.delete_item.assert_called_once_with(
            Key={"PK": "PR#github#owner/repo#7", "SK": sk})

    def test_malformed_finding_is_skipped(self):
        job = self._github_job()
        # line=0 fails the Finding validator; message missing on the second.
        findings = [{"path": "a.py", "line": 0, "message": "m"},
                    {"path": "b.py", "line": 2}]
        with patch("process_pr_check_handler._query_inline_rows", return_value={}), \
             patch("process_pr_check_handler._inline_provider_context") as mock_ctx, \
             patch("process_pr_check_handler._post_inline") as mock_post, \
             patch("process_pr_check_handler.dual_write.ddb_put_item"):
            # Both findings malformed -> desired empty, existing empty -> early return.
            handler_module._sync_inline_findings(job, findings)
        mock_ctx.assert_not_called()
        mock_post.assert_not_called()

    def test_empty_findings_and_no_rows_short_circuits(self):
        job = self._github_job()
        with patch("process_pr_check_handler._query_inline_rows", return_value={}), \
             patch("process_pr_check_handler._inline_provider_context") as mock_ctx, \
             patch("process_pr_check_handler._post_inline") as mock_post, \
             patch("process_pr_check_handler._patch_inline") as mock_patch, \
             patch("process_pr_check_handler._delete_inline") as mock_del:
            handler_module._sync_inline_findings(job, [])
        mock_ctx.assert_not_called()
        mock_post.assert_not_called()
        mock_patch.assert_not_called()
        mock_del.assert_not_called()

    def test_provider_post_exception_is_swallowed(self):
        job = self._github_job()
        findings = [{"path": "src/x.py", "line": 10, "message": "bad"}]
        with patch("process_pr_check_handler._query_inline_rows", return_value={}), \
             patch("process_pr_check_handler._inline_provider_context",
                   return_value={"token": "t", "head_sha": "h"}), \
             patch("process_pr_check_handler._post_inline",
                   side_effect=RuntimeError("provider 500")), \
             patch("process_pr_check_handler._store_inline_row") as mock_store:
            # Must not raise -- inline sync is best-effort/non-fatal.
            handler_module._sync_inline_findings(job, findings)
        # Store never reached because POST raised first.
        mock_store.assert_not_called()


class TestComputeAnchors:
    """_compute_anchors threads the right provider args; any failure -> {}."""

    def _job(self, provider, **extra):
        kwargs = dict(
            checkType="TEST_COVERAGE",
            pr_number=7,
            repository="owner/repo",
            changedFiles=["f.py"],
            provider=provider,
        )
        kwargs.update(extra)
        return CheckJob(**kwargs)

    def test_github(self):
        job = self._job("github")
        with patch("process_pr_check_handler.get_github_token", return_value="ghp"), \
             patch("shared.pr_metadata.fetch_pr_metadata",
                   return_value=SimpleNamespace(base_sha="b", head_sha="h")) as mock_meta, \
             patch("shared.pr_diff.added_lines_by_file",
                   return_value={"f.py": [3, 8]}) as mock_fal:
            out = handler_module._compute_anchors(job)
        assert out == {"f.py": [3, 8]}
        mock_meta.assert_called_once_with(
            "github", 7, repository="owner/repo", github_token="ghp")
        mock_fal.assert_called_once_with(
            "github", "owner/repo", "b", "h", ["f.py"], github_token="ghp")

    def test_gitlab(self):
        job = self._job("gitlab", repository="group/sub/proj", connection_id="c1")
        with patch("check_handlers.gitlab_context.get_gitlab_token",
                   return_value="glpat"), \
             patch("check_handlers.gitlab_context.get_gitlab_base_url",
                   return_value="https://gl.example"), \
             patch("shared.pr_metadata.fetch_pr_metadata",
                   return_value=SimpleNamespace(base_sha="b", head_sha="h")) as mock_meta, \
             patch("shared.pr_diff.added_lines_by_file",
                   return_value={"f.py": [2]}) as mock_fal:
            out = handler_module._compute_anchors(job)
        assert out == {"f.py": [2]}
        mock_meta.assert_called_once_with(
            "gitlab", 7, gitlab_project_path="group/sub/proj",
            gitlab_access_token="glpat", gitlab_base_url="https://gl.example")
        mock_fal.assert_called_once_with(
            "gitlab", "group/sub/proj", "b", "h", ["f.py"],
            gitlab_access_token="glpat", gitlab_base_url="https://gl.example")

    def test_azure_devops(self):
        job = self._job(
            "azure_devops", repository="example-repo", connection_id="c1",
            org="example-org", project="example-project", repo="example-repo")
        with patch("process_pr_check_handler._ado_refresh_access_token",
                   return_value="adotok"), \
             patch("process_pr_check_handler._get_ado_client_secret",
                   return_value="secret"), \
             patch("shared.pr_metadata.fetch_pr_metadata",
                   return_value=SimpleNamespace(base_sha="b", head_sha="h")) as mock_meta, \
             patch("shared.pr_diff.added_lines_by_file",
                   return_value={"f.py": [9, 27, 35]}) as mock_fal:
            out = handler_module._compute_anchors(job)
        assert out == {"f.py": [9, 27, 35]}
        mock_meta.assert_called_once_with(
            "azure_devops", 7, ado_org="example-org",
            ado_project="example-project", ado_repo="example-repo",
            ado_access_token="adotok")
        mock_fal.assert_called_once_with(
            "azure_devops", "example-repo", "b", "h", ["f.py"],
            ado_org="example-org", ado_project="example-project",
            ado_repo="example-repo", ado_access_token="adotok")

    def test_any_exception_returns_empty_dict(self):
        job = self._job("github")
        with patch("process_pr_check_handler.get_github_token",
                   side_effect=RuntimeError("SSM down")):
            out = handler_module._compute_anchors(job)
        assert out == {}


def _github_job():
    return CheckJob(checkType="TEST_COVERAGE", pr_number=7,
                    repository="owner/repo", changedFiles=["src/x.py"])


def _gitlab_job():
    return CheckJob(checkType="TEST_COVERAGE", pr_number=7,
                    repository="group/sub/proj", changedFiles=["src/x.py"],
                    provider="gitlab", connection_id="c1")


def _ado_job():
    return CheckJob(checkType="TEST_COVERAGE", pr_number=7,
                    repository="example-repo", changedFiles=["src/x.py"],
                    provider="azure_devops", connection_id="c1",
                    org="example-org", project="example-project", repo="example-repo")


class TestGithubPrHeadSha:
    @patch('process_pr_check_handler.requests.get')
    def test_returns_head_sha(self, mock_get):
        resp = Mock()
        resp.json.return_value = {"head": {"sha": "abc123"}}
        resp.raise_for_status = Mock()
        mock_get.return_value = resp
        out = handler_module._github_pr_head_sha("owner/repo", 7, "tok")
        assert out == "abc123"
        assert mock_get.call_args.args[0] == (
            "https://api.github.com/repos/owner/repo/pulls/7"
        )


class TestQueryInlineRows:
    @patch('process_pr_check_handler.dynamodb')
    def test_paginates_and_keys_by_sk(self, mock_ddb):
        table = Mock()
        mock_ddb.Table.return_value = table
        table.query.side_effect = [
            {"Items": [{"SK": "INLINE#tc#a.py#1", "path": "a.py", "line": 1}],
             "LastEvaluatedKey": {"PK": "p", "SK": "INLINE#tc#a.py#1"}},
            {"Items": [{"SK": "INLINE#tc#b.py#2", "path": "b.py", "line": 2}]},
        ]
        rows = handler_module._query_inline_rows(
            "PR#github#owner/repo#7", "INLINE#tc#")
        assert set(rows.keys()) == {"INLINE#tc#a.py#1", "INLINE#tc#b.py#2"}
        assert table.query.call_count == 2


class TestInlineProviderContext:
    def test_github(self):
        job = _github_job()
        with patch("process_pr_check_handler.get_github_token", return_value="t"), \
             patch("process_pr_check_handler._github_pr_head_sha", return_value="h"):
            ctx = handler_module._inline_provider_context(job)
        assert ctx == {"token": "t", "head_sha": "h"}

    def test_gitlab(self):
        job = _gitlab_job()
        with patch("check_handlers.gitlab_context.get_gitlab_token",
                   return_value="glpat"), \
             patch("check_handlers.gitlab_context.get_gitlab_base_url",
                   return_value="https://gl.example"), \
             patch("shared.gitlab_api.get_merge_request",
                   return_value={"diff_refs": {"base_sha": "b", "start_sha": "s",
                                               "head_sha": "h"}}):
            ctx = handler_module._inline_provider_context(job)
        assert ctx["token"] == "glpat"
        assert ctx["base_url"] == "https://gl.example"
        assert ctx["diff_refs"] == {"base_sha": "b", "start_sha": "s", "head_sha": "h"}

    def test_azure_devops(self):
        job = _ado_job()
        with patch("process_pr_check_handler._ado_refresh_access_token",
                   return_value="adotok"), \
             patch("process_pr_check_handler._get_ado_client_secret",
                   return_value="secret"):
            ctx = handler_module._inline_provider_context(job)
        assert ctx == {"access_token": "adotok", "org": "example-org",
                       "project": "example-project", "repo": "example-repo"}

    def test_azure_devops_no_token_raises(self):
        job = _ado_job()
        with patch("process_pr_check_handler._ado_refresh_access_token",
                   return_value=None), \
             patch("process_pr_check_handler._get_ado_client_secret",
                   return_value="secret"):
            with pytest.raises(RuntimeError, match="reconnect_required"):
                handler_module._inline_provider_context(job)


class TestPostInline:
    def test_github(self):
        job = _github_job()
        fd = Finding(path="src/x.py", line=12, message="m")
        ctx = {"token": "t", "head_sha": "h"}
        with patch("process_pr_check_handler.post_pr_review_comment",
                   return_value={"id": 55}) as mock_post:
            ids = handler_module._post_inline(job, ctx, "body", fd)
        assert ids == {"providerCommentId": 55}
        assert mock_post.call_args.kwargs == {
            "commit_id": "h", "path": "src/x.py", "line": 12, "side": "RIGHT"}

    def test_gitlab_right_side_uses_new_line(self):
        job = _gitlab_job()
        fd = Finding(path="src/x.py", line=12, message="m")  # RIGHT default
        ctx = {"token": "glpat", "base_url": "https://gl.example",
               "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}
        with patch("shared.gitlab_api.post_merge_request_discussion",
                   return_value={"id": "disc-1", "notes": [{"id": 7}]}) as mock_post:
            ids = handler_module._post_inline(job, ctx, "body", fd)
        assert ids == {"providerDiscussionId": "disc-1", "providerCommentId": 7}
        position = mock_post.call_args.args[4]
        assert position["new_line"] == 12
        assert position["new_path"] == "src/x.py"
        assert "old_line" not in position

    def test_gitlab_left_side_uses_old_line(self):
        job = _gitlab_job()
        fd = Finding(path="src/x.py", line=4, side="LEFT", message="m")
        ctx = {"token": "glpat", "base_url": "https://gl.example",
               "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}
        with patch("shared.gitlab_api.post_merge_request_discussion",
                   return_value={"id": "disc-1", "notes": []}) as mock_post:
            ids = handler_module._post_inline(job, ctx, "body", fd)
        # notes empty -> providerCommentId None
        assert ids == {"providerDiscussionId": "disc-1", "providerCommentId": None}
        position = mock_post.call_args.args[4]
        assert position["old_line"] == 4
        assert position["old_path"] == "src/x.py"
        assert "new_line" not in position

    def test_azure_devops(self):
        job = _ado_job()
        fd = Finding(path="src/x.py", line=9, message="m")
        ctx = {"access_token": "adotok", "org": "example-org",
               "project": "example-project", "repo": "example-repo"}
        with patch("shared.azure_devops_api.post_pull_request_inline_thread",
                   return_value={"id": 99, "comments": [{"id": 1}]}) as mock_post:
            ids = handler_module._post_inline(job, ctx, "body", fd)
        assert ids == {"providerThreadId": 99, "providerThreadCommentId": 1}
        assert mock_post.call_args.kwargs == {"file_path": "src/x.py", "line": 9}


class TestPatchInline:
    def test_github(self):
        job = _github_job()
        ctx = {"token": "t", "head_sha": "h"}
        row = {"providerCommentId": 55}
        with patch("process_pr_check_handler.patch_pr_review_comment") as mock_patch:
            handler_module._patch_inline(job, ctx, "body", row)
        mock_patch.assert_called_once_with("owner/repo", 55, "body", "t")

    def test_gitlab(self):
        job = _gitlab_job()
        ctx = {"token": "glpat", "base_url": "https://gl.example"}
        row = {"providerDiscussionId": "disc-1", "providerCommentId": 7}
        with patch("shared.gitlab_api.update_merge_request_discussion_note") as mock_u:
            handler_module._patch_inline(job, ctx, "body", row)
        mock_u.assert_called_once()
        assert mock_u.call_args.args[3] == "disc-1"  # discussion_id
        assert mock_u.call_args.args[4] == 7  # note_id

    def test_azure_devops(self):
        job = _ado_job()
        ctx = {"access_token": "adotok", "org": "example-org",
               "project": "example-project", "repo": "example-repo"}
        row = {"providerThreadId": 99, "providerThreadCommentId": 1}
        with patch("shared.azure_devops_api.update_pull_request_thread_comment") as mock_u:
            handler_module._patch_inline(job, ctx, "body", row)
        mock_u.assert_called_once()


class TestDeleteInline:
    def test_github(self):
        job = _github_job()
        ctx = {"token": "t"}
        row = {"providerCommentId": 55}
        with patch("process_pr_check_handler.delete_pr_review_comment") as mock_del:
            handler_module._delete_inline(job, ctx, row)
        mock_del.assert_called_once_with("owner/repo", 55, "t")

    def test_gitlab(self):
        job = _gitlab_job()
        ctx = {"token": "glpat", "base_url": "https://gl.example"}
        row = {"providerDiscussionId": "disc-1", "providerCommentId": 7}
        with patch("shared.gitlab_api.delete_merge_request_discussion_note") as mock_del:
            handler_module._delete_inline(job, ctx, row)
        mock_del.assert_called_once()

    def test_azure_devops(self):
        job = _ado_job()
        ctx = {"access_token": "adotok", "org": "example-org",
               "project": "example-project", "repo": "example-repo"}
        row = {"providerThreadId": 99, "providerThreadCommentId": 1}
        with patch("shared.azure_devops_api.delete_pull_request_thread_comment") as mock_del:
            handler_module._delete_inline(job, ctx, row)
        mock_del.assert_called_once()


class TestSyncInlineFindingsExtraBranches(_InlineJobMixin):
    """Remaining defensive branches in _sync_inline_findings."""

    def test_existing_row_with_unparseable_line_is_skipped(self):
        job = self._github_job()
        existing = {"INLINE#test_coverage#bad.py#x": {
            "path": "bad.py", "line": "not-an-int"}}
        findings = [{"path": "src/x.py", "line": 10, "message": "m"}]
        with patch("process_pr_check_handler._query_inline_rows", return_value=existing), \
             patch("process_pr_check_handler._inline_provider_context",
                   return_value={"token": "t", "head_sha": "h"}), \
             patch("process_pr_check_handler._post_inline",
                   return_value={"providerCommentId": 1}), \
             patch("process_pr_check_handler._delete_inline") as mock_del, \
             patch("process_pr_check_handler.dual_write.ddb_put_item"):
            handler_module._sync_inline_findings(job, findings)
        # The malformed existing row is dropped from existing_by_key, so it is
        # NOT treated as a stale row to resolve.
        mock_del.assert_not_called()

    def test_stale_resolve_failures_are_swallowed(self):
        job = self._github_job()
        sk = "INLINE#test_coverage#old.py#3"
        existing = {sk: {"PK": "PR#github#owner/repo#7", "SK": sk,
                         "path": "old.py", "line": 3}}
        with patch("process_pr_check_handler._query_inline_rows", return_value=existing), \
             patch("process_pr_check_handler._inline_provider_context",
                   return_value={"token": "t", "head_sha": "h"}), \
             patch("process_pr_check_handler._delete_inline",
                   side_effect=RuntimeError("provider down")), \
             patch("process_pr_check_handler.dynamodb") as mock_ddb:
            table = Mock()
            table.delete_item.side_effect = RuntimeError("ddb down")
            mock_ddb.Table.return_value = table
            # Both the provider delete AND the row delete raise -> both swallowed.
            handler_module._sync_inline_findings(job, [])


# --------------------------------------------------------------------------- #
# ADR-057: ADO summary completion fan-in
# (_ado_mark_done_and_maybe_post_summary posts the deferred summary as the
#  newest thread once every expected check has completed, exactly once).
# --------------------------------------------------------------------------- #
from unittest.mock import Mock as _Mock  # noqa: E402
from botocore.exceptions import ClientError as _ClientError  # noqa: E402


def _ddb_with(update_return=None, update_side_effect=None):
    table = _Mock()
    if update_side_effect is not None:
        table.update_item.side_effect = update_side_effect
    else:
        table.update_item.return_value = update_return
    ddb = _Mock()
    ddb.Table.return_value = table
    return ddb, table


def test_ado_fanin_noop_when_no_deferred(monkeypatch):
    monkeypatch.setattr(handler_module, "_get_existing_check_result_row",
                        lambda pk, sk: None)
    upsert = _Mock()
    monkeypatch.setattr(handler_module, "_ado_upsert_summary", upsert)
    handler_module._ado_mark_done_and_maybe_post_summary(_ado_job())
    upsert.assert_not_called()


def test_ado_fanin_partial_does_not_post(monkeypatch):
    monkeypatch.setattr(
        handler_module, "_get_existing_check_result_row",
        lambda pk, sk: {"summaryBody": "x",
                        "expectedChecks": {"ADR_COMPLIANCE", "LICENSE_COMPLIANCE"}})
    ddb, table = _ddb_with(update_return={"Attributes": {"completedChecks": {"ADR_COMPLIANCE"}}})
    monkeypatch.setattr(handler_module, "dynamodb", ddb)
    upsert = _Mock()
    monkeypatch.setattr(handler_module, "_ado_upsert_summary", upsert)
    handler_module._ado_mark_done_and_maybe_post_summary(_ado_job())
    upsert.assert_not_called()


def test_ado_fanin_posts_once_when_last(monkeypatch):
    monkeypatch.setattr(
        handler_module, "_get_existing_check_result_row",
        lambda pk, sk: {"summaryBody": "BODY",
                        "expectedChecks": {"ADR_COMPLIANCE", "LICENSE_COMPLIANCE"}})
    ddb, table = _ddb_with(update_return={"Attributes": {
        "completedChecks": {"ADR_COMPLIANCE", "LICENSE_COMPLIANCE"}}})
    monkeypatch.setattr(handler_module, "dynamodb", ddb)
    upsert = _Mock()
    monkeypatch.setattr(handler_module, "_ado_upsert_summary", upsert)
    handler_module._ado_mark_done_and_maybe_post_summary(_ado_job())
    upsert.assert_called_once()
    assert upsert.call_args[0][1] == "BODY"       # the stored body is posted
    assert table.update_item.call_count == 2      # ADD completion, then SET claim


def test_ado_fanin_skips_when_already_posted(monkeypatch):
    monkeypatch.setattr(
        handler_module, "_get_existing_check_result_row",
        lambda pk, sk: {"summaryBody": "B", "expectedChecks": {"ADR_COMPLIANCE"}})
    ddb, table = _ddb_with(update_return={"Attributes": {
        "completedChecks": {"ADR_COMPLIANCE"}, "summaryPosted": True}})
    monkeypatch.setattr(handler_module, "dynamodb", ddb)
    upsert = _Mock()
    monkeypatch.setattr(handler_module, "_ado_upsert_summary", upsert)
    handler_module._ado_mark_done_and_maybe_post_summary(_ado_job())
    upsert.assert_not_called()
    assert table.update_item.call_count == 1      # only the ADD; no claim attempt


def test_ado_fanin_claim_race_lost(monkeypatch):
    monkeypatch.setattr(
        handler_module, "_get_existing_check_result_row",
        lambda pk, sk: {"summaryBody": "B", "expectedChecks": {"ADR_COMPLIANCE"}})

    def _update(**kwargs):
        if "ADD" in kwargs.get("UpdateExpression", ""):
            return {"Attributes": {"completedChecks": {"ADR_COMPLIANCE"}}}
        raise _ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    ddb, table = _ddb_with(update_side_effect=_update)
    monkeypatch.setattr(handler_module, "dynamodb", ddb)
    upsert = _Mock()
    monkeypatch.setattr(handler_module, "_ado_upsert_summary", upsert)
    handler_module._ado_mark_done_and_maybe_post_summary(_ado_job())
    upsert.assert_not_called()                    # another worker won the claim


# --------------------------------------------------------------------------- #
# ADR-057: _ado_upsert_summary posts a FRESH thread (delete-and-recreate), not
# a PATCH. ADO orders Overview threads by publishedDate (newest on top) and a
# PATCH keeps the old publishedDate, so an edited thread stays at the bottom.
# --------------------------------------------------------------------------- #
def test_ado_upsert_summary_deletes_old_then_posts_fresh(monkeypatch):
    existing = {"PK": "PR#azure_devops#example-repo#7", "SK": "COMMENT#summary",
                "providerThreadId": 48, "providerThreadCommentId": 1}
    monkeypatch.setattr(handler_module, "_get_existing_check_result_row",
                        lambda pk, sk: existing)
    deleted = _Mock(return_value=True)
    monkeypatch.setattr(handler_module, "_try_delete_ado_comment", deleted)
    posted = _Mock(return_value={"id": 90, "comments": [{"id": 5}]})
    monkeypatch.setattr(handler_module, "_post_azure_devops_thread", posted)
    dw = _Mock()
    monkeypatch.setattr(handler_module, "dual_write", dw)

    handler_module._ado_upsert_summary(_ado_job(), "FRESH BODY")

    deleted.assert_called_once()                       # stale thread removed
    assert deleted.call_args.kwargs["existing_row"] is existing
    posted.assert_called_once()                        # then a brand-new POST
    assert posted.call_args[0][4] == "FRESH BODY"      # body positional arg
    dw.ddb_put_item.assert_called_once()
    item = dw.ddb_put_item.call_args[0][1]
    assert item["SK"] == "COMMENT#summary"
    assert item["providerThreadId"] == 90              # new thread id recorded
    assert item["providerThreadCommentId"] == 5


def test_ado_upsert_summary_no_prior_just_posts(monkeypatch):
    monkeypatch.setattr(handler_module, "_get_existing_check_result_row",
                        lambda pk, sk: None)
    deleted = _Mock()
    monkeypatch.setattr(handler_module, "_try_delete_ado_comment", deleted)
    posted = _Mock(return_value={"id": 91, "comments": [{"id": 2}]})
    monkeypatch.setattr(handler_module, "_post_azure_devops_thread", posted)
    dw = _Mock()
    monkeypatch.setattr(handler_module, "dual_write", dw)

    handler_module._ado_upsert_summary(_ado_job(), "BODY")

    deleted.assert_not_called()                        # nothing to delete
    posted.assert_called_once()
    dw.ddb_put_item.assert_called_once()


def test_ado_upsert_summary_delete_failure_still_posts(monkeypatch):
    monkeypatch.setattr(
        handler_module, "_get_existing_check_result_row",
        lambda pk, sk: {"providerThreadId": 48, "providerThreadCommentId": 1})
    monkeypatch.setattr(handler_module, "_try_delete_ado_comment",
                        _Mock(return_value=False))     # delete no-op/failed
    posted = _Mock(return_value={"id": 92, "comments": [{"id": 3}]})
    monkeypatch.setattr(handler_module, "_post_azure_devops_thread", posted)
    dw = _Mock()
    monkeypatch.setattr(handler_module, "dual_write", dw)

    handler_module._ado_upsert_summary(_ado_job(), "BODY")

    posted.assert_called_once()                        # fresh post regardless
    dw.ddb_put_item.assert_called_once()


class TestPrCheckWorkspaceAttribution:
    """PR-analyzer Bedrock spend must attribute to the PR repo's owning
    workspace in the audit row, not WS#SYSTEM. call_bedrock is the shared
    seam every check routes through, so the forwarding is verified there."""

    def test_call_bedrock_forwards_workspace_id_to_audit(self):
        from types import SimpleNamespace

        resp = SimpleNamespace(
            text="ok",
            stop_reason=SimpleNamespace(value="end_turn"),
            usage=SimpleNamespace(input_tokens=50, output_tokens=20),
        )
        with patch.object(
            bedrock_module, "invoke_model", return_value=resp
        ), patch.object(
            bedrock_module, "log_audit_entry"
        ) as mock_audit:
            bedrock_module.call_bedrock("prompt", workspace_id="ws_eng")

        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["workspace_id"] == "ws_eng"

    def test_call_bedrock_defaults_empty_workspace_id(self):
        from types import SimpleNamespace

        resp = SimpleNamespace(
            text="ok",
            stop_reason=SimpleNamespace(value="end_turn"),
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )
        with patch.object(
            bedrock_module, "invoke_model", return_value=resp
        ), patch.object(
            bedrock_module, "log_audit_entry"
        ) as mock_audit:
            bedrock_module.call_bedrock("prompt")

        assert mock_audit.call_args.kwargs["workspace_id"] == ""
