"""
Microsoft Graph API client for Teams (sync copy).
Shared module - identical to teams-integration/teams_api.py
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Microsoft Graph API base URL
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Timeouts
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300


class TeamsAPIError(Exception):
    """Exception for Microsoft Graph API errors."""

    def __init__(self, message: str, status_code: int = None, response: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class TeamsAuthError(TeamsAPIError):
    """Exception for authentication errors (401/403)."""
    pass


def _make_request(
    access_token: str,
    url: str,
    method: str = "GET",
    timeout: int = REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    """Make an authenticated request to the Microsoft Graph API."""
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
            raise TeamsAuthError(
                f"Authentication failed: {e.code}",
                status_code=e.code,
                response=error_body,
            )
        raise TeamsAPIError(
            f"API error: {e.code} - {error_body}",
            status_code=e.code,
            response=error_body,
        )
    except urllib.error.URLError as e:
        raise TeamsAPIError(f"Network error: {e.reason}")


def get_channel_messages(
    access_token: str,
    team_id: str,
    channel_id: str,
    top: int = 50,
    skip_token: Optional[str] = None,
) -> Tuple[List[Dict], Optional[str]]:
    """Get channel messages with pagination."""
    if skip_token:
        url = skip_token
    else:
        url = f"{GRAPH_API_BASE}/teams/{team_id}/channels/{channel_id}/messages?$top={top}"

    response = _make_request(access_token, url)
    messages = response.get("value", [])
    next_link = response.get("@odata.nextLink")

    return messages, next_link


def get_message_replies(
    access_token: str,
    team_id: str,
    channel_id: str,
    message_id: str,
) -> List[Dict[str, Any]]:
    """Get replies to a channel message."""
    all_replies = []
    url = f"{GRAPH_API_BASE}/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies?$top=50"

    while url:
        response = _make_request(access_token, url)
        replies = response.get("value", [])
        all_replies.extend(replies)
        url = response.get("@odata.nextLink")

    return all_replies


def get_messages_delta(
    access_token: str,
    team_id: str,
    channel_id: str,
    delta_link: Optional[str] = None,
) -> Dict[str, Any]:
    """Get messages using delta query for incremental sync."""
    if delta_link:
        url = delta_link
    else:
        url = f"{GRAPH_API_BASE}/teams/{team_id}/channels/{channel_id}/messages/delta"

    result = {"messages": [], "deleted_ids": [], "delta_link": None, "next_link": None}

    response = _make_request(access_token, url)
    items = response.get("value", [])

    for item in items:
        if "@removed" in item:
            item_id = item.get("id")
            if item_id:
                result["deleted_ids"].append(item_id)
        else:
            result["messages"].append(item)

    if "@odata.nextLink" in response:
        result["next_link"] = response["@odata.nextLink"]
    elif "@odata.deltaLink" in response:
        result["delta_link"] = response["@odata.deltaLink"]

    return result


def get_channel_files_folder(
    access_token: str,
    team_id: str,
    channel_id: str,
) -> Optional[Dict[str, Any]]:
    """Get the SharePoint files folder for a channel."""
    url = f"{GRAPH_API_BASE}/teams/{team_id}/channels/{channel_id}/filesFolder"
    try:
        return _make_request(access_token, url)
    except TeamsAPIError as e:
        logger.warning(f"Failed to get files folder for channel {channel_id}: {e}")
        return None


def list_folder_files(
    access_token: str,
    drive_id: str,
    folder_id: str,
) -> List[Dict[str, Any]]:
    """List all files in a drive folder."""
    all_files = []
    url = f"{GRAPH_API_BASE}/drives/{drive_id}/items/{folder_id}/children?$top=100"

    while url:
        response = _make_request(access_token, url)
        items = response.get("value", [])
        all_files.extend(items)
        url = response.get("@odata.nextLink")

    return all_files


def filter_messages_by_date(
    messages: List[Dict],
    days_back: int,
) -> List[Dict]:
    """Filter messages to only include those within days_back."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    filtered = []
    for msg in messages:
        created = msg.get("createdDateTime", "")
        if created:
            try:
                msg_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if msg_date >= cutoff:
                    filtered.append(msg)
            except ValueError:
                filtered.append(msg)
        else:
            filtered.append(msg)
    return filtered
