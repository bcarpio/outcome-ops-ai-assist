"""Unit tests for Outlook API client (outlook_api.py).

Tests both copies of the module:
- lambda/file-ingestion/outlook_api.py
- lambda/outlook-sync/outlook_api.py

Uses importlib.util.spec_from_file_location to load each copy as a separate
module object, then runs the same suite of tests against both.
"""

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

_TEST_DIR = os.path.dirname(__file__)


def _load_module(module_name, relative_path):
    """Load a Python module by file path.

    Args:
        module_name: Name to register in sys.modules.
        relative_path: Path relative to the test directory.

    Returns:
        The loaded module object.
    """
    if module_name in sys.modules:
        del sys.modules[module_name]

    abs_path = os.path.normpath(os.path.join(_TEST_DIR, relative_path))
    spec = importlib.util.spec_from_file_location(module_name, abs_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Load from file-ingestion path
fi_module = _load_module("outlook_api_fi", "../../file-ingestion/outlook_api.py")
# Load from outlook-sync path
os_module = _load_module("outlook_api_os", "../../outlook-sync/outlook_api.py")


# ============================================================================
# Helpers
# ============================================================================

TOKEN = "test-access-token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


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
    """Create a mock HTTPError with a readable body."""
    return urllib.error.HTTPError(
        url="https://graph.microsoft.com",
        code=code,
        msg=msg,
        hdrs={},
        fp=MagicMock(read=MagicMock(return_value=body)),
    )


# Parametrize fixture to run tests against both module copies.
@pytest.fixture(params=[fi_module, os_module], ids=["file-ingestion", "outlook-sync"])
def api(request):
    """Yield one of the two outlook_api module copies."""
    return request.param


# ============================================================================
# Exception Classes
# ============================================================================

class TestExceptions:
    """Test custom exception classes from both module copies."""

    def test_outlook_api_error_attributes(self, api):
        """Test OutlookAPIError stores message, status_code, and response."""
        err = api.OutlookAPIError("Test error", status_code=500, response="body")
        assert str(err) == "Test error"
        assert err.status_code == 500
        assert err.response == "body"

    def test_outlook_api_error_default_attributes(self, api):
        """Test OutlookAPIError defaults to None for optional fields."""
        err = api.OutlookAPIError("Simple error")
        assert str(err) == "Simple error"
        assert err.status_code is None
        assert err.response is None

    def test_outlook_auth_error_is_api_error(self, api):
        """Test OutlookAuthError inherits from OutlookAPIError."""
        err = api.OutlookAuthError("Auth failed", status_code=401)
        assert isinstance(err, api.OutlookAPIError)
        assert err.status_code == 401

    def test_outlook_auth_error_with_response(self, api):
        """Test OutlookAuthError stores response body."""
        err = api.OutlookAuthError(
            "Forbidden", status_code=403, response='{"error": "forbidden"}'
        )
        assert err.status_code == 403
        assert err.response == '{"error": "forbidden"}'

    def test_outlook_api_error_is_exception(self, api):
        """Test OutlookAPIError is a proper Exception subclass."""
        err = api.OutlookAPIError("Test")
        assert isinstance(err, Exception)


# ============================================================================
# _make_request
# ============================================================================

class TestMakeRequest:
    """Test the core HTTP request function."""

    def test_make_request_success(self, api):
        """Test successful API request returns parsed JSON."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response({"key": "value"})

            result = api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert result == {"key": "value"}

    def test_make_request_sets_auth_header(self, api):
        """Test request includes Bearer token in Authorization header."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response({})

            api._make_request("my_token_123", f"{GRAPH_BASE}/me")
            request = mock_urlopen.call_args[0][0]
            assert request.get_header("Authorization") == "Bearer my_token_123"

    def test_make_request_sets_accept_header(self, api):
        """Test request includes Accept: application/json header."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response({})

            api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            request = mock_urlopen.call_args[0][0]
            assert request.get_header("Accept") == "application/json"

    def test_make_request_uses_correct_method(self, api):
        """Test request uses the specified HTTP method."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response({})

            api._make_request(TOKEN, f"{GRAPH_BASE}/me", method="POST")
            request = mock_urlopen.call_args[0][0]
            assert request.get_method() == "POST"

    def test_make_request_default_method_is_get(self, api):
        """Test request defaults to GET method."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response({})

            api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            request = mock_urlopen.call_args[0][0]
            assert request.get_method() == "GET"

    def test_make_request_uses_default_timeout(self, api):
        """Test request uses REQUEST_TIMEOUT (60s) by default."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response({})

            api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            call_args = mock_urlopen.call_args
            # urlopen(request, timeout=60)
            assert call_args[1].get("timeout") == api.REQUEST_TIMEOUT or \
                api.REQUEST_TIMEOUT in call_args[0]

    def test_make_request_uses_custom_timeout(self, api):
        """Test request uses provided timeout value."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response({})

            api._make_request(TOKEN, f"{GRAPH_BASE}/me", timeout=30)
            call_args = mock_urlopen.call_args
            assert 30 in call_args[0] or call_args[1].get("timeout") == 30

    def test_make_request_401_raises_auth_error(self, api):
        """Test 401 response raises OutlookAuthError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _mock_http_error(
                401, "Unauthorized", b'{"error": "unauthorized"}'
            )

            with pytest.raises(api.OutlookAuthError) as exc_info:
                api._make_request("bad_token", f"{GRAPH_BASE}/me")
            assert exc_info.value.status_code == 401
            assert "Authentication failed" in str(exc_info.value)

    def test_make_request_403_raises_auth_error(self, api):
        """Test 403 response raises OutlookAuthError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _mock_http_error(
                403, "Forbidden", b'{"error": "forbidden"}'
            )

            with pytest.raises(api.OutlookAuthError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert exc_info.value.status_code == 403
            assert "Authentication failed" in str(exc_info.value)

    def test_make_request_auth_error_includes_response_body(self, api):
        """Test auth error includes the response body."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            body = b'{"message": "Token expired"}'
            mock_urlopen.side_effect = _mock_http_error(401, "Unauthorized", body)

            with pytest.raises(api.OutlookAuthError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert exc_info.value.response == '{"message": "Token expired"}'

    def test_make_request_404_raises_api_error_not_auth(self, api):
        """Test 404 response raises OutlookAPIError (not auth error)."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _mock_http_error(404, "Not Found", b"not found")

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert exc_info.value.status_code == 404
            assert not isinstance(exc_info.value, api.OutlookAuthError)

    def test_make_request_500_raises_api_error(self, api):
        """Test 500 response raises OutlookAPIError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _mock_http_error(
                500, "Server Error", b"Internal error"
            )

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert exc_info.value.status_code == 500
            assert not isinstance(exc_info.value, api.OutlookAuthError)
            assert exc_info.value.response == "Internal error"

    def test_make_request_429_raises_api_error(self, api):
        """Test 429 rate limit response raises OutlookAPIError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = _mock_http_error(
                429, "Too Many Requests", b"rate limited"
            )

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert exc_info.value.status_code == 429
            assert not isinstance(exc_info.value, api.OutlookAuthError)

    def test_make_request_http_error_no_fp(self, api):
        """Test HTTP error with no response body (fp is None)."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            error = urllib.error.HTTPError(
                url="https://graph.microsoft.com",
                code=502,
                msg="Bad Gateway",
                hdrs={},
                fp=None,
            )
            mock_urlopen.side_effect = error

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert exc_info.value.status_code == 502
            assert exc_info.value.response == ""

    def test_make_request_network_error(self, api):
        """Test network error raises OutlookAPIError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert "Network error" in str(exc_info.value)

    def test_make_request_timeout_error(self, api):
        """Test timeout as URLError raises OutlookAPIError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("timed out")

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api._make_request(TOKEN, f"{GRAPH_BASE}/me")
            assert "Network error" in str(exc_info.value)


# ============================================================================
# get_user_profile
# ============================================================================

class TestGetUserProfile:
    """Test get_user_profile."""

    def test_get_user_profile_success(self, api):
        """Test fetching user profile returns expected data."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "displayName": "Test User",
                "mail": "test@example.com",
                "userPrincipalName": "test@example.com",
            }

            result = api.get_user_profile(TOKEN)
            assert result["displayName"] == "Test User"
            assert result["mail"] == "test@example.com"

    def test_get_user_profile_calls_correct_url(self, api):
        """Test get_user_profile calls the /me endpoint."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {}

            api.get_user_profile(TOKEN)
            mock_request.assert_called_once_with(TOKEN, f"{GRAPH_BASE}/me")

    def test_get_user_profile_passes_token(self, api):
        """Test access token is forwarded to _make_request."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {}

            api.get_user_profile("specific_token")
            assert mock_request.call_args[0][0] == "specific_token"

    def test_get_user_profile_propagates_auth_error(self, api):
        """Test auth error from _make_request is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAuthError(
                "Auth failed", status_code=401
            )

            with pytest.raises(api.OutlookAuthError):
                api.get_user_profile("bad_token")

    def test_get_user_profile_propagates_api_error(self, api):
        """Test API error from _make_request is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAPIError(
                "Server error", status_code=500
            )

            with pytest.raises(api.OutlookAPIError):
                api.get_user_profile(TOKEN)


# ============================================================================
# list_mail_folders
# ============================================================================

class TestListMailFolders:
    """Test list_mail_folders."""

    def test_list_folders_single_page(self, api):
        """Test listing folders with single page of results."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [
                    {"id": "f1", "displayName": "Inbox", "totalItemCount": 100},
                    {"id": "f2", "displayName": "Sent Items", "totalItemCount": 50},
                ]
            }

            result = api.list_mail_folders(TOKEN)
            assert len(result) == 2
            assert result[0]["displayName"] == "Inbox"
            assert result[1]["displayName"] == "Sent Items"

    def test_list_folders_pagination(self, api):
        """Test listing folders with pagination across multiple pages."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = [
                {
                    "value": [{"id": "f1", "displayName": "Inbox"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders?$skip=100",
                },
                {
                    "value": [{"id": "f2", "displayName": "Archive"}],
                },
            ]

            result = api.list_mail_folders(TOKEN)
            assert len(result) == 2
            assert mock_request.call_count == 2

    def test_list_folders_three_pages(self, api):
        """Test pagination across three pages."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = [
                {
                    "value": [{"id": "f1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/page2",
                },
                {
                    "value": [{"id": "f2"}],
                    "@odata.nextLink": "https://graph.microsoft.com/page3",
                },
                {
                    "value": [{"id": "f3"}],
                },
            ]

            result = api.list_mail_folders(TOKEN)
            assert len(result) == 3
            assert mock_request.call_count == 3

    def test_list_folders_empty(self, api):
        """Test listing folders when none exist."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": []}

            result = api.list_mail_folders(TOKEN)
            assert result == []

    def test_list_folders_calls_correct_initial_url(self, api):
        """Test initial URL includes $top=100."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": []}

            api.list_mail_folders(TOKEN)
            called_url = mock_request.call_args[0][1]
            assert called_url == f"{GRAPH_BASE}/me/mailFolders?$top=100"

    def test_list_folders_follows_next_link(self, api):
        """Test second request uses the nextLink URL directly."""
        next_link = "https://graph.microsoft.com/v1.0/me/mailFolders?$skip=100"
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = [
                {"value": [{"id": "f1"}], "@odata.nextLink": next_link},
                {"value": []},
            ]

            api.list_mail_folders(TOKEN)
            second_call_url = mock_request.call_args_list[1][0][1]
            assert second_call_url == next_link

    def test_list_folders_propagates_auth_error(self, api):
        """Test auth error is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAuthError(
                "Auth failed", status_code=401
            )

            with pytest.raises(api.OutlookAuthError):
                api.list_mail_folders("bad_token")

    def test_list_folders_propagates_api_error(self, api):
        """Test API error is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAPIError(
                "Server error", status_code=500
            )

            with pytest.raises(api.OutlookAPIError):
                api.list_mail_folders(TOKEN)


# ============================================================================
# get_messages_delta
# ============================================================================

class TestGetMessagesDelta:
    """Test get_messages_delta."""

    def test_delta_initial_sync(self, api):
        """Test initial delta sync without prior delta link."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [{"id": "msg1", "subject": "Test"}],
                "@odata.deltaLink": "https://graph.microsoft.com/delta?token=abc",
            }

            result = api.get_messages_delta(TOKEN, "folder_id")
            assert len(result["messages"]) == 1
            assert result["messages"][0]["subject"] == "Test"
            assert result["delta_link"] == "https://graph.microsoft.com/delta?token=abc"
            assert result["next_link"] is None

    def test_delta_initial_sync_url(self, api):
        """Test initial sync constructs the correct URL with folder ID and select fields."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": [], "@odata.deltaLink": "link"}

            api.get_messages_delta(TOKEN, "my_folder_123")
            called_url = mock_request.call_args[0][1]
            assert "me/mailFolders/my_folder_123/messages/delta" in called_url
            assert "$select=" in called_url
            assert "$top=50" in called_url

    def test_delta_initial_sync_default_select_fields(self, api):
        """Test default select fields include expected message properties."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": [], "@odata.deltaLink": "link"}

            api.get_messages_delta(TOKEN, "folder_id")
            called_url = mock_request.call_args[0][1]
            for field in ["id", "subject", "from", "toRecipients", "receivedDateTime", "body", "hasAttachments"]:
                assert field in called_url

    def test_delta_custom_select_fields(self, api):
        """Test custom select fields are used in the URL."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": [], "@odata.deltaLink": "link"}

            api.get_messages_delta(TOKEN, "folder_id", select_fields="id,subject")
            called_url = mock_request.call_args[0][1]
            assert "$select=id,subject" in called_url

    def test_delta_incremental_sync(self, api):
        """Test incremental sync uses the delta link directly."""
        delta_link = "https://graph.microsoft.com/delta?token=abc"
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [{"id": "msg2", "subject": "New"}],
                "@odata.deltaLink": "https://graph.microsoft.com/delta?token=xyz",
            }

            result = api.get_messages_delta(TOKEN, "folder_id", delta_link=delta_link)
            assert len(result["messages"]) == 1
            # Should use the delta link directly as URL
            mock_request.assert_called_once_with(TOKEN, delta_link)

    def test_delta_with_next_link(self, api):
        """Test delta response with pagination (next_link set, delta_link is None)."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [{"id": "msg1"}],
                "@odata.nextLink": "https://graph.microsoft.com/next-page",
            }

            result = api.get_messages_delta(TOKEN, "folder_id")
            assert result["next_link"] == "https://graph.microsoft.com/next-page"
            assert result["delta_link"] is None

    def test_delta_next_link_takes_precedence_over_delta_link(self, api):
        """Test that when both nextLink and deltaLink are present, nextLink wins."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [{"id": "msg1"}],
                "@odata.nextLink": "https://graph.microsoft.com/next",
                "@odata.deltaLink": "https://graph.microsoft.com/delta",
            }

            result = api.get_messages_delta(TOKEN, "folder_id")
            assert result["next_link"] == "https://graph.microsoft.com/next"
            # delta_link is None because nextLink branch was taken first
            assert result["delta_link"] is None

    def test_delta_empty_response(self, api):
        """Test delta with no new messages still returns delta_link."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [],
                "@odata.deltaLink": "https://graph.microsoft.com/delta?token=same",
            }

            result = api.get_messages_delta(TOKEN, "folder_id")
            assert result["messages"] == []
            assert result["delta_link"] is not None

    def test_delta_no_next_or_delta_link(self, api):
        """Test response with neither nextLink nor deltaLink."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [{"id": "msg1"}],
            }

            result = api.get_messages_delta(TOKEN, "folder_id")
            assert len(result["messages"]) == 1
            assert result["next_link"] is None
            assert result["delta_link"] is None

    def test_delta_propagates_auth_error(self, api):
        """Test auth error is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAuthError(
                "Auth failed", status_code=401
            )

            with pytest.raises(api.OutlookAuthError):
                api.get_messages_delta("bad_token", "folder_id")

    def test_delta_propagates_api_error(self, api):
        """Test API error is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAPIError(
                "Server error", status_code=500
            )

            with pytest.raises(api.OutlookAPIError):
                api.get_messages_delta(TOKEN, "folder_id")


# ============================================================================
# get_message
# ============================================================================

class TestGetMessage:
    """Test get_message."""

    def test_get_message_success(self, api):
        """Test fetching a single message by ID."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "id": "msg1",
                "subject": "Test Email",
                "body": {"content": "<p>Hello</p>"},
            }

            result = api.get_message(TOKEN, "msg1")
            assert result["subject"] == "Test Email"
            assert result["body"]["content"] == "<p>Hello</p>"

    def test_get_message_calls_correct_url(self, api):
        """Test the correct URL is constructed with message ID."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {}

            api.get_message(TOKEN, "abc-123")
            mock_request.assert_called_once_with(
                TOKEN, f"{GRAPH_BASE}/me/messages/abc-123"
            )

    def test_get_message_passes_token(self, api):
        """Test access token is forwarded to _make_request."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {}

            api.get_message("special_token", "msg1")
            assert mock_request.call_args[0][0] == "special_token"

    def test_get_message_propagates_auth_error(self, api):
        """Test auth error is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAuthError(
                "Auth failed", status_code=401
            )

            with pytest.raises(api.OutlookAuthError):
                api.get_message("bad_token", "msg1")

    def test_get_message_propagates_not_found(self, api):
        """Test API error is propagated when message does not exist."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAPIError(
                "Not found", status_code=404
            )

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api.get_message(TOKEN, "nonexistent")
            assert exc_info.value.status_code == 404


# ============================================================================
# list_attachments
# ============================================================================

class TestListAttachments:
    """Test list_attachments."""

    def test_list_attachments_success(self, api):
        """Test listing message attachments."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {
                "value": [
                    {"id": "att1", "name": "doc.pdf", "contentType": "application/pdf"},
                    {"id": "att2", "name": "image.png", "contentType": "image/png", "isInline": True},
                ]
            }

            result = api.list_attachments(TOKEN, "msg1")
            assert len(result) == 2
            assert result[0]["name"] == "doc.pdf"
            assert result[1]["isInline"] is True

    def test_list_attachments_empty(self, api):
        """Test listing attachments when none exist."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": []}

            result = api.list_attachments(TOKEN, "msg1")
            assert result == []

    def test_list_attachments_calls_correct_url(self, api):
        """Test the correct URL is constructed with message ID."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": []}

            api.list_attachments(TOKEN, "msg-abc-123")
            mock_request.assert_called_once_with(
                TOKEN, f"{GRAPH_BASE}/me/messages/msg-abc-123/attachments"
            )

    def test_list_attachments_passes_token(self, api):
        """Test access token is forwarded."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.return_value = {"value": []}

            api.list_attachments("my_token", "msg1")
            assert mock_request.call_args[0][0] == "my_token"

    def test_list_attachments_propagates_auth_error(self, api):
        """Test auth error is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAuthError(
                "Auth failed", status_code=401
            )

            with pytest.raises(api.OutlookAuthError):
                api.list_attachments("bad_token", "msg1")

    def test_list_attachments_propagates_api_error(self, api):
        """Test API error is propagated."""
        with patch.object(api, "_make_request") as mock_request:
            mock_request.side_effect = api.OutlookAPIError(
                "Server error", status_code=500
            )

            with pytest.raises(api.OutlookAPIError):
                api.list_attachments(TOKEN, "msg1")


# ============================================================================
# download_attachment
# ============================================================================

class TestDownloadAttachment:
    """Test download_attachment."""

    def test_download_attachment_success(self, api):
        """Test downloading attachment content returns bytes."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response(
                b"PDF content bytes", is_bytes=True
            )

            result = api.download_attachment(TOKEN, "msg1", "att1")
            assert result == b"PDF content bytes"

    def test_download_attachment_returns_bytes_type(self, api):
        """Test download returns bytes, not str."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            binary_content = b"\x89PNG\r\n\x1a\n\x00\x00"
            mock_urlopen.return_value = _mock_urlopen_response(
                binary_content, is_bytes=True
            )

            result = api.download_attachment(TOKEN, "msg1", "att1")
            assert isinstance(result, bytes)
            assert result == binary_content

    def test_download_attachment_calls_correct_url(self, api):
        """Test the correct URL is constructed with message and attachment IDs."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

            api.download_attachment(TOKEN, "msg-abc", "att-xyz")
            request = mock_urlopen.call_args[0][0]
            expected = f"{GRAPH_BASE}/me/messages/msg-abc/attachments/att-xyz/$value"
            assert request.full_url == expected

    def test_download_attachment_sets_auth_header(self, api):
        """Test download request includes Bearer token."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

            api.download_attachment("my_token", "msg1", "att1")
            request = mock_urlopen.call_args[0][0]
            assert request.get_header("Authorization") == "Bearer my_token"

    def test_download_attachment_uses_download_timeout(self, api):
        """Test download uses the longer DOWNLOAD_TIMEOUT (300s)."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response(b"data", is_bytes=True)

            api.download_attachment(TOKEN, "msg1", "att1")
            call_args = mock_urlopen.call_args
            assert api.DOWNLOAD_TIMEOUT in call_args[0] or \
                call_args[1].get("timeout") == api.DOWNLOAD_TIMEOUT

    def test_download_attachment_http_error_404(self, api):
        """Test download failure with 404 raises OutlookAPIError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://graph.microsoft.com",
                code=404,
                msg="Not Found",
                hdrs={},
                fp=None,
            )

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api.download_attachment(TOKEN, "msg1", "att1")
            assert exc_info.value.status_code == 404

    def test_download_attachment_http_error_500(self, api):
        """Test download failure with 500 raises OutlookAPIError."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://graph.microsoft.com",
                code=500,
                msg="Internal Server Error",
                hdrs={},
                fp=None,
            )

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api.download_attachment(TOKEN, "msg1", "att1")
            assert exc_info.value.status_code == 500

    def test_download_attachment_error_message(self, api):
        """Test error message includes status code."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://graph.microsoft.com",
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=None,
            )

            with pytest.raises(api.OutlookAPIError) as exc_info:
                api.download_attachment(TOKEN, "msg1", "att1")
            assert "403" in str(exc_info.value)

    def test_download_attachment_empty_content(self, api):
        """Test downloading attachment with empty content."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_response(b"", is_bytes=True)

            result = api.download_attachment(TOKEN, "msg1", "att1")
            assert result == b""

    def test_download_attachment_large_content(self, api):
        """Test downloading a large attachment."""
        with patch.object(api.urllib.request, "urlopen") as mock_urlopen:
            large_content = b"x" * (1024 * 1024)  # 1MB
            mock_urlopen.return_value = _mock_urlopen_response(
                large_content, is_bytes=True
            )

            result = api.download_attachment(TOKEN, "msg1", "att1")
            assert len(result) == 1024 * 1024


# ============================================================================
# Constants
# ============================================================================

class TestConstants:
    """Test module-level constants."""

    def test_graph_api_base_url(self, api):
        """Test the Graph API base URL is correct."""
        assert api.GRAPH_API_BASE == "https://graph.microsoft.com/v1.0"

    def test_request_timeout(self, api):
        """Test the default request timeout is 60 seconds."""
        assert api.REQUEST_TIMEOUT == 60

    def test_download_timeout(self, api):
        """Test the download timeout is 300 seconds."""
        assert api.DOWNLOAD_TIMEOUT == 300

    def test_download_timeout_greater_than_request_timeout(self, api):
        """Test the download timeout is longer than the request timeout."""
        assert api.DOWNLOAD_TIMEOUT > api.REQUEST_TIMEOUT


# ============================================================================
# Module identity checks
# ============================================================================

class TestModuleIdentity:
    """Verify both module copies are loaded and functional independently."""

    def test_fi_module_has_all_public_functions(self):
        """Test file-ingestion copy exports all expected public functions."""
        assert callable(fi_module.get_user_profile)
        assert callable(fi_module.list_mail_folders)
        assert callable(fi_module.get_messages_delta)
        assert callable(fi_module.get_message)
        assert callable(fi_module.list_attachments)
        assert callable(fi_module.download_attachment)

    def test_os_module_has_all_public_functions(self):
        """Test outlook-sync copy exports all expected public functions."""
        assert callable(os_module.get_user_profile)
        assert callable(os_module.list_mail_folders)
        assert callable(os_module.get_messages_delta)
        assert callable(os_module.get_message)
        assert callable(os_module.list_attachments)
        assert callable(os_module.download_attachment)

    def test_fi_module_has_exception_classes(self):
        """Test file-ingestion copy exports exception classes."""
        assert issubclass(fi_module.OutlookAPIError, Exception)
        assert issubclass(fi_module.OutlookAuthError, fi_module.OutlookAPIError)

    def test_os_module_has_exception_classes(self):
        """Test outlook-sync copy exports exception classes."""
        assert issubclass(os_module.OutlookAPIError, Exception)
        assert issubclass(os_module.OutlookAuthError, os_module.OutlookAPIError)

    def test_modules_are_distinct_objects(self):
        """Test the two loaded modules are separate objects."""
        assert fi_module is not os_module

    def test_exception_classes_are_distinct(self):
        """Test exception classes from different modules are distinct types."""
        # They are separate class objects even though code is identical
        assert fi_module.OutlookAPIError is not os_module.OutlookAPIError
        assert fi_module.OutlookAuthError is not os_module.OutlookAuthError

    def test_fi_module_has_internal_function(self):
        """Test file-ingestion copy exports _make_request."""
        assert callable(fi_module._make_request)

    def test_os_module_has_internal_function(self):
        """Test outlook-sync copy exports _make_request."""
        assert callable(os_module._make_request)
