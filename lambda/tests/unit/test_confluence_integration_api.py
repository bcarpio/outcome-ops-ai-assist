"""
Unit tests for Confluence API clients from confluence-integration and file-ingestion.

Tests the confluence_api.py modules loaded from:
  - lambda/confluence-integration/confluence_api.py  (ci_module)
  - lambda/file-ingestion/confluence_api.py          (fi_module)

These are near-duplicates of confluence-sync/confluence_api.py (already tested in
test_confluence_api.py), but have distinct differences in get_accessible_resources
filtering logic and download_attachment implementation. This file covers both
modules, testing ci_module thoroughly and fi_module with targeted smoke tests
plus coverage of its unique code paths.
"""

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Module loading
# ============================================================================

def _load_module(mod_name, rel_path):
    """Load a module by file path, avoiding cache collisions."""
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    path = os.path.join(os.path.dirname(__file__), rel_path)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


ci_module = _load_module("confluence_api_ci", "../../confluence-integration/confluence_api.py")
fi_module = _load_module("confluence_api_fi", "../../file-ingestion/confluence_api.py")


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
# confluence-integration (ci_module): Exception Classes
# ============================================================================

class TestCIExceptions:
    """Test custom exception classes from confluence-integration."""

    def test_api_error_attributes(self):
        """Test ConfluenceAPIError stores status code and response."""
        err = ci_module.ConfluenceAPIError("Test error", status_code=500, response="body")
        assert str(err) == "Test error"
        assert err.status_code == 500
        assert err.response == "body"

    def test_api_error_default_attributes(self):
        """Test ConfluenceAPIError defaults to None for optional fields."""
        err = ci_module.ConfluenceAPIError("Simple error")
        assert str(err) == "Simple error"
        assert err.status_code is None
        assert err.response is None

    def test_auth_error_is_subclass_of_api_error(self):
        """Test ConfluenceAuthError inherits from ConfluenceAPIError."""
        err = ci_module.ConfluenceAuthError("Auth failed", status_code=401)
        assert isinstance(err, ci_module.ConfluenceAPIError)
        assert err.status_code == 401

    def test_auth_error_with_response(self):
        """Test ConfluenceAuthError stores response body."""
        err = ci_module.ConfluenceAuthError(
            "Forbidden", status_code=403, response='{"error": "forbidden"}'
        )
        assert err.status_code == 403
        assert err.response == '{"error": "forbidden"}'


# ============================================================================
# confluence-integration (ci_module): _make_request
# ============================================================================

class TestCIMakeRequest:
    """Test _make_request from confluence-integration."""

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_success_returns_parsed_json(self, mock_urlopen):
        """Test successful API request returns parsed JSON."""
        mock_urlopen.return_value = _mock_urlopen_response({"key": "value"})

        result = ci_module._make_request("token", "https://api.atlassian.com/test")
        assert result == {"key": "value"}

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_sets_auth_header(self, mock_urlopen):
        """Test request includes Bearer token in Authorization header."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        ci_module._make_request("my_access_token", "https://api.atlassian.com/test")
        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my_access_token"

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_sets_accept_header(self, mock_urlopen):
        """Test request includes Accept: application/json header."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        ci_module._make_request("token", "https://api.atlassian.com/test")
        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Accept") == "application/json"

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_uses_specified_method(self, mock_urlopen):
        """Test request uses the specified HTTP method."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        ci_module._make_request("token", "https://api.atlassian.com/test", method="POST")
        request = mock_urlopen.call_args[0][0]
        assert request.get_method() == "POST"

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_default_method_is_get(self, mock_urlopen):
        """Test request defaults to GET method."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        ci_module._make_request("token", "https://api.atlassian.com/test")
        request = mock_urlopen.call_args[0][0]
        assert request.get_method() == "GET"

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_uses_default_timeout(self, mock_urlopen):
        """Test request uses REQUEST_TIMEOUT by default."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        ci_module._make_request("token", "https://api.atlassian.com/test")
        call_args = mock_urlopen.call_args
        assert call_args[1].get("timeout") == ci_module.REQUEST_TIMEOUT or \
            ci_module.REQUEST_TIMEOUT in call_args[0]

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_uses_custom_timeout(self, mock_urlopen):
        """Test request uses provided timeout value."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        ci_module._make_request("token", "https://api.atlassian.com/test", timeout=30)
        call_args = mock_urlopen.call_args
        assert 30 in call_args[0] or call_args[1].get("timeout") == 30

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_401_raises_auth_error(self, mock_urlopen):
        """Test 401 response raises ConfluenceAuthError."""
        mock_urlopen.side_effect = _mock_http_error(401, "Unauthorized")

        with pytest.raises(ci_module.ConfluenceAuthError) as exc_info:
            ci_module._make_request("bad_token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 401
        assert "Authentication failed" in str(exc_info.value)

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_403_raises_auth_error(self, mock_urlopen):
        """Test 403 response raises ConfluenceAuthError."""
        mock_urlopen.side_effect = _mock_http_error(403, "Forbidden")

        with pytest.raises(ci_module.ConfluenceAuthError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 403

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_auth_error_includes_response_body(self, mock_urlopen):
        """Test auth error includes the response body."""
        body = b'{"message": "Token expired"}'
        mock_urlopen.side_effect = _mock_http_error(401, "Unauthorized", body)

        with pytest.raises(ci_module.ConfluenceAuthError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.response == '{"message": "Token expired"}'

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_404_raises_api_error_not_auth_error(self, mock_urlopen):
        """Test 404 response raises ConfluenceAPIError, not auth error."""
        mock_urlopen.side_effect = _mock_http_error(404, "Not Found", b"not found")

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 404
        assert not isinstance(exc_info.value, ci_module.ConfluenceAuthError)

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_500_raises_api_error(self, mock_urlopen):
        """Test 500 response raises ConfluenceAPIError with response body."""
        mock_urlopen.side_effect = _mock_http_error(500, "Server Error", b"Internal error")

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 500
        assert exc_info.value.response == "Internal error"

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_http_error_no_fp(self, mock_urlopen):
        """Test HTTP error with no response body (fp is None)."""
        error = urllib.error.HTTPError(
            url="https://example.com", code=502, msg="Bad Gateway",
            hdrs={}, fp=None,
        )
        mock_urlopen.side_effect = error

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 502
        assert exc_info.value.response == ""

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_429_raises_api_error(self, mock_urlopen):
        """Test 429 rate limit response raises ConfluenceAPIError."""
        mock_urlopen.side_effect = _mock_http_error(429, "Too Many Requests", b"rate limited")

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 429
        assert not isinstance(exc_info.value, ci_module.ConfluenceAuthError)

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_network_error(self, mock_urlopen):
        """Test network error raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert "Network error" in str(exc_info.value)

    @patch.object(ci_module.urllib.request, "urlopen")
    def test_timeout_url_error(self, mock_urlopen):
        """Test timeout as URLError raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.URLError("timed out")

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module._make_request("token", "https://api.atlassian.com/test")
        assert "Network error" in str(exc_info.value)


# ============================================================================
# confluence-integration (ci_module): _paginate
# ============================================================================

class TestCIPaginate:
    """Test cursor-based pagination helper from confluence-integration."""

    @patch.object(ci_module, "_make_request")
    def test_single_page(self, mock_request):
        """Test pagination with a single page of results."""
        mock_request.return_value = {
            "results": [{"id": "1"}, {"id": "2"}],
            "_links": {},
        }

        result = ci_module._paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 2
        assert result[0]["id"] == "1"
        assert mock_request.call_count == 1

    @patch.object(ci_module, "_make_request")
    def test_multiple_pages(self, mock_request):
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

        result = ci_module._paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 3
        assert mock_request.call_count == 3

    @patch.object(ci_module, "_make_request")
    def test_relative_next_link_gets_full_url(self, mock_request):
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

        ci_module._paginate("token", "https://api.atlassian.com/test")
        second_call_url = mock_request.call_args_list[1][0][1]
        assert second_call_url == "https://api.atlassian.com/ex/confluence/cloud123/wiki/api/v2/spaces?cursor=abc"

    @patch.object(ci_module, "_make_request")
    def test_absolute_next_link_used_directly(self, mock_request):
        """Test pagination uses absolute next link as-is."""
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

        ci_module._paginate("token", "https://api.atlassian.com/test")
        second_call_url = mock_request.call_args_list[1][0][1]
        assert second_call_url == absolute_url

    @patch.object(ci_module, "_make_request")
    def test_empty_results(self, mock_request):
        """Test pagination with no results."""
        mock_request.return_value = {"results": [], "_links": {}}

        result = ci_module._paginate("token", "https://api.atlassian.com/test")
        assert result == []

    @patch.object(ci_module, "_make_request")
    def test_custom_items_key(self, mock_request):
        """Test pagination with custom items key."""
        mock_request.return_value = {"data": [{"id": "1"}, {"id": "2"}], "_links": {}}

        result = ci_module._paginate("token", "https://api.atlassian.com/test", items_key="data")
        assert len(result) == 2

    @patch.object(ci_module, "_make_request")
    def test_custom_limit(self, mock_request):
        """Test pagination appends custom limit parameter."""
        mock_request.return_value = {"results": [], "_links": {}}

        ci_module._paginate("token", "https://api.atlassian.com/test", limit=50)
        called_url = mock_request.call_args[0][1]
        assert "limit=50" in called_url

    @patch.object(ci_module, "_make_request")
    def test_url_without_query_adds_question_mark(self, mock_request):
        """Test pagination adds ? separator for URL without query params."""
        mock_request.return_value = {"results": [], "_links": {}}

        ci_module._paginate("token", "https://api.atlassian.com/test")
        called_url = mock_request.call_args[0][1]
        assert "?limit=100" in called_url

    @patch.object(ci_module, "_make_request")
    def test_url_with_query_adds_ampersand(self, mock_request):
        """Test pagination adds & separator for URL with existing query params."""
        mock_request.return_value = {"results": [], "_links": {}}

        ci_module._paginate("token", "https://api.atlassian.com/test?foo=bar")
        called_url = mock_request.call_args[0][1]
        assert "&limit=100" in called_url
        assert "?foo=bar" in called_url

    @patch.object(ci_module, "_make_request")
    def test_missing_items_key_returns_empty(self, mock_request):
        """Test pagination when response lacks the items key."""
        mock_request.return_value = {"_links": {}}

        result = ci_module._paginate("token", "https://api.atlassian.com/test")
        assert result == []

    @patch.object(ci_module, "_make_request")
    def test_no_links_key_stops_pagination(self, mock_request):
        """Test pagination when response has no _links key."""
        mock_request.return_value = {"results": [{"id": "1"}]}

        result = ci_module._paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 1

    @patch.object(ci_module, "_make_request")
    def test_propagates_auth_error(self, mock_request):
        """Test pagination propagates ConfluenceAuthError."""
        mock_request.side_effect = ci_module.ConfluenceAuthError(
            "Authentication failed: 401", status_code=401
        )

        with pytest.raises(ci_module.ConfluenceAuthError):
            ci_module._paginate("bad_token", "https://api.atlassian.com/test")

    @patch.object(ci_module, "_make_request")
    def test_propagates_api_error(self, mock_request):
        """Test pagination propagates ConfluenceAPIError."""
        mock_request.side_effect = ci_module.ConfluenceAPIError(
            "API error: 500", status_code=500
        )

        with pytest.raises(ci_module.ConfluenceAPIError):
            ci_module._paginate("token", "https://api.atlassian.com/test")


# ============================================================================
# confluence-integration (ci_module): get_accessible_resources
#
# CI filtering: any("confluence" in s for s in scopes)
# This checks if any scope STRING CONTAINS "confluence" as a substring.
# ============================================================================

class TestCIGetAccessibleResources:
    """Test get_accessible_resources from confluence-integration."""

    @patch.object(ci_module, "_make_request")
    def test_filters_by_scope_containing_confluence(self, mock_request):
        """Test filtering resources where a scope string contains 'confluence'."""
        mock_request.return_value = [
            {"id": "c1", "name": "Site1", "scopes": ["read:confluence-content.all"]},
            {"id": "c2", "name": "Site2", "scopes": ["read:jira-work"]},
            {"id": "c3", "name": "Site3", "scopes": ["write:confluence-space"]},
        ]

        result = ci_module.get_accessible_resources("token")
        assert len(result) == 2
        ids = [r["id"] for r in result]
        assert "c1" in ids
        assert "c3" in ids

    @patch.object(ci_module, "_make_request")
    def test_exact_scope_confluence(self, mock_request):
        """Test resource with exact scope 'confluence' is matched."""
        mock_request.return_value = [
            {"id": "c1", "name": "Site1", "scopes": ["confluence"]},
        ]

        result = ci_module.get_accessible_resources("token")
        assert len(result) == 1

    @patch.object(ci_module, "_make_request")
    def test_no_confluence_scopes_returns_empty(self, mock_request):
        """Test returns empty when no scopes contain 'confluence'."""
        mock_request.return_value = [
            {"id": "c1", "name": "Jira Only", "scopes": ["jira", "read:jira-work"]},
        ]

        result = ci_module.get_accessible_resources("token")
        assert result == []

    @patch.object(ci_module, "_make_request")
    def test_empty_resources_returns_empty(self, mock_request):
        """Test empty resources list returns empty."""
        mock_request.return_value = []

        result = ci_module.get_accessible_resources("token")
        assert result == []

    @patch.object(ci_module, "_make_request")
    def test_missing_scopes_key_returns_empty(self, mock_request):
        """Test resource without scopes key is excluded (empty scopes list)."""
        mock_request.return_value = [
            {"id": "c1", "name": "No Scopes Site"},
        ]

        result = ci_module.get_accessible_resources("token")
        assert result == []

    @patch.object(ci_module, "_make_request")
    def test_calls_correct_url(self, mock_request):
        """Test the correct Atlassian OAuth endpoint is called."""
        mock_request.return_value = []

        ci_module.get_accessible_resources("my_token")
        mock_request.assert_called_once_with(
            "my_token",
            "https://api.atlassian.com/oauth/token/accessible-resources",
        )

    @patch.object(ci_module, "_make_request")
    def test_propagates_auth_error(self, mock_request):
        """Test auth error from _make_request is propagated."""
        mock_request.side_effect = ci_module.ConfluenceAuthError("Auth failed", status_code=401)

        with pytest.raises(ci_module.ConfluenceAuthError):
            ci_module.get_accessible_resources("bad_token")

    @patch.object(ci_module, "_make_request")
    def test_multiple_scopes_one_is_confluence(self, mock_request):
        """Test resource with multiple scopes where one contains confluence."""
        mock_request.return_value = [
            {"id": "c1", "name": "Multi", "scopes": ["jira", "read:confluence-content.all", "bitbucket"]},
        ]

        result = ci_module.get_accessible_resources("token")
        assert len(result) == 1
        assert result[0]["id"] == "c1"


# ============================================================================
# confluence-integration (ci_module): list_all_spaces
# ============================================================================

class TestCIListAllSpaces:
    """Test list_all_spaces from confluence-integration."""

    @patch.object(ci_module, "_paginate")
    def test_success(self, mock_paginate):
        """Test listing all spaces returns paginated results."""
        mock_paginate.return_value = [
            {"id": "s1", "key": "DEV", "name": "Development", "type": "global"},
            {"id": "s2", "key": "OPS", "name": "Operations", "type": "global"},
        ]

        result = ci_module.list_all_spaces("token", "cloud123")
        assert len(result) == 2
        assert result[0]["key"] == "DEV"

    @patch.object(ci_module, "_paginate")
    def test_calls_correct_url(self, mock_paginate):
        """Test the correct API URL is constructed."""
        mock_paginate.return_value = []

        ci_module.list_all_spaces("token", "cloud-abc-123")
        mock_paginate.assert_called_once_with(
            "token",
            f"{ci_module.CONFLUENCE_API_BASE}/cloud-abc-123/wiki/api/v2/spaces",
        )

    @patch.object(ci_module, "_paginate")
    def test_empty(self, mock_paginate):
        """Test listing spaces when none exist."""
        mock_paginate.return_value = []

        result = ci_module.list_all_spaces("token", "cloud123")
        assert result == []


# ============================================================================
# confluence-integration (ci_module): list_pages_in_space
# ============================================================================

class TestCIListPagesInSpace:
    """Test list_pages_in_space from confluence-integration."""

    @patch.object(ci_module, "_paginate")
    def test_success(self, mock_paginate):
        """Test listing pages in a space."""
        mock_paginate.return_value = [
            {"id": "p1", "title": "Welcome Page", "status": "current"},
            {"id": "p2", "title": "Architecture Guide", "status": "current"},
        ]

        result = ci_module.list_pages_in_space("token", "cloud123", "space456")
        assert len(result) == 2
        assert result[0]["title"] == "Welcome Page"

    @patch.object(ci_module, "_paginate")
    def test_calls_correct_url(self, mock_paginate):
        """Test the correct API URL is constructed with space ID."""
        mock_paginate.return_value = []

        ci_module.list_pages_in_space("token", "cloud-abc", "space-xyz")
        mock_paginate.assert_called_once_with(
            "token",
            f"{ci_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/spaces/space-xyz/pages",
        )

    @patch.object(ci_module, "_paginate")
    def test_empty(self, mock_paginate):
        """Test listing pages when space is empty."""
        mock_paginate.return_value = []

        result = ci_module.list_pages_in_space("token", "cloud123", "space456")
        assert result == []


# ============================================================================
# confluence-integration (ci_module): get_page_content
# ============================================================================

class TestCIGetPageContent:
    """Test get_page_content from confluence-integration."""

    @patch.object(ci_module, "_make_request")
    def test_success(self, mock_request):
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

        result = ci_module.get_page_content("token", "cloud123", "page123")
        assert result["title"] == "Test Page"
        assert result["body"]["storage"]["value"] == "<p>Page content</p>"

    @patch.object(ci_module, "_make_request")
    def test_calls_correct_url(self, mock_request):
        """Test the correct API URL with body-format=storage."""
        mock_request.return_value = {}

        ci_module.get_page_content("token", "cloud-abc", "page-xyz")
        mock_request.assert_called_once_with(
            "token",
            f"{ci_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/pages/page-xyz?body-format=storage",
        )

    @patch.object(ci_module, "_make_request")
    def test_propagates_auth_error(self, mock_request):
        """Test auth error is propagated when token is invalid."""
        mock_request.side_effect = ci_module.ConfluenceAuthError("Auth failed", status_code=401)

        with pytest.raises(ci_module.ConfluenceAuthError):
            ci_module.get_page_content("bad_token", "cloud123", "page123")

    @patch.object(ci_module, "_make_request")
    def test_propagates_not_found(self, mock_request):
        """Test API error is propagated when page does not exist."""
        mock_request.side_effect = ci_module.ConfluenceAPIError(
            "API error: 404", status_code=404
        )

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module.get_page_content("token", "cloud123", "nonexistent")
        assert exc_info.value.status_code == 404


# ============================================================================
# confluence-integration (ci_module): list_page_attachments
# ============================================================================

class TestCIListPageAttachments:
    """Test list_page_attachments from confluence-integration."""

    @patch.object(ci_module, "_paginate")
    def test_success(self, mock_paginate):
        """Test listing attachments on a page."""
        mock_paginate.return_value = [
            {"id": "att1", "title": "diagram.png", "mediaType": "image/png"},
            {"id": "att2", "title": "report.pdf", "mediaType": "application/pdf"},
        ]

        result = ci_module.list_page_attachments("token", "cloud123", "page456")
        assert len(result) == 2
        assert result[0]["title"] == "diagram.png"

    @patch.object(ci_module, "_paginate")
    def test_calls_correct_url(self, mock_paginate):
        """Test the correct API URL is constructed with page ID."""
        mock_paginate.return_value = []

        ci_module.list_page_attachments("token", "cloud-abc", "page-xyz")
        mock_paginate.assert_called_once_with(
            "token",
            f"{ci_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/pages/page-xyz/attachments",
        )

    @patch.object(ci_module, "_paginate")
    def test_empty(self, mock_paginate):
        """Test listing attachments when page has none."""
        mock_paginate.return_value = []

        result = ci_module.list_page_attachments("token", "cloud123", "page456")
        assert result == []


# ============================================================================
# confluence-integration (ci_module): search_pages_modified_since
# ============================================================================

class TestCISearchPagesModifiedSince:
    """Test search_pages_modified_since from confluence-integration."""

    @patch.object(ci_module, "_paginate")
    def test_success(self, mock_paginate):
        """Test searching for pages modified since a date."""
        mock_paginate.return_value = [
            {"id": "p1", "title": "Updated Page", "lastModified": "2024-01-15T10:00:00Z"},
        ]

        result = ci_module.search_pages_modified_since("token", "cloud123", "DEV", "2024-01-01")
        assert len(result) == 1
        assert result[0]["title"] == "Updated Page"

    @patch.object(ci_module, "_paginate")
    def test_cql_content(self, mock_paginate):
        """Test CQL query contains correct space key, type, and date filter."""
        mock_paginate.return_value = []

        ci_module.search_pages_modified_since("token", "cloud123", "MYSPACE", "2024-03-20")
        called_url = mock_paginate.call_args[0][1]
        decoded_url = urllib.parse.unquote(called_url)

        assert 'space = "MYSPACE"' in decoded_url
        assert "type = page" in decoded_url
        assert 'lastModified >= "2024-03-20"' in decoded_url

    @patch.object(ci_module, "_paginate")
    def test_uses_rest_api_search_endpoint(self, mock_paginate):
        """Test search uses v1 REST API (content/search) not v2 API."""
        mock_paginate.return_value = []

        ci_module.search_pages_modified_since("token", "cloud123", "DEV", "2024-01-01")
        called_url = mock_paginate.call_args[0][1]
        assert "/wiki/rest/api/content/search" in called_url

    @patch.object(ci_module, "_paginate")
    def test_empty_results(self, mock_paginate):
        """Test search returns empty when no pages match."""
        mock_paginate.return_value = []

        result = ci_module.search_pages_modified_since("token", "cloud123", "DEV", "2099-01-01")
        assert result == []


# ============================================================================
# confluence-integration (ci_module): download_attachment
#
# CI version: fetches metadata via _make_request first, then uses downloadLink.
# No page_id parameter.
# ============================================================================

class TestCIDownloadAttachment:
    """Test download_attachment from confluence-integration."""

    @patch.object(ci_module.urllib.request, "urlopen")
    @patch.object(ci_module, "_make_request")
    def test_success(self, mock_request, mock_urlopen):
        """Test successful attachment download via metadata lookup."""
        mock_request.return_value = {
            "downloadLink": "/download/attachments/123/file.pdf",
        }
        mock_urlopen.return_value = _mock_urlopen_response(b"PDF content", is_bytes=True)

        result = ci_module.download_attachment("token", "cloud123", "att456")
        assert result == b"PDF content"

    @patch.object(ci_module.urllib.request, "urlopen")
    @patch.object(ci_module, "_make_request")
    def test_fetches_metadata_from_correct_url(self, mock_request, mock_urlopen):
        """Test metadata is fetched from v2 attachments endpoint."""
        mock_request.return_value = {"downloadLink": "/download/file.pdf"}
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        ci_module.download_attachment("token", "cloud-abc", "att789")
        mock_request.assert_called_once_with(
            "token",
            f"{ci_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/attachments/att789",
        )

    @patch.object(ci_module.urllib.request, "urlopen")
    @patch.object(ci_module, "_make_request")
    def test_relative_download_link_gets_prefix(self, mock_request, mock_urlopen):
        """Test relative downloadLink is prefixed with base URL + /wiki."""
        mock_request.return_value = {
            "downloadLink": "/download/attachments/123/file.pdf",
        }
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        ci_module.download_attachment("token", "cloud123", "att456")
        request_obj = mock_urlopen.call_args[0][0]
        expected_url = f"{ci_module.CONFLUENCE_API_BASE}/cloud123/wiki/download/attachments/123/file.pdf"
        assert request_obj.full_url == expected_url

    @patch.object(ci_module, "_make_request")
    def test_no_download_link_raises_error(self, mock_request):
        """Test missing downloadLink raises ConfluenceAPIError."""
        mock_request.return_value = {"id": "att456"}

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module.download_attachment("token", "cloud123", "att456")
        assert "No download link" in str(exc_info.value)

    @patch.object(ci_module.urllib.request, "urlopen")
    @patch.object(ci_module, "_make_request")
    def test_sets_auth_header_on_download(self, mock_request, mock_urlopen):
        """Test download request includes Bearer token."""
        mock_request.return_value = {"downloadLink": "/download/file.pdf"}
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        ci_module.download_attachment("my_token", "cloud123", "att1")
        request_obj = mock_urlopen.call_args[0][0]
        assert request_obj.get_header("Authorization") == "Bearer my_token"

    @patch.object(ci_module.urllib.request, "urlopen")
    @patch.object(ci_module, "_make_request")
    def test_uses_download_timeout(self, mock_request, mock_urlopen):
        """Test download uses the longer DOWNLOAD_TIMEOUT."""
        mock_request.return_value = {"downloadLink": "/download/file.pdf"}
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        ci_module.download_attachment("token", "cloud123", "att1")
        call_args = mock_urlopen.call_args
        assert ci_module.DOWNLOAD_TIMEOUT in call_args[0] or \
            call_args[1].get("timeout") == ci_module.DOWNLOAD_TIMEOUT

    @patch.object(ci_module.urllib.request, "urlopen")
    @patch.object(ci_module, "_make_request")
    def test_http_error_on_download(self, mock_request, mock_urlopen):
        """Test download failure raises ConfluenceAPIError."""
        mock_request.return_value = {"downloadLink": "/download/file.pdf"}
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=404, msg="Not Found",
            hdrs={}, fp=None,
        )

        with pytest.raises(ci_module.ConfluenceAPIError) as exc_info:
            ci_module.download_attachment("token", "cloud123", "att1")
        assert exc_info.value.status_code == 404

    @patch.object(ci_module.urllib.request, "urlopen")
    @patch.object(ci_module, "_make_request")
    def test_returns_bytes(self, mock_request, mock_urlopen):
        """Test download returns raw bytes content."""
        binary_content = b"\x89PNG\r\n\x1a\n\x00\x00"
        mock_request.return_value = {"downloadLink": "/download/image.png"}
        mock_urlopen.return_value = _mock_urlopen_response(binary_content, is_bytes=True)

        result = ci_module.download_attachment("token", "cloud123", "att1")
        assert isinstance(result, bytes)
        assert result == binary_content


# ============================================================================
# confluence-integration (ci_module): Constants
# ============================================================================

class TestCIConstants:
    """Test module-level constants from confluence-integration."""

    def test_api_base_url(self):
        assert ci_module.CONFLUENCE_API_BASE == "https://api.atlassian.com/ex/confluence"

    def test_request_timeout(self):
        assert ci_module.REQUEST_TIMEOUT == 60

    def test_download_timeout(self):
        assert ci_module.DOWNLOAD_TIMEOUT == 300
        assert ci_module.DOWNLOAD_TIMEOUT > ci_module.REQUEST_TIMEOUT


# ============================================================================
# ============================================================================
# file-ingestion (fi_module): Smoke tests + unique code paths
# ============================================================================
# ============================================================================

# ============================================================================
# file-ingestion (fi_module): Exception Classes
# ============================================================================

class TestFIExceptions:
    """Test custom exception classes from file-ingestion."""

    def test_api_error_attributes(self):
        err = fi_module.ConfluenceAPIError("Test error", status_code=500, response="body")
        assert str(err) == "Test error"
        assert err.status_code == 500
        assert err.response == "body"

    def test_auth_error_is_subclass_of_api_error(self):
        err = fi_module.ConfluenceAuthError("Auth failed", status_code=401)
        assert isinstance(err, fi_module.ConfluenceAPIError)
        assert err.status_code == 401


# ============================================================================
# file-ingestion (fi_module): _make_request
# ============================================================================

class TestFIMakeRequest:
    """Smoke tests for _make_request from file-ingestion."""

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_success_returns_parsed_json(self, mock_urlopen):
        """Test successful API request returns parsed JSON."""
        mock_urlopen.return_value = _mock_urlopen_response({"key": "value"})

        result = fi_module._make_request("token", "https://api.atlassian.com/test")
        assert result == {"key": "value"}

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_sets_auth_header(self, mock_urlopen):
        """Test request includes Bearer token."""
        mock_urlopen.return_value = _mock_urlopen_response({})

        fi_module._make_request("my_token", "https://api.atlassian.com/test")
        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my_token"

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_401_raises_auth_error(self, mock_urlopen):
        """Test 401 response raises ConfluenceAuthError."""
        mock_urlopen.side_effect = _mock_http_error(401)

        with pytest.raises(fi_module.ConfluenceAuthError) as exc_info:
            fi_module._make_request("bad_token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 401

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_500_raises_api_error(self, mock_urlopen):
        """Test 500 response raises ConfluenceAPIError."""
        mock_urlopen.side_effect = _mock_http_error(500)

        with pytest.raises(fi_module.ConfluenceAPIError) as exc_info:
            fi_module._make_request("token", "https://api.atlassian.com/test")
        assert exc_info.value.status_code == 500

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_network_error(self, mock_urlopen):
        """Test network error raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with pytest.raises(fi_module.ConfluenceAPIError) as exc_info:
            fi_module._make_request("token", "https://api.atlassian.com/test")
        assert "Network error" in str(exc_info.value)


# ============================================================================
# file-ingestion (fi_module): _paginate
# ============================================================================

class TestFIPaginate:
    """Smoke tests for _paginate from file-ingestion."""

    @patch.object(fi_module, "_make_request")
    def test_single_page(self, mock_request):
        """Test pagination with a single page of results."""
        mock_request.return_value = {
            "results": [{"id": "1"}, {"id": "2"}],
            "_links": {},
        }

        result = fi_module._paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 2

    @patch.object(fi_module, "_make_request")
    def test_multiple_pages(self, mock_request):
        """Test pagination across multiple pages."""
        mock_request.side_effect = [
            {
                "results": [{"id": "1"}],
                "_links": {"next": "/rest/api/v2/spaces?cursor=abc&limit=100"},
            },
            {
                "results": [{"id": "2"}],
                "_links": {},
            },
        ]

        result = fi_module._paginate("token", "https://api.atlassian.com/test")
        assert len(result) == 2
        assert mock_request.call_count == 2


# ============================================================================
# file-ingestion (fi_module): get_accessible_resources
#
# FI filtering: "confluence" in r.get("scopes", []) or "confluence" in str(r.get("url", ""))
# This checks if the literal string "confluence" is an element of scopes
# OR if "confluence" appears as a substring in the URL string.
# ============================================================================

class TestFIGetAccessibleResources:
    """Test get_accessible_resources from file-ingestion -- unique filtering logic."""

    @patch.object(fi_module, "_make_request")
    def test_filters_by_exact_scope_confluence(self, mock_request):
        """Test resource with exact 'confluence' scope is matched."""
        mock_request.return_value = [
            {"id": "c1", "name": "Site1", "scopes": ["confluence"], "url": "https://site1.atlassian.net"},
            {"id": "c2", "name": "Site2", "scopes": ["jira"], "url": "https://site2.atlassian.net"},
        ]

        result = fi_module.get_accessible_resources("token")
        assert len(result) == 1
        assert result[0]["id"] == "c1"

    @patch.object(fi_module, "_make_request")
    def test_filters_by_url_containing_confluence(self, mock_request):
        """Test resource matched via URL containing 'confluence'."""
        mock_request.return_value = [
            {"id": "c1", "url": "https://confluence.example.com", "name": "Confluence Site", "scopes": []},
            {"id": "c2", "url": "https://jira.example.com", "name": "Jira Site", "scopes": []},
        ]

        result = fi_module.get_accessible_resources("token")
        assert len(result) == 1
        assert result[0]["id"] == "c1"

    @patch.object(fi_module, "_make_request")
    def test_substring_scope_not_matched(self, mock_request):
        """Test that scope containing 'confluence' as substring is NOT matched.

        Unlike ci_module, fi_module uses 'in' on the list, not substring match.
        So 'read:confluence-content.all' is NOT equal to 'confluence'.
        """
        mock_request.return_value = [
            {"id": "c1", "name": "Site1", "scopes": ["read:confluence-content.all"],
             "url": "https://site1.atlassian.net"},
        ]

        result = fi_module.get_accessible_resources("token")
        # fi_module checks `"confluence" in scopes_list` which is exact membership,
        # not substring. "read:confluence-content.all" != "confluence".
        # Also URL does not contain "confluence".
        assert result == []

    @patch.object(fi_module, "_make_request")
    def test_empty_resources_returns_empty(self, mock_request):
        """Test empty resources list returns empty."""
        mock_request.return_value = []

        result = fi_module.get_accessible_resources("token")
        assert result == []

    @patch.object(fi_module, "_make_request")
    def test_no_confluence_returns_empty(self, mock_request):
        """Test returns empty when neither scope nor URL matches."""
        mock_request.return_value = [
            {"id": "c1", "url": "https://jira.example.com", "name": "Jira Only", "scopes": ["jira"]},
        ]

        result = fi_module.get_accessible_resources("token")
        assert result == []

    @patch.object(fi_module, "_make_request")
    def test_missing_scopes_key_matched_by_url(self, mock_request):
        """Test resource without scopes key can still match via URL."""
        mock_request.return_value = [
            {"id": "c1", "url": "https://confluence.example.com", "name": "Site"},
        ]

        result = fi_module.get_accessible_resources("token")
        assert len(result) == 1

    @patch.object(fi_module, "_make_request")
    def test_calls_correct_url(self, mock_request):
        """Test the correct Atlassian OAuth endpoint is called."""
        mock_request.return_value = []

        fi_module.get_accessible_resources("my_token")
        mock_request.assert_called_once_with(
            "my_token",
            "https://api.atlassian.com/oauth/token/accessible-resources",
        )

    @patch.object(fi_module, "_make_request")
    def test_both_scope_and_url_match(self, mock_request):
        """Test resource that matches both scope and URL is included once."""
        mock_request.return_value = [
            {"id": "c1", "url": "https://confluence.example.com", "name": "Site",
             "scopes": ["confluence"]},
        ]

        result = fi_module.get_accessible_resources("token")
        assert len(result) == 1

    @patch.object(fi_module, "_make_request")
    def test_propagates_auth_error(self, mock_request):
        """Test auth error is propagated."""
        mock_request.side_effect = fi_module.ConfluenceAuthError("Auth failed", status_code=401)

        with pytest.raises(fi_module.ConfluenceAuthError):
            fi_module.get_accessible_resources("bad_token")


# ============================================================================
# file-ingestion (fi_module): list_all_spaces
# ============================================================================

class TestFIListAllSpaces:
    """Smoke tests for list_all_spaces from file-ingestion."""

    @patch.object(fi_module, "_paginate")
    def test_success(self, mock_paginate):
        mock_paginate.return_value = [
            {"id": "s1", "key": "DEV", "name": "Development"},
        ]

        result = fi_module.list_all_spaces("token", "cloud123")
        assert len(result) == 1
        assert result[0]["key"] == "DEV"

    @patch.object(fi_module, "_paginate")
    def test_calls_correct_url(self, mock_paginate):
        mock_paginate.return_value = []

        fi_module.list_all_spaces("token", "cloud-abc")
        mock_paginate.assert_called_once_with(
            "token",
            f"{fi_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/spaces",
        )


# ============================================================================
# file-ingestion (fi_module): list_pages_in_space
# ============================================================================

class TestFIListPagesInSpace:
    """Smoke tests for list_pages_in_space from file-ingestion."""

    @patch.object(fi_module, "_paginate")
    def test_success(self, mock_paginate):
        mock_paginate.return_value = [
            {"id": "p1", "title": "Welcome Page"},
        ]

        result = fi_module.list_pages_in_space("token", "cloud123", "space456")
        assert len(result) == 1

    @patch.object(fi_module, "_paginate")
    def test_calls_correct_url(self, mock_paginate):
        mock_paginate.return_value = []

        fi_module.list_pages_in_space("token", "cloud-abc", "space-xyz")
        mock_paginate.assert_called_once_with(
            "token",
            f"{fi_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/spaces/space-xyz/pages",
        )


# ============================================================================
# file-ingestion (fi_module): get_page_content
# ============================================================================

class TestFIGetPageContent:
    """Smoke tests for get_page_content from file-ingestion."""

    @patch.object(fi_module, "_make_request")
    def test_success(self, mock_request):
        mock_request.return_value = {
            "id": "page123",
            "title": "Test Page",
            "body": {"storage": {"value": "<p>Content</p>"}},
        }

        result = fi_module.get_page_content("token", "cloud123", "page123")
        assert result["title"] == "Test Page"

    @patch.object(fi_module, "_make_request")
    def test_calls_correct_url(self, mock_request):
        mock_request.return_value = {}

        fi_module.get_page_content("token", "cloud-abc", "page-xyz")
        mock_request.assert_called_once_with(
            "token",
            f"{fi_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/pages/page-xyz?body-format=storage",
        )


# ============================================================================
# file-ingestion (fi_module): list_page_attachments
# ============================================================================

class TestFIListPageAttachments:
    """Smoke tests for list_page_attachments from file-ingestion."""

    @patch.object(fi_module, "_paginate")
    def test_success(self, mock_paginate):
        mock_paginate.return_value = [
            {"id": "att1", "title": "diagram.png"},
        ]

        result = fi_module.list_page_attachments("token", "cloud123", "page456")
        assert len(result) == 1

    @patch.object(fi_module, "_paginate")
    def test_calls_correct_url(self, mock_paginate):
        mock_paginate.return_value = []

        fi_module.list_page_attachments("token", "cloud-abc", "page-xyz")
        mock_paginate.assert_called_once_with(
            "token",
            f"{fi_module.CONFLUENCE_API_BASE}/cloud-abc/wiki/api/v2/pages/page-xyz/attachments",
        )


# ============================================================================
# file-ingestion (fi_module): search_pages_modified_since
# ============================================================================

class TestFISearchPagesModifiedSince:
    """Smoke tests for search_pages_modified_since from file-ingestion."""

    @patch.object(fi_module, "_paginate")
    def test_success(self, mock_paginate):
        mock_paginate.return_value = [
            {"id": "p1", "title": "Updated Page"},
        ]

        result = fi_module.search_pages_modified_since("token", "cloud123", "DEV", "2024-01-01")
        assert len(result) == 1

    @patch.object(fi_module, "_paginate")
    def test_cql_content(self, mock_paginate):
        mock_paginate.return_value = []

        fi_module.search_pages_modified_since("token", "cloud123", "MYSPACE", "2024-03-20")
        called_url = mock_paginate.call_args[0][1]
        decoded_url = urllib.parse.unquote(called_url)

        assert 'space = "MYSPACE"' in decoded_url
        assert "type = page" in decoded_url
        assert 'lastModified >= "2024-03-20"' in decoded_url

    @patch.object(fi_module, "_paginate")
    def test_uses_rest_api_search_endpoint(self, mock_paginate):
        mock_paginate.return_value = []

        fi_module.search_pages_modified_since("token", "cloud123", "DEV", "2024-01-01")
        called_url = mock_paginate.call_args[0][1]
        assert "/wiki/rest/api/content/search" in called_url


# ============================================================================
# file-ingestion (fi_module): download_attachment
#
# FI version: takes optional page_id, strips "att" prefix from attachment_id,
# constructs OAuth-compatible REST URL directly (no metadata fetch).
# ============================================================================

class TestFIDownloadAttachment:
    """Test download_attachment from file-ingestion -- unique implementation."""

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_success_with_page_id(self, mock_urlopen):
        """Test downloading attachment with page ID (OAuth-compatible path)."""
        mock_urlopen.return_value = _mock_urlopen_response(b"PDF content", is_bytes=True)

        result = fi_module.download_attachment("token", "cloud123", "att456", page_id="page789")
        assert result == b"PDF content"

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_strips_att_prefix(self, mock_urlopen):
        """Test attachment ID with 'att' prefix is stripped for v1 REST endpoint."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        fi_module.download_attachment("token", "cloud123", "att456789", page_id="page123")
        request_obj = mock_urlopen.call_args[0][0]
        url = request_obj.full_url
        assert "/attachment/456789/download" in url
        assert "att456789" not in url

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_numeric_id_unchanged(self, mock_urlopen):
        """Test attachment ID already numeric (no 'att' prefix) is used as-is."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        fi_module.download_attachment("token", "cloud123", "123456", page_id="page789")
        request_obj = mock_urlopen.call_args[0][0]
        url = request_obj.full_url
        assert "/attachment/123456/download" in url

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_with_page_id_url_format(self, mock_urlopen):
        """Test OAuth-compatible URL is constructed with page ID."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        fi_module.download_attachment("token", "cloud-abc", "att999", page_id="page-xyz")
        request_obj = mock_urlopen.call_args[0][0]
        url = request_obj.full_url
        expected = (
            f"{fi_module.CONFLUENCE_API_BASE}/cloud-abc"
            f"/wiki/rest/api/content/page-xyz/child/attachment/999/download"
        )
        assert url == expected

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_without_page_id_fallback(self, mock_urlopen):
        """Test fallback URL when page ID is not provided."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        fi_module.download_attachment("token", "cloud123", "att789")
        request_obj = mock_urlopen.call_args[0][0]
        url = request_obj.full_url
        expected = f"{fi_module.CONFLUENCE_API_BASE}/cloud123/wiki/rest/api/content/789/download"
        assert url == expected

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_without_page_id_numeric(self, mock_urlopen):
        """Test fallback URL with numeric attachment ID (no prefix)."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        fi_module.download_attachment("token", "cloud123", "555")
        request_obj = mock_urlopen.call_args[0][0]
        url = request_obj.full_url
        expected = f"{fi_module.CONFLUENCE_API_BASE}/cloud123/wiki/rest/api/content/555/download"
        assert url == expected

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_sets_auth_header(self, mock_urlopen):
        """Test download request includes Bearer token."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        fi_module.download_attachment("my_token", "cloud123", "att1", page_id="p1")
        request_obj = mock_urlopen.call_args[0][0]
        assert request_obj.get_header("Authorization") == "Bearer my_token"

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_uses_download_timeout(self, mock_urlopen):
        """Test download uses the longer DOWNLOAD_TIMEOUT."""
        mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

        fi_module.download_attachment("token", "cloud123", "att1", page_id="p1")
        call_args = mock_urlopen.call_args
        assert fi_module.DOWNLOAD_TIMEOUT in call_args[0] or \
            call_args[1].get("timeout") == fi_module.DOWNLOAD_TIMEOUT

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_http_error(self, mock_urlopen):
        """Test download failure raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=404, msg="Not Found",
            hdrs={}, fp=None,
        )

        with pytest.raises(fi_module.ConfluenceAPIError) as exc_info:
            fi_module.download_attachment("token", "cloud123", "att1", page_id="p1")
        assert exc_info.value.status_code == 404

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_500_error(self, mock_urlopen):
        """Test server error during download raises ConfluenceAPIError."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.com", code=500, msg="Internal Server Error",
            hdrs={}, fp=None,
        )

        with pytest.raises(fi_module.ConfluenceAPIError) as exc_info:
            fi_module.download_attachment("token", "cloud123", "att1", page_id="p1")
        assert exc_info.value.status_code == 500

    @patch.object(fi_module.urllib.request, "urlopen")
    def test_returns_bytes(self, mock_urlopen):
        """Test download returns raw bytes content."""
        binary_content = b"\x89PNG\r\n\x1a\n\x00\x00"
        mock_urlopen.return_value = _mock_urlopen_response(binary_content, is_bytes=True)

        result = fi_module.download_attachment("token", "cloud123", "att1", page_id="p1")
        assert isinstance(result, bytes)
        assert result == binary_content


# ============================================================================
# file-ingestion (fi_module): Constants
# ============================================================================

class TestFIConstants:
    """Test module-level constants from file-ingestion."""

    def test_api_base_url(self):
        assert fi_module.CONFLUENCE_API_BASE == "https://api.atlassian.com/ex/confluence"

    def test_request_timeout(self):
        assert fi_module.REQUEST_TIMEOUT == 60

    def test_download_timeout(self):
        assert fi_module.DOWNLOAD_TIMEOUT == 300
        assert fi_module.DOWNLOAD_TIMEOUT > fi_module.REQUEST_TIMEOUT
