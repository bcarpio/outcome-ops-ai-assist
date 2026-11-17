# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Microsoft Graph API client for OneDrive.

Wraps the Graph API endpoints needed to enumerate drives, browse folders,
and run incremental delta sync. Uses delegated permissions with
user-consented OAuth tokens.

Endpoints used:
  - User profile:   GET /me
  - User's drive:   GET /me/drive
  - Listing:        GET /me/drive/root/children
                    GET /drives/{drive_id}/items/{item_id}/children
  - Get item:       GET /drives/{drive_id}/items/{item_id}
  - Delta sync:     GET /drives/{drive_id}/root/delta
                    GET /drives/{drive_id}/items/{folder_id}/delta
  - Download URL:   GET /drives/{drive_id}/items/{item_id}?select=@microsoft.graph.downloadUrl
  - Download blob:  GET <pre-authenticated download URL>
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300


class OneDriveAPIError(Exception):
    """Microsoft Graph API error."""

    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class OneDriveAuthError(OneDriveAPIError):
    """Authentication / authorization error (401, 403)."""
    pass


def _make_request(
    access_token: str,
    url: str,
    method: str = "GET",
    timeout: int = REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    """Authenticated GET to Microsoft Graph. Returns parsed JSON.

    `url` may be a full URL (used for paginated nextLinks and delta links)
    or a path that gets prefixed with GRAPH_API_BASE.
    """
    if not url.startswith("http"):
        url = f"{GRAPH_API_BASE}{url}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    try:
        request = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        if e.code in (401, 403):
            raise OneDriveAuthError(
                f"Authentication failed: {e.code}",
                status_code=e.code,
                response=error_body,
            )
        if e.code == 404:
            return {"value": []}
        raise OneDriveAPIError(
            f"API error: {e.code} - {error_body[:500]}",
            status_code=e.code,
            response=error_body,
        )
    except urllib.error.URLError as e:
        raise OneDriveAPIError(f"Network error: {e.reason}")


def _paginate(
    access_token: str,
    url: str,
    max_items: int = 10000,
) -> List[Dict[str, Any]]:
    """Walk @odata.nextLink pagination until exhausted (or max_items hit)."""
    items: List[Dict] = []
    next_url: Optional[str] = url

    while next_url and len(items) < max_items:
        data = _make_request(access_token, next_url)
        items.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")

    return items[:max_items]


# ============================================================================
# User & drive info
# ============================================================================


def get_user_profile(access_token: str) -> Dict[str, Any]:
    """Get the authenticated user's profile (id, mail, displayName, etc.)."""
    return _make_request(access_token, "/me")


def get_user_drive(access_token: str) -> Dict[str, Any]:
    """Get the user's default OneDrive: id, name, driveType, quota."""
    return _make_request(access_token, "/me/drive")


# ============================================================================
# Folder operations
# ============================================================================


def list_root_children(access_token: str) -> List[Dict[str, Any]]:
    """List items (files and folders) in the root of the user's OneDrive."""
    return _paginate(access_token, "/me/drive/root/children")


def list_folder_children(
    access_token: str,
    drive_id: str,
    item_id: str,
) -> List[Dict[str, Any]]:
    """List items in a specific folder by drive_id + folder item_id."""
    return _paginate(access_token, f"/drives/{drive_id}/items/{item_id}/children")


def get_item(
    access_token: str,
    drive_id: str,
    item_id: str,
) -> Optional[Dict[str, Any]]:
    """Get a specific drive item by ID. Returns None if not found."""
    try:
        result = _make_request(access_token, f"/drives/{drive_id}/items/{item_id}")
        if not result or not result.get("id"):
            return None
        return result
    except OneDriveAPIError:
        return None


# ============================================================================
# Delta sync
# ============================================================================


def get_drive_delta(
    access_token: str,
    drive_id: str,
    delta_token: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Get changes in a drive since last sync.

    Args:
        access_token: Valid OAuth access token
        drive_id: Drive ID
        delta_token: deltaLink URL from previous call (None for initial full sync)

    Returns:
        (changed_items, new_delta_link)
        - changed_items: added / modified / deleted items
        - new_delta_link: full URL to use as `delta_token` on the next call
    """
    return _walk_delta(
        access_token,
        delta_token or f"/drives/{drive_id}/root/delta",
    )


def get_folder_delta(
    access_token: str,
    drive_id: str,
    folder_id: str,
    delta_token: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Same as get_drive_delta but scoped to a specific folder subtree."""
    return _walk_delta(
        access_token,
        delta_token or f"/drives/{drive_id}/items/{folder_id}/delta",
    )


def _walk_delta(
    access_token: str,
    starting_url: str,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Shared delta-pagination loop for both drive- and folder-scoped delta."""
    items: List[Dict] = []
    url: Optional[str] = starting_url
    new_delta_link: Optional[str] = None

    while url:
        data = _make_request(access_token, url)
        items.extend(data.get("value", []))

        next_link = data.get("@odata.nextLink")
        delta_link = data.get("@odata.deltaLink")

        if next_link:
            url = next_link
        else:
            url = None
            new_delta_link = delta_link

    return items, new_delta_link


# ============================================================================
# Download
# ============================================================================


def get_item_download_url(
    access_token: str,
    drive_id: str,
    item_id: str,
) -> Optional[str]:
    """Get a short-lived pre-authenticated download URL for a file.

    The URL is signed and does not require the Bearer token to fetch the
    bytes - safe to hand off to a downstream Lambda or to use directly
    with download_file_content().
    """
    try:
        url = (
            f"/drives/{drive_id}/items/{item_id}"
            f"?{urllib.parse.urlencode({'select': '@microsoft.graph.downloadUrl'})}"
        )
        data = _make_request(access_token, url)
        return data.get("@microsoft.graph.downloadUrl")
    except OneDriveAPIError:
        return None


def download_file_content(download_url: str, timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    """Download file bytes from a pre-authenticated URL.

    No Authorization header needed - the URL itself is signed.
    """
    try:
        request = urllib.request.Request(download_url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise OneDriveAPIError(f"Download failed: {e.code}", status_code=e.code)
    except urllib.error.URLError as e:
        raise OneDriveAPIError(f"Download network error: {e.reason}")


# ============================================================================
# Helpers
# ============================================================================


def is_folder(item: Dict) -> bool:
    return "folder" in item


def is_file(item: Dict) -> bool:
    return "file" in item


def is_deleted(item: Dict) -> bool:
    """True if a delta-sync item represents a deletion."""
    return item.get("deleted") is not None or "@removed" in item


def get_item_path(item: Dict) -> str:
    """Path of an item relative to the drive root, e.g. '/Documents/foo/bar.pdf'.

    Built from `parentReference.path` (which Graph returns as
    `/drive/root:/Documents/foo`) plus the item name.
    """
    parent_ref = item.get("parentReference", {}) or {}
    parent_path = parent_ref.get("path", "") or ""

    if ":" in parent_path:
        parent_path = parent_path.split(":", 1)[1]

    name = item.get("name", "") or ""
    if parent_path:
        return f"{parent_path}/{name}"
    return f"/{name}"
