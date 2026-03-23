"""Unit tests for jira-integration/jira_api.py.

These tests import from the jira-integration directory (not jira-sync)
to ensure coverage is tracked for the correct module path.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Module loading - load from jira-integration path
# ============================================================================

def _load_api_module():
    """Load jira_api module from the jira-integration Lambda."""
    mod_name = 'jira_integration_api'
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    api_path = os.path.join(os.path.dirname(__file__), '../../jira-integration/jira_api.py')
    spec = importlib.util.spec_from_file_location(mod_name, api_path)
    api_module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = api_module
    spec.loader.exec_module(api_module)
    return api_module


api_module = _load_api_module()

# Import from module
_make_request = api_module._make_request
get_accessible_resources = api_module.get_accessible_resources
list_all_projects = api_module.list_all_projects
search_issues = api_module.search_issues
list_all_issues_in_project = api_module.list_all_issues_in_project
search_issues_modified_since = api_module.search_issues_modified_since
get_issue_comments = api_module.get_issue_comments
download_attachment = api_module.download_attachment
JiraAPIError = api_module.JiraAPIError
JiraAuthError = api_module.JiraAuthError
JIRA_API_BASE = api_module.JIRA_API_BASE
ATLASSIAN_RESOURCES_URL = api_module.ATLASSIAN_RESOURCES_URL


# ============================================================================
# Helpers
# ============================================================================

TOKEN = "test-access-token"
CLOUD_ID = "cloud-id-abc123"


def _mock_response(status_code=200, json_data=None, text="", ok=True, content_chunks=None):
    """Create a mock requests.Response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = ok
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    if content_chunks is not None:
        resp.iter_content.return_value = iter(content_chunks)
    return resp


# ============================================================================
# Exception Classes
# ============================================================================

class TestExceptions:
    """Test custom exception classes."""

    def test_jira_api_error_attributes(self):
        """Test JiraAPIError stores message and status code."""
        err = JiraAPIError("Test error", status_code=500)
        assert str(err) == "Test error"
        assert err.status_code == 500

    def test_jira_api_error_default_status_code(self):
        """Test JiraAPIError defaults status_code to None."""
        err = JiraAPIError("No status")
        assert err.status_code is None

    def test_jira_auth_error_is_api_error(self):
        """Test JiraAuthError inherits from JiraAPIError."""
        err = JiraAuthError("Auth failed", status_code=401)
        assert isinstance(err, JiraAPIError)
        assert err.status_code == 401


# ============================================================================
# _make_request
# ============================================================================

class TestMakeRequest:
    """Test the core HTTP request function."""

    @patch.object(api_module.requests, 'request')
    def test_make_request_success(self, mock_request):
        """Test successful API request returns response."""
        mock_resp = _mock_response(json_data={"key": "value"})
        mock_request.return_value = mock_resp

        result = _make_request("GET", "https://api.example.com/test", TOKEN)
        assert result == mock_resp

    @patch.object(api_module.requests, 'request')
    def test_make_request_sets_auth_header(self, mock_request):
        """Test request includes Bearer token in Authorization header."""
        mock_request.return_value = _mock_response()

        _make_request("GET", "https://api.example.com/test", "my_token_123")

        call_kwargs = mock_request.call_args
        headers = call_kwargs[1]["headers"] if "headers" in call_kwargs[1] else call_kwargs.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my_token_123"
        assert headers["Accept"] == "application/json"

    @patch.object(api_module.requests, 'request')
    def test_make_request_401_raises_auth_error(self, mock_request):
        """Test 401 response raises JiraAuthError."""
        mock_request.return_value = _mock_response(
            status_code=401, ok=False, text="Unauthorized"
        )

        with pytest.raises(JiraAuthError) as exc_info:
            _make_request("GET", "https://api.example.com/test", "bad_token")
        assert exc_info.value.status_code == 401

    @patch.object(api_module.requests, 'request')
    def test_make_request_500_raises_api_error(self, mock_request):
        """Test 500 response raises JiraAPIError."""
        mock_request.return_value = _mock_response(
            status_code=500, ok=False, text="Server Error"
        )

        with pytest.raises(JiraAPIError) as exc_info:
            _make_request("GET", "https://api.example.com/test", TOKEN)
        assert exc_info.value.status_code == 500

    @patch.object(api_module.requests, 'request')
    def test_make_request_passes_params(self, mock_request):
        """Test request forwards query parameters."""
        mock_request.return_value = _mock_response()
        params = {"startAt": 0, "maxResults": 50}

        _make_request("GET", "https://api.example.com/test", TOKEN, params=params)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1].get("params") == params or call_kwargs.kwargs.get("params") == params

    @patch.object(api_module.requests, 'request')
    def test_make_request_custom_timeout(self, mock_request):
        """Test request uses custom timeout."""
        mock_request.return_value = _mock_response()

        _make_request("GET", "https://api.example.com/test", TOKEN, timeout=300)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1].get("timeout") == 300 or call_kwargs.kwargs.get("timeout") == 300

    @patch.object(api_module.requests, 'request')
    def test_make_request_stream_parameter(self, mock_request):
        """Test request passes stream parameter."""
        mock_request.return_value = _mock_response()

        _make_request("GET", "https://api.example.com/test", TOKEN, stream=True)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1].get("stream") is True or call_kwargs.kwargs.get("stream") is True

    @patch.object(api_module.requests, 'request')
    def test_make_request_error_with_unreadable_body(self, mock_request):
        """Test error handling when response text raises an exception."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.ok = False
        type(mock_resp).text = property(lambda self: (_ for _ in ()).throw(Exception("read error")))
        mock_request.return_value = mock_resp

        with pytest.raises(JiraAPIError):
            _make_request("GET", "https://api.example.com/test", TOKEN)


# ============================================================================
# get_accessible_resources
# ============================================================================

class TestGetAccessibleResources:
    """Test get_accessible_resources."""

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_success(self, mock_get):
        """Test successful retrieval of accessible resources."""
        sites = [
            {"id": "cloud-1", "name": "My Site", "url": "https://mysite.atlassian.net"},
        ]
        mock_get.return_value = _mock_response(json_data=sites)

        result = get_accessible_resources(TOKEN)
        assert len(result) == 1
        assert result[0]["id"] == "cloud-1"

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_api_error(self, mock_get):
        """Test API error raises JiraAPIError."""
        mock_get.return_value = _mock_response(
            status_code=500, ok=False, text="Server error"
        )

        with pytest.raises(JiraAPIError):
            get_accessible_resources(TOKEN)


# ============================================================================
# list_all_projects
# ============================================================================

class TestListAllProjects:
    """Test list_all_projects."""

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_single_page(self, mock_request):
        """Test listing projects when all fit on one page."""
        projects = [
            {"id": "1", "key": "PROJ", "name": "Project One"},
        ]
        mock_request.return_value = _mock_response(
            json_data={"values": projects, "isLast": True}
        )

        result = list_all_projects(TOKEN, CLOUD_ID)
        assert len(result) == 1
        assert result[0]["key"] == "PROJ"

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_pagination(self, mock_request):
        """Test listing projects with multiple pages."""
        page1 = [{"id": str(i)} for i in range(50)]
        page2 = [{"id": str(i)} for i in range(50, 75)]

        mock_request.side_effect = [
            _mock_response(json_data={"values": page1, "isLast": False}),
            _mock_response(json_data={"values": page2, "isLast": True}),
        ]

        result = list_all_projects(TOKEN, CLOUD_ID)
        assert len(result) == 75

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_empty(self, mock_request):
        """Test listing projects when none exist."""
        mock_request.return_value = _mock_response(
            json_data={"values": [], "isLast": True}
        )

        result = list_all_projects(TOKEN, CLOUD_ID)
        assert result == []


# ============================================================================
# search_issues
# ============================================================================

class TestSearchIssues:
    """Test search_issues."""

    @patch.object(api_module, '_make_request')
    def test_search_issues_single_page(self, mock_request):
        """Test searching issues when all results fit on one page."""
        issues = [
            {"key": "PROJ-1", "fields": {"summary": "Issue 1"}},
        ]
        mock_request.return_value = _mock_response(
            json_data={"issues": issues, "total": 1}
        )

        result = search_issues(TOKEN, CLOUD_ID, "project = PROJ")
        assert len(result) == 1

    @patch.object(api_module, '_make_request')
    def test_search_issues_custom_fields(self, mock_request):
        """Test custom fields are passed through."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        search_issues(TOKEN, CLOUD_ID, "project = TEST", fields=["summary", "status"])

        call_kwargs = mock_request.call_args
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params")
        assert params["fields"] == "summary,status"

    @patch.object(api_module, '_make_request')
    def test_search_issues_with_expand(self, mock_request):
        """Test expand parameter is included when specified."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        search_issues(TOKEN, CLOUD_ID, "project = TEST", expand=["changelog"])

        call_kwargs = mock_request.call_args
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params")
        assert params["expand"] == "changelog"

    @patch.object(api_module, '_make_request')
    def test_search_issues_pagination(self, mock_request):
        """Test searching issues across multiple pages."""
        page1 = [{"key": f"PROJ-{i}"} for i in range(100)]
        page2 = [{"key": f"PROJ-{i}"} for i in range(100, 150)]

        mock_request.side_effect = [
            _mock_response(json_data={"issues": page1, "total": 150}),
            _mock_response(json_data={"issues": page2, "total": 150}),
        ]

        result = search_issues(TOKEN, CLOUD_ID, "project = PROJ")
        assert len(result) == 150


# ============================================================================
# list_all_issues_in_project
# ============================================================================

class TestListAllIssuesInProject:
    """Test list_all_issues_in_project."""

    @patch.object(api_module, 'search_issues')
    def test_list_all_issues_delegates_to_search(self, mock_search):
        """Test list_all_issues_in_project delegates to search_issues."""
        mock_search.return_value = [{"key": "PROJ-1"}]

        result = list_all_issues_in_project(TOKEN, CLOUD_ID, "PROJ")
        assert result == [{"key": "PROJ-1"}]

    @patch.object(api_module, 'search_issues')
    def test_list_all_issues_jql_format(self, mock_search):
        """Test correct JQL is generated for project."""
        mock_search.return_value = []

        list_all_issues_in_project(TOKEN, CLOUD_ID, "MYPROJ")

        mock_search.assert_called_once_with(
            TOKEN, CLOUD_ID, 'project = "MYPROJ" ORDER BY updated DESC'
        )


# ============================================================================
# search_issues_modified_since
# ============================================================================

class TestSearchIssuesModifiedSince:
    """Test search_issues_modified_since."""

    @patch.object(api_module, 'search_issues')
    def test_modified_since_generates_jql(self, mock_search):
        """Test function generates JQL with relative time."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "2024-01-01T00:00:00Z")

        jql = mock_search.call_args[0][2]
        assert 'project = "PROJ"' in jql
        assert "updated >=" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_minimum_65_minutes(self, mock_search):
        """Test minimum 65 minutes is enforced."""
        from datetime import datetime, timezone
        mock_search.return_value = []

        recent_time = datetime.now(timezone.utc).isoformat()
        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", recent_time)

        jql = mock_search.call_args[0][2]
        assert "-65m" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_invalid_date_uses_fallback(self, mock_search):
        """Test invalid date falls back to -65m."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "not-a-date")

        jql = mock_search.call_args[0][2]
        assert "-65m" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_fractional_seconds(self, mock_search):
        """Test ISO timestamp with fractional seconds."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "2024-06-15T12:30:45.123Z")

        mock_search.assert_called_once()


# ============================================================================
# get_issue_comments
# ============================================================================

class TestGetIssueComments:
    """Test get_issue_comments."""

    @patch.object(api_module, '_make_request')
    def test_get_comments_single_page(self, mock_request):
        """Test retrieving comments that fit on one page."""
        comments = [
            {"id": "c1", "body": "Comment 1"},
        ]
        mock_request.return_value = _mock_response(
            json_data={"comments": comments, "total": 1}
        )

        result = get_issue_comments(TOKEN, CLOUD_ID, "PROJ-123")
        assert len(result) == 1

    @patch.object(api_module, '_make_request')
    def test_get_comments_pagination(self, mock_request):
        """Test retrieving comments across multiple pages."""
        page1 = [{"id": f"c{i}"} for i in range(100)]
        page2 = [{"id": f"c{i}"} for i in range(100, 130)]

        mock_request.side_effect = [
            _mock_response(json_data={"comments": page1, "total": 130}),
            _mock_response(json_data={"comments": page2, "total": 130}),
        ]

        result = get_issue_comments(TOKEN, CLOUD_ID, "PROJ-1")
        assert len(result) == 130

    @patch.object(api_module, '_make_request')
    def test_get_comments_empty(self, mock_request):
        """Test issue with no comments."""
        mock_request.return_value = _mock_response(
            json_data={"comments": [], "total": 0}
        )

        result = get_issue_comments(TOKEN, CLOUD_ID, "PROJ-1")
        assert result == []


# ============================================================================
# download_attachment
# ============================================================================

class TestDownloadAttachment:
    """Test download_attachment."""

    @patch.object(api_module, '_make_request')
    def test_download_attachment_success(self, mock_request):
        """Test successful attachment download."""
        content = b"PDF file content bytes"
        mock_resp = _mock_response(content_chunks=[content])
        mock_request.return_value = mock_resp

        result = download_attachment(TOKEN, CLOUD_ID, "att-123")
        assert result == content

    @patch.object(api_module, '_make_request')
    def test_download_attachment_multiple_chunks(self, mock_request):
        """Test downloading attachment streamed in multiple chunks."""
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        mock_request.return_value = _mock_response(content_chunks=chunks)

        result = download_attachment(TOKEN, CLOUD_ID, "att-1")
        assert result == b"chunk1chunk2chunk3"

    @patch.object(api_module, '_make_request')
    def test_download_attachment_empty(self, mock_request):
        """Test downloading empty attachment."""
        mock_request.return_value = _mock_response(content_chunks=[])

        result = download_attachment(TOKEN, CLOUD_ID, "att-1")
        assert result == b""

    @patch.object(api_module, '_make_request')
    def test_download_attachment_uses_stream_and_timeout(self, mock_request):
        """Test request uses stream=True and 300s timeout."""
        mock_request.return_value = _mock_response(content_chunks=[b"data"])

        download_attachment(TOKEN, CLOUD_ID, "att-1")

        call_kwargs = mock_request.call_args
        assert call_kwargs[1].get("stream") is True or call_kwargs.kwargs.get("stream") is True
        assert call_kwargs[1].get("timeout") == 300 or call_kwargs.kwargs.get("timeout") == 300

    @patch.object(api_module, '_make_request')
    def test_download_attachment_api_error(self, mock_request):
        """Test JiraAPIError propagates from _make_request."""
        mock_request.side_effect = JiraAPIError("Not found", status_code=404)

        with pytest.raises(JiraAPIError):
            download_attachment(TOKEN, CLOUD_ID, "nonexistent")


# ============================================================================
# Constants
# ============================================================================

class TestConstants:
    """Test module-level constants."""

    def test_jira_api_base_url(self):
        """Test JIRA_API_BASE is the expected Atlassian API URL."""
        assert JIRA_API_BASE == "https://api.atlassian.com/ex/jira"

    def test_atlassian_resources_url(self):
        """Test ATLASSIAN_RESOURCES_URL is the expected OAuth resources URL."""
        assert ATLASSIAN_RESOURCES_URL == "https://api.atlassian.com/oauth/token/accessible-resources"
