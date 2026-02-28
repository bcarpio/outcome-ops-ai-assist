"""
Microsoft Graph API client for SharePoint.

Handles requests to Microsoft Graph API for SharePoint operations.
Uses OAuth 2.0 access tokens for authentication.
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

# System sites to hide from users
SYSTEM_SITES = {"contentTypeHub", "contenttypehub", "appcatalog", "CompliancePolicyCenter"}

# Generic display names to fall back from
GENERIC_DISPLAY_NAMES = {"Team Site", "Communication site", "Communication Site"}


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


def get_user_profile(access_token: str) -> Dict[str, Any]:
    """Get the authenticated user's profile."""
    url = f"{GRAPH_API_BASE}/me"
    return _make_request(access_token, url)


def get_user_sites(access_token: str) -> List[Dict[str, Any]]:
    """
    Get available SharePoint sites.

    Searches for all sites and filters out system sites.
    Returns sites with a friendly display name.
    """
    all_sites = []
    url = f"{GRAPH_API_BASE}/sites?search=*&$top=100"

    while url:
        response = _make_request(access_token, url)
        sites = response.get("value", [])
        all_sites.extend(sites)
        url = response.get("@odata.nextLink")

    # Filter out system sites and add friendly names
    filtered = []
    for site in all_sites:
        site_name = site.get("name", "")
        if site_name in SYSTEM_SITES:
            continue

        site["friendly_name"] = _get_site_display_name(site)
        filtered.append(site)

    return filtered


def _get_site_display_name(site: Dict) -> str:
    """Get a friendly display name for a site."""
    display_name = site.get("displayName", "")
    name = site.get("name", "")

    # Use displayName if it's not generic
    if display_name and display_name not in GENERIC_DISPLAY_NAMES:
        return display_name

    # Fall back to name
    if name:
        return name

    # Extract from webUrl
    web_url = site.get("webUrl", "")
    if "/sites/" in web_url:
        return web_url.split("/sites/")[-1].strip("/")

    return display_name or "Unknown Site"


def list_site_drives(access_token: str, site_id: str) -> List[Dict[str, Any]]:
    """List document libraries (drives) for a site."""
    all_drives = []
    url = f"{GRAPH_API_BASE}/sites/{site_id}/drives"

    while url:
        response = _make_request(access_token, url)
        drives = response.get("value", [])
        all_drives.extend(drives)
        url = response.get("@odata.nextLink")

    return all_drives


def get_drive_delta(
    access_token: str,
    drive_id: str,
    delta_token: Optional[str] = None,
) -> Tuple[List[Dict], Optional[str]]:
    """
    Get drive items using delta query for incremental sync.

    Returns:
        Tuple of (items, new_delta_token)
    """
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
            # Pages API may not be available for all sites
            logger.warning(f"Failed to list pages for site {site_id}: {e}")
            break

    return all_pages


def get_page_content(
    access_token: str,
    site_id: str,
    page_id: str,
) -> Optional[Dict[str, Any]]:
    """Get a single page with canvas layout."""
    url = f"{GRAPH_API_BASE}/sites/{site_id}/pages/{page_id}/microsoft.graph.sitePage?$expand=canvasLayout"
    try:
        return _make_request(access_token, url)
    except SharePointAPIError as e:
        logger.warning(f"Failed to get page {page_id}: {e}")
        return None


def extract_page_text(page: Dict) -> str:
    """
    Extract text content from a SharePoint page's canvasLayout.

    Iterates through horizontalSections -> columns -> webparts -> innerHtml
    and strips HTML tags to get plain text.
    """
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
                    # Strip HTML tags
                    text = re.sub(r'<[^>]+>', ' ', inner_html)
                    # Normalize whitespace
                    text = re.sub(r'\s+', ' ', text).strip()
                    if text:
                        text_parts.append(text)

    return "\n\n".join(text_parts)


def download_file(
    download_url: str,
) -> bytes:
    """Download file content from a pre-authenticated URL."""
    try:
        request = urllib.request.Request(download_url)
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise SharePointAPIError(
            f"Failed to download file: {e.code}",
            status_code=e.code,
        )
