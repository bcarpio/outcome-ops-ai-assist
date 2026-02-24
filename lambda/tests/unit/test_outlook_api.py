"""Unit tests for Outlook API client (outlook_api.py)."""

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
    """Load or reload the outlook_api module."""
    if 'outlook_api' in sys.modules:
        del sys.modules['outlook_api']

    api_path = os.path.join(os.path.dirname(__file__), '../../outlook-integration/outlook_api.py')
    spec = importlib.util.spec_from_file_location("outlook_api", api_path)
    api_module = importlib.util.module_from_spec(spec)
    sys.modules['outlook_api'] = api_module
    spec.loader.exec_module(api_module)
    return api_module


api_module = _load_api_module()

# Import from module
_make_request = api_module._make_request
get_user_profile = api_module.get_user_profile
list_mail_folders = api_module.list_mail_folders
get_messages_delta = api_module.get_messages_delta
get_message = api_module.get_message
list_attachments = api_module.list_attachments
download_attachment = api_module.download_attachment
OutlookAPIError = api_module.OutlookAPIError
OutlookAuthError = api_module.OutlookAuthError


# ============================================================================
# Exception Classes
# ============================================================================

class TestExceptions:
    """Test custom exception classes."""

    def test_outlook_api_error_attributes(self):
        """Test OutlookAPIError stores status code and response."""
        err = OutlookAPIError("Test error", status_code=500, response="body")
        assert str(err) == "Test error"
        assert err.status_code == 500
        assert err.response == "body"

    def test_outlook_auth_error_is_api_error(self):
        """Test OutlookAuthError inherits from OutlookAPIError."""
        err = OutlookAuthError("Auth failed", status_code=401)
        assert isinstance(err, OutlookAPIError)
        assert err.status_code == 401


# ============================================================================
# _make_request
# ============================================================================

class TestMakeRequest:
    """Test the core HTTP request function."""

    @patch('outlook_api.urllib.request.urlopen')
    def test_make_request_success(self, mock_urlopen):
        """Test successful API request."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"value": "test"}).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = _make_request("access_token", "https://graph.microsoft.com/v1.0/me")
        assert result == {"value": "test"}

    @patch('outlook_api.urllib.request.urlopen')
    def test_make_request_sets_auth_header(self, mock_urlopen):
        """Test request includes Bearer token header."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        _make_request("my_token", "https://example.com/api")
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my_token"

    @patch('outlook_api.urllib.request.urlopen')
    def test_make_request_401_raises_auth_error(self, mock_urlopen):
        """Test 401 response raises OutlookAuthError."""
        mock_error = urllib.error.HTTPError(
            url="https://example.com",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "unauthorized"}')),
        )
        mock_urlopen.side_effect = mock_error

        with pytest.raises(OutlookAuthError) as exc_info:
            _make_request("bad_token", "https://example.com/api")
        assert exc_info.value.status_code == 401

    @patch('outlook_api.urllib.request.urlopen')
    def test_make_request_403_raises_auth_error(self, mock_urlopen):
        """Test 403 response raises OutlookAuthError."""
        mock_error = urllib.error.HTTPError(
            url="https://example.com",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "forbidden"}')),
        )
        mock_urlopen.side_effect = mock_error

        with pytest.raises(OutlookAuthError) as exc_info:
            _make_request("token", "https://example.com/api")
        assert exc_info.value.status_code == 403

    @patch('outlook_api.urllib.request.urlopen')
    def test_make_request_500_raises_api_error(self, mock_urlopen):
        """Test 500 response raises OutlookAPIError."""
        mock_error = urllib.error.HTTPError(
            url="https://example.com",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'Internal error')),
        )
        mock_urlopen.side_effect = mock_error

        with pytest.raises(OutlookAPIError) as exc_info:
            _make_request("token", "https://example.com/api")
        assert exc_info.value.status_code == 500
        assert not isinstance(exc_info.value, OutlookAuthError)

    @patch('outlook_api.urllib.request.urlopen')
    def test_make_request_network_error(self, mock_urlopen):
        """Test network error raises OutlookAPIError."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with pytest.raises(OutlookAPIError) as exc_info:
            _make_request("token", "https://example.com/api")
        assert "Network error" in str(exc_info.value)


# ============================================================================
# API Functions
# ============================================================================

class TestGetUserProfile:
    """Test get_user_profile."""

    @patch('outlook_api._make_request')
    def test_get_user_profile_success(self, mock_request):
        """Test fetching user profile."""
        mock_request.return_value = {
            "displayName": "Test User",
            "mail": "test@example.com",
            "userPrincipalName": "test@example.com",
        }

        result = get_user_profile("access_token")
        assert result["displayName"] == "Test User"
        assert result["mail"] == "test@example.com"
        mock_request.assert_called_once_with("access_token", "https://graph.microsoft.com/v1.0/me")


class TestListMailFolders:
    """Test list_mail_folders."""

    @patch('outlook_api._make_request')
    def test_list_folders_single_page(self, mock_request):
        """Test listing folders with single page."""
        mock_request.return_value = {
            "value": [
                {"id": "f1", "displayName": "Inbox", "totalItemCount": 100},
                {"id": "f2", "displayName": "Sent Items", "totalItemCount": 50},
            ]
        }

        result = list_mail_folders("token")
        assert len(result) == 2
        assert result[0]["displayName"] == "Inbox"

    @patch('outlook_api._make_request')
    def test_list_folders_pagination(self, mock_request):
        """Test listing folders with pagination."""
        mock_request.side_effect = [
            {
                "value": [{"id": "f1", "displayName": "Inbox"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders?$skip=100",
            },
            {
                "value": [{"id": "f2", "displayName": "Archive"}],
            },
        ]

        result = list_mail_folders("token")
        assert len(result) == 2
        assert mock_request.call_count == 2

    @patch('outlook_api._make_request')
    def test_list_folders_empty(self, mock_request):
        """Test listing folders when none exist."""
        mock_request.return_value = {"value": []}

        result = list_mail_folders("token")
        assert result == []


class TestGetMessagesDelta:
    """Test get_messages_delta."""

    @patch('outlook_api._make_request')
    def test_delta_initial_sync(self, mock_request):
        """Test initial delta sync without prior delta link."""
        mock_request.return_value = {
            "value": [{"id": "msg1", "subject": "Test"}],
            "@odata.deltaLink": "https://graph.microsoft.com/delta?token=abc",
        }

        result = get_messages_delta("token", "folder_id")
        assert len(result["messages"]) == 1
        assert result["delta_link"] == "https://graph.microsoft.com/delta?token=abc"
        assert result["next_link"] is None

    @patch('outlook_api._make_request')
    def test_delta_incremental_sync(self, mock_request):
        """Test incremental sync with prior delta link."""
        delta_link = "https://graph.microsoft.com/delta?token=abc"
        mock_request.return_value = {
            "value": [{"id": "msg2", "subject": "New"}],
            "@odata.deltaLink": "https://graph.microsoft.com/delta?token=xyz",
        }

        result = get_messages_delta("token", "folder_id", delta_link=delta_link)
        assert len(result["messages"]) == 1
        # Should use the delta link directly as URL
        mock_request.assert_called_once_with("token", delta_link)

    @patch('outlook_api._make_request')
    def test_delta_with_next_link(self, mock_request):
        """Test delta response with pagination (next_link)."""
        mock_request.return_value = {
            "value": [{"id": "msg1"}],
            "@odata.nextLink": "https://graph.microsoft.com/next-page",
        }

        result = get_messages_delta("token", "folder_id")
        assert result["next_link"] == "https://graph.microsoft.com/next-page"
        assert result["delta_link"] is None

    @patch('outlook_api._make_request')
    def test_delta_empty_response(self, mock_request):
        """Test delta with no new messages."""
        mock_request.return_value = {
            "value": [],
            "@odata.deltaLink": "https://graph.microsoft.com/delta?token=same",
        }

        result = get_messages_delta("token", "folder_id")
        assert result["messages"] == []
        assert result["delta_link"] is not None


class TestGetMessage:
    """Test get_message."""

    @patch('outlook_api._make_request')
    def test_get_message_success(self, mock_request):
        """Test fetching a single message."""
        mock_request.return_value = {
            "id": "msg1",
            "subject": "Test Email",
            "body": {"content": "<p>Hello</p>"},
        }

        result = get_message("token", "msg1")
        assert result["subject"] == "Test Email"
        mock_request.assert_called_once_with(
            "token", "https://graph.microsoft.com/v1.0/me/messages/msg1"
        )


class TestListAttachments:
    """Test list_attachments."""

    @patch('outlook_api._make_request')
    def test_list_attachments_success(self, mock_request):
        """Test listing message attachments."""
        mock_request.return_value = {
            "value": [
                {"id": "att1", "name": "doc.pdf", "contentType": "application/pdf"},
                {"id": "att2", "name": "image.png", "contentType": "image/png", "isInline": True},
            ]
        }

        result = list_attachments("token", "msg1")
        assert len(result) == 2
        assert result[0]["name"] == "doc.pdf"

    @patch('outlook_api._make_request')
    def test_list_attachments_empty(self, mock_request):
        """Test listing attachments when none exist."""
        mock_request.return_value = {"value": []}

        result = list_attachments("token", "msg1")
        assert result == []


class TestDownloadAttachment:
    """Test download_attachment."""

    @patch('outlook_api.urllib.request.urlopen')
    def test_download_attachment_success(self, mock_urlopen):
        """Test downloading attachment content."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"PDF content bytes"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = download_attachment("token", "msg1", "att1")
        assert result == b"PDF content bytes"

    @patch('outlook_api.urllib.request.urlopen')
    def test_download_attachment_http_error(self, mock_urlopen):
        """Test download failure raises OutlookAPIError."""
        mock_error = urllib.error.HTTPError(
            url="https://example.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = mock_error

        with pytest.raises(OutlookAPIError) as exc_info:
            download_attachment("token", "msg1", "att1")
        assert exc_info.value.status_code == 404
