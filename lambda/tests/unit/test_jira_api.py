# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Jira API client (jira_api.py)."""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest


# ============================================================================
# Module loading
# ============================================================================

def _load_api_module():
    """Load or reload the jira_api module from the jira-sync Lambda."""
    if 'jira_api' in sys.modules:
        del sys.modules['jira_api']

    api_path = os.path.join(os.path.dirname(__file__), '../../jira-sync/jira_api.py')
    spec = importlib.util.spec_from_file_location("jira_api", api_path)
    api_module = importlib.util.module_from_spec(spec)
    sys.modules['jira_api'] = api_module
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

    def test_jira_auth_error_message(self):
        """Test JiraAuthError stores its message."""
        err = JiraAuthError("Token expired")
        assert str(err) == "Token expired"


# ============================================================================
# _make_request
# ============================================================================

class TestMakeRequest:
    """Test the core HTTP request function."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module.requests, 'request')
    def test_make_request_success(self, mock_request):
        """Test successful API request returns response."""
        mock_resp = _mock_response(json_data={"key": "value"})
        mock_request.return_value = mock_resp

        result = _make_request("GET", "https://api.example.com/test", TOKEN)
        assert result == mock_resp
        assert result.json() == {"key": "value"}

    @patch.object(api_module.requests, 'request')
    def test_make_request_sets_auth_header(self, mock_request):
        """Test request includes Bearer token in Authorization header."""
        mock_request.return_value = _mock_response()

        _make_request("GET", "https://api.example.com/test", "my_token_123")

        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args
        headers = call_kwargs[1]["headers"] if "headers" in call_kwargs[1] else call_kwargs.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my_token_123"
        assert headers["Accept"] == "application/json"

    @patch.object(api_module.requests, 'request')
    def test_make_request_passes_method(self, mock_request):
        """Test request uses the specified HTTP method."""
        mock_request.return_value = _mock_response()

        _make_request("POST", "https://api.example.com/test", TOKEN)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["method"] == "POST" or call_kwargs.kwargs.get("method") == "POST"

    @patch.object(api_module.requests, 'request')
    def test_make_request_passes_params(self, mock_request):
        """Test request forwards query parameters."""
        mock_request.return_value = _mock_response()
        params = {"startAt": 0, "maxResults": 50}

        _make_request("GET", "https://api.example.com/test", TOKEN, params=params)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["params"] == params or call_kwargs.kwargs.get("params") == params

    @patch.object(api_module.requests, 'request')
    def test_make_request_default_timeout(self, mock_request):
        """Test request uses default 60s timeout."""
        mock_request.return_value = _mock_response()

        _make_request("GET", "https://api.example.com/test", TOKEN)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["timeout"] == 60 or call_kwargs.kwargs.get("timeout") == 60

    @patch.object(api_module.requests, 'request')
    def test_make_request_custom_timeout(self, mock_request):
        """Test request uses custom timeout when specified."""
        mock_request.return_value = _mock_response()

        _make_request("GET", "https://api.example.com/test", TOKEN, timeout=300)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["timeout"] == 300 or call_kwargs.kwargs.get("timeout") == 300

    @patch.object(api_module.requests, 'request')
    def test_make_request_stream_parameter(self, mock_request):
        """Test request passes stream parameter."""
        mock_request.return_value = _mock_response()

        _make_request("GET", "https://api.example.com/test", TOKEN, stream=True)

        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["stream"] is True or call_kwargs.kwargs.get("stream") is True

    @patch.object(api_module.requests, 'request')
    def test_make_request_401_raises_auth_error(self, mock_request):
        """Test 401 response raises JiraAuthError."""
        mock_request.return_value = _mock_response(
            status_code=401, ok=False, text="Unauthorized"
        )

        with pytest.raises(JiraAuthError) as exc_info:
            _make_request("GET", "https://api.example.com/test", "bad_token")
        assert exc_info.value.status_code == 401
        assert "Authentication failed" in str(exc_info.value)

    @patch.object(api_module.requests, 'request')
    def test_make_request_403_raises_api_error(self, mock_request):
        """Test 403 response raises JiraAPIError (not auth error)."""
        mock_request.return_value = _mock_response(
            status_code=403, ok=False, text="Forbidden"
        )

        with pytest.raises(JiraAPIError) as exc_info:
            _make_request("GET", "https://api.example.com/test", TOKEN)
        assert exc_info.value.status_code == 403
        # 403 is not treated as auth error in the Jira API wrapper
        assert not isinstance(exc_info.value, JiraAuthError)

    @patch.object(api_module.requests, 'request')
    def test_make_request_404_raises_api_error(self, mock_request):
        """Test 404 response raises JiraAPIError."""
        mock_request.return_value = _mock_response(
            status_code=404, ok=False, text="Not Found"
        )

        with pytest.raises(JiraAPIError) as exc_info:
            _make_request("GET", "https://api.example.com/test", TOKEN)
        assert exc_info.value.status_code == 404

    @patch.object(api_module.requests, 'request')
    def test_make_request_500_raises_api_error(self, mock_request):
        """Test 500 response raises JiraAPIError."""
        mock_request.return_value = _mock_response(
            status_code=500, ok=False, text="Internal Server Error"
        )

        with pytest.raises(JiraAPIError) as exc_info:
            _make_request("GET", "https://api.example.com/test", TOKEN)
        assert exc_info.value.status_code == 500
        assert not isinstance(exc_info.value, JiraAuthError)

    @patch.object(api_module.requests, 'request')
    def test_make_request_error_body_logged(self, mock_request):
        """Test error response body is available for logging."""
        mock_request.return_value = _mock_response(
            status_code=429, ok=False, text='{"message": "Rate limited"}'
        )

        with pytest.raises(JiraAPIError) as exc_info:
            _make_request("GET", "https://api.example.com/test", TOKEN)
        assert exc_info.value.status_code == 429

    @patch.object(api_module.requests, 'request')
    def test_make_request_error_with_unreadable_body(self, mock_request):
        """Test error handling when response text raises an exception."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.ok = False
        # Make .text raise an exception
        type(mock_resp).text = property(lambda self: (_ for _ in ()).throw(Exception("read error")))
        mock_request.return_value = mock_resp

        with pytest.raises(JiraAPIError):
            _make_request("GET", "https://api.example.com/test", TOKEN)


# ============================================================================
# get_accessible_resources
# ============================================================================

class TestGetAccessibleResources:
    """Test get_accessible_resources."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_success(self, mock_get):
        """Test successful retrieval of accessible resources."""
        sites = [
            {"id": "cloud-1", "name": "My Site", "url": "https://mysite.atlassian.net"},
            {"id": "cloud-2", "name": "Other Site", "url": "https://other.atlassian.net"},
        ]
        mock_get.return_value = _mock_response(json_data=sites)

        result = get_accessible_resources(TOKEN)
        assert len(result) == 2
        assert result[0]["id"] == "cloud-1"
        assert result[1]["name"] == "Other Site"

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_auth_header(self, mock_get):
        """Test request includes correct Authorization header."""
        mock_get.return_value = _mock_response(json_data=[])

        get_accessible_resources("my_special_token")

        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1]["headers"] if "headers" in call_kwargs[1] else call_kwargs.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my_special_token"

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_uses_correct_url(self, mock_get):
        """Test request targets the Atlassian accessible-resources URL."""
        mock_get.return_value = _mock_response(json_data=[])

        get_accessible_resources(TOKEN)

        call_args = mock_get.call_args
        assert call_args[0][0] == ATLASSIAN_RESOURCES_URL

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_timeout(self, mock_get):
        """Test request uses 30s timeout."""
        mock_get.return_value = _mock_response(json_data=[])

        get_accessible_resources(TOKEN)

        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["timeout"] == 30 or call_kwargs.kwargs.get("timeout") == 30

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_empty(self, mock_get):
        """Test empty list when no sites are accessible."""
        mock_get.return_value = _mock_response(json_data=[])

        result = get_accessible_resources(TOKEN)
        assert result == []

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_api_error(self, mock_get):
        """Test API error raises JiraAPIError."""
        mock_get.return_value = _mock_response(
            status_code=500, ok=False, text="Server error"
        )

        with pytest.raises(JiraAPIError) as exc_info:
            get_accessible_resources(TOKEN)
        assert "500" in str(exc_info.value)

    @patch.object(api_module.requests, 'get')
    def test_get_accessible_resources_401_raises_api_error(self, mock_get):
        """Test 401 raises JiraAPIError (not JiraAuthError since it uses requests.get directly)."""
        mock_get.return_value = _mock_response(
            status_code=401, ok=False, text="Unauthorized"
        )

        with pytest.raises(JiraAPIError):
            get_accessible_resources("expired_token")


# ============================================================================
# list_all_projects
# ============================================================================

class TestListAllProjects:
    """Test list_all_projects."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_single_page(self, mock_request):
        """Test listing projects when all fit on one page."""
        projects = [
            {"id": "1", "key": "PROJ", "name": "Project One"},
            {"id": "2", "key": "TEST", "name": "Test Project"},
        ]
        mock_request.return_value = _mock_response(
            json_data={"values": projects, "isLast": True}
        )

        result = list_all_projects(TOKEN, CLOUD_ID)
        assert len(result) == 2
        assert result[0]["key"] == "PROJ"
        assert result[1]["key"] == "TEST"

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_uses_correct_url(self, mock_request):
        """Test request targets the correct project search URL."""
        mock_request.return_value = _mock_response(
            json_data={"values": [], "isLast": True}
        )

        list_all_projects(TOKEN, CLOUD_ID)

        expected_url = f"{JIRA_API_BASE}/{CLOUD_ID}/rest/api/3/project/search"
        call_args = mock_request.call_args
        assert call_args[0][1] == expected_url

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_pagination(self, mock_request):
        """Test listing projects with multiple pages."""
        page1_projects = [{"id": str(i), "key": f"P{i}", "name": f"Project {i}"} for i in range(50)]
        page2_projects = [{"id": str(i), "key": f"P{i}", "name": f"Project {i}"} for i in range(50, 75)]

        mock_request.side_effect = [
            _mock_response(json_data={"values": page1_projects, "isLast": False}),
            _mock_response(json_data={"values": page2_projects, "isLast": True}),
        ]

        result = list_all_projects(TOKEN, CLOUD_ID)
        assert len(result) == 75
        assert mock_request.call_count == 2

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_pagination_stops_when_fewer_than_max(self, mock_request):
        """Test pagination stops when returned values are less than maxResults."""
        page1_projects = [{"id": str(i)} for i in range(50)]
        page2_projects = [{"id": str(i)} for i in range(50, 60)]

        mock_request.side_effect = [
            _mock_response(json_data={"values": page1_projects, "isLast": False}),
            _mock_response(json_data={"values": page2_projects, "isLast": False}),
        ]

        result = list_all_projects(TOKEN, CLOUD_ID)
        # Second page has only 10 items (< maxResults of 50), so pagination stops
        assert len(result) == 60
        assert mock_request.call_count == 2

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_stops_when_is_last_missing(self, mock_request):
        """Test pagination stops when isLast key is missing (defaults to True)."""
        page1_projects = [{"id": str(i)} for i in range(50)]

        mock_request.return_value = _mock_response(
            json_data={"values": page1_projects}
        )

        result = list_all_projects(TOKEN, CLOUD_ID)
        # isLast defaults to True when missing, so only one page fetched
        assert len(result) == 50
        assert mock_request.call_count == 1

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_empty(self, mock_request):
        """Test listing projects when none exist."""
        mock_request.return_value = _mock_response(
            json_data={"values": [], "isLast": True}
        )

        result = list_all_projects(TOKEN, CLOUD_ID)
        assert result == []

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_pagination_params(self, mock_request):
        """Test pagination parameters are sent correctly."""
        mock_request.return_value = _mock_response(
            json_data={"values": [], "isLast": True}
        )

        list_all_projects(TOKEN, CLOUD_ID)

        call_kwargs = mock_request.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs.kwargs.get("params")
        assert params["startAt"] == 0
        assert params["maxResults"] == 50

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_increments_start_at(self, mock_request):
        """Test startAt increments by maxResults on each page."""
        page1 = [{"id": str(i)} for i in range(50)]

        mock_request.side_effect = [
            _mock_response(json_data={"values": page1, "isLast": False}),
            _mock_response(json_data={"values": [], "isLast": True}),
        ]

        list_all_projects(TOKEN, CLOUD_ID)

        second_call = mock_request.call_args_list[1]
        params = second_call[1]["params"] if "params" in second_call[1] else second_call.kwargs.get("params")
        assert params["startAt"] == 50

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_api_error_propagates(self, mock_request):
        """Test JiraAPIError propagates from _make_request."""
        mock_request.side_effect = JiraAPIError("Server error", status_code=500)

        with pytest.raises(JiraAPIError):
            list_all_projects(TOKEN, CLOUD_ID)

    @patch.object(api_module, '_make_request')
    def test_list_all_projects_auth_error_propagates(self, mock_request):
        """Test JiraAuthError propagates from _make_request."""
        mock_request.side_effect = JiraAuthError("Auth failed", status_code=401)

        with pytest.raises(JiraAuthError):
            list_all_projects(TOKEN, CLOUD_ID)


# ============================================================================
# search_issues
# ============================================================================

class TestSearchIssues:
    """Test search_issues."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module, '_make_request')
    def test_search_issues_single_page(self, mock_request):
        """Test searching issues when all results fit on one page."""
        issues = [
            {"key": "PROJ-1", "fields": {"summary": "Issue 1"}},
            {"key": "PROJ-2", "fields": {"summary": "Issue 2"}},
        ]
        mock_request.return_value = _mock_response(
            json_data={"issues": issues, "total": 2}
        )

        result = search_issues(TOKEN, CLOUD_ID, "project = PROJ")
        assert len(result) == 2
        assert result[0]["key"] == "PROJ-1"

    @patch.object(api_module, '_make_request')
    def test_search_issues_uses_correct_url(self, mock_request):
        """Test request targets the JQL search endpoint."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        search_issues(TOKEN, CLOUD_ID, "project = TEST")

        expected_url = f"{JIRA_API_BASE}/{CLOUD_ID}/rest/api/3/search/jql"
        call_args = mock_request.call_args
        assert call_args[0][1] == expected_url

    @patch.object(api_module, '_make_request')
    def test_search_issues_default_fields(self, mock_request):
        """Test default fields are requested when not specified."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        search_issues(TOKEN, CLOUD_ID, "project = TEST")

        call_kwargs = mock_request.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs.kwargs.get("params")
        fields_str = params["fields"]
        expected_fields = [
            "summary", "description", "status", "assignee", "reporter",
            "created", "updated", "attachment", "comment", "priority",
            "issuetype", "labels",
        ]
        for field in expected_fields:
            assert field in fields_str

    @patch.object(api_module, '_make_request')
    def test_search_issues_custom_fields(self, mock_request):
        """Test custom fields are passed through."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        search_issues(TOKEN, CLOUD_ID, "project = TEST", fields=["summary", "status"])

        call_kwargs = mock_request.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs.kwargs.get("params")
        assert params["fields"] == "summary,status"

    @patch.object(api_module, '_make_request')
    def test_search_issues_with_expand(self, mock_request):
        """Test expand parameter is included when specified."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        search_issues(TOKEN, CLOUD_ID, "project = TEST", expand=["changelog", "renderedFields"])

        call_kwargs = mock_request.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs.kwargs.get("params")
        assert params["expand"] == "changelog,renderedFields"

    @patch.object(api_module, '_make_request')
    def test_search_issues_no_expand_by_default(self, mock_request):
        """Test expand is not included when not specified."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        search_issues(TOKEN, CLOUD_ID, "project = TEST")

        call_kwargs = mock_request.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs.kwargs.get("params")
        assert "expand" not in params

    @patch.object(api_module, '_make_request')
    def test_search_issues_jql_in_params(self, mock_request):
        """Test JQL query is included in request parameters."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        jql = 'project = "PROJ" AND status = "Open"'
        search_issues(TOKEN, CLOUD_ID, jql)

        call_kwargs = mock_request.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs.kwargs.get("params")
        assert params["jql"] == jql

    @patch.object(api_module, '_make_request')
    def test_search_issues_pagination(self, mock_request):
        """Test searching issues across multiple pages."""
        page1_issues = [{"key": f"PROJ-{i}"} for i in range(100)]
        page2_issues = [{"key": f"PROJ-{i}"} for i in range(100, 150)]

        mock_request.side_effect = [
            _mock_response(json_data={"issues": page1_issues, "total": 150}),
            _mock_response(json_data={"issues": page2_issues, "total": 150}),
        ]

        result = search_issues(TOKEN, CLOUD_ID, "project = PROJ")
        assert len(result) == 150
        assert mock_request.call_count == 2

    @patch.object(api_module, '_make_request')
    def test_search_issues_three_pages(self, mock_request):
        """Test pagination across three pages."""
        page1 = [{"key": f"P-{i}"} for i in range(100)]
        page2 = [{"key": f"P-{i}"} for i in range(100, 200)]
        page3 = [{"key": f"P-{i}"} for i in range(200, 250)]

        mock_request.side_effect = [
            _mock_response(json_data={"issues": page1, "total": 250}),
            _mock_response(json_data={"issues": page2, "total": 250}),
            _mock_response(json_data={"issues": page3, "total": 250}),
        ]

        result = search_issues(TOKEN, CLOUD_ID, "project = P")
        assert len(result) == 250
        assert mock_request.call_count == 3

    @patch.object(api_module, '_make_request')
    def test_search_issues_pagination_start_at_increments(self, mock_request):
        """Test startAt increments by maxResults between pages."""
        page1 = [{"key": f"P-{i}"} for i in range(100)]

        mock_request.side_effect = [
            _mock_response(json_data={"issues": page1, "total": 150}),
            _mock_response(json_data={"issues": [], "total": 150}),
        ]

        search_issues(TOKEN, CLOUD_ID, "project = P")

        second_call = mock_request.call_args_list[1]
        params = second_call[1]["params"] if "params" in second_call[1] else second_call.kwargs.get("params")
        assert params["startAt"] == 100

    @patch.object(api_module, '_make_request')
    def test_search_issues_empty_results(self, mock_request):
        """Test empty search results."""
        mock_request.return_value = _mock_response(
            json_data={"issues": [], "total": 0}
        )

        result = search_issues(TOKEN, CLOUD_ID, "project = EMPTY")
        assert result == []

    @patch.object(api_module, '_make_request')
    def test_search_issues_api_error_propagates(self, mock_request):
        """Test JiraAPIError propagates from _make_request."""
        mock_request.side_effect = JiraAPIError("Search failed", status_code=400)

        with pytest.raises(JiraAPIError):
            search_issues(TOKEN, CLOUD_ID, "invalid jql !!!")

    @patch.object(api_module, '_make_request')
    def test_search_issues_auth_error_propagates(self, mock_request):
        """Test JiraAuthError propagates from _make_request."""
        mock_request.side_effect = JiraAuthError("Token expired", status_code=401)

        with pytest.raises(JiraAuthError):
            search_issues(TOKEN, CLOUD_ID, "project = TEST")


# ============================================================================
# list_all_issues_in_project
# ============================================================================

class TestListAllIssuesInProject:
    """Test list_all_issues_in_project."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module, 'search_issues')
    def test_list_all_issues_delegates_to_search(self, mock_search):
        """Test list_all_issues_in_project delegates to search_issues."""
        expected_issues = [{"key": "PROJ-1"}, {"key": "PROJ-2"}]
        mock_search.return_value = expected_issues

        result = list_all_issues_in_project(TOKEN, CLOUD_ID, "PROJ")
        assert result == expected_issues

    @patch.object(api_module, 'search_issues')
    def test_list_all_issues_jql_format(self, mock_search):
        """Test correct JQL is generated for project."""
        mock_search.return_value = []

        list_all_issues_in_project(TOKEN, CLOUD_ID, "MYPROJ")

        mock_search.assert_called_once_with(
            TOKEN, CLOUD_ID, 'project = "MYPROJ" ORDER BY updated DESC'
        )

    @patch.object(api_module, 'search_issues')
    def test_list_all_issues_passes_token_and_cloud_id(self, mock_search):
        """Test access_token and cloud_id are forwarded."""
        mock_search.return_value = []

        list_all_issues_in_project("specific_token", "specific_cloud", "PROJ")

        call_args = mock_search.call_args
        assert call_args[0][0] == "specific_token"
        assert call_args[0][1] == "specific_cloud"

    @patch.object(api_module, 'search_issues')
    def test_list_all_issues_empty_project(self, mock_search):
        """Test empty result for project with no issues."""
        mock_search.return_value = []

        result = list_all_issues_in_project(TOKEN, CLOUD_ID, "EMPTY")
        assert result == []


# ============================================================================
# search_issues_modified_since
# ============================================================================

class TestSearchIssuesModifiedSince:
    """Test search_issues_modified_since."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module, 'search_issues')
    def test_modified_since_delegates_to_search(self, mock_search):
        """Test function delegates to search_issues with JQL."""
        mock_search.return_value = [{"key": "PROJ-1"}]

        result = search_issues_modified_since(
            TOKEN, CLOUD_ID, "PROJ", "2024-01-01T00:00:00Z"
        )
        assert result == [{"key": "PROJ-1"}]
        mock_search.assert_called_once()

    @patch.object(api_module, 'search_issues')
    def test_modified_since_jql_contains_project(self, mock_search):
        """Test generated JQL contains project key."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "MYPROJ", "2024-06-15T12:00:00Z")

        jql = mock_search.call_args[0][2]
        assert 'project = "MYPROJ"' in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_jql_contains_updated_clause(self, mock_search):
        """Test generated JQL contains updated >= clause."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "2024-06-15T12:00:00Z")

        jql = mock_search.call_args[0][2]
        assert "updated >=" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_jql_uses_relative_time(self, mock_search):
        """Test generated JQL uses relative time format (-Xm)."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "2024-01-01T00:00:00Z")

        jql = mock_search.call_args[0][2]
        # Should contain a relative time like -65m or more
        assert "m ORDER BY updated DESC" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_minimum_65_minutes(self, mock_search):
        """Test minimum 65 minutes is enforced regardless of actual delta."""
        from datetime import datetime, timezone
        mock_search.return_value = []

        # Use a timestamp just 5 minutes ago (but function enforces 65 min minimum)
        recent_time = datetime.now(timezone.utc).isoformat()
        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", recent_time)

        jql = mock_search.call_args[0][2]
        assert "-65m" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_with_fractional_seconds(self, mock_search):
        """Test parsing ISO timestamp with fractional seconds."""
        mock_search.return_value = []

        # Timestamp with fractional seconds
        search_issues_modified_since(
            TOKEN, CLOUD_ID, "PROJ", "2024-06-15T12:30:45.123456Z"
        )

        # Should not raise; JQL should be generated
        mock_search.assert_called_once()
        jql = mock_search.call_args[0][2]
        assert 'project = "PROJ"' in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_invalid_date_uses_fallback(self, mock_search):
        """Test invalid date falls back to -65m."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "not-a-date")

        jql = mock_search.call_args[0][2]
        assert "-65m" in jql
        assert 'project = "PROJ"' in jql
        assert "ORDER BY updated DESC" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_empty_date_uses_fallback(self, mock_search):
        """Test empty date string falls back to -65m."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "")

        jql = mock_search.call_args[0][2]
        assert "-65m" in jql

    @patch.object(api_module, 'search_issues')
    def test_modified_since_order_by_updated(self, mock_search):
        """Test JQL orders results by updated DESC."""
        mock_search.return_value = []

        search_issues_modified_since(TOKEN, CLOUD_ID, "PROJ", "2024-01-01T00:00:00Z")

        jql = mock_search.call_args[0][2]
        assert "ORDER BY updated DESC" in jql


# ============================================================================
# get_issue_comments
# ============================================================================

class TestGetIssueComments:
    """Test get_issue_comments."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module, '_make_request')
    def test_get_comments_single_page(self, mock_request):
        """Test retrieving comments that fit on one page."""
        comments = [
            {"id": "c1", "body": "Comment 1", "author": {"displayName": "User A"}},
            {"id": "c2", "body": "Comment 2", "author": {"displayName": "User B"}},
        ]
        mock_request.return_value = _mock_response(
            json_data={"comments": comments, "total": 2}
        )

        result = get_issue_comments(TOKEN, CLOUD_ID, "PROJ-123")
        assert len(result) == 2
        assert result[0]["id"] == "c1"
        assert result[1]["body"] == "Comment 2"

    @patch.object(api_module, '_make_request')
    def test_get_comments_uses_correct_url(self, mock_request):
        """Test request targets the correct issue comment URL."""
        mock_request.return_value = _mock_response(
            json_data={"comments": [], "total": 0}
        )

        get_issue_comments(TOKEN, CLOUD_ID, "PROJ-456")

        expected_url = f"{JIRA_API_BASE}/{CLOUD_ID}/rest/api/3/issue/PROJ-456/comment"
        call_args = mock_request.call_args
        assert call_args[0][1] == expected_url

    @patch.object(api_module, '_make_request')
    def test_get_comments_pagination(self, mock_request):
        """Test retrieving comments across multiple pages."""
        page1_comments = [{"id": f"c{i}"} for i in range(100)]
        page2_comments = [{"id": f"c{i}"} for i in range(100, 130)]

        mock_request.side_effect = [
            _mock_response(json_data={"comments": page1_comments, "total": 130}),
            _mock_response(json_data={"comments": page2_comments, "total": 130}),
        ]

        result = get_issue_comments(TOKEN, CLOUD_ID, "PROJ-1")
        assert len(result) == 130
        assert mock_request.call_count == 2

    @patch.object(api_module, '_make_request')
    def test_get_comments_pagination_start_at_increments(self, mock_request):
        """Test startAt increments correctly across pages."""
        page1 = [{"id": f"c{i}"} for i in range(100)]

        mock_request.side_effect = [
            _mock_response(json_data={"comments": page1, "total": 150}),
            _mock_response(json_data={"comments": [], "total": 150}),
        ]

        get_issue_comments(TOKEN, CLOUD_ID, "PROJ-1")

        second_call = mock_request.call_args_list[1]
        params = second_call[1]["params"] if "params" in second_call[1] else second_call.kwargs.get("params")
        assert params["startAt"] == 100
        assert params["maxResults"] == 100

    @patch.object(api_module, '_make_request')
    def test_get_comments_empty(self, mock_request):
        """Test issue with no comments."""
        mock_request.return_value = _mock_response(
            json_data={"comments": [], "total": 0}
        )

        result = get_issue_comments(TOKEN, CLOUD_ID, "PROJ-1")
        assert result == []

    @patch.object(api_module, '_make_request')
    def test_get_comments_api_error_propagates(self, mock_request):
        """Test JiraAPIError propagates from _make_request."""
        mock_request.side_effect = JiraAPIError("Issue not found", status_code=404)

        with pytest.raises(JiraAPIError):
            get_issue_comments(TOKEN, CLOUD_ID, "NONEXIST-1")

    @patch.object(api_module, '_make_request')
    def test_get_comments_auth_error_propagates(self, mock_request):
        """Test JiraAuthError propagates from _make_request."""
        mock_request.side_effect = JiraAuthError("Token expired", status_code=401)

        with pytest.raises(JiraAuthError):
            get_issue_comments(TOKEN, CLOUD_ID, "PROJ-1")


# ============================================================================
# download_attachment
# ============================================================================

class TestDownloadAttachment:
    """Test download_attachment."""

    @pytest.fixture(autouse=True)
    def reset_mocks(self):
        """Reset state between tests."""
        yield

    @patch.object(api_module, '_make_request')
    def test_download_attachment_success(self, mock_request):
        """Test successful attachment download."""
        content = b"PDF file content bytes"
        mock_resp = _mock_response(content_chunks=[content])
        mock_request.return_value = mock_resp

        result = download_attachment(TOKEN, CLOUD_ID, "att-123")
        assert result == content

    @patch.object(api_module, '_make_request')
    def test_download_attachment_uses_correct_url(self, mock_request):
        """Test request targets the correct attachment content URL."""
        mock_request.return_value = _mock_response(content_chunks=[b"data"])

        download_attachment(TOKEN, CLOUD_ID, "att-456")

        expected_url = f"{JIRA_API_BASE}/{CLOUD_ID}/rest/api/3/attachment/content/att-456"
        call_args = mock_request.call_args
        assert call_args[0][1] == expected_url

    @patch.object(api_module, '_make_request')
    def test_download_attachment_uses_stream(self, mock_request):
        """Test request uses stream=True for downloads."""
        mock_request.return_value = _mock_response(content_chunks=[b"data"])

        download_attachment(TOKEN, CLOUD_ID, "att-1")

        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["stream"] is True or call_kwargs.kwargs.get("stream") is True

    @patch.object(api_module, '_make_request')
    def test_download_attachment_extended_timeout(self, mock_request):
        """Test request uses 300s timeout for large files."""
        mock_request.return_value = _mock_response(content_chunks=[b"data"])

        download_attachment(TOKEN, CLOUD_ID, "att-1")

        call_kwargs = mock_request.call_args
        assert call_kwargs[1]["timeout"] == 300 or call_kwargs.kwargs.get("timeout") == 300

    @patch.object(api_module, '_make_request')
    def test_download_attachment_multiple_chunks(self, mock_request):
        """Test downloading attachment streamed in multiple chunks."""
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        mock_request.return_value = _mock_response(content_chunks=chunks)

        result = download_attachment(TOKEN, CLOUD_ID, "att-1")
        assert result == b"chunk1chunk2chunk3"

    @patch.object(api_module, '_make_request')
    def test_download_attachment_empty_content(self, mock_request):
        """Test downloading empty attachment."""
        mock_request.return_value = _mock_response(content_chunks=[])

        result = download_attachment(TOKEN, CLOUD_ID, "att-1")
        assert result == b""

    @patch.object(api_module, '_make_request')
    def test_download_attachment_large_file(self, mock_request):
        """Test downloading a large attachment in multiple chunks."""
        # Simulate 3 chunks of 8KB each
        chunk = b"x" * 8192
        chunks = [chunk, chunk, chunk]
        mock_request.return_value = _mock_response(content_chunks=chunks)

        result = download_attachment(TOKEN, CLOUD_ID, "att-large")
        assert len(result) == 8192 * 3

    @patch.object(api_module, '_make_request')
    def test_download_attachment_api_error_propagates(self, mock_request):
        """Test JiraAPIError propagates from _make_request."""
        mock_request.side_effect = JiraAPIError("Attachment not found", status_code=404)

        with pytest.raises(JiraAPIError):
            download_attachment(TOKEN, CLOUD_ID, "nonexistent")

    @patch.object(api_module, '_make_request')
    def test_download_attachment_auth_error_propagates(self, mock_request):
        """Test JiraAuthError propagates from _make_request."""
        mock_request.side_effect = JiraAuthError("Token expired", status_code=401)

        with pytest.raises(JiraAuthError):
            download_attachment(TOKEN, CLOUD_ID, "att-1")


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
