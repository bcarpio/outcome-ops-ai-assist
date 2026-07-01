"""
Unit tests for analyze-pr Lambda function.

Tests cover:
- Request/response schema validation
- GitHub API integration (PR fetch, files, comments)
- File change parsing and check determination
- SQS job queueing
- End-to-end handler orchestration
- Error handling
"""

import json
from unittest.mock import Mock, patch, MagicMock
import pytest
import sys
import os
import importlib.util
from botocore.exceptions import ClientError

# Load the analyze-pr handler module with unique name to avoid conflicts
handler_path = os.path.join(os.path.dirname(__file__), '../../analyze-pr/handler.py')
spec = importlib.util.spec_from_file_location("analyze_pr_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['analyze_pr_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import from the loaded module
handler = handler_module.handler
CheckType = handler_module.CheckType
AnalyzePrRequest = handler_module.AnalyzePrRequest
AnalyzePrResponse = handler_module.AnalyzePrResponse
CheckJob = handler_module.CheckJob
GitHubPullRequest = handler_module.GitHubPullRequest
GitHubFile = handler_module.GitHubFile
get_github_token = handler_module.get_github_token
fetch_pull_request = handler_module.fetch_pull_request
fetch_pr_files = handler_module.fetch_pr_files
post_pr_comment = handler_module.post_pr_comment
parse_changed_files = handler_module.parse_changed_files
queue_check_jobs = handler_module.queue_check_jobs
fetch_repo_config = handler_module.fetch_repo_config
get_backend_from_config = handler_module.get_backend_from_config
BackendPatterns = handler_module.BackendPatterns
BACKEND_PATTERNS = handler_module.BACKEND_PATTERNS


class TestSchemas:
    """Test Pydantic schema validation."""

    def test_analyze_pr_request_valid(self):
        """Test valid request schema."""
        # Arrange & Act
        request = AnalyzePrRequest(
            pr_number=123,
            repository="owner/repo"
        )

        # Assert
        assert request.pr_number == 123
        assert request.repository == "owner/repo"

    def test_analyze_pr_request_invalid_pr_number(self):
        """Test request with invalid PR number."""
        # Act & Assert
        with pytest.raises(ValueError):
            AnalyzePrRequest(pr_number=-1, repository="owner/repo")

    def test_analyze_pr_request_invalid_repository_format(self):
        """Test request with invalid repository format."""
        # Act & Assert
        with pytest.raises(ValueError):
            AnalyzePrRequest(pr_number=123, repository="invalid")

        with pytest.raises(ValueError):
            AnalyzePrRequest(pr_number=123, repository="owner/repo/extra")

    def test_analyze_pr_response_valid(self):
        """Test valid response schema."""
        # Arrange & Act
        response = AnalyzePrResponse(
            message="Success",
            pr_number=123,
            checks_queued=3
        )

        # Assert
        assert response.message == "Success"
        assert response.pr_number == 123
        assert response.checks_queued == 3

    def test_check_job_valid(self):
        """Test valid check job schema."""
        # Arrange & Act
        job = CheckJob(
            check_type=CheckType.ADR_COMPLIANCE,
            pr_number=123,
            repository="owner/repo",
            changed_files=["lambda/handler/handler.py"]
        )

        # Assert
        assert job.check_type == CheckType.ADR_COMPLIANCE
        assert job.pr_number == 123
        assert len(job.changed_files) == 1


class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM."""

    @patch('analyze_pr_handler.ssm_client')
    def test_get_github_token_success(self, mock_ssm):
        """Test successful token retrieval."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {
                "Value": "ghp_testtoken123"
            }
        }

        # Act
        token = get_github_token()

        # Assert
        assert token == "ghp_testtoken123"
        mock_ssm.get_parameter.assert_called_once()
        call_kwargs = mock_ssm.get_parameter.call_args[1]
        assert call_kwargs["WithDecryption"] is True

    @patch('analyze_pr_handler.ssm_client')
    def test_get_github_token_not_found(self, mock_ssm):
        """Test token not found in SSM."""
        # Arrange
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            get_github_token()


class TestFetchPullRequest:
    """Test fetching PR details from GitHub API."""

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pull_request_success(self, mock_get):
        """Test successful PR fetch."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "number": 123,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/123",
            "base": {"ref": "main"},
            "head": {"ref": "feature-branch"}
        }
        mock_get.return_value = mock_response

        # Act
        pr = fetch_pull_request("owner/repo", 123, "test-token")

        # Assert
        assert pr.number == 123
        assert pr.title == "Test PR"
        assert pr.html_url == "https://github.com/owner/repo/pull/123"
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args[1]
        assert "Bearer test-token" in call_kwargs["headers"]["Authorization"]

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pull_request_api_error(self, mock_get):
        """Test PR fetch with API error."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("Not Found")
        mock_get.return_value = mock_response

        # Act & Assert
        with pytest.raises(Exception):
            fetch_pull_request("owner/repo", 123, "test-token")


class TestFetchPrFiles:
    """Test fetching PR file changes from GitHub API."""

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pr_files_success(self, mock_get):
        """Test successful file fetch."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "filename": "lambda/test-handler/handler.py",
                "status": "modified",
                "additions": 10,
                "deletions": 5,
                "changes": 15
            },
            {
                "filename": "terraform/main.tf",
                "status": "added",
                "additions": 50,
                "deletions": 0,
                "changes": 50
            }
        ]
        mock_get.return_value = mock_response

        # Act
        files = fetch_pr_files("owner/repo", 123, "test-token")

        # Assert
        assert len(files) == 2
        assert files[0].filename == "lambda/test-handler/handler.py"
        assert files[0].status == "modified"
        assert files[1].filename == "terraform/main.tf"
        assert files[1].status == "added"

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_pr_files_empty(self, mock_get):
        """Test file fetch with no changes."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        # Act
        files = fetch_pr_files("owner/repo", 123, "test-token")

        # Assert
        assert len(files) == 0


class TestPostPrComment:
    """Test posting comments to PR."""

    @patch('analyze_pr_handler.requests.post')
    def test_post_pr_comment_success(self, mock_post):
        """Test successful comment posting."""
        # Arrange
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 456, "body": "Test comment"}
        mock_post.return_value = mock_response

        # Act
        result = post_pr_comment("owner/repo", 123, "Test comment", "test-token")

        # Assert
        assert result["id"] == 456
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["body"] == "Test comment"

    @patch('analyze_pr_handler.requests.post')
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
    """PATCH-side sibling to TestPostPrComment. Chunk 01 of the
    update-in-place PR-check UX plan."""

    @patch('analyze_pr_handler.requests.patch')
    def test_patch_pr_comment_success(self, mock_patch_req):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": 999, "body": "new body"}
        mock_patch_req.return_value = mock_response

        result = handler_module.patch_pr_comment(
            "owner/repo", 999, "new body", "ghp",
        )
        assert result["id"] == 999
        req_url = mock_patch_req.call_args.args[0]
        assert req_url == (
            "https://api.github.com/repos/owner/repo/issues/comments/999"
        )
        assert mock_patch_req.call_args.kwargs["json"] == {"body": "new body"}

    @patch('analyze_pr_handler.requests.patch')
    def test_patch_pr_comment_404_raises(self, mock_patch_req):
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = (
            handler_module.requests.exceptions.HTTPError("404 Not Found")
        )
        mock_patch_req.return_value = mock_response
        with pytest.raises(Exception, match="GitHub API error"):
            handler_module.patch_pr_comment(
                "owner/repo", 999, "body", "ghp",
            )


class TestAnalyzePrUpsertHelpers:
    """Chunk 04 of the update-in-place plan: analyze-pr's three
    self-emitted comments (analysis_started / structural_impact /
    no_checks_needed) get PATCHed on subsequent runs instead of POSTed."""

    def test_build_comment_pk_shape(self):
        assert handler_module._build_comment_pk(
            "github", "owner/repo", 123,
        ) == "PR#github#owner/repo#123"
        assert handler_module._build_comment_pk(
            "azure_devops", "org/project/repo", 42,
        ) == "PR#azure_devops#org/project/repo#42"

    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.patch_pr_comment')
    @patch('analyze_pr_handler._get_existing_comment_row', return_value=None)
    @patch('analyze_pr_handler._store_comment_row')
    def test_upsert_github_first_run_posts(
        self, mock_store, _mock_exist, mock_patch, mock_post,
    ):
        mock_post.return_value = {
            "id": 555,
            "html_url": "https://github.com/o/r/pull/1#issuecomment-555",
        }
        handler_module._upsert_github_comment(
            repository="owner/repo",
            pr_number=1,
            role=handler_module.COMMENT_ROLE_ANALYSIS_STARTED,
            body="hi",
            token="ghp",
        )
        mock_post.assert_called_once()
        mock_patch.assert_not_called()
        # Row stored with the returned comment ID.
        _, kwargs = mock_store.call_args
        assert kwargs["provider_comment_id"] == 555
        assert kwargs["sk"] == "COMMENT#analysis_started"

    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.patch_pr_comment')
    @patch('analyze_pr_handler._get_existing_comment_row')
    @patch('analyze_pr_handler._store_comment_row')
    def test_upsert_github_second_run_patches(
        self, mock_store, mock_exist, mock_patch, mock_post,
    ):
        mock_exist.return_value = {
            "PK": "PR#github#owner/repo#1",
            "SK": "COMMENT#analysis_started",
            "providerCommentId": 555,
        }
        mock_patch.return_value = {
            "id": 555,
            "html_url": "https://github.com/o/r/pull/1#issuecomment-555",
        }
        handler_module._upsert_github_comment(
            repository="owner/repo",
            pr_number=1,
            role=handler_module.COMMENT_ROLE_ANALYSIS_STARTED,
            body="new body",
            token="ghp",
        )
        mock_patch.assert_called_once()
        mock_post.assert_not_called()
        _, kwargs = mock_store.call_args
        assert kwargs["provider_comment_id"] == 555

    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.patch_pr_comment',
           side_effect=Exception("GitHub API error: 404"))
    @patch('analyze_pr_handler._get_existing_comment_row')
    @patch('analyze_pr_handler._store_comment_row')
    def test_upsert_github_patch_failure_falls_back_to_post(
        self, mock_store, mock_exist, mock_patch, mock_post,
    ):
        mock_exist.return_value = {
            "providerCommentId": 999,  # stale (deleted)
        }
        mock_post.return_value = {
            "id": 777,
            "html_url": "https://github.com/o/r/pull/1#issuecomment-777",
        }
        handler_module._upsert_github_comment(
            repository="owner/repo",
            pr_number=1,
            role=handler_module.COMMENT_ROLE_ANALYSIS_STARTED,
            body="hi",
            token="ghp",
        )
        mock_patch.assert_called_once()
        mock_post.assert_called_once()
        # The NEW POST id gets stored, not the stale 999.
        _, kwargs = mock_store.call_args
        assert kwargs["provider_comment_id"] == 777

    @patch('analyze_pr_handler._get_existing_comment_row', return_value=None)
    @patch('analyze_pr_handler._store_comment_row')
    def test_upsert_ado_first_run_posts(self, mock_store, _mock_exist):
        # First-run POST populates thread + comment IDs from response.
        with patch.dict(sys.modules, {
            'azure_devops_api': Mock(
                post_pull_request_thread=Mock(return_value={
                    "id": 88, "comments": [{"id": 1, "content": "x"}],
                }),
                update_pull_request_thread_comment=Mock(),
                AzureDevOpsAPIError=Exception,
            ),
        }):
            handler_module._upsert_ado_comment(
                repository="org/project/repo",
                ado_org="org", ado_project="project", ado_repo="repo",
                pr_number=42, access_token="tok",
                role=handler_module.COMMENT_ROLE_ANALYSIS_STARTED,
                body="hi",
            )
        _, kwargs = mock_store.call_args
        assert kwargs["provider_thread_id"] == 88
        assert kwargs["provider_thread_comment_id"] == 1
        assert kwargs["sk"] == "COMMENT#analysis_started"

    @patch('analyze_pr_handler._get_existing_comment_row')
    @patch('analyze_pr_handler._store_comment_row')
    def test_upsert_ado_second_run_patches(self, mock_store, mock_exist):
        mock_exist.return_value = {
            "providerThreadId": 88,
            "providerThreadCommentId": 1,
        }
        patch_mock = Mock(return_value={"id": 1})
        post_mock = Mock()
        with patch.dict(sys.modules, {
            'azure_devops_api': Mock(
                post_pull_request_thread=post_mock,
                update_pull_request_thread_comment=patch_mock,
                AzureDevOpsAPIError=Exception,
            ),
        }):
            handler_module._upsert_ado_comment(
                repository="org/project/repo",
                ado_org="org", ado_project="project", ado_repo="repo",
                pr_number=42, access_token="tok",
                role=handler_module.COMMENT_ROLE_ANALYSIS_STARTED,
                body="new body",
            )
        patch_mock.assert_called_once()
        post_mock.assert_not_called()
        _, kwargs = mock_store.call_args
        assert kwargs["provider_thread_id"] == 88
        assert kwargs["provider_thread_comment_id"] == 1


class TestParseChangedFiles:
    """Test file change parsing and check determination."""

    def test_parse_changed_files_lambda_handler(self):
        """Test parsing Lambda handler changes."""
        # Arrange
        files = [
            GitHubFile(
                filename="lambda/test-handler/handler.py",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert "lambda/test-handler/handler.py" in result["changed_files"]
        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.BREAKING_CHANGES.value in result["checks_to_run"]
        assert CheckType.ARCHITECTURAL_DUPLICATION.value in result["checks_to_run"]

    def test_parse_changed_files_terraform(self):
        """Test parsing Terraform changes."""
        # Arrange
        files = [
            GitHubFile(
                filename="terraform/main.tf",
                status="modified",
                additions=20,
                deletions=10,
                changes=30
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.README_FRESHNESS.value in result["checks_to_run"]

    def test_parse_changed_files_new_lambda_handler(self):
        """Test parsing new Lambda handler."""
        # Arrange
        files = [
            GitHubFile(
                filename="lambda/new-handler/handler.py",
                status="added",
                additions=100,
                deletions=0,
                changes=100
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.TEST_COVERAGE.value in result["checks_to_run"]
        assert CheckType.ARCHITECTURAL_DUPLICATION.value in result["checks_to_run"]

    def test_parse_changed_files_test_files_excluded(self):
        """Test that test files don't trigger checks."""
        # Arrange
        files = [
            GitHubFile(
                filename="lambda/tests/unit/test_handler.py",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.ADR_COMPLIANCE.value not in result["checks_to_run"]
        assert CheckType.ARCHITECTURAL_DUPLICATION.value not in result["checks_to_run"]

    def test_parse_changed_files_documentation(self):
        """Test parsing documentation changes."""
        # Arrange
        files = [
            GitHubFile(
                filename="docs/architecture.md",
                status="modified",
                additions=5,
                deletions=2,
                changes=7
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert CheckType.README_FRESHNESS.value in result["checks_to_run"]

    def test_parse_changed_files_no_checks(self):
        """Test parsing files that don't trigger checks."""
        # Arrange
        files = [
            GitHubFile(
                filename="assets/logo.png",  # Image file shouldn't trigger any checks
                status="modified",
                additions=5,
                deletions=2,
                changes=7
            )
        ]

        # Act
        result = parse_changed_files(files)

        # Assert
        assert len(result["checks_to_run"]) == 0


class TestBackendPatterns:
    """Test backend-specific file pattern configuration."""

    def test_backend_patterns_exist(self):
        """Test that all expected backends have patterns defined."""
        assert "python" in BACKEND_PATTERNS
        assert "java" in BACKEND_PATTERNS
        assert "typescript" in BACKEND_PATTERNS

    def test_java_patterns(self):
        """Test Java backend patterns."""
        patterns = BACKEND_PATTERNS["java"]
        assert "src/main/java/" in patterns.source_dirs
        assert ".java" in patterns.source_extensions
        assert "src/test/java/" in patterns.test_dirs
        assert "*Controller.java" in patterns.handler_patterns

    def test_typescript_patterns(self):
        """Test TypeScript backend patterns."""
        patterns = BACKEND_PATTERNS["typescript"]
        assert "src/" in patterns.source_dirs
        assert ".ts" in patterns.source_extensions
        assert "test/" in patterns.test_dirs
        assert "*.spec.ts" in patterns.test_patterns


class TestParseChangedFilesJava:
    """Test file change parsing for Java backend."""

    def test_parse_java_controller(self):
        """Test parsing Java controller changes."""
        files = [
            GitHubFile(
                filename="src/main/java/com/example/PetController.java",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        result = parse_changed_files(files, backend="java")

        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.BREAKING_CHANGES.value in result["checks_to_run"]
        assert CheckType.TEST_COVERAGE.value in result["checks_to_run"]

    def test_parse_java_new_controller(self):
        """Test parsing new Java controller."""
        files = [
            GitHubFile(
                filename="src/main/java/com/example/stats/StatisticsController.java",
                status="added",
                additions=100,
                deletions=0,
                changes=100
            )
        ]

        result = parse_changed_files(files, backend="java")

        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.TEST_COVERAGE.value in result["checks_to_run"]
        assert CheckType.ARCHITECTURAL_DUPLICATION.value in result["checks_to_run"]

    def test_parse_java_test_files_excluded(self):
        """Test that Java test files don't trigger ADR checks."""
        files = [
            GitHubFile(
                filename="src/test/java/com/example/PetControllerTests.java",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        result = parse_changed_files(files, backend="java")

        assert CheckType.ADR_COMPLIANCE.value not in result["checks_to_run"]

    def test_parse_java_model_file(self):
        """Test parsing Java model/entity changes."""
        files = [
            GitHubFile(
                filename="src/main/java/com/example/model/Pet.java",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        result = parse_changed_files(files, backend="java")

        # Models should trigger ADR compliance but not handler-specific checks
        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.LICENSE_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.TEST_COVERAGE.value not in result["checks_to_run"]


class TestParseChangedFilesTypeScript:
    """Test file change parsing for TypeScript backend."""

    def test_parse_typescript_handler(self):
        """Test parsing TypeScript handler changes."""
        files = [
            GitHubFile(
                filename="src/handlers/index.ts",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        result = parse_changed_files(files, backend="typescript")

        assert CheckType.ADR_COMPLIANCE.value in result["checks_to_run"]
        assert CheckType.BREAKING_CHANGES.value in result["checks_to_run"]

    def test_parse_typescript_spec_file_excluded(self):
        """Test that TypeScript spec files don't trigger ADR checks."""
        files = [
            GitHubFile(
                filename="src/handlers/api.spec.ts",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]

        result = parse_changed_files(files, backend="typescript")

        # Spec files are in test_dirs/test_patterns, should be excluded from ADR
        # But they're source files so license compliance still applies
        assert CheckType.LICENSE_COMPLIANCE.value in result["checks_to_run"]


class TestFetchRepoConfig:
    """Test fetching .outcomeops.yaml from GitHub."""

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_repo_config_success(self, mock_get):
        """Test successful config fetch."""
        import base64
        config_content = "code_generation:\n  backend: java\n"
        encoded = base64.b64encode(config_content.encode()).decode()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"content": encoded}
        mock_get.return_value = mock_response

        result = fetch_repo_config("owner/repo", "test-token")

        assert result is not None
        assert result["code_generation"]["backend"] == "java"

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_repo_config_not_found(self, mock_get):
        """Test config not found returns None."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = fetch_repo_config("owner/repo", "test-token")

        assert result is None

    @patch('analyze_pr_handler.requests.get')
    def test_fetch_repo_config_invalid_yaml(self, mock_get):
        """Test invalid YAML returns None."""
        import base64
        invalid_yaml = "invalid: yaml: content: ["
        encoded = base64.b64encode(invalid_yaml.encode()).decode()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"content": encoded}
        mock_get.return_value = mock_response

        result = fetch_repo_config("owner/repo", "test-token")

        assert result is None


class TestGetBackendFromConfig:
    """Test extracting backend type from config."""

    def test_get_backend_java(self):
        """Test getting java backend."""
        config = {"code_generation": {"backend": "java"}}
        result = get_backend_from_config(config)
        assert result == "java"

    def test_get_backend_typescript(self):
        """Test getting typescript backend."""
        config = {"code_generation": {"backend": "typescript"}}
        result = get_backend_from_config(config)
        assert result == "typescript"

    def test_get_backend_python_default(self):
        """Test defaulting to python when no config."""
        result = get_backend_from_config(None)
        assert result == "python"

    def test_get_backend_unknown_defaults_to_python(self):
        """Test unknown backend defaults to python."""
        config = {"code_generation": {"backend": "unknown"}}
        result = get_backend_from_config(config)
        assert result == "python"

    def test_get_backend_missing_code_generation(self):
        """Test missing code_generation section defaults to python."""
        config = {"other_section": {}}
        result = get_backend_from_config(config)
        assert result == "python"


class TestQueueCheckJobs:
    """Test queueing check jobs to SQS."""

    @patch('analyze_pr_handler.ssm_client')
    @patch('analyze_pr_handler.sqs_client')
    def test_queue_check_jobs_success(self, mock_sqs, mock_ssm):
        """Test successful job queueing."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo"}
        }
        mock_sqs.send_message.return_value = {"MessageId": "test-id"}

        checks = [CheckType.ADR_COMPLIANCE.value, CheckType.TEST_COVERAGE.value]
        changed_files = ["lambda/handler/handler.py"]

        # Act
        jobs_queued = queue_check_jobs("owner/repo", 123, checks, changed_files)

        # Assert
        assert jobs_queued == 2
        assert mock_sqs.send_message.call_count == 2

        # Verify message format
        first_call_kwargs = mock_sqs.send_message.call_args_list[0][1]
        assert "MessageBody" in first_call_kwargs
        assert "MessageGroupId" in first_call_kwargs
        assert "MessageDeduplicationId" in first_call_kwargs
        assert "pr-owner-repo-123" in first_call_kwargs["MessageGroupId"]

    @patch('analyze_pr_handler.ssm_client')
    @patch('analyze_pr_handler.sqs_client')
    def test_queue_check_jobs_sqs_error(self, mock_sqs, mock_ssm):
        """Test job queueing with SQS error."""
        # Arrange
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-east-1.amazonaws.com/123/test-queue.fifo"}
        }
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist"}}, "SendMessage"
        )

        checks = [CheckType.ADR_COMPLIANCE.value]
        changed_files = ["lambda/handler/handler.py"]

        # Act & Assert
        with pytest.raises(ClientError):
            queue_check_jobs("owner/repo", 123, checks, changed_files)


class TestHandler:
    """Test end-to-end handler orchestration."""

    @patch('analyze_pr_handler.queue_check_jobs')
    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.fetch_pr_files')
    @patch('analyze_pr_handler.fetch_repo_config')
    @patch('analyze_pr_handler.fetch_pull_request')
    @patch('analyze_pr_handler.get_github_token')
    def test_handler_success(
        self,
        mock_get_token,
        mock_fetch_pr,
        mock_fetch_config,
        mock_fetch_files,
        mock_post_comment,
        mock_queue_jobs
    ):
        """Test successful PR analysis."""
        # Arrange
        event = {
            "pr_number": 123,
            "repository": "owner/repo"
        }

        mock_get_token.return_value = "test-token"
        mock_fetch_pr.return_value = GitHubPullRequest(
            number=123,
            title="Test PR",
            html_url="https://github.com/owner/repo/pull/123",
            base={"ref": "main"},
            head={"ref": "feature"}
        )
        mock_fetch_config.return_value = None  # Defaults to python backend
        mock_fetch_files.return_value = [
            GitHubFile(
                filename="lambda/handler/handler.py",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]
        mock_post_comment.return_value = {"id": 456}
        mock_queue_jobs.return_value = 3

        # Act
        result = handler(event, None)

        # Assert
        assert result["pr_number"] == 123
        assert result["checks_queued"] == 3
        assert "Analysis started" in result["message"]

        mock_get_token.assert_called_once()
        mock_fetch_pr.assert_called_once()
        mock_fetch_config.assert_called_once()
        mock_fetch_files.assert_called_once()
        assert mock_post_comment.call_count == 1
        mock_queue_jobs.assert_called_once()

    @patch('analyze_pr_handler.log_admin_action')
    @patch('analyze_pr_handler.queue_check_jobs')
    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.fetch_pr_files')
    @patch('analyze_pr_handler.fetch_repo_config')
    @patch('analyze_pr_handler.fetch_pull_request')
    @patch('analyze_pr_handler.get_github_token')
    def test_handler_emits_pr_analyzed_audit_row(
        self,
        mock_get_token,
        mock_fetch_pr,
        mock_fetch_config,
        mock_fetch_files,
        mock_post_comment,
        mock_queue_jobs,
        mock_audit,
    ):
        """plan/audit-completeness chunk 07 - analyze-pr emits
        ACTION_PR_ANALYZED with repo / pr_number / pr_title / shas /
        backend / checks_run / duration. This is the parent event for
        the per-check Bedrock rows that process-pr-check writes."""
        event = {"pr_number": 142, "repository": "boeing/avionics"}
        mock_get_token.return_value = "tok"
        mock_fetch_pr.return_value = GitHubPullRequest(
            number=142,
            title="Bump cert chain handling",
            html_url="https://github.com/boeing/avionics/pull/142",
            base={"ref": "main", "sha": "9f8e7d"},
            head={"ref": "feature", "sha": "a1b2c3"},
        )
        mock_fetch_config.return_value = None
        mock_fetch_files.return_value = [
            GitHubFile(filename="lambda/handler/handler.py",
                       status="modified", additions=10, deletions=5, changes=15),
        ]
        mock_post_comment.return_value = {"id": 1}
        mock_queue_jobs.return_value = 3

        with patch.dict(os.environ, {"AUDIT_TABLE": "test-audit"}):
            handler(event, None)

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "PR_ANALYZED"
        d = kwargs["details"]
        assert d["repo"] == "boeing/avionics"
        assert d["pr_number"] == 142
        assert d["pr_title"] == "Bump cert chain handling"
        assert d["head_sha"] == "a1b2c3"
        assert d["base_sha"] == "9f8e7d"
        assert d["checks_queued_count"] == len(d["checks_run"])
        assert d["no_checks_needed"] is False
        assert d["duration_ms"] >= 0

    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.fetch_pr_files')
    @patch('analyze_pr_handler.fetch_repo_config')
    @patch('analyze_pr_handler.fetch_pull_request')
    @patch('analyze_pr_handler.get_github_token')
    def test_handler_no_checks_needed(
        self,
        mock_get_token,
        mock_fetch_pr,
        mock_fetch_config,
        mock_fetch_files,
        mock_post_comment
    ):
        """Test PR with no relevant changes."""
        # Arrange
        event = {
            "pr_number": 123,
            "repository": "owner/repo"
        }

        mock_get_token.return_value = "test-token"
        mock_fetch_pr.return_value = GitHubPullRequest(
            number=123,
            title="Test PR",
            html_url="https://github.com/owner/repo/pull/123",
            base={"ref": "main"},
            head={"ref": "feature"}
        )
        mock_fetch_config.return_value = None
        mock_fetch_files.return_value = [
            GitHubFile(
                filename=".gitignore",
                status="modified",
                additions=1,
                deletions=0,
                changes=1
            )
        ]
        mock_post_comment.return_value = {"id": 456}

        # Act
        result = handler(event, None)

        # Assert
        assert result["pr_number"] == 123
        assert result["checks_queued"] == 0
        assert "No checks needed" in result["message"]

    @patch('analyze_pr_handler.queue_check_jobs')
    @patch('analyze_pr_handler.post_pr_comment')
    @patch('analyze_pr_handler.fetch_pr_files')
    @patch('analyze_pr_handler.fetch_repo_config')
    @patch('analyze_pr_handler.fetch_pull_request')
    @patch('analyze_pr_handler.get_github_token')
    def test_handler_java_backend(
        self,
        mock_get_token,
        mock_fetch_pr,
        mock_fetch_config,
        mock_fetch_files,
        mock_post_comment,
        mock_queue_jobs
    ):
        """Test PR analysis with Java backend config."""
        # Arrange
        event = {
            "pr_number": 123,
            "repository": "owner/repo"
        }

        mock_get_token.return_value = "test-token"
        mock_fetch_pr.return_value = GitHubPullRequest(
            number=123,
            title="Test PR",
            html_url="https://github.com/owner/repo/pull/123",
            base={"ref": "main"},
            head={"ref": "feature"}
        )
        mock_fetch_config.return_value = {"code_generation": {"backend": "java"}}
        mock_fetch_files.return_value = [
            GitHubFile(
                filename="src/main/java/com/example/PetController.java",
                status="modified",
                additions=10,
                deletions=5,
                changes=15
            )
        ]
        mock_post_comment.return_value = {"id": 456}
        mock_queue_jobs.return_value = 4

        # Act
        result = handler(event, None)

        # Assert
        assert result["pr_number"] == 123
        assert result["checks_queued"] == 4
        mock_fetch_config.assert_called_once()

    def test_handler_invalid_request(self):
        """Test handler with invalid request."""
        # Arrange
        event = {
            "pr_number": -1,
            "repository": "invalid"
        }

        # Act & Assert
        with pytest.raises(Exception):
            handler(event, None)

    @patch('analyze_pr_handler.get_github_token')
    def test_handler_github_api_error(self, mock_get_token):
        """Test handler with GitHub API error."""
        # Arrange
        event = {
            "pr_number": 123,
            "repository": "owner/repo"
        }

        mock_get_token.side_effect = Exception("GitHub API error")

        # Act & Assert
        with pytest.raises(Exception):
            handler(event, None)


# =============================================================================
# Chunk 01 of the AI PR Analyzer dispatch entry-point plan: widen
# analyze-pr for provider="azure_devops"
# =============================================================================


class TestAnalyzePrRequestProviderAwareness:
    """AnalyzePrRequest accepts optional provider + ADO context."""

    def test_defaults_to_github_when_provider_unset(self):
        request = AnalyzePrRequest(pr_number=1, repository="owner/repo")
        assert request.provider == "github"
        assert request.connection_id is None
        assert request.ado_org is None

    def test_ado_shape_accepted(self):
        request = AnalyzePrRequest(
            pr_number=42,
            repository="example-org/example-project/example-repo",
            provider="azure_devops",
            connection_id="conn-xyz",
            ado_org="example-org",
            ado_project="example-project",
            ado_repo="example-repo",
        )
        assert request.provider == "azure_devops"
        assert request.connection_id == "conn-xyz"
        assert request.ado_org == "example-org"

    def test_ado_repository_shape_rejects_two_segments(self):
        # GitHub-shape slug on an ADO provider should fail validation.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AnalyzePrRequest(
                pr_number=1,
                repository="owner/repo",           # only 1 slash
                provider="azure_devops",
                connection_id="c",
                ado_org="o", ado_project="p", ado_repo="r",
            )

    def test_github_repository_shape_rejects_three_segments(self):
        # ADO-shape slug on a github provider should fail validation.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AnalyzePrRequest(
                pr_number=1,
                repository="org/project/repo",     # 2 slashes
                provider="github",
            )

    def test_ado_without_connection_id_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AnalyzePrRequest(
                pr_number=1,
                repository="o/p/r",
                provider="azure_devops",
                ado_org="o", ado_project="p", ado_repo="r",
                # missing connection_id
            )

    def test_unknown_provider_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AnalyzePrRequest(
                pr_number=1, repository="o/r",
                provider="bitbucket",
            )


class TestCheckJobShapeParity:
    """analyze-pr.CheckJob must be JSON round-trip compatible with
    process-pr-check.CheckJob. If either drifts, ADO PR-checks break
    silently at SQS-consume time."""

    def test_check_job_carries_ado_fields(self):
        job = CheckJob(
            check_type=CheckType.ADR_COMPLIANCE,
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changed_files=["src/x.py"],
            provider="azure_devops",
            connection_id="conn-xyz",
            azure_devops_slug="example-org/example-project/example-repo",
            azure_devops_org="example-org",
            azure_devops_project="example-project",
            azure_devops_repo="example-repo",
        )
        assert job.provider == "azure_devops"
        assert job.connection_id == "conn-xyz"
        assert job.azure_devops_org == "example-org"

    def test_github_check_job_still_validates_without_ado_fields(self):
        # Legacy shape from before the widening: no provider, no ADO
        # fields. Must continue to validate cleanly.
        job = CheckJob(
            check_type=CheckType.ADR_COMPLIANCE,
            pr_number=1,
            repository="o/r",
            changed_files=[],
        )
        assert job.provider == "github"
        assert job.connection_id is None

    def test_json_wire_round_trip_preserves_ado_fields(self):
        # Instantiate analyze-pr CheckJob → serialize → deserialize back
        # into analyze-pr CheckJob (same class). If the serialization uses
        # any keys the class can't re-read from, this fails.
        analyze_pr_job = CheckJob(
            check_type=CheckType.ADR_COMPLIANCE,
            pr_number=42,
            repository="example-org/example-project/example-repo",
            changed_files=["src/x.py"],
            provider="azure_devops",
            connection_id="conn-xyz",
            azure_devops_slug="example-org/example-project/example-repo",
            azure_devops_org="example-org",
            azure_devops_project="example-project",
            azure_devops_repo="example-repo",
        )
        wire_dict = json.loads(analyze_pr_job.model_dump_json())
        # Round-trip.
        round_trip = CheckJob(**wire_dict)
        assert round_trip.provider == "azure_devops"
        assert round_trip.azure_devops_org == "example-org"
        assert round_trip.check_type == CheckType.ADR_COMPLIANCE

    def test_shape_parity_with_process_pr_check_source(self):
        # Regression guard: read process-pr-check.CheckJob's field
        # names + aliases from source and confirm analyze-pr.CheckJob
        # matches. If either drifts, ADO SQS wire breaks silently at
        # consume time -- this test catches it at build time.
        pp_source_path = os.path.join(
            os.path.dirname(__file__),
            '../../process-pr-check/handler.py',
        )
        with open(pp_source_path) as f:
            source = f.read()
        # Confirm all analyze-pr.CheckJob ADO fields are declared in
        # process-pr-check.CheckJob source (same names + aliases).
        for expected in [
            'provider: Literal["github", "azure_devops"]',
            'connection_id: Optional[str]',
            'azure_devops_slug',
            'azure_devops_org',
            'azure_devops_project',
            'azure_devops_repo',
            'alias="slug"',
            'alias="org"',
            'alias="project"',
            'alias="repo"',
            'populate_by_name = True',
        ]:
            assert expected in source, (
                f"process-pr-check.CheckJob missing {expected!r}; "
                f"analyze-pr.CheckJob would drift on SQS wire"
            )


class TestQueueCheckJobsWithAdo:
    """queue_check_jobs embeds ADO context in CheckJob when provider is
    azure_devops."""

    @patch('analyze_pr_handler.sqs_client')
    @patch('analyze_pr_handler.ssm_client')
    def test_ado_context_embedded_in_queued_message(self, mock_ssm, mock_sqs):
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-west-2.amazonaws.com/x/q"},
        }
        mock_sqs.send_message.return_value = {"MessageId": "abc"}

        queued = queue_check_jobs(
            repository="example-org/example-project/example-repo",
            pr_number=42,
            checks=["ADR_COMPLIANCE"],
            changed_files=["src/x.py"],
            provider="azure_devops",
            connection_id="conn-xyz",
            ado_org="example-org",
            ado_project="example-project",
            ado_repo="example-repo",
        )
        assert queued == 1
        body = mock_sqs.send_message.call_args.kwargs["MessageBody"]
        payload = json.loads(body)
        assert payload["provider"] == "azure_devops"
        assert payload["connection_id"] == "conn-xyz"
        assert payload["azure_devops_org"] == "example-org"
        assert payload["azure_devops_project"] == "example-project"
        assert payload["azure_devops_repo"] == "example-repo"
        assert payload["azure_devops_slug"] == "example-org/example-project/example-repo"

    @patch('analyze_pr_handler.sqs_client')
    @patch('analyze_pr_handler.ssm_client')
    def test_github_context_unchanged_by_ado_addition(self, mock_ssm, mock_sqs):
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-west-2.amazonaws.com/x/q"},
        }
        mock_sqs.send_message.return_value = {"MessageId": "abc"}

        queued = queue_check_jobs(
            repository="owner/repo",
            pr_number=1,
            checks=["ADR_COMPLIANCE"],
            changed_files=[],
        )
        assert queued == 1
        payload = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert payload["provider"] == "github"
        assert payload["connection_id"] is None
        assert payload["azure_devops_org"] is None


class TestHandlerAzureDevOpsDispatch:
    """handler() dispatches to _handle_azure_devops when
    provider=azure_devops."""

    @patch('analyze_pr_handler._handle_azure_devops')
    def test_ado_event_routes_to_ado_branch(self, mock_ado_handler):
        mock_ado_handler.return_value = {
            "message": "Analysis started",
            "pr_number": 42,
            "checks_queued": 6,
        }
        event = {
            "pr_number": 42,
            "repository": "example-org/example-project/example-repo",
            "provider": "azure_devops",
            "connection_id": "conn-xyz",
            "ado_org": "example-org",
            "ado_project": "example-project",
            "ado_repo": "example-repo",
        }
        result = handler(event, None)
        mock_ado_handler.assert_called_once()
        assert result["checks_queued"] == 6

    @patch('analyze_pr_handler.get_github_token')
    @patch('analyze_pr_handler.fetch_pull_request')
    def test_github_event_unchanged_by_ado_addition(
        self, mock_fetch_pr, mock_token,
    ):
        # Confirm the GitHub path still runs the original code (get_github_token
        # + fetch_pull_request are called), not the ADO branch.
        mock_token.return_value = "ghp_xxx"
        mock_fetch_pr.side_effect = Exception("stop here")

        event = {"pr_number": 1, "repository": "owner/repo"}
        with pytest.raises(Exception, match="stop here"):
            handler(event, None)
        mock_token.assert_called_once()


class TestHandleAzureDevOpsEndToEnd:
    """_handle_azure_devops runs the full ADO flow with mocked
    primitives -- refresh token, fetch PR meta, fetch config,
    parse files, post comment, queue jobs."""

    def _ado_request(self):
        return AnalyzePrRequest(
            pr_number=42,
            repository="example-org/example-project/example-repo",
            provider="azure_devops",
            connection_id="conn-xyz",
            ado_org="example-org",
            ado_project="example-project",
            ado_repo="example-repo",
        )

    @patch('analyze_pr_handler.queue_check_jobs', return_value=6)
    @patch('analyze_pr_handler._upsert_ado_comment')
    @patch('analyze_pr_handler._refresh_ado_access_token', return_value="ado-tok")
    def test_ado_e2e_queues_check_jobs(
        self, _mock_refresh, _mock_post, mock_queue,
    ):
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
                   return_value=b"code_generation:\n  backend: python\n"):
            result = handler_module._handle_azure_devops(self._ado_request(), 0.0)

        assert result["checks_queued"] == 6
        # queue_check_jobs must have been called with ADO context.
        _, kwargs = mock_queue.call_args
        assert kwargs["provider"] == "azure_devops"
        assert kwargs["connection_id"] == "conn-xyz"
        assert kwargs["ado_org"] == "example-org"

    @patch('analyze_pr_handler.queue_check_jobs', return_value=0)
    @patch('analyze_pr_handler._upsert_ado_comment')
    @patch('analyze_pr_handler._refresh_ado_access_token', return_value="ado-tok")
    def test_ado_e2e_no_relevant_changes_short_circuits(
        self, _mock_refresh, mock_post, mock_queue,
    ):
        # PR with no changed files at all: post "no checks needed"
        # comment, skip queue_check_jobs, return checks_queued=0.
        from shared.pr_metadata import PRMetadata

        fake_pr = PRMetadata(
            head_sha="h", base_sha="b", base_branch="main",
            changed_files=[],
            author="u", title="t",
        )
        with patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   return_value=b"code_generation:\n  backend: python\n"):
            result = handler_module._handle_azure_devops(self._ado_request(), 0.0)

        assert result["checks_queued"] == 0
        mock_queue.assert_not_called()
        mock_post.assert_called_once()

    @patch('analyze_pr_handler.queue_check_jobs', return_value=6)
    @patch('analyze_pr_handler._upsert_ado_comment')
    @patch('analyze_pr_handler._refresh_ado_access_token', return_value="ado-tok")
    def test_ado_e2e_missing_config_still_runs(
        self, _mock_refresh, _mock_post, mock_queue,
    ):
        # If .outcomeops.yaml is not present at the base branch,
        # SourceFetchError is caught and backend defaults kick in
        # via get_backend_from_config(None).
        from shared.pr_metadata import PRMetadata
        from shared.source_fetch import SourceFetchError

        fake_pr = PRMetadata(
            head_sha="h", base_sha="b", base_branch="main",
            changed_files=["lambda/hello/handler.py"],
            author="u", title="t",
        )
        with patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   side_effect=SourceFetchError("404")):
            result = handler_module._handle_azure_devops(self._ado_request(), 0.0)

        assert result["checks_queued"] == 6

    @patch('analyze_pr_handler.queue_check_jobs', return_value=5)
    @patch('analyze_pr_handler._upsert_ado_comment')
    @patch('analyze_pr_handler._refresh_ado_access_token', return_value="ado-tok")
    def test_ado_e2e_new_file_does_not_queue_breaking_changes(
        self, _mock_refresh, _mock_post, mock_queue,
    ):
        # Bug fix: prior to shared.pr_metadata surfacing ADO's changeType,
        # every ADO file was force-labelled "modified" and
        # BREAKING_CHANGES ran on newly-added handler files. GitHub had
        # it right (added files are skipped). This asserts the wrapper
        # now honors file_change_types so the two providers agree.
        from shared.pr_metadata import PRMetadata

        fake_pr = PRMetadata(
            head_sha="h", base_sha="b", base_branch="main",
            changed_files=["lambda/probe-health/handler.py"],
            author="u", title="t",
            file_change_types={"lambda/probe-health/handler.py": "added"},
        )
        with patch("shared.pr_metadata.fetch_pr_metadata", return_value=fake_pr), \
             patch("shared.source_fetch.fetch_file_at_sha",
                   return_value=b"code_generation:\n  backend: python\n"):
            handler_module._handle_azure_devops(self._ado_request(), 0.0)

        _, kwargs = mock_queue.call_args
        checks_queued = kwargs.get("checks") or (
            mock_queue.call_args.args[2] if len(mock_queue.call_args.args) >= 3 else []
        )
        assert "BREAKING_CHANGES" not in checks_queued, (
            "Newly-added file must not trigger BREAKING_CHANGES on ADO "
            "(parity with GitHub: an added file has no prior version "
            "that could have been broken)."
        )

    def test_refresh_ado_access_token_missing_env_vars(self, monkeypatch):
        # Sanity: refresh helper raises if OAuth app not configured.
        monkeypatch.delenv("AZURE_DEVOPS_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_DEVOPS_CLIENT_ID", raising=False)
        with pytest.raises(RuntimeError, match="OAuth app not configured"):
            handler_module._refresh_ado_access_token("conn-xyz")

    def test_refresh_ado_access_token_missing_connection_id(self):
        with pytest.raises(ValueError):
            handler_module._refresh_ado_access_token("")
