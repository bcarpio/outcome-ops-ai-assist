"""
Microsoft Graph API client for Outlook Mail.

Handles requests to Microsoft Graph API for mail operations.
Uses OAuth 2.0 access tokens for authentication.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Microsoft Graph API base URL
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Timeouts
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300


class OutlookAPIError(Exception):
    """Exception for Microsoft Graph API errors."""

    def __init__(self, message: str, status_code: int = None, response: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class OutlookAuthError(OutlookAPIError):
    """Exception for authentication errors (401/403)."""
    pass


def _make_request(
    access_token: str,
    url: str,
    method: str = "GET",
    timeout: int = REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    """
    Make an authenticated request to the Microsoft Graph API.

    Args:
        access_token: OAuth access token
        url: Full URL to request
        method: HTTP method
        timeout: Request timeout in seconds

    Returns:
        JSON response as dict

    Raises:
        OutlookAuthError: For 401/403 responses
        OutlookAPIError: For other errors
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    try:
        request = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        if e.code in (401, 403):
            raise OutlookAuthError(
                f"Authentication failed: {e.code}",
                status_code=e.code,
                response=error_body,
            )
        raise OutlookAPIError(
            f"API error: {e.code} - {error_body}",
            status_code=e.code,
            response=error_body,
        )
    except urllib.error.URLError as e:
        raise OutlookAPIError(f"Network error: {e.reason}")


def get_user_profile(access_token: str) -> Dict[str, Any]:
    """
    Get the authenticated user's profile.

    Returns:
        User profile with displayName, mail, userPrincipalName
    """
    url = f"{GRAPH_API_BASE}/me"
    return _make_request(access_token, url)


def list_mail_folders(access_token: str) -> List[Dict[str, Any]]:
    """
    List all mail folders for the authenticated user.

    Returns:
        List of mail folders with id, displayName, totalItemCount, unreadItemCount
    """
    all_folders = []
    url = f"{GRAPH_API_BASE}/me/mailFolders?$top=100"

    while url:
        response = _make_request(access_token, url)
        folders = response.get("value", [])
        all_folders.extend(folders)

        # Check for next page
        url = response.get("@odata.nextLink")

    return all_folders


def get_messages_delta(
    access_token: str,
    folder_id: str,
    delta_link: Optional[str] = None,
    select_fields: str = "id,subject,from,toRecipients,receivedDateTime,body,hasAttachments",
) -> Dict[str, Any]:
    """
    Get messages using delta query for incremental sync.

    Args:
        access_token: OAuth access token
        folder_id: Mail folder ID
        delta_link: Previous delta link for incremental sync (None for initial)
        select_fields: Comma-separated fields to select

    Returns:
        Dict with 'messages' list, 'delta_link' for next sync, and 'next_link' if more pages
    """
    if delta_link:
        url = delta_link
    else:
        url = (
            f"{GRAPH_API_BASE}/me/mailFolders/{folder_id}/messages/delta"
            f"?$select={select_fields}&$top=50"
        )

    result = {"messages": [], "delta_link": None, "next_link": None}

    response = _make_request(access_token, url)
    messages = response.get("value", [])
    result["messages"] = messages

    # Check for next page or delta link
    if "@odata.nextLink" in response:
        result["next_link"] = response["@odata.nextLink"]
    elif "@odata.deltaLink" in response:
        result["delta_link"] = response["@odata.deltaLink"]

    return result


def get_message(
    access_token: str,
    message_id: str,
) -> Dict[str, Any]:
    """
    Get a single message by ID.

    Args:
        access_token: OAuth access token
        message_id: Message ID

    Returns:
        Message object
    """
    url = f"{GRAPH_API_BASE}/me/messages/{message_id}"
    return _make_request(access_token, url)


def list_attachments(
    access_token: str,
    message_id: str,
) -> List[Dict[str, Any]]:
    """
    List attachments for a message.

    Args:
        access_token: OAuth access token
        message_id: Message ID

    Returns:
        List of attachments
    """
    url = f"{GRAPH_API_BASE}/me/messages/{message_id}/attachments"
    response = _make_request(access_token, url)
    return response.get("value", [])


def download_attachment(
    access_token: str,
    message_id: str,
    attachment_id: str,
) -> bytes:
    """
    Download attachment content.

    Args:
        access_token: OAuth access token
        message_id: Message ID
        attachment_id: Attachment ID

    Returns:
        Attachment content as bytes
    """
    url = f"{GRAPH_API_BASE}/me/messages/{message_id}/attachments/{attachment_id}/$value"

    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise OutlookAPIError(
            f"Failed to download attachment: {e.code}",
            status_code=e.code,
        )
