# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Confluence API client (confluence_api.py)."""

import importlib.util
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Module loading
# ============================================================================

def _load_api_module():
    """Load or reload the confluence_api module from confluence-sync."""
    if 'confluence_api' in sys.modules:
        del sys.modules['confluence_api']

    api_path = os.path.join(os.path.dirname(__file__), '../../confluence-sync/confluence_api.py')
    spec = importlib.util.spec_from_file_location("confluence_api", api_path)
    api_module = importlib.util.module_from_spec(spec)
    sys.modules['confluence_api'] = api_module
    spec.loader.exec_module(api_module)
    return api_module


api_module = _load_api_module()

# Import from module
_make_request = api_module._make_request
_paginate = api_module._paginate
get_accessible_resources = api_module.get_accessible_resources
list_all_spaces = api_module.list_all_spaces
list_pages_in_space = api_module.list_pages_in_space
get_page_content = api_module.get_page_content
list_page_attachments = api_module.list_page_attachments
search_pages_modified_since = api_module.search_pages_modified_since
download_attachment = api_module.download_attachment
ConfluenceAPIError = api_module.ConfluenceAPIError
ConfluenceAuthError = api_module.ConfluenceAuthError
CONFLUENCE_API_BASE = api_module.CONFLUENCE_API_BASE
REQUEST_TIMEOUT = api_module.REQUEST_TIMEOUT
DOWNLOAD_TIMEOUT = api_module.DOWNLOAD_TIMEOUT


# ============================================================================
# Helpers
# ============================================================================

def _mock_urlopen_response(data, is_bytes=False):
    """Create a mock urlopen context-manager response."""
    mock_response = MagicMock()
    if is_bytes:
        mock_response.read.return_value = data
    else:
        mock_response.read.return_value = json.dumps(data).encode("utf-8")
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def _mock_http_error(code, msg="Error", body=b"error body"):
    """Create a mock HTTPError."""
    return urllib.error.HTTPError(
        url="https://example.com",
        code=code,
        msg=msg,
        hdrs={},
        fp=MagicMock(read=MagicMock(return_value=body)),
    )


# ============================================================================
# Exception Classes
# ============================================================================

class TestExceptions:
    """Test custom exception classes."""

    def test_confluence_api_error_attributes(self):
        """Test ConfluenceAPIError stores status code and response."""
        err = ConfluenceAPIError("Test error", status_code=500, response="body")
        assert str(err) == "Test error"
        assert err.status_code == 500
        assert err.response == "body"

    def test_confluence_api_error_default_attributes(self):
        """Test ConfluenceAPIError defaults to None for optional fields."""
        err = ConfluenceAPIError("Simple error")
        assert str(err) == "Simple error"
        assert err.status_code is None
        assert err.response is None

    def test_confluence_auth_error_is_api_error(self):
        """Test ConfluenceAuthError inherits from ConfluenceAPIError."""
        err = ConfluenceAuthError("Auth failed", status_code=401)
        assert isinstance(err, ConfluenceAPIError)
        assert err.status_code == 401

    def test_confluence_auth_error_with_response(self):
        """Test ConfluenceAuthError stores response body."""
        err = ConfluenceAuthError(
            "Forbidden", status_code=403, response='{"error": "forbidden"}'
        )
        assert err.status_code == 403
        assert err.response == '{"error": "forbidden"}'


# ============================================================================
# _make_request
# ============================================================================

class TestMakeRequest:
    """Test the core HTTP request function."""

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_success(self, mock_urlopen):
        """Test successful API request returns parsed JSON."""
        mock_urlopen.return_value = _mock_urlopen_response({"key": "value"})

        result = _make_request("token", "https://api.atlassian.com/test")
        assert result == {"key": "value"}

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_sets_auth_header(self, mock_urlopen):
        """Test request includes Bearer token in Authorization header."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        _make_request("my_access_token", "https://api.atlassian.com/test")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my_access_token"

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_sets_accept_header(self, mock_urlopen):
        """Test request includes Accept: application/json header."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        _make_request("token", "https://api.atlassian.com/test")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_header("Accept") == "application/json"

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_uses_correct_method(self, mock_urlopen):
        """Test request uses the specified HTTP method."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        _make_request("token", "https://api.atlassian.com/test", method="POST")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_method() == "POST"

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_default_method_is_get(self, mock_urlopen):
        """Test request defaults to GET method."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        _make_request("token", "https://api.atlassian.com/test")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_method() == "GET"

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_uses_default_timeout(self, mock_urlopen):
        """Test request uses REQUEST_TIMEOUT by default."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        _make_request("token", "https://api.atlassian.com/test")
        call_args = mock_urlopen.call_args
        assert call_args[1].get("timeout") == REQUEST_TIMEOUT or call_args[0][1] == REQUEST_TIMEOUT

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_uses_custom_timeout(self, mock_urlopen):
        """Test request uses provided timeout value."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        _make_request("token", "https://api.atlassian.com/test", timeout=30)
        call_args = mock_urlopen.call_args
        # urlopen is called with (request, timeout=30) - check keyword or positional
        assert 30 in call_args[0] or call_args[1].get("timeout") == 30

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_401_raises_auth_error(self, mock_urlopen):
        """Test 401 response raises ConfluenceAuthError."""
        mock_urlopen.side_effect = _mock_http_error(401, "Unauthorized", b'{"error": "unauthorized"}')

        with pytest.raises(ConfluenceAuthError) as exc_info:
            _make_request("bad_token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 401
        assert "Authentication failed" in str(exc_info.value)

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_403_raises_auth_error(self, mock_urlopen):
        """Test 403 response raises ConfluenceAuthError."""
        mock_urlopen.side_effect = _mock_http_error(403, "Forbidden", b'{"error": "forbidden"}')

        with pytest.raises(ConfluenceAuthError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 403
        assert "Authentication failed" in str(exc_info.value)

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_auth_error_includes_response_body(self, mock_urlopen):
        """Test auth error includes the response body."""
        body = b'{"message": "Token expired"}'
        mock_urlopen.side_effect = _mock_http_error(401, "Unauthorized", body)

        with pytest.raises(ConfluenceAuthError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.response == '{"message": "Token expired"}'

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_404_raises_api_error(self, mock_urlopen):
        """Test 404 response raises ConfluenceAPIError (not auth error)."""
        mock_urlopen.side_effect = _mock_http_error(404, "Not Found", b"not found")

        with pytest.raises(ConfluenceAPIError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 404
        assert not isinstance(exc_info.value, ConfluenceAuthError)

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_500_raises_api_error(self, mock_urlopen):
        """Test 500 response raises ConfluenceAPIError."""
        mock_urlopen.side_effect = _mock_http_error(500, "Server Error", b"Internal error")

        with pytest.raises(ConfluenceAPIError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 500
        assert not isinstance(exc_info.value, ConfluenceAuthError)
        assert exc_info.value.response == "Internal error"

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_http_error_no_fp(self, mock_urlopen):
        """Test HTTP error with no response body (fp is None)."""
        error = urllib.error.HTTPError(
            url="https://example.com",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = error

        with pytest.raises(ConfluenceAPIError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 502
        assert exc_info.value.response == ""

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_429_raises_api_error(self, mock_urlopen):
        """Test 429 rate limit response raises ConfluenceAPIError."""
        mock_urlopen.side_effect = _mock_http_error(429, "Too Many Requests", b"rate limited")

        with pytest.raises(ConfluenceAPIError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 429
        assert not isinstance(exc_info.value, ConfluenceAuthError)

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_network_error(self, mock_urlopen):
        """Test network error raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with pytest.raises(ConfluenceAPIError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert "Network error" in str(exc_info.value)

    @patch('confluence_api.urllib.request.urlopen')
    def test_make_request_url_error_timeout(self, mock_urlopen):
        """Test timeout as URLError raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.URLError("timed out")

        with pytest.raises(ConfluenceAPIError) as exc_info:
            _make_request("token", "https://api.atlassian.com/test")
        assert "Network error" in str(exc_info.value)


# ============================================================================
# _paginate
# ============================================================================

class TestPaginate:
    """Test cursor-based pagination helper."""

    @patch('confluence_api._make_request')
    def test_paginate_single_page(self, mock_request):
        """Test pagination with a single page of results."""
        mock_request.return_value = {
            "results": [{"id": "1"}, {"id": "2"}],
            "_links": {},
        }

        result = _paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 2
        assert result[0]["id"] == "1"
        assert mock_request.call_count == 1

    @patch('confluence_api._make_request')
    def test_paginate_multiple_pages(self, mock_request):
        """Test pagination across multiple pages."""
        mock_request.side_effect = [
            {
                "results": [{"id": "1"}],
                "_links": {"next": "/rest/api/v2/spaces?cursor=abc&limit=100"},
            },
            {
                "results": [{"id": "2"}],
                "_links": {"next": "/rest/api/v2/spaces?cursor=def&limit=100"},
            },
            {
                "results": [{"id": "3"}],
                "_links": {},
            },
        ]

        result = _paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 3
        assert mock_request.call_count == 3

    @patch('confluence_api._make_request')
    def test_paginate_next_link_relative(self, mock_request):
        """Test pagination builds full URL from relative next link."""
        mock_request.side_effect = [
            {
                "results": [{"id": "1"}],
                "_links": {"next": "/ex/confluence/cloud123/wiki/api/v2/spaces?cursor=abc"},
            },
            {
                "results": [{"id": "2"}],
                "_links": {},
            },
        ]

        _paginate("token", "https://api.atlassian.com/test")
        # Second call should use full URL with atlassian.com prefix
        second_call_url = mock_request.call_args_list[1][0][1]
        assert second_call_url == "https://api.atlassian.com/ex/confluence/cloud123/wiki/api/v2/spaces?cursor=abc"

    @patch('confluence_api._make_request')
    def test_paginate_next_link_absolute(self, mock_request):
        """Test pagination uses absolute next link directly."""
        absolute_url = "https://api.atlassian.com/ex/confluence/cloud123/wiki/api/v2/spaces?cursor=abc"
        mock_request.side_effect = [
            {
                "results": [{"id": "1"}],
                "_links": {"next": absolute_url},
            },
            {
                "results": [{"id": "2"}],
                "_links": {},
            },
        ]

        _paginate("token", "https://api.atlassian.com/test")
        second_call_url = mock_request.call_args_list[1][0][1]
        assert second_call_url == absolute_url

    @patch('confluence_api._make_request')
    def test_paginate_empty_results(self, mock_request):
        """Test pagination with no results."""
        mock_request.return_value = {
            "results": [],
            "_links": {},
        }

        result = _paginate("token", "https://api.atlassian.com/test")
        assert result == []

    @patch('confluence_api._make_request')
    def test_paginate_custom_items_key(self, mock_request):
        """Test pagination with custom items key."""
        mock_request.return_value = {
            "data": [{"id": "1"}, {"id": "2"}],
            "_links": {},
        }

        result = _paginate("token", "https://api.atlassian.com/test", items_key="data")
        assert len(result) == 2

    @patch('confluence_api._make_request')
    def test_paginate_custom_limit(self, mock_request):
        """Test pagination appends custom limit parameter."""
        mock_request.return_value = {
            "results": [],
            "_links": {},
        }

        _paginate("token", "https://api.atlassian.com/test", limit=50)
        called_url = mock_request.call_args[0][1]
        assert "limit=50" in called_url

    @patch('confluence_api._make_request')
    def test_paginate_url_without_query_params(self, mock_request):
        """Test pagination adds ? separator for URL without query params."""
        mock_request.return_value = {
            "results": [],
            "_links": {},
        }

        _paginate("token", "https://api.atlassian.com/test")
        called_url = mock_request.call_args[0][1]
        assert "?limit=100" in called_url

    @patch('confluence_api._make_request')
    def test_paginate_url_with_existing_query_params(self, mock_request):
        """Test pagination adds & separator for URL with existing query params."""
        mock_request.return_value = {
            "results": [],
            "_links": {},
        }

        _paginate("token", "https://api.atlassian.com/test?foo=bar")
        called_url = mock_request.call_args[0][1]
        assert "&limit=100" in called_url
        assert "?foo=bar" in called_url

    @patch('confluence_api._make_request')
    def test_paginate_missing_items_key_returns_empty(self, mock_request):
        """Test pagination when response lacks the items key."""
        mock_request.return_value = {
            "_links": {},
        }

        result = _paginate("token", "https://api.atlassian.com/test")
        assert result == []

    @patch('confluence_api._make_request')
    def test_paginate_no_links_key(self, mock_request):
        """Test pagination when response has no _links key."""
        mock_request.return_value = {
            "results": [{"id": "1"}],
        }

        result = _paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 1

    @patch('confluence_api._make_request')
    def test_paginate_propagates_auth_error(self, mock_request):
        """Test pagination propagates ConfluenceAuthError from _make_request."""
        mock_request.side_effect = ConfluenceAuthError(
            "Authentication failed: 401", status_code=401
        )

        with pytest.raises(ConfluenceAuthError):
            _paginate("bad_token", "https://api.atlassian.com/test")

    @patch('confluence_api._make_request')
    def test_paginate_propagates_api_error(self, mock_request):
        """Test pagination propagates ConfluenceAPIError from _make_request."""
        mock_request.side_effect = ConfluenceAPIError(
            "API error: 500", status_code=500
        )

        with pytest.raises(ConfluenceAPIError):
            _paginate("token", "https://api.atlassian.com/test")


# ============================================================================
# get_accessible_resources
# ============================================================================

class TestGetAccessibleResources:
    """Test get_accessible_resources."""

    @patch('confluence_api._make_request')
    def test_get_accessible_resources_filters_confluence(self, mock_request):
        """Test filtering resources to Confluence only via scopes."""
        mock_request.return_value = [
            {"id": "cloud1", "url": "https://site1.atlassian.net", "name": "Site1", "scopes": ["confluence"]},
            {"id": "cloud2", "url": "https://site2.atlassian.net", "name": "Site2", "scopes": ["jira"]},
            {"id": "cloud3", "url": "https://site3.atlassian.net", "name": "Site3", "scopes": ["confluence", "jira"]},
        ]

        result = get_accessible_resources("token")
        assert len(result) == 2
        assert result[0]["id"] == "cloud1"
        assert result[1]["id"] == "cloud3"

    @patch('confluence_api._make_request')
    def test_get_accessible_resources_filters_by_url(self, mock_request):
        """Test filtering resources to Confluence by URL matching."""
        mock_request.return_value = [
            {"id": "cloud1", "url": "https://confluence.example.com", "name": "Confluence Site", "scopes": []},
            {"id": "cloud2", "url": "https://jira.example.com", "name": "Jira Site", "scopes": []},
        ]

        result = get_accessible_resources("token")
        assert len(result) == 1
        assert result[0]["id"] == "cloud1"

    @patch('confluence_api._make_request')
    def test_get_accessible_resources_empty(self, mock_request):
        """Test no accessible resources returns empty list."""
        mock_request.return_value = []

        result = get_accessible_resources("token")
        assert result == []

    @patch('confluence_api._make_request')
    def test_get_accessible_resources_no_confluence(self, mock_request):
        """Test returns empty when no Confluence sites found."""
        mock_request.return_value = [
            {"id": "cloud1", "url": "https://jira.example.com", "name": "Jira Only", "scopes": ["jira"]},
        ]

        result = get_accessible_resources("token")
        assert result == []

    @patch('confluence_api._make_request')
    def test_get_accessible_resources_calls_correct_url(self, mock_request):
        """Test the correct Atlassian OAuth endpoint is called."""
        mock_request.return_value = []

        get_accessible_resources("my_token")
        mock_request.assert_called_once_with(
            "my_token",
            "https://api.atlassian.com/oauth/token/accessible-resources",
        )

    @patch('confluence_api._make_request')
    def test_get_accessible_resources_missing_scopes_key(self, mock_request):
        """Test resource without scopes key does not crash."""
        mock_request.return_value = [
            {"id": "cloud1", "url": "https://confluence.example.com", "name": "Site"},
        ]

        result = get_accessible_resources("token")
        # Should match via URL containing 'confluence'
        assert len(result) == 1

    @patch('confluence_api._make_request')
    def test_get_accessible_resources_propagates_auth_error(self, mock_request):
        """Test auth error from _make_request is propagated."""
        mock_request.side_effect = ConfluenceAuthError("Auth failed", status_code=401)

        with pytest.raises(ConfluenceAuthError):
            get_accessible_resources("bad_token")


# ============================================================================
# list_all_spaces
# ============================================================================

class TestListAllSpaces:
    """Test list_all_spaces."""

    @patch('confluence_api._paginate')
    def test_list_all_spaces_success(self, mock_paginate):
        """Test listing all spaces in a site."""
        mock_paginate.return_value = [
            {"id": "s1", "key": "DEV", "name": "Development", "type": "global"},
            {"id": "s2", "key": "OPS", "name": "Operations", "type": "global"},
        ]

        result = list_all_spaces("token", "cloud123")
        assert len(result) == 2
        assert result[0]["key"] == "DEV"

    @patch('confluence_api._paginate')
    def test_list_all_spaces_calls_correct_url(self, mock_paginate):
        """Test the correct API URL is constructed."""
        mock_paginate.return_value = []

        list_all_spaces("token", "cloud-abc-123")
        mock_paginate.assert_called_once_with(
            "token",
            f"{CONFLUENCE_API_BASE}/cloud-abc-123/wiki/api/v2/spaces",
        )

    @patch('confluence_api._paginate')
    def test_list_all_spaces_empty(self, mock_paginate):
        """Test listing spaces when none exist."""
        mock_paginate.return_value = []

        result = list_all_spaces("token", "cloud123")
        assert result == []


# ============================================================================
# list_pages_in_space
# ============================================================================

class TestListPagesInSpace:
    """Test list_pages_in_space."""

    @patch('confluence_api._paginate')
    def test_list_pages_in_space_success(self, mock_paginate):
        """Test listing pages in a space."""
        mock_paginate.return_value = [
            {"id": "p1", "title": "Welcome Page", "status": "current"},
            {"id": "p2", "title": "Architecture Guide", "status": "current"},
        ]

        result = list_pages_in_space("token", "cloud123", "space456")
        assert len(result) == 2
        assert result[0]["title"] == "Welcome Page"

    @patch('confluence_api._paginate')
    def test_list_pages_in_space_calls_correct_url(self, mock_paginate):
        """Test the correct API URL is constructed with space ID."""
        mock_paginate.return_value = []

        list_pages_in_space("token", "cloud-abc", "space-xyz")
        mock_paginate.assert_called_once_with(
            "token",
            f"{CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/spaces/space-xyz/pages",
        )

    @patch('confluence_api._paginate')
    def test_list_pages_in_space_empty(self, mock_paginate):
        """Test listing pages when space is empty."""
        mock_paginate.return_value = []

        result = list_pages_in_space("token", "cloud123", "space456")
        assert result == []


# ============================================================================
# get_page_content
# ============================================================================

class TestGetPageContent:
    """Test get_page_content."""

    @patch('confluence_api._make_request')
    def test_get_page_content_success(self, mock_request):
        """Test fetching page content in storage format."""
        mock_request.return_value = {
            "id": "page123",
            "title": "Test Page",
            "body": {
                "storage": {
                    "value": "<p>Page content</p>",
                    "representation": "storage",
                }
            },
        }

        result = get_page_content("token", "cloud123", "page123")
        assert result["title"] == "Test Page"
        assert result["body"]["storage"]["value"] == "<p>Page content</p>"

    @patch('confluence_api._make_request')
    def test_get_page_content_calls_correct_url(self, mock_request):
        """Test the correct API URL with body-format=storage."""
        mock_request.return_value = {}

        get_page_content("token", "cloud-abc", "page-xyz")
        mock_request.assert_called_once_with(
            "token",
            f"{CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/pages/page-xyz?body-format=storage",
        )

    @patch('confluence_api._make_request')
    def test_get_page_content_propagates_auth_error(self, mock_request):
        """Test auth error is propagated when token is invalid."""
        mock_request.side_effect = ConfluenceAuthError("Auth failed", status_code=401)

        with pytest.raises(ConfluenceAuthError):
            get_page_content("bad_token", "cloud123", "page123")

    @patch('confluence_api._make_request')
    def test_get_page_content_propagates_not_found(self, mock_request):
        """Test API error is propagated when page does not exist."""
        mock_request.side_effect = ConfluenceAPIError(
            "API error: 404", status_code=404
        )

        with pytest.raises(ConfluenceAPIError) as exc_info:
            get_page_content("token", "cloud123", "nonexistent")
        assert exc_info.value.status_code == 404


# ============================================================================
# list_page_attachments
# ============================================================================

class TestListPageAttachments:
    """Test list_page_attachments."""

    @patch('confluence_api._paginate')
    def test_list_page_attachments_success(self, mock_paginate):
        """Test listing attachments on a page."""
        mock_paginate.return_value = [
            {"id": "att1", "title": "diagram.png", "mediaType": "image/png"},
            {"id": "att2", "title": "report.pdf", "mediaType": "application/pdf"},
        ]

        result = list_page_attachments("token", "cloud123", "page456")
        assert len(result) == 2
        assert result[0]["title"] == "diagram.png"

    @patch('confluence_api._paginate')
    def test_list_page_attachments_calls_correct_url(self, mock_paginate):
        """Test the correct API URL is constructed with page ID."""
        mock_paginate.return_value = []

        list_page_attachments("token", "cloud-abc", "page-xyz")
        mock_paginate.assert_called_once_with(
            "token",
            f"{CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/pages/page-xyz/attachments",
        )

    @patch('confluence_api._paginate')
    def test_list_page_attachments_empty(self, mock_paginate):
        """Test listing attachments when page has none."""
        mock_paginate.return_value = []

        result = list_page_attachments("token", "cloud123", "page456")
        assert result == []


# ============================================================================
# search_pages_modified_since
# ============================================================================

class TestSearchPagesModifiedSince:
    """Test search_pages_modified_since."""

    @patch('confluence_api._paginate')
    def test_search_pages_modified_since_success(self, mock_paginate):
        """Test searching for pages modified since a date."""
        mock_paginate.return_value = [
            {"id": "p1", "title": "Updated Page", "lastModified": "2024-01-15T10:00:00Z"},
        ]

        result = search_pages_modified_since("token", "cloud123", "DEV", "2024-01-01")
        assert len(result) == 1
        assert result[0]["title"] == "Updated Page"

    @patch('confluence_api._paginate')
    def test_search_pages_modified_since_constructs_cql(self, mock_paginate):
        """Test CQL query is correctly URL-encoded in the request."""
        mock_paginate.return_value = []

        search_pages_modified_since("token", "cloud123", "OPS", "2024-06-15")
        called_url = mock_paginate.call_args[0][1]

        # Verify URL structure
        assert f"{CONFLUENCE_API_BASE}/cloud123/wiki/rest/api/content/search?cql=" in called_url
        # Verify CQL is URL-encoded
        assert "OPS" in called_url or "OPS" in urllib.parse.unquote(called_url)
        assert "2024-06-15" in called_url or "2024-06-15" in urllib.parse.unquote(called_url)

    @patch('confluence_api._paginate')
    def test_search_pages_modified_since_cql_content(self, mock_paginate):
        """Test CQL query contains correct space key, type, and date filter."""
        mock_paginate.return_value = []

        search_pages_modified_since("token", "cloud123", "MYSPACE", "2024-03-20")
        called_url = mock_paginate.call_args[0][1]
        decoded_url = urllib.parse.unquote(called_url)

        assert 'space = "MYSPACE"' in decoded_url
        assert "type = page" in decoded_url
        assert 'lastModified >= "2024-03-20"' in decoded_url

    @patch('confluence_api._paginate')
    def test_search_pages_modified_since_empty_results(self, mock_paginate):
        """Test search returns empty when no pages match."""
        mock_paginate.return_value = []

        result = search_pages_modified_since("token", "cloud123", "DEV", "2099-01-01")
        assert result == []

    @patch('confluence_api._paginate')
    def test_search_pages_modified_since_uses_rest_api(self, mock_paginate):
        """Test search uses v1 REST API (content/search) not v2 API."""
        mock_paginate.return_value = []

        search_pages_modified_since("token", "cloud123", "DEV", "2024-01-01")
        called_url = mock_paginate.call_args[0][1]
        assert "/wiki/rest/api/content/search" in called_url


# ============================================================================
# download_attachment
# ============================================================================

class TestDownloadAttachment:
    """Test download_attachment."""

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_success_with_page_id(self, mock_urlopen):
        """Test downloading attachment with page ID (OAuth-compatible path)."""
        mock_urlopen.return_value = _mock_urlopen_response(b"PDF content", is_bytes=True)

        result = download_attachment("token", "cloud123", "att456", page_id="page789")
        assert result == b"PDF content"

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_strips_att_prefix(self, mock_urlopen):
        """Test attachment ID with 'att' prefix is stripped for v1 REST endpoint."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        download_attachment("token", "cloud123", "att456789", page_id="page123")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        url = request.full_url
        assert "/attachment/456789/download" in url
        assert "att456789" not in url

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_numeric_id(self, mock_urlopen):
        """Test attachment ID that is already numeric (no 'att' prefix)."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        download_attachment("token", "cloud123", "123456", page_id="page789")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        url = request.full_url
        assert "/attachment/123456/download" in url

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_with_page_id_url(self, mock_urlopen):
        """Test OAuth-compatible URL is constructed with page ID."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        download_attachment("token", "cloud-abc", "att999", page_id="page-xyz")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        url = request.full_url
        expected = f"{CONFLUENCE_API_BASE}/cloud-abc/wiki/rest/api/content/page-xyz/child/attachment/999/download"
        assert url == expected

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_without_page_id(self, mock_urlopen):
        """Test fallback URL when page ID is not provided."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        download_attachment("token", "cloud123", "att789")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        url = request.full_url
        expected = f"{CONFLUENCE_API_BASE}/cloud123/wiki/rest/api/content/789/download"
        assert url == expected

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_without_page_id_numeric(self, mock_urlopen):
        """Test fallback URL with numeric attachment ID (no prefix)."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        download_attachment("token", "cloud123", "555")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        url = request.full_url
        expected = f"{CONFLUENCE_API_BASE}/cloud123/wiki/rest/api/content/555/download"
        assert url == expected

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_sets_auth_header(self, mock_urlopen):
        """Test download request includes Bearer token."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        download_attachment("my_token", "cloud123", "att1", page_id="p1")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my_token"

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_uses_download_timeout(self, mock_urlopen):
        """Test download uses the longer DOWNLOAD_TIMEOUT."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        download_attachment("token", "cloud123", "att1", page_id="p1")
        call_args = mock_urlopen.call_args
        assert DOWNLOAD_TIMEOUT in call_args[0] or call_args[1].get("timeout") == DOWNLOAD_TIMEOUT

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_http_error(self, mock_urlopen):
        """Test download failure raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )

        with pytest.raises(ConfluenceAPIError) as exc_info:
            download_attachment("token", "cloud123", "att1", page_id="p1")
        assert exc_info.value.status_code == 404

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_500_error(self, mock_urlopen):
        """Test server error during download raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )

        with pytest.raises(ConfluenceAPIError) as exc_info:
            download_attachment("token", "cloud123", "att1", page_id="p1")
        assert exc_info.value.status_code == 500

    @patch('confluence_api.urllib.request.urlopen')
    def test_download_attachment_returns_bytes(self, mock_urlopen):
        """Test download returns raw bytes content."""
        binary_content = b"\x89PNG\r\n\x1a\n\x00\x00"
        mock_urlopen.return_value = _mock_urlopen_response(binary_content, is_bytes=True)

        result = download_attachment("token", "cloud123", "att1", page_id="p1")
        assert isinstance(result, bytes)
        assert result == binary_content


# ============================================================================
# Constants
# ============================================================================

class TestConstants:
    """Test module-level constants."""

    def test_confluence_api_base_url(self):
        """Test the Confluence API base URL is correct."""
        assert CONFLUENCE_API_BASE == "https://api.atlassian.com/ex/confluence"

    def test_request_timeout(self):
        """Test the default request timeout."""
        assert REQUEST_TIMEOUT == 60

    def test_download_timeout(self):
        """Test the download timeout is longer than request timeout."""
        assert DOWNLOAD_TIMEOUT == 300
        assert DOWNLOAD_TIMEOUT > REQUEST_TIMEOUT
