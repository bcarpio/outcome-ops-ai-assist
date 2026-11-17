# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for file-ingestion modules: jira_api, jira_token_manager, token_manager."""

import base64
import importlib.util
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading - uses importlib to load from exact file paths for Sonar
# ============================================================================

FILE_INGESTION_DIR = os.path.join(os.path.dirname(__file__), "../../file-ingestion")

# Add file-ingestion directory to sys.path so internal imports resolve
if FILE_INGESTION_DIR not in sys.path:
    sys.path.insert(0, FILE_INGESTION_DIR)


def _load_module(module_name, filename):
    """Load a module from the file-ingestion directory by exact file path."""
    if module_name in sys.modules:
        del sys.modules[module_name]
    filepath = os.path.join(FILE_INGESTION_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Use unique module names to avoid collisions with test_jira_api.py which
# loads jira-sync/jira_api.py under the name "jira_api".
jira_api_module = _load_module("fi_jira_api", "jira_api.py")
jira_token_module = _load_module("fi_jira_token_manager", "jira_token_manager.py")
confluence_token_module = _load_module("fi_token_manager", "token_manager.py")

# ---------------------------------------------------------------------------
# jira_api imports
# ---------------------------------------------------------------------------
_make_request = jira_api_module._make_request
get_accessible_resources = jira_api_module.get_accessible_resources
list_all_projects = jira_api_module.list_all_projects
search_issues = jira_api_module.search_issues
list_all_issues_in_project = jira_api_module.list_all_issues_in_project
search_issues_modified_since = jira_api_module.search_issues_modified_since
get_issue_comments = jira_api_module.get_issue_comments
download_attachment = jira_api_module.download_attachment
JiraAPIError = jira_api_module.JiraAPIError
JiraAuthError = jira_api_module.JiraAuthError

# ---------------------------------------------------------------------------
# jira_token_manager imports
# ---------------------------------------------------------------------------
jira_save_tokens = jira_token_module.save_tokens
jira_get_refresh_token = jira_token_module.get_refresh_token
jira_refresh_access_token = jira_token_module.refresh_access_token
jira_encrypt_token = jira_token_module._encrypt_token
jira_decrypt_token = jira_token_module._decrypt_token

# ---------------------------------------------------------------------------
# token_manager (Confluence) imports
# ---------------------------------------------------------------------------
confluence_save_tokens = confluence_token_module.save_tokens
confluence_get_refresh_token = confluence_token_module.get_refresh_token
confluence_refresh_access_token = confluence_token_module.refresh_access_token
confluence_delete_tokens = confluence_token_module.delete_tokens
confluence_exchange_code_for_tokens = confluence_token_module.exchange_code_for_tokens
confluence_encrypt_token = confluence_token_module._encrypt_token
confluence_decrypt_token = confluence_token_module._decrypt_token


# ############################################################################
#
#  JIRA API TESTS (jira_api.py)
#
# ############################################################################


# ============================================================================
# Exception Classes
# ============================================================================

class TestJiraExceptions:
    """Test custom Jira exception classes."""

    def test_jira_api_error_attributes(self):
        """Test JiraAPIError stores message and status code."""
        err = JiraAPIError("Something failed", status_code=500)
        assert str(err) == "Something failed"
        assert err.status_code == 500

    def test_jira_api_error_default_status_code(self):
        """Test JiraAPIError defaults status_code to None."""
        err = JiraAPIError("No code")
        assert err.status_code is None

    def test_jira_auth_error_is_api_error(self):
        """Test JiraAuthError inherits from JiraAPIError."""
        err = JiraAuthError("Auth failed", status_code=401)
        assert isinstance(err, JiraAPIError)
        assert err.status_code == 401


# ============================================================================
# _make_request
# ============================================================================

class TestJiraMakeRequest:
    """Test the core HTTP request function for Jira API."""

    @patch.object(jira_api_module.requests, "request")
    def test_make_request_success(self, mock_request):
        """Test successful API request returns response object."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.ok = True
        mock_request.return_value = mock_response

        result = _make_request("GET", "https://api.atlassian.com/test", "access_token")
        assert result == mock_response
        mock_request.assert_called_once_with(
            method="GET",
            url="https://api.atlassian.com/test",
            headers={
                "Authorization": "Bearer access_token",
                "Accept": "application/json",
            },
            params=None,
            timeout=60,
            stream=False,
        )

    @patch.object(jira_api_module.requests, "request")
    def test_make_request_sets_auth_header(self, mock_request):
        """Test request includes correct Bearer token header."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.ok = True
        mock_request.return_value = mock_response

        _make_request("GET", "https://api.atlassian.com/test", "my_token")
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer my_token"

    @patch.object(jira_api_module.requests, "request")
    def test_make_request_passes_params(self, mock_request):
        """Test request forwards query parameters."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.ok = True
        mock_request.return_value = mock_response

        params = {"startAt": 0, "maxResults": 50}
        _make_request("GET", "https://api.atlassian.com/test", "tok", params=params)
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["params"] == params

    @patch.object(jira_api_module.requests, "request")
    def test_make_request_custom_timeout_and_stream(self, mock_request):
        """Test request forwards timeout and stream options."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.ok = True
        mock_request.return_value = mock_response

        _make_request("GET", "https://example.com", "tok", timeout=300, stream=True)
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["timeout"] == 300
        assert call_kwargs["stream"] is True

    @patch.object(jira_api_module.requests, "request")
    def test_make_request_401_raises_auth_error(self, mock_request):
        """Test 401 response raises JiraAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.ok = False
        mock_request.return_value = mock_response

        with pytest.raises(JiraAuthError) as exc_info:
            _make_request("GET", "https://example.com", "bad_token")
        assert exc_info.value.status_code == 401

    @patch.object(jira_api_module.requests, "request")
    def test_make_request_500_raises_api_error(self, mock_request):
        """Test 500 response raises JiraAPIError (not JiraAuthError)."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.ok = False
        mock_response.text = "Internal Server Error"
        mock_request.return_value = mock_response

        with pytest.raises(JiraAPIError) as exc_info:
            _make_request("GET", "https://example.com", "tok")
        assert exc_info.value.status_code == 500
        assert not isinstance(exc_info.value, JiraAuthError)

    @patch.object(jira_api_module.requests, "request")
    def test_make_request_error_body_logging(self, mock_request):
        """Test non-OK response attempts to log error body."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.ok = False
        mock_response.text = '{"message": "Forbidden"}'
        mock_request.return_value = mock_response

        with pytest.raises(JiraAPIError) as exc_info:
            _make_request("GET", "https://example.com", "tok")
        assert exc_info.value.status_code == 403


# ============================================================================
# get_accessible_resources
# ============================================================================

class TestGetAccessibleResources:
    """Test get_accessible_resources."""

    @patch.object(jira_api_module.requests, "get")
    def test_get_accessible_resources_success(self, mock_get):
        """Test successful retrieval of accessible resources."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = [
            {"id": "cloud-1", "name": "My Site", "url": "https://mysite.atlassian.net"}
        ]
        mock_get.return_value = mock_response

        result = get_accessible_resources("access_token")
        assert len(result) == 1
        assert result[0]["id"] == "cloud-1"
        mock_get.assert_called_once_with(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={"Authorization": "Bearer access_token"},
            timeout=30,
        )

    @patch.object(jira_api_module.requests, "get")
    def test_get_accessible_resources_multiple(self, mock_get):
        """Test retrieval of multiple accessible resources."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = [
            {"id": "cloud-1", "name": "Site A"},
            {"id": "cloud-2", "name": "Site B"},
        ]
        mock_get.return_value = mock_response

        result = get_accessible_resources("tok")
        assert len(result) == 2

    @patch.object(jira_api_module.requests, "get")
    def test_get_accessible_resources_failure(self, mock_get):
        """Test raises JiraAPIError on non-OK response."""
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_get.return_value = mock_response

        with pytest.raises(JiraAPIError):
            get_accessible_resources("bad_token")


# ============================================================================
# list_all_projects
# ============================================================================

class TestListAllProjects:
    """Test list_all_projects."""

    @patch.object(jira_api_module, "_make_request")
    def test_list_all_projects_single_page(self, mock_request):
        """Test listing projects that fit in a single page."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "values": [
                {"id": "1", "key": "PROJ", "name": "Project One"},
                {"id": "2", "key": "PROJ2", "name": "Project Two"},
            ],
            "isLast": True,
        }
        mock_request.return_value = mock_response

        result = list_all_projects("tok", "cloud-1")
        assert len(result) == 2
        assert result[0]["key"] == "PROJ"
        mock_request.assert_called_once()

    @patch.object(jira_api_module, "_make_request")
    def test_list_all_projects_pagination(self, mock_request):
        """Test listing projects across multiple pages."""
        page1_response = MagicMock()
        page1_response.json.return_value = {
            "values": [{"id": str(i)} for i in range(50)],
            "isLast": False,
        }
        page2_response = MagicMock()
        page2_response.json.return_value = {
            "values": [{"id": "50"}, {"id": "51"}],
            "isLast": True,
        }
        mock_request.side_effect = [page1_response, page2_response]

        result = list_all_projects("tok", "cloud-1")
        assert len(result) == 52
        assert mock_request.call_count == 2

    @patch.object(jira_api_module, "_make_request")
    def test_list_all_projects_empty(self, mock_request):
        """Test listing projects when none exist."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"values": [], "isLast": True}
        mock_request.return_value = mock_response

        result = list_all_projects("tok", "cloud-1")
        assert result == []

    @patch.object(jira_api_module, "_make_request")
    def test_list_all_projects_stops_when_less_than_max(self, mock_request):
        """Test pagination stops when values count < maxResults."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "values": [{"id": "1"}],
            # isLast not present, but < maxResults should stop
        }
        mock_request.return_value = mock_response

        result = list_all_projects("tok", "cloud-1")
        assert len(result) == 1
        mock_request.assert_called_once()


# ============================================================================
# search_issues
# ============================================================================

class TestSearchIssues:
    """Test search_issues."""

    @patch.object(jira_api_module, "_make_request")
    def test_search_issues_single_page(self, mock_request):
        """Test searching issues that fit in a single page."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "issues": [
                {"key": "PROJ-1", "fields": {"summary": "Issue 1"}},
                {"key": "PROJ-2", "fields": {"summary": "Issue 2"}},
            ],
            "total": 2,
        }
        mock_request.return_value = mock_response

        result = search_issues("tok", "cloud-1", 'project = "PROJ"')
        assert len(result) == 2
        assert result[0]["key"] == "PROJ-1"

    @patch.object(jira_api_module, "_make_request")
    def test_search_issues_pagination(self, mock_request):
        """Test searching issues across multiple pages."""
        page1_response = MagicMock()
        page1_response.json.return_value = {
            "issues": [{"key": f"P-{i}"} for i in range(100)],
            "total": 150,
        }
        page2_response = MagicMock()
        page2_response.json.return_value = {
            "issues": [{"key": f"P-{i}"} for i in range(100, 150)],
            "total": 150,
        }
        mock_request.side_effect = [page1_response, page2_response]

        result = search_issues("tok", "cloud-1", 'project = "P"')
        assert len(result) == 150
        assert mock_request.call_count == 2

    @patch.object(jira_api_module, "_make_request")
    def test_search_issues_default_fields(self, mock_request):
        """Test default fields are included in request params."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"issues": [], "total": 0}
        mock_request.return_value = mock_response

        search_issues("tok", "cloud-1", "project = X")
        call_kwargs = mock_request.call_args[1]
        params = call_kwargs["params"]
        assert "summary" in params["fields"]
        assert "description" in params["fields"]
        assert "status" in params["fields"]
        assert "attachment" in params["fields"]
        assert "comment" in params["fields"]

    @patch.object(jira_api_module, "_make_request")
    def test_search_issues_custom_fields(self, mock_request):
        """Test custom fields override defaults."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"issues": [], "total": 0}
        mock_request.return_value = mock_response

        search_issues("tok", "cloud-1", "project = X", fields=["summary", "status"])
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["params"]["fields"] == "summary,status"

    @patch.object(jira_api_module, "_make_request")
    def test_search_issues_with_expand(self, mock_request):
        """Test expand parameter is forwarded."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"issues": [], "total": 0}
        mock_request.return_value = mock_response

        search_issues("tok", "cloud-1", "project = X", expand=["renderedFields"])
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["params"]["expand"] == "renderedFields"

    @patch.object(jira_api_module, "_make_request")
    def test_search_issues_empty_result(self, mock_request):
        """Test search with no matching issues."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"issues": [], "total": 0}
        mock_request.return_value = mock_response

        result = search_issues("tok", "cloud-1", 'project = "EMPTY"')
        assert result == []


# ============================================================================
# list_all_issues_in_project
# ============================================================================

class TestListAllIssuesInProject:
    """Test list_all_issues_in_project."""

    @patch.object(jira_api_module, "search_issues")
    def test_list_all_issues_delegates_to_search(self, mock_search):
        """Test delegates to search_issues with correct JQL."""
        mock_search.return_value = [{"key": "PROJ-1"}]

        result = list_all_issues_in_project("tok", "cloud-1", "PROJ")
        assert len(result) == 1
        mock_search.assert_called_once_with(
            "tok", "cloud-1", 'project = "PROJ" ORDER BY updated DESC'
        )


# ============================================================================
# search_issues_modified_since
# ============================================================================

class TestSearchIssuesModifiedSince:
    """Test search_issues_modified_since."""

    @patch.object(jira_api_module, "search_issues")
    def test_search_modified_since_valid_date(self, mock_search):
        """Test generates JQL with minutes-ago for valid ISO date."""
        mock_search.return_value = []

        search_issues_modified_since("tok", "cloud-1", "PROJ", "2020-01-01T00:00:00Z")
        jql = mock_search.call_args[0][2]
        assert 'project = "PROJ"' in jql
        assert "updated >= -" in jql
        assert "ORDER BY updated DESC" in jql

    @patch.object(jira_api_module, "search_issues")
    def test_search_modified_since_minimum_65_minutes(self, mock_search):
        """Test enforces minimum 65 minutes window."""
        mock_search.return_value = []
        # Use a very recent timestamp (just now) - should still be >= 65m
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat()

        search_issues_modified_since("tok", "cloud-1", "PROJ", recent)
        jql = mock_search.call_args[0][2]
        # Extract minutes value
        import re
        match = re.search(r"-(\d+)m", jql)
        assert match is not None
        minutes = int(match.group(1))
        assert minutes >= 65

    @patch.object(jira_api_module, "search_issues")
    def test_search_modified_since_invalid_date_fallback(self, mock_search):
        """Test falls back to -65m on unparseable date."""
        mock_search.return_value = []

        search_issues_modified_since("tok", "cloud-1", "PROJ", "not-a-date")
        jql = mock_search.call_args[0][2]
        assert "-65m" in jql

    @patch.object(jira_api_module, "search_issues")
    def test_search_modified_since_with_fractional_seconds(self, mock_search):
        """Test handles ISO dates with fractional seconds."""
        mock_search.return_value = []

        search_issues_modified_since("tok", "cloud-1", "PROJ", "2020-06-15T12:30:45.123Z")
        jql = mock_search.call_args[0][2]
        assert 'project = "PROJ"' in jql
        assert "updated >= -" in jql


# ============================================================================
# get_issue_comments
# ============================================================================

class TestGetIssueComments:
    """Test get_issue_comments."""

    @patch.object(jira_api_module, "_make_request")
    def test_get_issue_comments_single_page(self, mock_request):
        """Test retrieving comments that fit in a single page."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "comments": [
                {"id": "c1", "body": "Comment 1"},
                {"id": "c2", "body": "Comment 2"},
            ],
            "total": 2,
        }
        mock_request.return_value = mock_response

        result = get_issue_comments("tok", "cloud-1", "PROJ-123")
        assert len(result) == 2
        assert result[0]["id"] == "c1"

    @patch.object(jira_api_module, "_make_request")
    def test_get_issue_comments_pagination(self, mock_request):
        """Test retrieving comments across multiple pages."""
        page1_response = MagicMock()
        page1_response.json.return_value = {
            "comments": [{"id": f"c{i}"} for i in range(100)],
            "total": 120,
        }
        page2_response = MagicMock()
        page2_response.json.return_value = {
            "comments": [{"id": f"c{i}"} for i in range(100, 120)],
            "total": 120,
        }
        mock_request.side_effect = [page1_response, page2_response]

        result = get_issue_comments("tok", "cloud-1", "PROJ-123")
        assert len(result) == 120
        assert mock_request.call_count == 2

    @patch.object(jira_api_module, "_make_request")
    def test_get_issue_comments_empty(self, mock_request):
        """Test retrieving comments when none exist."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"comments": [], "total": 0}
        mock_request.return_value = mock_response

        result = get_issue_comments("tok", "cloud-1", "PROJ-123")
        assert result == []

    @patch.object(jira_api_module, "_make_request")
    def test_get_issue_comments_url_format(self, mock_request):
        """Test the correct API URL is constructed."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"comments": [], "total": 0}
        mock_request.return_value = mock_response

        get_issue_comments("tok", "cloud-1", "PROJ-456")
        call_args = mock_request.call_args
        url = call_args[0][1]
        assert url == "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/issue/PROJ-456/comment"


# ============================================================================
# download_attachment
# ============================================================================

class TestJiraDownloadAttachment:
    """Test download_attachment."""

    @patch.object(jira_api_module, "_make_request")
    def test_download_attachment_success(self, mock_request):
        """Test downloading attachment content as bytes."""
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]
        mock_request.return_value = mock_response

        result = download_attachment("tok", "cloud-1", "att-123")
        assert result == b"chunk1chunk2"
        mock_request.assert_called_once_with(
            "GET",
            "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/attachment/content/att-123",
            "tok",
            timeout=300,
            stream=True,
        )

    @patch.object(jira_api_module, "_make_request")
    def test_download_attachment_empty(self, mock_request):
        """Test downloading empty attachment."""
        mock_response = MagicMock()
        mock_response.iter_content.return_value = []
        mock_request.return_value = mock_response

        result = download_attachment("tok", "cloud-1", "att-empty")
        assert result == b""

    @patch.object(jira_api_module, "_make_request")
    def test_download_attachment_auth_error(self, mock_request):
        """Test download raises JiraAuthError on 401."""
        mock_request.side_effect = JiraAuthError("Auth failed", 401)

        with pytest.raises(JiraAuthError):
            download_attachment("tok", "cloud-1", "att-123")


# ############################################################################
#
#  JIRA TOKEN MANAGER TESTS (jira_token_manager.py)
#
# ############################################################################


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_jira_token_module_globals():
    """Reset cached Jira token manager globals between tests."""
    jira_token_module._dynamodb = None
    jira_token_module._kms = None
    yield


@pytest.fixture
def jira_mock_kms():
    """Create mock KMS client for Jira token tests."""
    mock = MagicMock()
    mock.encrypt.return_value = {"CiphertextBlob": b"jira_encrypted_data"}
    mock.decrypt.return_value = {"Plaintext": b"jira_decrypted_token"}
    return mock


@pytest.fixture
def jira_mock_table():
    """Create mock DynamoDB table for Jira token tests."""
    return MagicMock()


@pytest.fixture
def jira_mock_dynamodb(jira_mock_table):
    """Create mock DynamoDB resource for Jira token tests."""
    mock = MagicMock()
    mock.Table.return_value = jira_mock_table
    return mock


# ============================================================================
# Encrypt/Decrypt (Jira)
# ============================================================================

class TestJiraEncryptDecrypt:
    """Test Jira token encryption and decryption."""

    @patch.object(jira_token_module, "_get_kms")
    def test_encrypt_token(self, mock_kms_fn, jira_mock_kms):
        """Test encrypting a token returns base64-encoded string."""
        mock_kms_fn.return_value = jira_mock_kms

        result = jira_encrypt_token("my_secret_token")
        assert isinstance(result, str)
        # Should be valid base64
        base64.b64decode(result)
        jira_mock_kms.encrypt.assert_called_once()

    @patch.object(jira_token_module, "_get_kms")
    def test_decrypt_token(self, mock_kms_fn, jira_mock_kms):
        """Test decrypting a token returns plain text."""
        mock_kms_fn.return_value = jira_mock_kms

        encrypted = base64.b64encode(b"jira_encrypted_data").decode("utf-8")
        result = jira_decrypt_token(encrypted)
        assert result == "jira_decrypted_token"
        jira_mock_kms.decrypt.assert_called_once()


# ============================================================================
# Save Tokens (Jira)
# ============================================================================

class TestJiraSaveTokens:
    """Test saving Jira OAuth tokens."""

    @patch.object(jira_token_module, "_encrypt_token")
    @patch.object(jira_token_module, "_get_dynamodb")
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt, jira_mock_dynamodb, jira_mock_table):
        """Test saving tokens stores encrypted refresh token in DynamoDB."""
        mock_db_fn.return_value = jira_mock_dynamodb
        mock_encrypt.return_value = "encrypted_refresh"

        result = jira_save_tokens("conn_jira", "access_tok", "refresh_tok", 3600)
        assert result is True
        jira_mock_table.put_item.assert_called_once()

        item = jira_mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "JIRA_TOKEN#conn_jira"
        assert item["SK"] == "CREDENTIALS"
        assert item["encrypted_refresh_token"] == "encrypted_refresh"
        assert item["connection_id"] == "conn_jira"

    @patch.object(jira_token_module, "_encrypt_token")
    @patch.object(jira_token_module, "_get_dynamodb")
    def test_save_tokens_dynamodb_error(self, mock_db_fn, mock_encrypt, jira_mock_dynamodb, jira_mock_table):
        """Test returns False on DynamoDB error."""
        mock_db_fn.return_value = jira_mock_dynamodb
        mock_encrypt.return_value = "encrypted"
        jira_mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutItem"
        )

        result = jira_save_tokens("conn_jira", "access", "refresh")
        assert result is False


# ============================================================================
# Get Refresh Token (Jira)
# ============================================================================

class TestJiraGetRefreshToken:
    """Test retrieving Jira refresh tokens."""

    @patch.object(jira_token_module, "_decrypt_token")
    @patch.object(jira_token_module, "_get_dynamodb")
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt, jira_mock_dynamodb, jira_mock_table):
        """Test getting and decrypting Jira refresh token."""
        mock_db_fn.return_value = jira_mock_dynamodb
        jira_mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "encrypted_value"}
        }
        mock_decrypt.return_value = "plain_refresh_token"

        result = jira_get_refresh_token("conn_jira")
        assert result == "plain_refresh_token"
        mock_decrypt.assert_called_once_with("encrypted_value")

    @patch.object(jira_token_module, "_get_dynamodb")
    def test_get_refresh_token_not_found(self, mock_db_fn, jira_mock_dynamodb, jira_mock_table):
        """Test returns None when Jira token not found."""
        mock_db_fn.return_value = jira_mock_dynamodb
        jira_mock_table.get_item.return_value = {}

        result = jira_get_refresh_token("nonexistent")
        assert result is None

    @patch.object(jira_token_module, "_get_dynamodb")
    def test_get_refresh_token_no_encrypted_field(self, mock_db_fn, jira_mock_dynamodb, jira_mock_table):
        """Test returns None when encrypted field is missing."""
        mock_db_fn.return_value = jira_mock_dynamodb
        jira_mock_table.get_item.return_value = {"Item": {}}

        result = jira_get_refresh_token("conn_jira")
        assert result is None

    @patch.object(jira_token_module, "_get_dynamodb")
    def test_get_refresh_token_dynamodb_error(self, mock_db_fn, jira_mock_dynamodb, jira_mock_table):
        """Test returns None on DynamoDB error."""
        mock_db_fn.return_value = jira_mock_dynamodb
        jira_mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "GetItem"
        )

        result = jira_get_refresh_token("conn_jira")
        assert result is None

    @patch.object(jira_token_module, "_get_dynamodb")
    def test_get_refresh_token_uses_jira_prefix(self, mock_db_fn, jira_mock_dynamodb, jira_mock_table):
        """Test get_item uses JIRA_TOKEN# prefix."""
        mock_db_fn.return_value = jira_mock_dynamodb
        jira_mock_table.get_item.return_value = {}

        jira_get_refresh_token("conn_test")
        call_kwargs = jira_mock_table.get_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "JIRA_TOKEN#conn_test"
        assert call_kwargs["Key"]["SK"] == "CREDENTIALS"


# ============================================================================
# Refresh Access Token (Jira)
# ============================================================================

class TestJiraRefreshAccessToken:
    """Test refreshing Jira access tokens via Atlassian token endpoint."""

    @patch.object(jira_token_module, "save_tokens")
    @patch.object(jira_token_module.urllib.request, "urlopen")
    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_success_rolling_token(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test successful token refresh with rolling refresh token."""
        mock_get_refresh.return_value = "old_refresh_token"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = jira_refresh_access_token("conn_jira", "client_id", "client_secret")
        assert result == "new_access_token"
        mock_save.assert_called_once_with("conn_jira", "new_access_token", "new_refresh_token", 3600)

    @patch.object(jira_token_module, "save_tokens")
    @patch.object(jira_token_module.urllib.request, "urlopen")
    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_success_same_token(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test refresh when same refresh token is returned."""
        mock_get_refresh.return_value = "same_refresh_token"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access",
            "refresh_token": "same_refresh_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = jira_refresh_access_token("conn_jira", "cid", "cs")
        assert result == "new_access"
        # Should still save (just update timestamp)
        mock_save.assert_called_once()

    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_no_refresh_token(self, mock_get_refresh):
        """Test returns None when no refresh token stored."""
        mock_get_refresh.return_value = None

        result = jira_refresh_access_token("conn_jira", "client_id", "client_secret")
        assert result is None

    @patch.object(jira_token_module.urllib.request, "urlopen")
    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_http_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on HTTP error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = jira_refresh_access_token("conn_jira", "cid", "cs")
        assert result is None

    @patch.object(jira_token_module.urllib.request, "urlopen")
    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_http_error_invalid_grant(self, mock_get_refresh, mock_urlopen):
        """Test handles invalid_grant error body (token revoked)."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = jira_refresh_access_token("conn_jira", "cid", "cs")
        assert result is None

    @patch.object(jira_token_module.urllib.request, "urlopen")
    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_network_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on network error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = jira_refresh_access_token("conn_jira", "cid", "cs")
        assert result is None

    @patch.object(jira_token_module.urllib.request, "urlopen")
    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_no_access_token_in_response(self, mock_get_refresh, mock_urlopen):
        """Test returns None when response lacks access_token."""
        mock_get_refresh.return_value = "refresh_token"
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"error": "something"}).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = jira_refresh_access_token("conn_jira", "cid", "cs")
        assert result is None

    @patch.object(jira_token_module.urllib.request, "urlopen")
    @patch.object(jira_token_module, "get_refresh_token")
    def test_refresh_http_error_no_fp(self, mock_get_refresh, mock_urlopen):
        """Test handles HTTP error with no file pointer (fp=None)."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )

        result = jira_refresh_access_token("conn_jira", "cid", "cs")
        assert result is None


# ############################################################################
#
#  CONFLUENCE TOKEN MANAGER TESTS (token_manager.py)
#
# ############################################################################


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_confluence_token_module_globals():
    """Reset cached Confluence token manager globals between tests."""
    confluence_token_module._dynamodb = None
    confluence_token_module._kms = None
    yield


@pytest.fixture
def confluence_mock_kms():
    """Create mock KMS client for Confluence token tests."""
    mock = MagicMock()
    mock.encrypt.return_value = {"CiphertextBlob": b"confluence_encrypted_data"}
    mock.decrypt.return_value = {"Plaintext": b"confluence_decrypted_token"}
    return mock


@pytest.fixture
def confluence_mock_table():
    """Create mock DynamoDB table for Confluence token tests."""
    return MagicMock()


@pytest.fixture
def confluence_mock_dynamodb(confluence_mock_table):
    """Create mock DynamoDB resource for Confluence token tests."""
    mock = MagicMock()
    mock.Table.return_value = confluence_mock_table
    return mock


# ============================================================================
# Encrypt/Decrypt (Confluence)
# ============================================================================

class TestConfluenceEncryptDecrypt:
    """Test Confluence token encryption and decryption."""

    @patch.object(confluence_token_module, "_get_kms")
    def test_encrypt_token(self, mock_kms_fn, confluence_mock_kms):
        """Test encrypting a token returns base64-encoded string."""
        mock_kms_fn.return_value = confluence_mock_kms

        result = confluence_encrypt_token("my_secret_token")
        assert isinstance(result, str)
        base64.b64decode(result)
        confluence_mock_kms.encrypt.assert_called_once()

    @patch.object(confluence_token_module, "_get_kms")
    def test_decrypt_token(self, mock_kms_fn, confluence_mock_kms):
        """Test decrypting a token returns plain text."""
        mock_kms_fn.return_value = confluence_mock_kms

        encrypted = base64.b64encode(b"confluence_encrypted_data").decode("utf-8")
        result = confluence_decrypt_token(encrypted)
        assert result == "confluence_decrypted_token"
        confluence_mock_kms.decrypt.assert_called_once()


# ============================================================================
# Save Tokens (Confluence)
# ============================================================================

class TestConfluenceSaveTokens:
    """Test saving Confluence OAuth tokens."""

    @patch.object(confluence_token_module, "_encrypt_token")
    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt, confluence_mock_dynamodb, confluence_mock_table):
        """Test saving tokens stores encrypted refresh token in DynamoDB."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        mock_encrypt.return_value = "encrypted_refresh"

        result = confluence_save_tokens("conn_conf", "access_tok", "refresh_tok", 3600)
        assert result is True
        confluence_mock_table.put_item.assert_called_once()

        item = confluence_mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "CONFLUENCE_TOKEN#conn_conf"
        assert item["SK"] == "CREDENTIALS"
        assert item["encrypted_refresh_token"] == "encrypted_refresh"
        assert item["connection_id"] == "conn_conf"

    @patch.object(confluence_token_module, "_encrypt_token")
    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_save_tokens_dynamodb_error(self, mock_db_fn, mock_encrypt, confluence_mock_dynamodb, confluence_mock_table):
        """Test returns False on DynamoDB error."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        mock_encrypt.return_value = "encrypted"
        confluence_mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutItem"
        )

        result = confluence_save_tokens("conn_conf", "access", "refresh")
        assert result is False

    @patch.object(confluence_token_module, "_encrypt_token")
    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_save_tokens_default_expires_in(self, mock_db_fn, mock_encrypt, confluence_mock_dynamodb, confluence_mock_table):
        """Test save_tokens uses default expires_in of 3600."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        mock_encrypt.return_value = "enc"

        result = confluence_save_tokens("conn", "access", "refresh")
        assert result is True
        item = confluence_mock_table.put_item.call_args[1]["Item"]
        assert "expires_at" in item


# ============================================================================
# Get Refresh Token (Confluence)
# ============================================================================

class TestConfluenceGetRefreshToken:
    """Test retrieving Confluence refresh tokens."""

    @patch.object(confluence_token_module, "_decrypt_token")
    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt, confluence_mock_dynamodb, confluence_mock_table):
        """Test getting and decrypting Confluence refresh token."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        confluence_mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "encrypted_value"}
        }
        mock_decrypt.return_value = "plain_refresh_token"

        result = confluence_get_refresh_token("conn_conf")
        assert result == "plain_refresh_token"
        mock_decrypt.assert_called_once_with("encrypted_value")

    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_get_refresh_token_not_found(self, mock_db_fn, confluence_mock_dynamodb, confluence_mock_table):
        """Test returns None when Confluence token not found."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        confluence_mock_table.get_item.return_value = {}

        result = confluence_get_refresh_token("nonexistent")
        assert result is None

    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_get_refresh_token_no_encrypted_field(self, mock_db_fn, confluence_mock_dynamodb, confluence_mock_table):
        """Test returns None when encrypted field is missing."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        confluence_mock_table.get_item.return_value = {"Item": {}}

        result = confluence_get_refresh_token("conn_conf")
        assert result is None

    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_get_refresh_token_dynamodb_error(self, mock_db_fn, confluence_mock_dynamodb, confluence_mock_table):
        """Test returns None on DynamoDB error."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        confluence_mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "GetItem"
        )

        result = confluence_get_refresh_token("conn_conf")
        assert result is None

    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_get_refresh_token_uses_confluence_prefix(self, mock_db_fn, confluence_mock_dynamodb, confluence_mock_table):
        """Test get_item uses CONFLUENCE_TOKEN# prefix."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        confluence_mock_table.get_item.return_value = {}

        confluence_get_refresh_token("conn_test")
        call_kwargs = confluence_mock_table.get_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "CONFLUENCE_TOKEN#conn_test"
        assert call_kwargs["Key"]["SK"] == "CREDENTIALS"


# ============================================================================
# Refresh Access Token (Confluence)
# ============================================================================

class TestConfluenceRefreshAccessToken:
    """Test refreshing Confluence access tokens via Atlassian token endpoint."""

    @patch.object(confluence_token_module, "save_tokens")
    @patch.object(confluence_token_module.urllib.request, "urlopen")
    @patch.object(confluence_token_module, "get_refresh_token")
    def test_refresh_success_rolling_token(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test successful token refresh with rolling refresh token."""
        mock_get_refresh.return_value = "old_refresh_token"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = confluence_refresh_access_token("conn_conf", "client_id", "client_secret")
        assert result == "new_access_token"
        mock_save.assert_called_once_with("conn_conf", "new_access_token", "new_refresh_token", 3600)

    @patch.object(confluence_token_module, "save_tokens")
    @patch.object(confluence_token_module.urllib.request, "urlopen")
    @patch.object(confluence_token_module, "get_refresh_token")
    def test_refresh_success_same_token(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test refresh when same refresh token is returned."""
        mock_get_refresh.return_value = "same_refresh_token"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access",
            "refresh_token": "same_refresh_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = confluence_refresh_access_token("conn_conf", "cid", "cs")
        assert result == "new_access"
        mock_save.assert_called_once()

    @patch.object(confluence_token_module, "get_refresh_token")
    def test_refresh_no_refresh_token(self, mock_get_refresh):
        """Test returns None when no refresh token stored."""
        mock_get_refresh.return_value = None

        result = confluence_refresh_access_token("conn_conf", "client_id", "client_secret")
        assert result is None

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    @patch.object(confluence_token_module, "get_refresh_token")
    def test_refresh_http_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on HTTP error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = confluence_refresh_access_token("conn_conf", "cid", "cs")
        assert result is None

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    @patch.object(confluence_token_module, "get_refresh_token")
    def test_refresh_network_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on network error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = confluence_refresh_access_token("conn_conf", "cid", "cs")
        assert result is None

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    @patch.object(confluence_token_module, "get_refresh_token")
    def test_refresh_no_access_token_in_response(self, mock_get_refresh, mock_urlopen):
        """Test returns None when response lacks access_token."""
        mock_get_refresh.return_value = "refresh_token"
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"error": "something"}).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = confluence_refresh_access_token("conn_conf", "cid", "cs")
        assert result is None

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    @patch.object(confluence_token_module, "get_refresh_token")
    def test_refresh_http_error_no_fp(self, mock_get_refresh, mock_urlopen):
        """Test handles HTTP error with no file pointer (fp=None)."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )

        result = confluence_refresh_access_token("conn_conf", "cid", "cs")
        assert result is None


# ============================================================================
# Delete Tokens (Confluence)
# ============================================================================

class TestConfluenceDeleteTokens:
    """Test deleting Confluence tokens."""

    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_delete_tokens_success(self, mock_db_fn, confluence_mock_dynamodb, confluence_mock_table):
        """Test deleting tokens from DynamoDB."""
        mock_db_fn.return_value = confluence_mock_dynamodb

        result = confluence_delete_tokens("conn_conf")
        assert result is True
        confluence_mock_table.delete_item.assert_called_once_with(
            Key={
                "PK": "CONFLUENCE_TOKEN#conn_conf",
                "SK": "CREDENTIALS",
            }
        )

    @patch.object(confluence_token_module, "_get_dynamodb")
    def test_delete_tokens_dynamodb_error(self, mock_db_fn, confluence_mock_dynamodb, confluence_mock_table):
        """Test returns False on DynamoDB error."""
        mock_db_fn.return_value = confluence_mock_dynamodb
        confluence_mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "DeleteItem"
        )

        result = confluence_delete_tokens("conn_conf")
        assert result is False


# ============================================================================
# Exchange Code for Tokens (Confluence)
# ============================================================================

class TestConfluenceExchangeCodeForTokens:
    """Test exchanging authorization code for Confluence tokens."""

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    def test_exchange_success(self, mock_urlopen):
        """Test successful code exchange."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "access_tok",
            "refresh_token": "refresh_tok",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        access, refresh, expires = confluence_exchange_code_for_tokens(
            "auth_code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access == "access_tok"
        assert refresh == "refresh_tok"
        assert expires == 3600

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    def test_exchange_http_error(self, mock_urlopen):
        """Test returns None tuple on HTTP error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_code"}')),
        )

        access, refresh, expires = confluence_exchange_code_for_tokens(
            "bad_code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    def test_exchange_network_error(self, mock_urlopen):
        """Test returns None tuple on network error."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        access, refresh, expires = confluence_exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    def test_exchange_default_expires(self, mock_urlopen):
        """Test defaults to 3600 when expires_in not in response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "tok",
            "refresh_token": "ref",
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        _, _, expires = confluence_exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert expires == 3600

    @patch.object(confluence_token_module.urllib.request, "urlopen")
    def test_exchange_http_error_no_fp(self, mock_urlopen):
        """Test handles HTTP error with no file pointer."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )

        access, refresh, expires = confluence_exchange_code_for_tokens(
            "code", "cid", "cs", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None
