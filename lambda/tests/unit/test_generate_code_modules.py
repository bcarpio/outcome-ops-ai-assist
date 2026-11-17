
# === Legacy dict -> unified Response adapter (ADR-035 chunk 11 fallout) ===
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

"""Unit tests for generate-code support modules.

Tests cover:
- github_api.py: Authentication, branch ops, file ops, PR ops
- models.py: Pydantic models, validators, properties
- repo_config.py: YAML parsing, config loading, defaults
- sqs_client.py: Queue URL retrieval, message sending
- utils.py: Cost calculation, text formatting, webhook verification, URL extraction
- bedrock_client.py: Claude invocation, JSON extraction, retry logic

All modules are loaded via importlib.util.spec_from_file_location
to ensure Sonar coverage maps to the exact source paths.
"""

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
import urllib.error
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest
from botocore.exceptions import ClientError


# In the full test suite, test_check_handlers_init.py replaces
# botocore.exceptions.ClientError with Exception. Detect this so we can
# adapt tests that rely on ClientError having .response attribute.
_CLIENT_ERROR_IS_MOCKED = (ClientError is Exception)


def _make_client_error(error_response, operation_name):
    """Create an error compatible with bedrock_client's exception handling.

    When ClientError is replaced with Exception in the full suite, real
    ClientError objects lose their .response attribute. This helper creates
    errors that always have .response so the code under test works correctly.
    """
    if _CLIENT_ERROR_IS_MOCKED:
        err = Exception(f"{operation_name}: {error_response}")
        err.response = error_response
        return err
    return ClientError(error_response, operation_name)


# ============================================================================
# Module loading helpers
# ============================================================================

GENERATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../generate-code")
)

# Add generate-code directory to sys.path so intra-module imports work
if GENERATE_CODE_DIR not in sys.path:
    sys.path.insert(0, GENERATE_CODE_DIR)

# Also add the lambda root for shared.audit (needed by bedrock_client)
LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, LAMBDA_ROOT)


def _load_module(module_name: str, filename: str):
    """Load a module from generate-code directory by file path."""
    if module_name in sys.modules:
        del sys.modules[module_name]
    file_path = os.path.join(GENERATE_CODE_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Load models first (dependency for other modules)
# ---------------------------------------------------------------------------
models_module = _load_module("models", "models.py")

GitHubLabel = models_module.GitHubLabel
GitHubIssue = models_module.GitHubIssue
GitHubRepository = models_module.GitHubRepository
GitHubWebhookEvent = models_module.GitHubWebhookEvent
APIGatewayProxyEvent = models_module.APIGatewayProxyEvent
TokenUsage = models_module.TokenUsage
PlanStep = models_module.PlanStep
ExecutionPlan = models_module.ExecutionPlan
StepExecutionMessage = models_module.StepExecutionMessage
PRCommandMessage = models_module.PRCommandMessage
SQSRecord = models_module.SQSRecord
SQSEvent = models_module.SQSEvent
BedrockUsage = models_module.BedrockUsage
ClaudeResponse = models_module.ClaudeResponse
GeneratedFile = models_module.GeneratedFile
GeneratedFilesResponse = models_module.GeneratedFilesResponse

# ---------------------------------------------------------------------------
# Load utils (depends on models)
# ---------------------------------------------------------------------------
utils_module = _load_module("utils", "utils.py")

calculate_cost = utils_module.calculate_cost
to_kebab_case = utils_module.to_kebab_case
generate_branch_name = utils_module.generate_branch_name
verify_webhook_signature = utils_module.verify_webhook_signature
extract_pr_number_from_url = utils_module.extract_pr_number_from_url

# ---------------------------------------------------------------------------
# Load github_api (uses boto3 at module level)
# ---------------------------------------------------------------------------
github_api_module = _load_module("github_api", "github_api.py")

get_webhook_secret = github_api_module.get_webhook_secret
get_github_token = github_api_module.get_github_token
get_headers = github_api_module.get_headers
create_branch = github_api_module.create_branch
get_branch_head_sha = github_api_module.get_branch_head_sha
get_file = github_api_module.get_file
commit_file = github_api_module.commit_file
update_file = github_api_module.update_file
create_pull_request = github_api_module.create_pull_request
post_pr_comment = github_api_module.post_pr_comment
get_pr_comments = github_api_module.get_pr_comments
get_pr_changed_files = github_api_module.get_pr_changed_files
get_pr_diff = github_api_module.get_pr_diff
get_pr_info = github_api_module.get_pr_info

# ---------------------------------------------------------------------------
# Load repo_config
# ---------------------------------------------------------------------------
repo_config_module = _load_module("repo_config", "repo_config.py")

RepoConfig = repo_config_module.RepoConfig
CodeGenerationConfig = repo_config_module.CodeGenerationConfig
TestingConfig = repo_config_module.TestingConfig
InfrastructureConfig = repo_config_module.InfrastructureConfig
RequiredAdrsConfig = repo_config_module.RequiredAdrsConfig
IngestionConfig = repo_config_module.IngestionConfig
SymbolGraphConfig = repo_config_module.SymbolGraphConfig
parse_repo_config = repo_config_module.parse_repo_config
load_repo_config_from_github = repo_config_module.load_repo_config_from_github
get_default_config = repo_config_module.get_default_config

# ---------------------------------------------------------------------------
# Load sqs_client (uses boto3 at module level)
# ---------------------------------------------------------------------------
sqs_client_module = _load_module("sqs_client", "sqs_client.py")

get_queue_url = sqs_client_module.get_queue_url
send_step_message = sqs_client_module.send_step_message

# ---------------------------------------------------------------------------
# Load bedrock_client (uses boto3 at module level, imports shared.audit)
# ---------------------------------------------------------------------------
bedrock_client_module = _load_module("bedrock_client", "bedrock_client.py")

invoke_claude = bedrock_client_module.invoke_claude
extract_json_from_response = bedrock_client_module.extract_json_from_response
_extract_balanced_json = bedrock_client_module._extract_balanced_json


# ############################################################################
# GITHUB_API.PY TESTS
# ############################################################################


class TestGetWebhookSecret:
    """Test GitHub webhook secret retrieval from SSM."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset module-level clients before each test."""
        yield

    @patch("github_api.ssm_client")
    def test_get_webhook_secret_success(self, mock_ssm):
        """Test successful webhook secret retrieval."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "my-secret-123"}
        }
        result = get_webhook_secret()
        assert result == "my-secret-123"
        mock_ssm.get_parameter.assert_called_once()

    @patch("github_api.ssm_client")
    def test_get_webhook_secret_empty_value(self, mock_ssm):
        """Test exception when SSM returns empty value."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": ""}}
        with pytest.raises(Exception, match="Webhook secret not found"):
            get_webhook_secret()

    @patch("github_api.ssm_client")
    def test_get_webhook_secret_missing_value(self, mock_ssm):
        """Test exception when SSM returns no Value key."""
        mock_ssm.get_parameter.return_value = {"Parameter": {}}
        with pytest.raises(Exception, match="Webhook secret not found"):
            get_webhook_secret()

    @patch("github_api.ssm_client")
    def test_get_webhook_secret_client_error(self, mock_ssm):
        """Test ClientError is re-raised."""
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
            "GetParameter",
        )
        with pytest.raises(ClientError):
            get_webhook_secret()


class TestGetGitHubToken:
    """Test GitHub token retrieval from SSM."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.ssm_client")
    def test_get_github_token_success(self, mock_ssm):
        """Test successful token retrieval."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ghp_test_token_123"}
        }
        result = get_github_token()
        assert result == "ghp_test_token_123"

    @patch("github_api.ssm_client")
    def test_get_github_token_empty_raises(self, mock_ssm):
        """Test exception when token is empty."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": ""}}
        with pytest.raises(Exception, match="GitHub token not found"):
            get_github_token()

    @patch("github_api.ssm_client")
    def test_get_github_token_client_error(self, mock_ssm):
        """Test ClientError propagation."""
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "GetParameter",
        )
        with pytest.raises(ClientError):
            get_github_token()


class TestGetHeaders:
    """Test GitHub API header construction."""

    def test_get_headers_includes_bearer_token(self):
        """Test headers include Bearer authorization."""
        headers = get_headers("my-token")
        assert headers["Authorization"] == "Bearer my-token"

    def test_get_headers_includes_accept(self):
        """Test headers include GitHub JSON accept header."""
        headers = get_headers("tok")
        assert headers["Accept"] == "application/vnd.github+json"

    def test_get_headers_includes_api_version(self):
        """Test headers include API version."""
        headers = get_headers("tok")
        assert "X-GitHub-Api-Version" in headers


class TestCreateBranch:
    """Test GitHub branch creation."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.post")
    @patch("github_api.requests.get")
    def test_create_branch_success(self, mock_get, mock_post):
        """Test successful branch creation."""
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"object": {"sha": "abc123"}}),
            raise_for_status=Mock(),
        )
        mock_post.return_value = Mock(status_code=201)

        result = create_branch("owner/repo", "feature-branch", "main", "token")
        assert result["success"] is True
        assert result["branch_name"] == "feature-branch"
        assert result["sha"] == "abc123"

    @patch("github_api.requests.post")
    @patch("github_api.requests.get")
    def test_create_branch_already_exists(self, mock_get, mock_post):
        """Test branch creation when branch already exists."""
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"object": {"sha": "abc123"}}),
            raise_for_status=Mock(),
        )
        mock_post.return_value = Mock(
            status_code=422,
            json=Mock(return_value={"message": "Reference already exists"}),
            raise_for_status=Mock(),
        )

        result = create_branch("owner/repo", "existing-branch", "main", "token")
        assert result["success"] is True
        assert result.get("already_exists") is True

    @patch("github_api.requests.get")
    def test_create_branch_api_error(self, mock_get):
        """Test branch creation with API error raises exception."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("timeout")

        with pytest.raises(Exception, match="GitHub API error"):
            create_branch("owner/repo", "branch", "main", "token")


class TestGetBranchHeadSha:
    """Test branch HEAD SHA retrieval."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.get")
    def test_get_branch_head_sha_success(self, mock_get):
        """Test successful SHA retrieval."""
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"object": {"sha": "deadbeef123"}}),
            raise_for_status=Mock(),
        )
        result = get_branch_head_sha("owner/repo", "main", "token")
        assert result == "deadbeef123"

    @patch("github_api.requests.get")
    def test_get_branch_head_sha_failure_returns_none(self, mock_get):
        """Test failure returns None."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("fail")
        result = get_branch_head_sha("owner/repo", "main", "token")
        assert result is None


class TestGetFile:
    """Test GitHub file retrieval."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.get")
    def test_get_file_success(self, mock_get):
        """Test successful file retrieval."""
        file_data = {"content": base64.b64encode(b"hello").decode(), "sha": "abc"}
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(return_value=file_data),
            raise_for_status=Mock(),
        )
        result = get_file("owner/repo", "README.md", "main", "token")
        assert result["sha"] == "abc"

    @patch("github_api.requests.get")
    def test_get_file_not_found_returns_none(self, mock_get):
        """Test 404 returns None."""
        mock_get.return_value = Mock(status_code=404)
        result = get_file("owner/repo", "missing.txt", "main", "token")
        assert result is None

    @patch("github_api.requests.get")
    def test_get_file_error_returns_none(self, mock_get):
        """Test request error returns None."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("fail")
        result = get_file("owner/repo", "file.py", "main", "token")
        assert result is None


class TestCommitFile:
    """Test file creation via GitHub API."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.put")
    def test_commit_file_success(self, mock_put):
        """Test successful file commit."""
        mock_put.return_value = Mock(status_code=201, raise_for_status=Mock())

        result = commit_file(
            "owner/repo", "src/main.py", "print('hello')",
            "feature", "Add main.py", "token"
        )
        assert result["success"] is True
        assert result["file_path"] == "src/main.py"

        # Verify base64-encoded content was sent
        call_kwargs = mock_put.call_args[1]
        sent_content = call_kwargs["json"]["content"]
        decoded = base64.b64decode(sent_content).decode("utf-8")
        assert decoded == "print('hello')"

    @patch("github_api.requests.put")
    def test_commit_file_api_error(self, mock_put):
        """Test commit failure raises exception."""
        import requests as req_lib
        mock_put.side_effect = req_lib.exceptions.HTTPError("403")

        with pytest.raises(Exception, match="GitHub API error"):
            commit_file("owner/repo", "f.py", "x", "branch", "msg", "token")


class TestUpdateFile:
    """Test file update via GitHub API."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.put")
    @patch("github_api.get_file")
    def test_update_file_success(self, mock_get_file, mock_put):
        """Test successful file update."""
        mock_get_file.return_value = {"sha": "old-sha-123", "content": "old"}
        mock_put.return_value = Mock(status_code=200, raise_for_status=Mock())

        result = update_file(
            "owner/repo", "src/main.py", "updated content",
            "feature", "Update main.py", "token"
        )
        assert result["success"] is True

        # Verify SHA was included in update payload
        call_kwargs = mock_put.call_args[1]
        assert call_kwargs["json"]["sha"] == "old-sha-123"

    @patch("github_api.get_file")
    def test_update_file_not_found_raises(self, mock_get_file):
        """Test update of non-existent file raises exception."""
        mock_get_file.return_value = None

        with pytest.raises(Exception, match="File not found"):
            update_file("owner/repo", "nope.py", "x", "b", "msg", "token")


class TestCreatePullRequest:
    """Test pull request creation."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.post")
    def test_create_pull_request_success(self, mock_post):
        """Test successful PR creation."""
        mock_post.return_value = Mock(
            status_code=201,
            json=Mock(return_value={
                "html_url": "https://github.com/owner/repo/pull/42",
                "number": 42,
            }),
            raise_for_status=Mock(),
        )

        result = create_pull_request(
            "owner/repo", "Add feature", "Description",
            "feature", "main", "token"
        )
        assert result["success"] is True
        assert result["html_url"] == "https://github.com/owner/repo/pull/42"
        assert result["number"] == 42

    @patch("github_api.requests.post")
    def test_create_pull_request_error(self, mock_post):
        """Test PR creation failure raises exception."""
        import requests as req_lib
        mock_post.side_effect = req_lib.exceptions.HTTPError("422")

        with pytest.raises(Exception, match="GitHub API error"):
            create_pull_request("o/r", "title", "body", "h", "b", "tok")


class TestPostPrComment:
    """Test posting comments on pull requests."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.post")
    def test_post_pr_comment_success(self, mock_post):
        """Test successful comment posting."""
        mock_post.return_value = Mock(status_code=201, raise_for_status=Mock())

        result = post_pr_comment("owner/repo", 42, "LGTM!", "token")
        assert result["success"] is True

    @patch("github_api.requests.post")
    def test_post_pr_comment_error(self, mock_post):
        """Test comment posting failure raises exception."""
        import requests as req_lib
        mock_post.side_effect = req_lib.exceptions.HTTPError("500")

        with pytest.raises(Exception, match="GitHub API error"):
            post_pr_comment("o/r", 1, "fail", "tok")


class TestGetPrComments:
    """Test PR comment retrieval."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.get")
    def test_get_pr_comments_success(self, mock_get):
        """Test successful comment retrieval."""
        comments = [{"body": "Nice!", "user": {"login": "dev"}}]
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(return_value=comments),
            raise_for_status=Mock(),
        )
        result = get_pr_comments("owner/repo", 1, "token")
        assert len(result) == 1
        assert result[0]["body"] == "Nice!"

    @patch("github_api.requests.get")
    def test_get_pr_comments_error_returns_empty(self, mock_get):
        """Test error returns empty list."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("fail")
        result = get_pr_comments("o/r", 1, "tok")
        assert result == []


class TestGetPrChangedFiles:
    """Test PR changed files retrieval."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.get")
    def test_get_pr_changed_files_success(self, mock_get):
        """Test successful file list retrieval."""
        files = [{"filename": "src/a.py"}, {"filename": "src/b.py"}]
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(return_value=files),
            raise_for_status=Mock(),
        )
        result = get_pr_changed_files("owner/repo", 5, "token")
        assert result == ["src/a.py", "src/b.py"]

    @patch("github_api.requests.get")
    def test_get_pr_changed_files_error_returns_empty(self, mock_get):
        """Test error returns empty list."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("fail")
        result = get_pr_changed_files("o/r", 5, "tok")
        assert result == []


class TestGetPrDiff:
    """Test PR diff retrieval."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.get")
    def test_get_pr_diff_success(self, mock_get):
        """Test successful diff retrieval."""
        mock_get.return_value = Mock(
            status_code=200,
            text="diff --git a/file.py b/file.py\n+added line",
            raise_for_status=Mock(),
        )
        result = get_pr_diff("owner/repo", 10, "token")
        assert "diff --git" in result

    @patch("github_api.requests.get")
    def test_get_pr_diff_error_returns_empty(self, mock_get):
        """Test error returns empty string."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("fail")
        result = get_pr_diff("o/r", 10, "tok")
        assert result == ""


class TestGetPrInfo:
    """Test PR info retrieval."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("github_api.requests.get")
    def test_get_pr_info_success(self, mock_get):
        """Test successful PR info retrieval."""
        mock_get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={
                "head": {"ref": "feature-branch"},
                "base": {"ref": "main"},
                "title": "Add feature",
                "html_url": "https://github.com/o/r/pull/3",
            }),
            raise_for_status=Mock(),
        )
        result = get_pr_info("owner/repo", 3, "token")
        assert result["head_branch"] == "feature-branch"
        assert result["base_branch"] == "main"
        assert result["title"] == "Add feature"

    @patch("github_api.requests.get")
    def test_get_pr_info_error_returns_none(self, mock_get):
        """Test error returns None."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError("fail")
        result = get_pr_info("o/r", 3, "tok")
        assert result is None


# ############################################################################
# MODELS.PY TESTS
# ############################################################################


class TestGitHubLabel:
    """Test GitHubLabel model."""

    def test_create_label(self):
        """Test basic label creation."""
        label = GitHubLabel(name="bug", color="ff0000")
        assert label.name == "bug"
        assert label.color == "ff0000"


class TestGitHubIssue:
    """Test GitHubIssue model."""

    def test_create_issue_with_body(self):
        """Test issue creation with body."""
        issue = GitHubIssue(
            number=42, title="Fix bug", body="Details here",
            html_url="https://github.com/o/r/issues/42", state="open"
        )
        assert issue.number == 42
        assert issue.body == "Details here"

    def test_create_issue_without_body(self):
        """Test issue creation without body (optional)."""
        issue = GitHubIssue(
            number=1, title="T", html_url="https://url", state="open"
        )
        assert issue.body is None


class TestGitHubRepository:
    """Test GitHubRepository model."""

    def test_owner_name_property(self):
        """Test owner_name extracts login."""
        repo = GitHubRepository(
            name="repo", full_name="owner/repo",
            owner={"login": "owner", "id": 1}
        )
        assert repo.owner_name == "owner"

    def test_default_branch_defaults_to_main(self):
        """Test default_branch defaults to main."""
        repo = GitHubRepository(
            name="r", full_name="o/r", owner={"login": "o"}
        )
        assert repo.default_branch == "main"

    def test_owner_name_missing_login(self):
        """Test owner_name returns empty string when login missing."""
        repo = GitHubRepository(
            name="r", full_name="o/r", owner={"id": 1}
        )
        assert repo.owner_name == ""


class TestGitHubWebhookEvent:
    """Test GitHubWebhookEvent model with validators."""

    def _make_event_data(self, action="labeled", label_name="approved-for-generation"):
        return {
            "action": action,
            "label": {"name": label_name, "color": "0e8a16"},
            "issue": {
                "number": 1, "title": "Test",
                "html_url": "https://github.com/o/r/issues/1",
                "state": "open",
            },
            "repository": {
                "name": "repo", "full_name": "owner/repo",
                "owner": {"login": "owner"},
            },
        }

    def test_valid_event_approved_for_generation(self):
        """Test valid event with approved-for-generation label."""
        event = GitHubWebhookEvent(**self._make_event_data())
        assert event.action == "labeled"
        assert event.label.name == "approved-for-generation"
        assert event.is_plan_only is False

    def test_valid_event_approved_for_plan(self):
        """Test valid event with approved-for-plan label."""
        event = GitHubWebhookEvent(
            **self._make_event_data(label_name="approved-for-plan")
        )
        assert event.is_plan_only is True

    def test_invalid_action_raises(self):
        """Test unsupported action raises ValueError."""
        with pytest.raises(Exception):
            GitHubWebhookEvent(**self._make_event_data(action="unlabeled"))

    def test_unsupported_label_raises(self):
        """Test unsupported label name raises ValueError."""
        with pytest.raises(Exception):
            GitHubWebhookEvent(**self._make_event_data(label_name="bug"))


class TestAPIGatewayProxyEvent:
    """Test APIGatewayProxyEvent model."""

    def test_parsed_body(self):
        """Test parsed_body property parses JSON."""
        event = APIGatewayProxyEvent(
            body='{"key": "value"}',
            headers={"content-type": "application/json"},
            requestContext={"requestId": "abc"},
        )
        assert event.parsed_body == {"key": "value"}


class TestTokenUsage:
    """Test TokenUsage model."""

    def test_default_values(self):
        """Test defaults are all zero."""
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_cost_usd == 0.0

    def test_custom_values(self):
        """Test custom values."""
        usage = TokenUsage(input_tokens=100, output_tokens=200, total_cost_usd=0.005)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 200


class TestPlanStep:
    """Test PlanStep model."""

    def test_default_status_is_pending(self):
        """Test default step status is pending."""
        step = PlanStep(step_number=1, title="Do it", description="Description")
        assert step.status == "pending"
        assert step.files_to_create == []
        assert step.kb_queries == []
        assert step.error is None

    def test_step_with_all_fields(self):
        """Test step with all fields populated."""
        step = PlanStep(
            step_number=2,
            title="Create handler",
            description="Build the handler",
            files_to_create=["handler.py"],
            kb_queries=["lambda handler patterns"],
            status="completed",
            completed_at="2025-01-01T00:00:00",
            cost=TokenUsage(input_tokens=50, output_tokens=100, total_cost_usd=0.001),
        )
        assert step.status == "completed"
        assert len(step.files_to_create) == 1


class TestExecutionPlan:
    """Test ExecutionPlan model."""

    def test_minimal_plan(self):
        """Test minimal execution plan creation."""
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="1-test",
            repo_full_name="owner/repo",
        )
        assert plan.steps == []
        assert plan.backend_name == "python"
        assert plan.created_at is not None

    def test_plan_with_steps(self):
        """Test plan with steps."""
        step = PlanStep(step_number=1, title="S1", description="D1")
        plan = ExecutionPlan(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            branch_name="1-test",
            repo_full_name="owner/repo",
            steps=[step],
        )
        assert len(plan.steps) == 1


class TestStepExecutionMessage:
    """Test StepExecutionMessage model."""

    def test_defaults(self):
        """Test default values."""
        msg = StepExecutionMessage(
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="o/r",
            branch_name="1-test",
        )
        assert msg.action == "execute_step"
        assert msg.current_step == 0
        assert msg.base_branch == "main"
        assert msg.plan_only is False
        assert msg.customer_id is None

    def test_generate_plan_action(self):
        """Test generate_plan action."""
        msg = StepExecutionMessage(
            action="generate_plan",
            issue_number=1,
            issue_title="Test",
            issue_description="Desc",
            repo_full_name="o/r",
            branch_name="1-test",
        )
        assert msg.action == "generate_plan"


class TestPRCommandMessage:
    """Test PRCommandMessage model."""

    def test_fix_readme_message(self):
        """Test fix_readme command message."""
        msg = PRCommandMessage(
            action="fix_readme",
            pr_number=42,
            repo_full_name="o/r",
            branch_name="feature",
            commenter="dev",
        )
        assert msg.action == "fix_readme"
        assert msg.guidance is None

    def test_message_with_guidance(self):
        """Test command message with guidance text."""
        msg = PRCommandMessage(
            action="fix_tests",
            pr_number=10,
            repo_full_name="o/r",
            branch_name="b",
            commenter="dev",
            guidance="Use pytest-mock instead of unittest.mock",
        )
        assert msg.guidance == "Use pytest-mock instead of unittest.mock"


class TestSQSModels:
    """Test SQS event/record models."""

    def test_sqs_record_parsed_body(self):
        """Test SQSRecord parsed_body parses JSON."""
        record = SQSRecord(
            body='{"action": "execute_step"}',
            messageId="msg-1",
            receiptHandle="handle-1",
        )
        assert record.parsed_body["action"] == "execute_step"

    def test_sqs_event_with_records(self):
        """Test SQSEvent with multiple records."""
        event = SQSEvent(
            Records=[
                SQSRecord(body="{}", messageId="1", receiptHandle="h1"),
                SQSRecord(body="{}", messageId="2", receiptHandle="h2"),
            ]
        )
        assert len(event.Records) == 2


class TestBedrockModels:
    """Test Bedrock response models."""

    def test_bedrock_usage(self):
        """Test BedrockUsage model."""
        usage = BedrockUsage(inputTokens=1000, outputTokens=500)
        assert usage.inputTokens == 1000
        assert usage.outputTokens == 500

    def test_claude_response(self):
        """Test ClaudeResponse model."""
        resp = ClaudeResponse(
            text="Hello world",
            usage=BedrockUsage(inputTokens=10, outputTokens=5),
            stop_reason="end_turn",
        )
        assert resp.text == "Hello world"
        assert resp.stop_reason == "end_turn"


class TestGeneratedFile:
    """Test GeneratedFile model."""

    def test_decoded_content_from_plain_text(self):
        """Test decoded_content from plain text field."""
        gf = GeneratedFile(path="main.py", content="print('hello')")
        assert gf.decoded_content == "print('hello')"

    def test_decoded_content_from_base64(self):
        """Test decoded_content from base64 field."""
        encoded = base64.b64encode(b"import os").decode()
        gf = GeneratedFile(path="main.py", contentBase64=encoded)
        assert gf.decoded_content == "import os"

    def test_decoded_content_base64_takes_priority(self):
        """Test base64 content is preferred over plain text."""
        encoded = base64.b64encode(b"base64_content").decode()
        gf = GeneratedFile(path="f.py", content="plain_content", contentBase64=encoded)
        assert gf.decoded_content == "base64_content"

    def test_decoded_content_no_content_raises(self):
        """Test ValueError when no content is provided."""
        gf = GeneratedFile(path="empty.py")
        with pytest.raises(ValueError, match="No content provided"):
            gf.decoded_content

    def test_decoded_content_latin1_fallback(self):
        """Test latin-1 fallback for non-UTF-8 bytes."""
        # Latin-1 byte that is not valid UTF-8: 0xe9 (e-acute)
        raw_bytes = b"\xe9"
        encoded = base64.b64encode(raw_bytes).decode()
        gf = GeneratedFile(path="f.txt", contentBase64=encoded)
        assert gf.decoded_content == "\xe9"


class TestGeneratedFilesResponse:
    """Test GeneratedFilesResponse model."""

    def test_response_with_files(self):
        """Test response containing generated files."""
        resp = GeneratedFilesResponse(
            files=[
                GeneratedFile(path="a.py", content="code_a"),
                GeneratedFile(path="b.py", content="code_b"),
            ]
        )
        assert len(resp.files) == 2


# ############################################################################
# REPO_CONFIG.PY TESTS
# ############################################################################


class TestRepoConfigDefaults:
    """Test default configuration values."""

    def test_default_config(self):
        """Test get_default_config returns sensible defaults."""
        config = get_default_config()
        assert config.version == "1.0"
        assert config.code_generation.backend == "python"
        assert config.testing.framework == "pytest"
        assert config.testing.min_coverage == 80
        assert config.infrastructure.tool == "terraform"
        assert config.project_context is None

    def test_default_ingestion_paths(self):
        """Test default ingestion paths."""
        config = get_default_config()
        assert config.ingestion.standards_paths == ["docs/adr"]
        assert config.ingestion.docs_paths == ["docs"]


class TestParseRepoConfig:
    """Test YAML configuration parsing."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    def test_parse_minimal_yaml(self):
        """Test parsing minimal YAML returns defaults."""
        config = parse_repo_config("version: '1.0'")
        assert config.version == "1.0"
        assert config.code_generation.backend == "python"

    def test_parse_empty_yaml(self):
        """Test parsing empty YAML returns defaults."""
        config = parse_repo_config("")
        assert config.code_generation.backend == "python"

    def test_parse_full_yaml(self):
        """Test parsing full configuration."""
        yaml_content = """
version: "1.0"
code_generation:
  backend: java
  additional_kb_queries:
    - "Spring Boot patterns"
  custom_guidelines: "Use our internal logger"
  exclude_patterns:
    - "*.generated.java"
testing:
  framework: junit
  test_directory: src/test/java
  min_coverage: 90
infrastructure:
  tool: cloudformation
  directory: infra
required_adrs:
  repo:
    - ADR-001
  global:
    - ADR-010
ingestion:
  standards_paths:
    - "docs/standards"
  docs_paths:
    - "docs"
    - "wiki"
project_context: "Java microservices platform"
"""
        config = parse_repo_config(yaml_content)
        assert config.code_generation.backend == "java"
        assert config.code_generation.additional_kb_queries == ["Spring Boot patterns"]
        assert config.code_generation.custom_guidelines == "Use our internal logger"
        assert config.testing.framework == "junit"
        assert config.testing.min_coverage == 90
        assert config.infrastructure.tool == "cloudformation"
        assert config.required_adrs.repo == ["ADR-001"]
        assert config.required_adrs.global_adrs == ["ADR-010"]
        assert config.ingestion.standards_paths == ["docs/standards"]
        assert config.project_context == "Java microservices platform"

    def test_parse_invalid_yaml_raises(self):
        """Test invalid YAML raises ValueError."""
        with pytest.raises(ValueError, match="Invalid YAML"):
            parse_repo_config("{{invalid: yaml: [")

    def test_parse_yaml_with_extra_fields(self):
        """Test YAML with unknown fields does not crash."""
        # Pydantic v1 ignores extra fields by default
        yaml_content = """
version: "1.0"
unknown_field: "should be ignored"
"""
        # This should not raise
        config = parse_repo_config(yaml_content)
        assert config.version == "1.0"


class TestLoadRepoConfigFromGithub:
    """Test loading config from GitHub API."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("urllib.request.urlopen")
    def test_load_config_success(self, mock_urlopen):
        """Test successful config loading from GitHub."""
        yaml_content = "version: '2.0'\ncode_generation:\n  backend: typescript\n"
        encoded = base64.b64encode(yaml_content.encode()).decode()
        response_data = json.dumps({"content": encoded}).encode("utf-8")

        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = load_repo_config_from_github("owner/repo", "main", "token")
        assert config is not None
        assert config.code_generation.backend == "typescript"

    @patch("urllib.request.urlopen")
    def test_load_config_404_returns_none(self, mock_urlopen):
        """Test 404 returns None (no config file)."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/repos/o/r/contents/.outcomeops.yaml",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b"")),
        )

        result = load_repo_config_from_github("o/r", "main", "token")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_load_config_500_returns_none(self, mock_urlopen):
        """Test 500 error returns None gracefully."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.github.com/repos/o/r/contents/.outcomeops.yaml",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b"")),
        )

        result = load_repo_config_from_github("o/r", "main", "token")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_load_config_generic_exception_returns_none(self, mock_urlopen):
        """Test generic exception returns None gracefully."""
        mock_urlopen.side_effect = Exception("Network timeout")

        result = load_repo_config_from_github("o/r", "main", "token")
        assert result is None


class TestConfigModels:
    """Test individual config sub-models."""

    def test_code_generation_config_defaults(self):
        """Test CodeGenerationConfig defaults."""
        cfg = CodeGenerationConfig()
        assert cfg.backend == "python"
        assert cfg.additional_kb_queries == []
        assert cfg.custom_guidelines is None
        assert cfg.exclude_patterns == []

    def test_testing_config_defaults(self):
        """Test TestingConfig defaults."""
        cfg = TestingConfig()
        assert cfg.framework == "pytest"
        assert cfg.test_directory == "tests/unit"
        assert cfg.min_coverage == 80

    def test_infrastructure_config_defaults(self):
        """Test InfrastructureConfig defaults."""
        cfg = InfrastructureConfig()
        assert cfg.tool == "terraform"
        assert cfg.directory == "terraform"

    def test_required_adrs_config_defaults(self):
        """Test RequiredAdrsConfig defaults."""
        cfg = RequiredAdrsConfig()
        assert cfg.repo == []
        assert cfg.global_adrs == []

    def test_ingestion_config_defaults(self):
        """Test IngestionConfig defaults."""
        cfg = IngestionConfig()
        assert cfg.standards_paths == ["docs/adr"]
        assert cfg.docs_paths == ["docs"]

    def test_ingestion_config_has_symbol_graph_default(self):
        """IngestionConfig nests a default SymbolGraphConfig."""
        cfg = IngestionConfig()
        assert isinstance(cfg.symbol_graph, SymbolGraphConfig)
        assert cfg.symbol_graph.enabled is True


class TestSymbolGraphConfig:
    """Pydantic schema for ``ingestion.symbol_graph``."""

    def test_defaults(self):
        cfg = SymbolGraphConfig()
        assert cfg.enabled is True
        assert cfg.include == []
        assert cfg.exclude == []
        assert cfg.max_file_bytes == 524288
        assert cfg.languages is None

    def test_round_trip_yaml(self):
        yaml_content = """
version: "1.0"
ingestion:
  symbol_graph:
    enabled: false
    include:
      - "lambda/**/*.py"
    exclude:
      - "**/generated/**"
    max_file_bytes: 1024
    languages:
      - python
      - typescript
"""
        cfg = parse_repo_config(yaml_content)
        sg = cfg.ingestion.symbol_graph
        assert sg.enabled is False
        assert sg.include == ["lambda/**/*.py"]
        assert sg.exclude == ["**/generated/**"]
        assert sg.max_file_bytes == 1024
        assert sg.languages == ["python", "typescript"]

    def test_negative_max_file_bytes_rejected(self):
        yaml_content = """
ingestion:
  symbol_graph:
    max_file_bytes: -1
"""
        with pytest.raises(ValueError):
            parse_repo_config(yaml_content)

    def test_unknown_language_rejected(self):
        yaml_content = """
ingestion:
  symbol_graph:
    languages:
      - python
      - cobol
"""
        with pytest.raises(ValueError):
            parse_repo_config(yaml_content)

    def test_known_languages_accepted(self):
        # Mirrors the set in lambda/shared/repo_config.py.
        for lang in ("python", "typescript", "java", "go", "csharp", "abap"):
            cfg = SymbolGraphConfig(languages=[lang])
            assert cfg.languages == [lang]


# ############################################################################
# SQS_CLIENT.PY TESTS
# ############################################################################


class TestGetQueueUrl:
    """Test SQS queue URL retrieval from SSM."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("sqs_client.ssm_client")
    def test_get_queue_url_success(self, mock_ssm):
        """Test successful queue URL retrieval."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "https://sqs.us-east-1.amazonaws.com/123/queue.fifo"}
        }
        result = get_queue_url()
        assert result == "https://sqs.us-east-1.amazonaws.com/123/queue.fifo"

    @patch("sqs_client.ssm_client")
    def test_get_queue_url_empty_value_raises(self, mock_ssm):
        """Test exception when queue URL is empty."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": ""}}
        with pytest.raises(Exception, match="Queue URL not found"):
            get_queue_url()

    @patch("sqs_client.ssm_client")
    def test_get_queue_url_missing_value_raises(self, mock_ssm):
        """Test exception when Value key is missing."""
        mock_ssm.get_parameter.return_value = {"Parameter": {}}
        with pytest.raises(Exception, match="Queue URL not found"):
            get_queue_url()

    @patch("sqs_client.ssm_client")
    def test_get_queue_url_client_error(self, mock_ssm):
        """Test ClientError propagation."""
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "gone"}},
            "GetParameter",
        )
        with pytest.raises(ClientError):
            get_queue_url()


class TestSendStepMessage:
    """Test sending step execution messages to SQS."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("sqs_client.sqs_client")
    @patch("sqs_client.get_queue_url")
    def test_send_step_message_success(self, mock_get_url, mock_sqs):
        """Test successful message sending."""
        mock_get_url.return_value = "https://sqs.us-east-1.amazonaws.com/123/q.fifo"
        mock_sqs.send_message.return_value = {"MessageId": "msg-abc-123"}

        msg = StepExecutionMessage(
            issue_number=42,
            issue_title="Test Issue",
            issue_description="Description",
            repo_full_name="owner/repo",
            branch_name="42-test-issue",
            current_step=1,
            total_steps=3,
        )

        result = send_step_message(msg)
        assert result["success"] is True
        assert result["message_id"] == "msg-abc-123"

        # Verify FIFO params were set
        call_kwargs = mock_sqs.send_message.call_args[1]
        assert "MessageGroupId" in call_kwargs
        assert "MessageDeduplicationId" in call_kwargs
        assert "code-generation-42" in call_kwargs["MessageGroupId"]

    @patch("sqs_client.sqs_client")
    @patch("sqs_client.get_queue_url")
    def test_send_step_message_sqs_error(self, mock_get_url, mock_sqs):
        """Test SQS error raises exception."""
        mock_get_url.return_value = "https://sqs.url/q.fifo"
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "InvalidParameter", "Message": "bad"}},
            "SendMessage",
        )

        msg = StepExecutionMessage(
            issue_number=1,
            issue_title="T",
            issue_description="D",
            repo_full_name="o/r",
            branch_name="1-t",
        )

        with pytest.raises(Exception, match="SQS error"):
            send_step_message(msg)

    @patch("sqs_client.sqs_client")
    @patch("sqs_client.get_queue_url")
    def test_send_step_message_deduplication_id_format(self, mock_get_url, mock_sqs):
        """Test deduplication ID is a SHA256 hash truncated to 128 chars."""
        mock_get_url.return_value = "https://sqs.url/q.fifo"
        mock_sqs.send_message.return_value = {"MessageId": "id-1"}

        msg = StepExecutionMessage(
            issue_number=99,
            issue_title="Long Title",
            issue_description="Desc",
            repo_full_name="o/r",
            branch_name="99-long-title",
        )

        send_step_message(msg)

        call_kwargs = mock_sqs.send_message.call_args[1]
        dedup_id = call_kwargs["MessageDeduplicationId"]
        # SHA256 hex is 64 chars, but code does [:128], so it should be 64
        assert len(dedup_id) <= 128
        assert len(dedup_id) == 64  # SHA256 hex digest length


# ############################################################################
# UTILS.PY TESTS
# ############################################################################


class TestCalculateCost:
    """Test token cost calculation."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    def test_calculate_cost_sonnet_default(self):
        """Test Sonnet cost calculation (default model)."""
        usage = calculate_cost(1_000_000, 1_000_000)
        # Input: $3/1M = $3.00, Output: $15/1M = $15.00, Total: $18.00
        assert usage.input_tokens == 1_000_000
        assert usage.output_tokens == 1_000_000
        assert usage.total_cost_usd == 18.0

    def test_calculate_cost_haiku(self):
        """Test Haiku cost calculation."""
        usage = calculate_cost(1_000_000, 1_000_000, model="haiku")
        # Input: $0.80/1M = $0.80, Output: $4/1M = $4.00, Total: $4.80
        assert usage.total_cost_usd == 4.8

    def test_calculate_cost_zero_tokens(self):
        """Test zero token cost."""
        usage = calculate_cost(0, 0)
        assert usage.total_cost_usd == 0.0

    def test_calculate_cost_small_tokens(self):
        """Test small token count cost calculation."""
        usage = calculate_cost(1000, 500, model="sonnet")
        # Input: (1000/1M) * 3 = 0.003, Output: (500/1M) * 15 = 0.0075
        expected = round(0.003 + 0.0075, 6)
        assert usage.total_cost_usd == expected

    def test_calculate_cost_returns_token_usage(self):
        """Test return type is TokenUsage."""
        usage = calculate_cost(100, 200)
        assert isinstance(usage, TokenUsage)


class TestToKebabCase:
    """Test text to kebab-case conversion."""

    def test_simple_text(self):
        """Test simple text conversion."""
        assert to_kebab_case("Add User Authentication") == "add-user-authentication"

    def test_special_characters(self):
        """Test special characters are replaced with hyphens."""
        assert to_kebab_case("Fix: Bug #123!") == "fix-bug-123"

    def test_leading_trailing_hyphens_stripped(self):
        """Test leading/trailing hyphens are removed."""
        assert to_kebab_case("  Hello World  ") == "hello-world"

    def test_max_length_50(self):
        """Test output is truncated to 50 characters."""
        long_text = "a" * 100
        result = to_kebab_case(long_text)
        assert len(result) <= 50

    def test_empty_string(self):
        """Test empty string input."""
        assert to_kebab_case("") == ""

    def test_already_kebab(self):
        """Test already kebab-case text passes through."""
        assert to_kebab_case("already-kebab-case") == "already-kebab-case"

    def test_uppercase_converted(self):
        """Test uppercase is converted to lowercase."""
        assert to_kebab_case("UPPERCASE") == "uppercase"


class TestGenerateBranchName:
    """Test branch name generation."""

    def test_basic_branch_name(self):
        """Test basic branch name generation."""
        result = generate_branch_name(42, "Add User Feature")
        assert result == "42-add-user-feature"

    def test_branch_name_with_special_chars(self):
        """Test branch name strips special characters."""
        result = generate_branch_name(7, "Fix: API endpoint (v2)")
        assert result == "7-fix-api-endpoint-v2"


class TestVerifyWebhookSignature:
    """Test GitHub webhook signature verification."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    def _compute_signature(self, payload: str, secret: str) -> str:
        """Compute expected HMAC-SHA256 signature."""
        sig = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"sha256={sig}"

    def test_valid_signature(self):
        """Test valid signature returns True."""
        payload = '{"action": "labeled"}'
        secret = "webhook-secret"
        signature = self._compute_signature(payload, secret)

        assert verify_webhook_signature(payload, signature, secret) is True

    def test_invalid_signature(self):
        """Test invalid signature returns False."""
        result = verify_webhook_signature(
            '{"action": "labeled"}', "sha256=invalid_hex", "secret"
        )
        assert result is False

    def test_empty_signature_header(self):
        """Test empty signature header returns False."""
        assert verify_webhook_signature("payload", "", "secret") is False

    def test_none_signature_header(self):
        """Test None signature header returns False."""
        assert verify_webhook_signature("payload", None, "secret") is False

    def test_wrong_prefix(self):
        """Test signature without sha256= prefix returns False."""
        assert verify_webhook_signature("payload", "md5=abc", "secret") is False


class TestExtractPrNumberFromUrl:
    """Test PR number extraction from GitHub URL."""

    def test_valid_url(self):
        """Test extracting PR number from valid URL."""
        result = extract_pr_number_from_url(
            "https://github.com/owner/repo/pull/123"
        )
        assert result == 123

    def test_url_with_extra_path(self):
        """Test URL with extra path segments."""
        result = extract_pr_number_from_url(
            "https://github.com/owner/repo/pull/456/files"
        )
        assert result == 456

    def test_invalid_url_returns_none(self):
        """Test invalid URL returns None."""
        assert extract_pr_number_from_url("https://github.com/owner/repo") is None

    def test_empty_string_returns_none(self):
        """Test empty string returns None."""
        assert extract_pr_number_from_url("") is None

    def test_issues_url_returns_none(self):
        """Test issues URL (not /pull/) returns None."""
        assert extract_pr_number_from_url(
            "https://github.com/owner/repo/issues/99"
        ) is None


# ############################################################################
# BEDROCK_CLIENT.PY TESTS
# ############################################################################


class TestInvokeClaude:
    """Test Claude invocation via Bedrock."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_success(self, mock_invoke, mock_audit):
        """Test successful Claude invocation."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": "Hello from Claude"}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "stopReason": "end_turn",
        })

        result = invoke_claude("Tell me about Python")

        assert isinstance(result, ClaudeResponse)
        assert result.text == "Hello from Claude"
        assert result.usage.inputTokens == 100
        assert result.usage.outputTokens == 50
        assert result.stop_reason == "end_turn"

    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_forwards_workspace_id_to_audit(
        self, mock_invoke, mock_audit
    ):
        """Code-gen Bedrock spend must attribute to the owning workspace in
        the audit row, not WS#SYSTEM."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {"message": {"content": [{"text": "code"}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "stopReason": "end_turn",
        })

        invoke_claude("gen code", workspace_id="ws_eng")

        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["workspace_id"] == "ws_eng"

    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_defaults_empty_workspace_id(
        self, mock_invoke, mock_audit
    ):
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {"message": {"content": [{"text": "code"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        })

        invoke_claude("gen code")

        assert mock_audit.call_args.kwargs["workspace_id"] == ""

    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_with_system_prompt(self, mock_invoke, mock_audit):
        """Test invocation with system prompt."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {"message": {"content": [{"text": "response"}]}},
            "usage": {"inputTokens": 50, "outputTokens": 25},
            "stopReason": "end_turn",
        })

        invoke_claude("prompt", system_prompt="You are a helpful assistant")

        # Shared client receives system as a plain string (NOT the
        # Converse-shaped [{"text": ...}] block list). The Converse
        # backend's translator wraps it into blocks; consumers don't
        # need to.
        call_kwargs = mock_invoke.call_args[1]
        assert call_kwargs["system"] == "You are a helpful assistant"

    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_without_system_prompt(self, mock_invoke, mock_audit):
        """Test invocation without system prompt omits system key."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {"message": {"content": [{"text": "response"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        })

        invoke_claude("prompt")

        # bedrock_client passes system=None when no system_prompt is set.
        # The shared client's translator drops None at the boundary.
        call_kwargs = mock_invoke.call_args[1]
        assert call_kwargs.get("system") is None

    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_custom_temperature_and_tokens(self, mock_invoke, mock_audit):
        """Test custom temperature and max_tokens are passed."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {"message": {"content": [{"text": "creative"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        })

        invoke_claude("be creative", temperature=0.9, max_tokens=8000)

        # Shared client receives temperature / max_tokens as top-level
        # kwargs (NOT Converse's nested inferenceConfig).
        call_kwargs = mock_invoke.call_args[1]
        assert call_kwargs["temperature"] == 0.9
        assert call_kwargs["max_tokens"] == 8000

    @pytest.mark.skipif(
        _CLIENT_ERROR_IS_MOCKED,
        reason="When ClientError=Exception, except ClientError catches internal exceptions"
    )
    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_empty_content_raises(self, mock_invoke, mock_audit):
        """Test empty content blocks raise exception."""
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {"message": {"content": []}},
            "usage": {"inputTokens": 10, "outputTokens": 0},
            "stopReason": "end_turn",
        })

        with pytest.raises(Exception, match="No text in"):
            invoke_claude("prompt")

    @patch("bedrock_client.time.sleep")
    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_throttle_retry_success(self, mock_invoke, mock_audit, mock_sleep):
        """Test throttling triggers retry and eventually succeeds."""
        throttle_error = _make_client_error(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Converse",
        )
        success_response = _LegacyDictResponse({
            "output": {"message": {"content": [{"text": "success after retry"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        })

        mock_invoke.side_effect = [throttle_error, success_response]

        result = invoke_claude("prompt")
        assert result.text == "success after retry"
        assert mock_sleep.call_count == 1
        # First retry: BASE_BACKOFF_SECONDS * (2^0) = 5
        mock_sleep.assert_called_with(5)

    @patch("bedrock_client.time.sleep")
    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_throttle_all_retries_exhausted(self, mock_invoke, mock_audit, mock_sleep):
        """Test all retries exhausted raises exception."""
        throttle_error = _make_client_error(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Converse",
        )
        mock_invoke.side_effect = [throttle_error] * 8

        with pytest.raises(Exception, match=r"Failed to invoke .* after"):
            invoke_claude("prompt")

    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_non_throttle_error_raises_immediately(self, mock_invoke, mock_audit):
        """Test non-throttle ClientError raises immediately without retry."""
        error = _make_client_error(
            {"Error": {"Code": "ValidationException", "Message": "bad request"}},
            "Converse",
        )
        mock_invoke.side_effect = error

        with pytest.raises(Exception):
            invoke_claude("prompt")

        # Should NOT retry for non-throttle errors
        assert mock_invoke.call_count == 1

    @pytest.mark.skipif(
        _CLIENT_ERROR_IS_MOCKED,
        reason="When ClientError=Exception, except ClientError catches all exceptions"
    )
    @patch("bedrock_client.log_audit_entry")
    @patch("bedrock_client.invoke_model")
    def test_invoke_claude_unexpected_error_raises(self, mock_invoke, mock_audit):
        """Test unexpected exception raises immediately."""
        mock_invoke.side_effect = RuntimeError("unexpected")

        with pytest.raises(RuntimeError, match="unexpected"):
            invoke_claude("prompt")


class TestExtractJsonFromResponse:
    """Test JSON extraction from Claude responses."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        yield

    def test_extract_files_json(self):
        """Test extracting files JSON from response."""
        response = '{"files": [{"path": "main.py", "content": "code"}]}'
        result = extract_json_from_response(response)
        assert "files" in result
        assert result["files"][0]["path"] == "main.py"

    def test_extract_steps_json(self):
        """Test extracting steps JSON from response."""
        response = '{"steps": [{"title": "Step 1"}]}'
        result = extract_json_from_response(response)
        assert "steps" in result

    def test_extract_json_with_markdown_fences(self):
        """Test extracting JSON surrounded by markdown code fences."""
        response = "Here is the plan:\n```json\n{\"steps\": [{\"title\": \"S1\"}]}\n```"
        result = extract_json_from_response(response)
        assert "steps" in result

    def test_extract_json_with_preamble(self):
        """Test extracting JSON with text before it."""
        response = "I generated the following:\n{\"files\": []}"
        result = extract_json_from_response(response)
        assert result == {"files": []}

    def test_no_json_raises(self):
        """Test response without JSON raises ValueError."""
        with pytest.raises(ValueError, match="No JSON found"):
            extract_json_from_response("No JSON here at all")

    def test_extract_nested_json(self):
        """Test extracting JSON with nested objects."""
        data = {
            "files": [
                {"path": "a.py", "content": "def foo():\n    return {\"key\": \"val\"}"}
            ]
        }
        response = json.dumps(data)
        result = extract_json_from_response(response)
        assert result["files"][0]["path"] == "a.py"

    def test_extract_json_with_control_characters(self):
        """Test JSON with control characters gets sanitized."""
        # JSON with an embedded control character (BEL = 0x07) inside a string
        response = '{"files": [{"path": "f.py", "content": "hello\\u0007world"}]}'
        result = extract_json_from_response(response)
        assert "files" in result

    def test_fallback_to_first_brace(self):
        """Test fallback to first brace when no known pattern found."""
        response = '{"custom_key": "value"}'
        result = extract_json_from_response(response)
        assert result["custom_key"] == "value"


class TestExtractBalancedJson:
    """Test balanced JSON extraction helper."""

    def test_simple_object(self):
        """Test extracting a simple JSON object."""
        result = _extract_balanced_json('{"key": "val"}', 0)
        assert result == '{"key": "val"}'

    def test_nested_objects(self):
        """Test extracting nested JSON objects."""
        text = '{"outer": {"inner": "val"}}'
        result = _extract_balanced_json(text, 0)
        assert result == text

    def test_with_offset(self):
        """Test extracting from non-zero offset."""
        text = 'prefix {"key": "val"} suffix'
        result = _extract_balanced_json(text, 7)
        assert result == '{"key": "val"}'

    def test_with_strings_containing_braces(self):
        """Test strings containing braces don't break counting."""
        text = '{"content": "function() { return {}; }"}'
        result = _extract_balanced_json(text, 0)
        assert result == text

    def test_unbalanced_returns_empty(self):
        """Test unbalanced braces return empty string."""
        result = _extract_balanced_json('{"key": "val"', 0)
        assert result == ""

    def test_not_a_brace_at_start_returns_empty(self):
        """Test non-brace at start position returns empty."""
        result = _extract_balanced_json('abc', 0)
        assert result == ""

    def test_escaped_quotes_in_strings(self):
        """Test escaped quotes inside strings are handled."""
        text = '{"key": "value with \\"quotes\\""}'
        result = _extract_balanced_json(text, 0)
        assert result == text

    def test_start_pos_beyond_text(self):
        """Test start position beyond text length returns empty."""
        result = _extract_balanced_json("short", 100)
        assert result == ""

    def test_deeply_nested(self):
        """Test deeply nested JSON objects."""
        text = '{"a": {"b": {"c": {"d": "val"}}}}'
        result = _extract_balanced_json(text, 0)
        assert result == text


class TestResolveOwningWorkspace:
    """Repo -> owning-workspace resolution for code-gen audit attribution.
    Fail-soft: never raises, so attribution can't break code generation."""

    def test_returns_first_sorted_workspace(self):
        with patch(
            "shared.pr_check_scope.find_workspaces_for_repo",
            return_value=["ws_zeta", "ws_alpha"],
        ):
            assert (
                utils_module.resolve_owning_workspace("owner/repo") == "ws_alpha"
            )

    def test_empty_repo_returns_empty(self):
        assert utils_module.resolve_owning_workspace("") == ""

    def test_no_workspaces_returns_empty(self):
        with patch(
            "shared.pr_check_scope.find_workspaces_for_repo",
            return_value=[],
        ):
            assert utils_module.resolve_owning_workspace("owner/repo") == ""

    def test_resolution_error_is_failsoft(self):
        """A scan/IAM failure must return '' (row stays WS#SYSTEM), not raise."""
        with patch(
            "shared.pr_check_scope.find_workspaces_for_repo",
            side_effect=RuntimeError("AccessDenied"),
        ):
            assert utils_module.resolve_owning_workspace("owner/repo") == ""
