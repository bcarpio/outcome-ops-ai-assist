# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Confluence REST API v2 client.

Handles pagination, rate limiting, and error handling for Confluence Cloud API.
Uses OAuth 2.0 access tokens for authentication.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Confluence API base URL
CONFLUENCE_API_BASE = "https://api.atlassian.com/ex/confluence"

# Timeouts
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300


class ConfluenceAPIError(Exception):
    """Exception for Confluence API errors."""

    def __init__(self, message: str, status_code: int = None, response: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class ConfluenceAuthError(ConfluenceAPIError):
    """Exception for authentication errors (401/403)."""
    pass


def _make_request(
    access_token: str,
    url: str,
    method: str = "GET",
    timeout: int = REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    """
    Make an authenticated request to the Confluence API.

    Args:
        access_token: OAuth access token
        url: Full URL to request
        method: HTTP method
        timeout: Request timeout in seconds

    Returns:
        JSON response as dict

    Raises:
        ConfluenceAuthError: For 401/403 responses
        ConfluenceAPIError: For other errors
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
            raise ConfluenceAuthError(
                f"Authentication failed: {e.code}",
                status_code=e.code,
                response=error_body,
            )
        raise ConfluenceAPIError(
            f"API error: {e.code} - {error_body}",
            status_code=e.code,
            response=error_body,
        )
    except urllib.error.URLError as e:
        raise ConfluenceAPIError(f"Network error: {e.reason}")


def _paginate(
    access_token: str,
    base_url: str,
    items_key: str = "results",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Paginate through a Confluence API endpoint.

    Uses cursor-based pagination for v2 API.

    Args:
        access_token: OAuth access token
        base_url: Base URL without pagination params
        items_key: Key in response containing items
        limit: Items per page

    Returns:
        List of all items
    """
    all_items = []
    separator = "&" if "?" in base_url else "?"
    url = f"{base_url}{separator}limit={limit}"

    while url:
        response = _make_request(access_token, url)
        items = response.get(items_key, [])
        all_items.extend(items)

        # Check for next page (cursor-based)
        links = response.get("_links", {})
        next_link = links.get("next")
        if next_link:
            # Next link is relative, need to build full URL
            if next_link.startswith("/"):
                url = f"https://api.atlassian.com{next_link}"
            else:
                url = next_link
        else:
            url = None

    return all_items


def get_accessible_resources(access_token: str) -> List[Dict[str, Any]]:
    """
    Get list of Confluence sites accessible with the token.

    Returns:
        List of accessible resources with id, url, name
    """
    url = "https://api.atlassian.com/oauth/token/accessible-resources"
    resources = _make_request(access_token, url)

    logger.info(f"Accessible resources: {resources}")

    # Filter to Confluence - check scopes list for any confluence scope
    confluence_resources = []
    for r in resources:
        scopes = r.get("scopes", [])
        # Check if any scope contains "confluence"
        has_confluence = any("confluence" in s for s in scopes)
        if has_confluence:
            confluence_resources.append(r)
            logger.info(f"Found Confluence resource: {r.get('name')} ({r.get('id')})")

    if not confluence_resources:
        logger.warning(f"No Confluence resources found. All resources: {[r.get('name') for r in resources]}")

    return confluence_resources


def list_all_spaces(access_token: str, cloud_id: str) -> List[Dict[str, Any]]:
    """
    List all spaces in a Confluence site.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID

    Returns:
        List of spaces with id, key, name, type
    """
    url = f"{CONFLUENCE_API_BASE}/{cloud_id}/wiki/api/v2/spaces"
    return _paginate(access_token, url)


def list_pages_in_space(
    access_token: str,
    cloud_id: str,
    space_id: str,
) -> List[Dict[str, Any]]:
    """
    List all pages in a space.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        space_id: Space ID

    Returns:
        List of pages
    """
    url = f"{CONFLUENCE_API_BASE}/{cloud_id}/wiki/api/v2/spaces/{space_id}/pages"
    return _paginate(access_token, url)


def get_page_content(
    access_token: str,
    cloud_id: str,
    page_id: str,
) -> Dict[str, Any]:
    """
    Get page content in storage format.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        page_id: Page ID

    Returns:
        Page with body content
    """
    url = f"{CONFLUENCE_API_BASE}/{cloud_id}/wiki/api/v2/pages/{page_id}?body-format=storage"
    return _make_request(access_token, url)


def list_page_attachments(
    access_token: str,
    cloud_id: str,
    page_id: str,
) -> List[Dict[str, Any]]:
    """
    List attachments on a page.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        page_id: Page ID

    Returns:
        List of attachments
    """
    url = f"{CONFLUENCE_API_BASE}/{cloud_id}/wiki/api/v2/pages/{page_id}/attachments"
    return _paginate(access_token, url)


def search_pages_modified_since(
    access_token: str,
    cloud_id: str,
    space_key: str,
    since_date: str,
) -> List[Dict[str, Any]]:
    """
    Search for pages modified since a date using CQL.

    Note: CQL only filters by date, not time. Client-side filtering may be needed.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        space_key: Space key
        since_date: ISO date string (YYYY-MM-DD)

    Returns:
        List of matching pages
    """
    # CQL query for pages modified since date
    cql = f'space = "{space_key}" AND type = page AND lastModified >= "{since_date}"'
    encoded_cql = urllib.parse.quote(cql)
    url = f"{CONFLUENCE_API_BASE}/{cloud_id}/wiki/rest/api/content/search?cql={encoded_cql}"

    return _paginate(access_token, url)


def download_attachment(
    access_token: str,
    cloud_id: str,
    attachment_id: str,
) -> bytes:
    """
    Download attachment content.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        attachment_id: Attachment ID

    Returns:
        Attachment content as bytes
    """
    # First get attachment metadata to find download URL
    url = f"{CONFLUENCE_API_BASE}/{cloud_id}/wiki/api/v2/attachments/{attachment_id}"
    attachment = _make_request(access_token, url)

    download_url = attachment.get("downloadLink")
    if not download_url:
        raise ConfluenceAPIError(f"No download link for attachment {attachment_id}")

    # Download the file
    if download_url.startswith("/"):
        download_url = f"{CONFLUENCE_API_BASE}/{cloud_id}/wiki{download_url}"

    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    try:
        request = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise ConfluenceAPIError(
            f"Failed to download attachment: {e.code}",
            status_code=e.code,
        )
