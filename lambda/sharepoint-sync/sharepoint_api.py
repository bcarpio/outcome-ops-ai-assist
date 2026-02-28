"""
Microsoft Graph API client for SharePoint (sync copy).
Shared module - identical to sharepoint-integration/sharepoint_api.py
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Microsoft Graph API base URL
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Timeouts
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300


class SharePointAPIError(Exception):
    """Exception for Microsoft Graph API errors."""

    def __init__(self, message: str, status_code: int = None, response: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class SharePointAuthError(SharePointAPIError):
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
            raise SharePointAuthError(
                f"Authentication failed: {e.code}",
                status_code=e.code,
                response=error_body,
            )
        raise SharePointAPIError(
            f"API error: {e.code} - {error_body}",
            status_code=e.code,
            response=error_body,
        )
    except urllib.error.URLError as e:
        raise SharePointAPIError(f"Network error: {e.reason}")


def get_drive_delta(
    access_token: str,
    drive_id: str,
    delta_token: Optional[str] = None,
) -> Tuple[List[Dict], Optional[str]]:
    """Get drive items using delta query for incremental sync."""
    if delta_token:
        url = delta_token
    else:
        url = f"{GRAPH_API_BASE}/drives/{drive_id}/root/delta?$top=100"

    all_items = []
    new_delta = None

    while url:
        response = _make_request(access_token, url)
        items = response.get("value", [])
        all_items.extend(items)

        if "@odata.nextLink" in response:
            url = response["@odata.nextLink"]
        elif "@odata.deltaLink" in response:
            new_delta = response["@odata.deltaLink"]
            url = None
        else:
            url = None

    return all_items, new_delta


def list_site_pages(
    access_token: str,
    site_id: str,
) -> List[Dict[str, Any]]:
    """List SharePoint pages with canvas layout for content extraction."""
    all_pages = []
    url = f"{GRAPH_API_BASE}/sites/{site_id}/pages/microsoft.graph.sitePage?$expand=canvasLayout&$top=100"

    while url:
        try:
            response = _make_request(access_token, url)
            pages = response.get("value", [])
            all_pages.extend(pages)
            url = response.get("@odata.nextLink")
        except SharePointAPIError as e:
            logger.warning(f"Failed to list pages for site {site_id}: {e}")
            break

    return all_pages


def extract_page_text(page: Dict) -> str:
    """Extract text content from a SharePoint page's canvasLayout."""
    text_parts = []
    title = page.get("title", "Untitled")
    text_parts.append(f"# {title}\n")

    canvas = page.get("canvasLayout", {})
    sections = canvas.get("horizontalSections", [])

    for section in sections:
        columns = section.get("columns", [])
        for column in columns:
            webparts = column.get("webparts", [])
            for webpart in webparts:
                inner_html = webpart.get("innerHtml", "")
                if inner_html:
                    text = re.sub(r'<[^>]+>', ' ', inner_html)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if text:
                        text_parts.append(text)

    return "\n\n".join(text_parts)


def get_item_download_url(
    access_token: str,
    drive_id: str,
    item_id: str,
) -> Optional[str]:
    """Get the download URL for a drive item."""
    url = f"{GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}?select=@microsoft.graph.downloadUrl,name"
    try:
        response = _make_request(access_token, url)
        return response.get("@microsoft.graph.downloadUrl")
    except SharePointAPIError as e:
        logger.warning(f"Failed to get download URL for item {item_id}: {e}")
        return None
