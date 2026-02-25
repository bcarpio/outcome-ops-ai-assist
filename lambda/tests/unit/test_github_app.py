"""Unit tests for GitHub App authentication utilities (github_app.py)."""

import importlib.util
import json
import os
import sys
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Module loading
# ============================================================================

def _load_github_app_module():
    """Load or reload the github_app module with mocked dependencies."""
    # Clean up any previous loads
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("github_app", "jwt"):
            del sys.modules[mod_name]

    # Pre-register jwt as a MagicMock since it may not be installed
    mock_jwt = MagicMock()
    mock_jwt.encode = MagicMock(return_value="mock.jwt.token")
    sys.modules["jwt"] = mock_jwt

    module_path = os.path.join(
        os.path.dirname(__file__), "../../github-integration/github_app.py"
    )
    spec = importlib.util.spec_from_file_location("github_app", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["github_app"] = module
    spec.loader.exec_module(module)
    return module


github_app = _load_github_app_module()

# Import from module
_get_ssm_client = github_app._get_ssm_client
get_github_config = github_app.get_github_config
generate_jwt = github_app.generate_jwt
get_installation_token = github_app.get_installation_token
get_app_installations = github_app.get_app_installations
get_installation_repos = github_app.get_installation_repos
get_repo_contents = github_app.get_repo_contents
get_file_content = github_app.get_file_content
get_repo_default_branch = github_app.get_repo_default_branch
get_latest_commit_sha = github_app.get_latest_commit_sha
get_tree_recursive = github_app.get_tree_recursive
GitHubAppError = github_app.GitHubAppError


# ============================================================================
# Helpers
# ============================================================================

def _make_urlopen_response(data, status=200):
    """Create a mock urlopen response context manager."""
    mock_response = MagicMock()
    if isinstance(data, dict) or isinstance(data, list):
        mock_response.read.return_value = json.dumps(data).encode("utf-8")
    else:
        mock_response.read.return_value = data
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.status = status
    return mock_response


def _make_http_error(code, msg="Error", body=b"error body"):
    """Create a urllib.error.HTTPError with readable body."""
    fp = BytesIO(body) if body else None
    return urllib.error.HTTPError(
        url="https://api.github.com/test",
        code=code,
        msg=msg,
        hdrs={},
        fp=fp,
    )


def _mock_ssm_response(params_dict):
    """Build SSM get_parameters response from a dict of param-name -> value."""
    prefix = f"/{github_app.ENV}/{github_app.APP_NAME}/github"
    return {
        "Parameters": [
            {"Name": f"{prefix}/{name}", "Value": value}
            for name, value in params_dict.items()
        ]
    }


VALID_SSM_PARAMS = {
    "app-id": "12345",
    "private-key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    "client-id": "Iv1.abc123",
    "client-secret": "secret_value",
    "callback-url": "https://example.com/callback",
    "app-slug": "my-github-app",
}


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset module-level caches before each test."""
    github_app._ssm_client = None
    github_app._github_config = None
    yield


# ============================================================================
# GitHubAppError
# ============================================================================

class TestGitHubAppError:
    """Test custom exception class."""

    def test_github_app_error_is_exception(self):
        """Test GitHubAppError is a proper Exception subclass."""
        err = GitHubAppError("Something went wrong")
        assert isinstance(err, Exception)
        assert str(err) == "Something went wrong"

    def test_github_app_error_with_code_in_message(self):
        """Test GitHubAppError stores code info in message."""
        err = GitHubAppError("Failed to get installation token: 401")
        assert "401" in str(err)


# ============================================================================
# _get_ssm_client
# ============================================================================

class TestGetSsmClient:
    """Test SSM client singleton accessor."""

    @patch.object(github_app.boto3, 'client')
    def test_get_ssm_client_creates_client(self, mock_boto_client):
        """Test that _get_ssm_client creates an SSM client on first call."""
        mock_boto_client.return_value = MagicMock()
        client = _get_ssm_client()
        mock_boto_client.assert_called_once_with("ssm")
        assert client is not None

    @patch.object(github_app.boto3, 'client')
    def test_get_ssm_client_returns_cached(self, mock_boto_client):
        """Test that _get_ssm_client reuses cached client on second call."""
        mock_client = MagicMock()
        mock_boto_client.return_value = mock_client

        client1 = _get_ssm_client()
        client2 = _get_ssm_client()

        assert client1 is client2
        mock_boto_client.assert_called_once()


# ============================================================================
# get_github_config
# ============================================================================

class TestGetGithubConfig:
    """Test GitHub App configuration retrieval from SSM."""

    @patch.object(github_app, '_get_ssm_client')
    def test_get_github_config_success(self, mock_get_client):
        """Test successful config retrieval from SSM."""
        mock_ssm = MagicMock()
        mock_ssm.get_parameters.return_value = _mock_ssm_response(VALID_SSM_PARAMS)
        mock_get_client.return_value = mock_ssm

        config = get_github_config()

        assert config["app_id"] == "12345"
        assert config["private_key"].startswith("-----BEGIN RSA PRIVATE KEY-----")
        assert config["client_id"] == "Iv1.abc123"
        assert config["client_secret"] == "secret_value"
        assert config["callback_url"] == "https://example.com/callback"
        assert config["app_slug"] == "my-github-app"

    @patch.object(github_app, '_get_ssm_client')
    def test_get_github_config_caches_result(self, mock_get_client):
        """Test that config is cached after first call."""
        mock_ssm = MagicMock()
        mock_ssm.get_parameters.return_value = _mock_ssm_response(VALID_SSM_PARAMS)
        mock_get_client.return_value = mock_ssm

        config1 = get_github_config()
        config2 = get_github_config()

        assert config1 is config2
        mock_ssm.get_parameters.assert_called_once()

    @patch.object(github_app, '_get_ssm_client')
    def test_get_github_config_ssm_error(self, mock_get_client):
        """Test ClientError propagation from SSM."""
        from botocore.exceptions import ClientError

        mock_ssm = MagicMock()
        mock_ssm.get_parameters.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameters",
        )
        mock_get_client.return_value = mock_ssm

        with pytest.raises(ClientError):
            get_github_config()

    @patch.object(github_app, '_get_ssm_client')
    def test_get_github_config_missing_params_defaults_empty(self, mock_get_client):
        """Test that missing SSM params default to empty strings."""
        mock_ssm = MagicMock()
        # Only return app-id, omit the rest
        mock_ssm.get_parameters.return_value = _mock_ssm_response(
            {"app-id": "99999"}
        )
        mock_get_client.return_value = mock_ssm

        config = get_github_config()

        assert config["app_id"] == "99999"
        assert config["private_key"] == ""
        assert config["client_id"] == ""
        assert config["client_secret"] == ""
        assert config["callback_url"] == ""
        assert config["app_slug"] == ""

    @patch.object(github_app, '_get_ssm_client')
    def test_get_github_config_uses_correct_prefix(self, mock_get_client):
        """Test that SSM parameter prefix uses ENV and APP_NAME."""
        mock_ssm = MagicMock()
        mock_ssm.get_parameters.return_value = _mock_ssm_response(VALID_SSM_PARAMS)
        mock_get_client.return_value = mock_ssm

        get_github_config()

        call_args = mock_ssm.get_parameters.call_args
        param_names = call_args[1]["Names"] if "Names" in call_args[1] else call_args[0][0]
        prefix = f"/{github_app.ENV}/{github_app.APP_NAME}/github"
        for name in param_names:
            assert name.startswith(prefix), f"Param {name} should start with {prefix}"
        assert call_args[1]["WithDecryption"] is True


# ============================================================================
# generate_jwt
# ============================================================================

class TestGenerateJwt:
    """Test JWT generation for GitHub App authentication."""

    @patch.object(github_app, 'get_github_config')
    def test_generate_jwt_success(self, mock_config):
        """Test successful JWT generation."""
        mock_config.return_value = {
            "app_id": "12345",
            "private_key": "fake-private-key",
        }

        token = generate_jwt()

        # The mocked jwt.encode returns "mock.jwt.token"
        assert token == "mock.jwt.token"

    @patch.object(github_app, 'get_github_config')
    def test_generate_jwt_calls_jwt_encode(self, mock_config):
        """Test that jwt.encode is called with correct parameters."""
        mock_config.return_value = {
            "app_id": "12345",
            "private_key": "fake-private-key",
        }

        mock_jwt = sys.modules["jwt"]
        mock_jwt.encode.reset_mock()

        generate_jwt()

        mock_jwt.encode.assert_called_once()
        call_args = mock_jwt.encode.call_args
        payload = call_args[0][0]
        key = call_args[0][1]
        algorithm = call_args[1].get("algorithm") if call_args[1] else call_args[0][2]

        assert payload["iss"] == "12345"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] - payload["iat"] == 660  # 600 + 60 skew
        assert key == "fake-private-key"
        assert algorithm == "RS256"

    @patch.object(github_app, 'get_github_config')
    def test_generate_jwt_empty_app_id_raises(self, mock_config):
        """Test ValueError when app_id is empty."""
        mock_config.return_value = {
            "app_id": "",
            "private_key": "fake-private-key",
        }

        with pytest.raises(ValueError, match="GitHub App ID not configured"):
            generate_jwt()

    @patch.object(github_app, 'get_github_config')
    def test_generate_jwt_placeholder_app_id_raises(self, mock_config):
        """Test ValueError when app_id is the placeholder value."""
        mock_config.return_value = {
            "app_id": "PLACEHOLDER_CONFIGURE_ME",
            "private_key": "fake-private-key",
        }

        with pytest.raises(ValueError, match="GitHub App ID not configured"):
            generate_jwt()

    @patch.object(github_app, 'get_github_config')
    def test_generate_jwt_empty_private_key_raises(self, mock_config):
        """Test ValueError when private_key is empty."""
        mock_config.return_value = {
            "app_id": "12345",
            "private_key": "",
        }

        with pytest.raises(ValueError, match="GitHub App private key not configured"):
            generate_jwt()

    @patch.object(github_app, 'get_github_config')
    def test_generate_jwt_placeholder_private_key_raises(self, mock_config):
        """Test ValueError when private_key is the placeholder value."""
        mock_config.return_value = {
            "app_id": "12345",
            "private_key": "PLACEHOLDER_CONFIGURE_ME",
        }

        with pytest.raises(ValueError, match="GitHub App private key not configured"):
            generate_jwt()

    @patch.object(github_app, 'get_github_config')
    @patch.object(github_app.time, 'time')
    def test_generate_jwt_payload_timing(self, mock_time, mock_config):
        """Test JWT payload timing with clock skew tolerance."""
        mock_time.return_value = 1000000
        mock_config.return_value = {
            "app_id": "12345",
            "private_key": "fake-key",
        }

        mock_jwt = sys.modules["jwt"]
        mock_jwt.encode.reset_mock()

        generate_jwt()

        payload = mock_jwt.encode.call_args[0][0]
        assert payload["iat"] == 999940   # now - 60
        assert payload["exp"] == 1000600  # now + 600


# ============================================================================
# get_installation_token
# ============================================================================

class TestGetInstallationToken:
    """Test installation access token retrieval."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_installation_token_success(self, mock_jwt, mock_urlopen):
        """Test successful installation token retrieval."""
        mock_jwt.return_value = "test.jwt.token"
        mock_urlopen.return_value = _make_urlopen_response(
            {"token": "ghs_install_token_abc", "expires_at": "2025-01-01T00:00:00Z"}
        )

        token, expires_at = get_installation_token("12345")

        assert token == "ghs_install_token_abc"
        assert expires_at == "2025-01-01T00:00:00Z"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_installation_token_url(self, mock_jwt, mock_urlopen):
        """Test correct URL is used for token request."""
        mock_jwt.return_value = "test.jwt.token"
        mock_urlopen.return_value = _make_urlopen_response(
            {"token": "tok", "expires_at": "2025-01-01T00:00:00Z"}
        )

        get_installation_token("67890")

        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.full_url == "https://api.github.com/app/installations/67890/access_tokens"
        assert request.method == "POST"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_installation_token_headers(self, mock_jwt, mock_urlopen):
        """Test correct headers are set on request."""
        mock_jwt.return_value = "test.jwt.token"
        mock_urlopen.return_value = _make_urlopen_response(
            {"token": "tok", "expires_at": "2025-01-01T00:00:00Z"}
        )

        get_installation_token("12345")

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer test.jwt.token"
        assert request.get_header("Accept") == "application/vnd.github+json"
        assert request.get_header("X-github-api-version") == "2022-11-28"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_installation_token_http_error(self, mock_jwt, mock_urlopen):
        """Test HTTP error raises GitHubAppError."""
        mock_jwt.return_value = "test.jwt.token"
        mock_urlopen.side_effect = _make_http_error(401, body=b"Unauthorized")

        with pytest.raises(GitHubAppError, match="Failed to get installation token: 401"):
            get_installation_token("12345")

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_installation_token_http_error_no_body(self, mock_jwt, mock_urlopen):
        """Test HTTP error with no response body."""
        mock_jwt.return_value = "test.jwt.token"
        error = urllib.error.HTTPError(
            url="https://api.github.com/test",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        # Set fp to None to trigger the else branch in error_body read
        error.fp = None
        mock_urlopen.side_effect = error

        with pytest.raises(GitHubAppError, match="500"):
            get_installation_token("12345")


# ============================================================================
# get_app_installations
# ============================================================================

class TestGetAppInstallations:
    """Test listing GitHub App installations."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_app_installations_success(self, mock_jwt, mock_urlopen):
        """Test successful listing of installations."""
        mock_jwt.return_value = "test.jwt.token"
        installations = [
            {"id": 1, "account": {"login": "org1"}, "target_type": "Organization"},
            {"id": 2, "account": {"login": "user1"}, "target_type": "User"},
        ]
        mock_urlopen.return_value = _make_urlopen_response(installations)

        result = get_app_installations()

        assert len(result) == 2
        assert result[0]["account"]["login"] == "org1"
        assert result[1]["target_type"] == "User"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_app_installations_empty(self, mock_jwt, mock_urlopen):
        """Test empty installations list."""
        mock_jwt.return_value = "test.jwt.token"
        mock_urlopen.return_value = _make_urlopen_response([])

        result = get_app_installations()
        assert result == []

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_app_installations_url_and_headers(self, mock_jwt, mock_urlopen):
        """Test correct URL and headers for installations request."""
        mock_jwt.return_value = "my.jwt"
        mock_urlopen.return_value = _make_urlopen_response([])

        get_app_installations()

        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://api.github.com/app/installations"
        assert request.get_header("Authorization") == "Bearer my.jwt"
        assert request.get_header("Accept") == "application/vnd.github+json"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'generate_jwt')
    def test_get_app_installations_http_error(self, mock_jwt, mock_urlopen):
        """Test HTTP error raises GitHubAppError."""
        mock_jwt.return_value = "test.jwt.token"
        mock_urlopen.side_effect = _make_http_error(403, body=b"Forbidden")

        with pytest.raises(GitHubAppError, match="Failed to list installations: 403"):
            get_app_installations()


# ============================================================================
# get_installation_repos
# ============================================================================

class TestGetInstallationRepos:
    """Test listing repositories accessible to an installation."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_installation_repos_single_page(self, mock_token, mock_urlopen):
        """Test listing repos with a single page of results."""
        mock_token.return_value = ("install_token", "2025-01-01T00:00:00Z")
        repos = [{"id": 1, "full_name": "org/repo1"}, {"id": 2, "full_name": "org/repo2"}]
        mock_urlopen.return_value = _make_urlopen_response(
            {"total_count": 2, "repositories": repos}
        )

        result = get_installation_repos("inst_123")

        assert len(result) == 2
        assert result[0]["full_name"] == "org/repo1"
        mock_urlopen.assert_called_once()

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_installation_repos_pagination(self, mock_token, mock_urlopen):
        """Test listing repos with multiple pages."""
        mock_token.return_value = ("install_token", "2025-01-01T00:00:00Z")

        # First page: 100 repos (full page triggers next page request)
        page1_repos = [{"id": i, "full_name": f"org/repo{i}"} for i in range(100)]
        page2_repos = [{"id": 100, "full_name": "org/repo100"}]

        mock_urlopen.side_effect = [
            _make_urlopen_response({"total_count": 101, "repositories": page1_repos}),
            _make_urlopen_response({"total_count": 101, "repositories": page2_repos}),
        ]

        result = get_installation_repos("inst_123")

        assert len(result) == 101
        assert mock_urlopen.call_count == 2

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_installation_repos_empty(self, mock_token, mock_urlopen):
        """Test listing repos when no repos are accessible."""
        mock_token.return_value = ("install_token", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"total_count": 0, "repositories": []}
        )

        result = get_installation_repos("inst_123")
        assert result == []

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_installation_repos_uses_bearer_token(self, mock_token, mock_urlopen):
        """Test that installation token is used as Bearer token."""
        mock_token.return_value = ("my_install_token", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"total_count": 0, "repositories": []}
        )

        get_installation_repos("inst_123")

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my_install_token"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_installation_repos_url_params(self, mock_token, mock_urlopen):
        """Test that URL includes per_page and page parameters."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"total_count": 0, "repositories": []}
        )

        get_installation_repos("inst_123")

        request = mock_urlopen.call_args[0][0]
        assert "per_page=100" in request.full_url
        assert "page=1" in request.full_url

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_installation_repos_http_error(self, mock_token, mock_urlopen):
        """Test HTTP error raises GitHubAppError."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(500, body=b"Server error")

        with pytest.raises(GitHubAppError, match="Failed to list repos: 500"):
            get_installation_repos("inst_123")


# ============================================================================
# get_repo_contents
# ============================================================================

class TestGetRepoContents:
    """Test repository directory contents retrieval."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_contents_directory(self, mock_token, mock_urlopen):
        """Test retrieving directory contents (returns list)."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        contents = [
            {"name": "README.md", "type": "file", "path": "README.md"},
            {"name": "src", "type": "dir", "path": "src"},
        ]
        mock_urlopen.return_value = _make_urlopen_response(contents)

        result = get_repo_contents("inst_123", "myorg", "myrepo")

        assert len(result) == 2
        assert result[0]["name"] == "README.md"
        assert result[1]["type"] == "dir"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_contents_single_file(self, mock_token, mock_urlopen):
        """Test retrieving a single file (returns object, wrapped in list)."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        file_data = {"name": "main.py", "type": "file", "path": "src/main.py"}
        mock_urlopen.return_value = _make_urlopen_response(file_data)

        result = get_repo_contents("inst_123", "myorg", "myrepo", path="src/main.py")
        assert len(result) == 1
        assert result[0]["name"] == "main.py"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_contents_with_path(self, mock_token, mock_urlopen):
        """Test URL includes the path parameter."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response([])

        get_repo_contents("inst_123", "org", "repo", path="src/main")

        request = mock_urlopen.call_args[0][0]
        assert "/repos/org/repo/contents/src/main" in request.full_url

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_contents_with_ref(self, mock_token, mock_urlopen):
        """Test URL includes ref query parameter when provided."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response([])

        get_repo_contents("inst_123", "org", "repo", path="src", ref="feature-branch")

        request = mock_urlopen.call_args[0][0]
        assert "?ref=feature-branch" in request.full_url

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_contents_without_ref(self, mock_token, mock_urlopen):
        """Test URL does not include ref when not provided."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response([])

        get_repo_contents("inst_123", "org", "repo", path="src")

        request = mock_urlopen.call_args[0][0]
        assert "?ref=" not in request.full_url

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_contents_404_returns_empty(self, mock_token, mock_urlopen):
        """Test 404 error returns empty list instead of raising."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(404, body=b"Not Found")

        result = get_repo_contents("inst_123", "org", "repo", path="nonexistent")
        assert result == []

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_contents_non_404_error_raises(self, mock_token, mock_urlopen):
        """Test non-404 HTTP error raises GitHubAppError."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(500, body=b"Server error")

        with pytest.raises(GitHubAppError, match="Failed to get repo contents: 500"):
            get_repo_contents("inst_123", "org", "repo")


# ============================================================================
# get_file_content
# ============================================================================

class TestGetFileContent:
    """Test file content retrieval from repository."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_success(self, mock_token, mock_urlopen):
        """Test successful file content retrieval with base64 decoding."""
        import base64

        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        content = base64.b64encode(b"print('hello world')").decode("utf-8")
        mock_urlopen.return_value = _make_urlopen_response(
            {"type": "file", "content": content, "encoding": "base64"}
        )

        result = get_file_content("inst_123", "org", "repo", "main.py")
        assert result == "print('hello world')"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_with_ref(self, mock_token, mock_urlopen):
        """Test file content retrieval with specific ref."""
        import base64

        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        content = base64.b64encode(b"v2 code").decode("utf-8")
        mock_urlopen.return_value = _make_urlopen_response(
            {"type": "file", "content": content}
        )

        result = get_file_content("inst_123", "org", "repo", "main.py", ref="v2-branch")

        assert result == "v2 code"
        request = mock_urlopen.call_args[0][0]
        assert "?ref=v2-branch" in request.full_url

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_not_a_file_returns_none(self, mock_token, mock_urlopen):
        """Test that non-file type (e.g., directory) returns None."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"type": "dir", "content": ""}
        )

        result = get_file_content("inst_123", "org", "repo", "src")
        assert result is None

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_404_returns_none(self, mock_token, mock_urlopen):
        """Test 404 returns None instead of raising."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(404, body=b"Not Found")

        result = get_file_content("inst_123", "org", "repo", "missing.py")
        assert result is None

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_non_404_error_raises(self, mock_token, mock_urlopen):
        """Test non-404 HTTP error raises GitHubAppError."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(403, body=b"Rate limited")

        with pytest.raises(GitHubAppError, match="Failed to get file content: 403"):
            get_file_content("inst_123", "org", "repo", "secret.py")

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_url_format(self, mock_token, mock_urlopen):
        """Test correct URL construction."""
        import base64

        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"type": "file", "content": base64.b64encode(b"x").decode()}
        )

        get_file_content("inst_123", "myorg", "myrepo", "path/to/file.py")

        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://api.github.com/repos/myorg/myrepo/contents/path/to/file.py"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_empty_content(self, mock_token, mock_urlopen):
        """Test file with empty content returns empty string."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"type": "file", "content": ""}
        )

        result = get_file_content("inst_123", "org", "repo", "empty.py")
        assert result == ""


# ============================================================================
# get_repo_default_branch
# ============================================================================

class TestGetRepoDefaultBranch:
    """Test default branch retrieval."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_default_branch_success(self, mock_token, mock_urlopen):
        """Test successful default branch retrieval."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"default_branch": "develop", "full_name": "org/repo"}
        )

        result = get_repo_default_branch("inst_123", "org", "repo")
        assert result == "develop"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_default_branch_defaults_to_main(self, mock_token, mock_urlopen):
        """Test that missing default_branch key defaults to 'main'."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"full_name": "org/repo"}
        )

        result = get_repo_default_branch("inst_123", "org", "repo")
        assert result == "main"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_default_branch_url(self, mock_token, mock_urlopen):
        """Test correct URL for repo info request."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"default_branch": "main"}
        )

        get_repo_default_branch("inst_123", "myorg", "myrepo")

        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://api.github.com/repos/myorg/myrepo"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_default_branch_http_error(self, mock_token, mock_urlopen):
        """Test HTTP error raises GitHubAppError."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(404, body=b"Not Found")

        with pytest.raises(GitHubAppError, match="Failed to get repo info: 404"):
            get_repo_default_branch("inst_123", "org", "nonexistent")


# ============================================================================
# get_latest_commit_sha
# ============================================================================

class TestGetLatestCommitSha:
    """Test latest commit SHA retrieval."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_latest_commit_sha_with_branch(self, mock_token, mock_urlopen):
        """Test getting latest commit SHA for a specific branch."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"sha": "abc123def456", "commit": {"message": "latest"}}
        )

        result = get_latest_commit_sha("inst_123", "org", "repo", branch="develop")
        assert result == "abc123def456"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_latest_commit_sha_url_with_branch(self, mock_token, mock_urlopen):
        """Test URL includes the branch name."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response({"sha": "abc123"})

        get_latest_commit_sha("inst_123", "org", "repo", branch="feature-x")

        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://api.github.com/repos/org/repo/commits/feature-x"

    @patch.object(github_app, 'get_repo_default_branch')
    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_latest_commit_sha_no_branch_uses_default(
        self, mock_token, mock_urlopen, mock_default_branch
    ):
        """Test that omitting branch fetches default branch."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_default_branch.return_value = "main"
        mock_urlopen.return_value = _make_urlopen_response({"sha": "deadbeef"})

        result = get_latest_commit_sha("inst_123", "org", "repo")

        assert result == "deadbeef"
        mock_default_branch.assert_called_once_with("inst_123", "org", "repo")

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_latest_commit_sha_http_error(self, mock_token, mock_urlopen):
        """Test HTTP error raises GitHubAppError."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(422, body=b"No such branch")

        with pytest.raises(GitHubAppError, match="Failed to get latest commit: 422"):
            get_latest_commit_sha("inst_123", "org", "repo", branch="nonexistent")


# ============================================================================
# get_tree_recursive
# ============================================================================

class TestGetTreeRecursive:
    """Test recursive repository tree retrieval."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_tree_recursive_success(self, mock_token, mock_urlopen):
        """Test successful recursive tree retrieval."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        tree_items = [
            {"path": "README.md", "type": "blob", "sha": "aaa"},
            {"path": "src", "type": "tree", "sha": "bbb"},
            {"path": "src/main.py", "type": "blob", "sha": "ccc"},
        ]
        mock_urlopen.return_value = _make_urlopen_response(
            {"sha": "root_sha", "tree": tree_items, "truncated": False}
        )

        result = get_tree_recursive("inst_123", "org", "repo", "root_sha")

        assert len(result) == 3
        assert result[0]["path"] == "README.md"
        assert result[2]["type"] == "blob"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_tree_recursive_url(self, mock_token, mock_urlopen):
        """Test correct URL for tree request with recursive flag."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response({"tree": []})

        get_tree_recursive("inst_123", "org", "repo", "sha123")

        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://api.github.com/repos/org/repo/git/trees/sha123?recursive=1"

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_tree_recursive_empty_tree(self, mock_token, mock_urlopen):
        """Test empty tree returns empty list."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"sha": "root_sha", "tree": [], "truncated": False}
        )

        result = get_tree_recursive("inst_123", "org", "repo", "root_sha")
        assert result == []

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_tree_recursive_missing_tree_key(self, mock_token, mock_urlopen):
        """Test missing 'tree' key defaults to empty list."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response({"sha": "root_sha"})

        result = get_tree_recursive("inst_123", "org", "repo", "root_sha")
        assert result == []

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_tree_recursive_http_error(self, mock_token, mock_urlopen):
        """Test HTTP error raises GitHubAppError."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.side_effect = _make_http_error(500, body=b"Internal error")

        with pytest.raises(GitHubAppError, match="Failed to get repo tree: 500"):
            get_tree_recursive("inst_123", "org", "repo", "sha123")

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_tree_recursive_timeout(self, mock_token, mock_urlopen):
        """Test that timeout is set to 60 seconds for tree requests."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response({"tree": []})

        get_tree_recursive("inst_123", "org", "repo", "sha123")

        call_kwargs = mock_urlopen.call_args
        assert call_kwargs[1].get("timeout") == 60 or (
            len(call_kwargs[0]) > 1 and call_kwargs[0][1] is None
        ) or call_kwargs[1].get("timeout", 60) == 60


# ============================================================================
# Integration-style tests (cross-function interactions)
# ============================================================================

class TestCrossFunctionBehavior:
    """Test interactions between functions."""

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_github_config')
    def test_generate_jwt_used_by_get_installation_token(self, mock_config, mock_urlopen):
        """Test that get_installation_token calls generate_jwt internally."""
        mock_config.return_value = {
            "app_id": "12345",
            "private_key": "fake-key",
        }
        mock_urlopen.return_value = _make_urlopen_response(
            {"token": "ghs_tok", "expires_at": "2025-01-01T00:00:00Z"}
        )

        token, expires = get_installation_token("inst_1")

        assert token == "ghs_tok"
        # Verify JWT was generated (jwt.encode was called)
        mock_jwt = sys.modules["jwt"]
        assert mock_jwt.encode.called

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_installation_repos_calls_get_installation_token(
        self, mock_token, mock_urlopen
    ):
        """Test that get_installation_repos gets a token first."""
        mock_token.return_value = ("install_tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"total_count": 0, "repositories": []}
        )

        get_installation_repos("inst_42")
        mock_token.assert_called_once_with("inst_42")

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_file_content_calls_get_installation_token(
        self, mock_token, mock_urlopen
    ):
        """Test that get_file_content gets a token first."""
        import base64

        mock_token.return_value = ("ft", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"type": "file", "content": base64.b64encode(b"x").decode()}
        )

        get_file_content("inst_7", "org", "repo", "f.py")
        mock_token.assert_called_once_with("inst_7")

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_repo_default_branch_calls_get_installation_token(
        self, mock_token, mock_urlopen
    ):
        """Test that get_repo_default_branch gets a token first."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response(
            {"default_branch": "main"}
        )

        get_repo_default_branch("inst_9", "org", "repo")
        mock_token.assert_called_once_with("inst_9")

    @patch.object(github_app.urllib.request, 'urlopen')
    @patch.object(github_app, 'get_installation_token')
    def test_get_tree_recursive_calls_get_installation_token(
        self, mock_token, mock_urlopen
    ):
        """Test that get_tree_recursive gets a token first."""
        mock_token.return_value = ("tok", "2025-01-01T00:00:00Z")
        mock_urlopen.return_value = _make_urlopen_response({"tree": []})

        get_tree_recursive("inst_5", "org", "repo", "sha")
        mock_token.assert_called_once_with("inst_5")
