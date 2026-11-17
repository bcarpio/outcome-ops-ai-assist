# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Microsoft Graph API client for OneNote.

Wraps the Graph endpoints needed to enumerate notebooks/sections/pages
and fetch page HTML. Uses delegated permissions (Notes.Read.All) with
user-consented OAuth tokens.

Endpoints used:
  - User profile:     GET /me
  - Notebooks:        GET /me/onenote/notebooks
  - Sections:         GET /me/onenote/notebooks/{nb_id}/sections
  - Pages (per sec):  GET /me/onenote/sections/{sec_id}/pages
  - Page metadata:    GET /me/onenote/pages/{page_id}
  - Page content:     GET /me/onenote/pages/{page_id}/content  (returns HTML)

OneNote does NOT support delta sync. Change detection is done by the
caller via `lastModifiedDateTime` comparison stored per-page in DDB.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

REQUEST_TIMEOUT = 60
CONTENT_FETCH_TIMEOUT = 60


class OneNoteAPIError(Exception):
    """Microsoft Graph API error."""

    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class OneNoteAuthError(OneNoteAPIError):
    """Authentication / authorization error (401, 403)."""
    pass


def _make_request(
    access_token: str,
    url: str,
    timeout: int = REQUEST_TIMEOUT,
    accept: str = "application/json",
) -> Any:
    """Authenticated GET to Microsoft Graph.

    Returns parsed JSON when `accept` is application/json; raw text body
    (HTML) when `accept` is text/html. The caller chooses which they
    want via the accept parameter.

    `url` may be a full URL (used for paginated nextLinks) or a path that
    gets prefixed with GRAPH_API_BASE.
    """
    if not url.startswith("http"):
        url = f"{GRAPH_API_BASE}{url}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": accept,
    }

    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            if not body:
                return {} if accept == "application/json" else ""
            if accept == "application/json":
                return json.loads(body)
            return body
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        if e.code in (401, 403):
            raise OneNoteAuthError(
                f"Authentication failed: {e.code}",
                status_code=e.code,
                response=error_body,
            )
        if e.code == 404:
            return {"value": []} if accept == "application/json" else ""
        raise OneNoteAPIError(
            f"API error: {e.code} - {error_body[:500]}",
            status_code=e.code,
            response=error_body,
        )
    except urllib.error.URLError as e:
        raise OneNoteAPIError(f"Network error: {e.reason}")


def _paginate(
    access_token: str,
    url: str,
    params: Optional[Dict[str, str]] = None,
    max_items: int = 10000,
) -> List[Dict[str, Any]]:
    """Walk @odata.nextLink pagination until exhausted (or max_items hit)."""
    items: List[Dict] = []

    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"

    next_url: Optional[str] = url

    while next_url and len(items) < max_items:
        data = _make_request(access_token, next_url)
        items.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")

    return items[:max_items]


# ============================================================================
# User profile
# ============================================================================


def get_user_profile(access_token: str) -> Dict[str, Any]:
    """Get the authenticated user's profile (id, mail, displayName, etc.).

    The `id` field is used as the OneNote per-user license-tracking
    identity (`onenote:{user_id}`).
    """
    return _make_request(access_token, "/me")


# ============================================================================
# Notebooks
# ============================================================================


def list_notebooks(access_token: str) -> List[Dict[str, Any]]:
    """List all notebooks the user has access to (personal + shared)."""
    return _paginate(access_token, "/me/onenote/notebooks")


def get_notebook(access_token: str, notebook_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific notebook by ID; None if not found."""
    try:
        result = _make_request(access_token, f"/me/onenote/notebooks/{notebook_id}")
        if not result or not result.get("id"):
            return None
        return result
    except OneNoteAPIError:
        return None


# ============================================================================
# Sections
# ============================================================================


def list_sections(access_token: str, notebook_id: str) -> List[Dict[str, Any]]:
    """List all sections in a notebook."""
    return _paginate(access_token, f"/me/onenote/notebooks/{notebook_id}/sections")


# ============================================================================
# Pages
# ============================================================================


def list_pages(
    access_token: str,
    section_id: str,
    select: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all pages in a section.

    Args:
        select: Optional OData $select query. Pass e.g.
            "id,title,lastModifiedDateTime" to minimize payload when you
            only need metadata for change-detection.
    """
    params = {"$select": select} if select else None
    return _paginate(access_token, f"/me/onenote/sections/{section_id}/pages", params)


def get_page_metadata(access_token: str, page_id: str) -> Optional[Dict[str, Any]]:
    """Get metadata for a specific page; None if not found."""
    try:
        result = _make_request(access_token, f"/me/onenote/pages/{page_id}")
        if not result or not result.get("id"):
            return None
        return result
    except OneNoteAPIError:
        return None


def get_page_content(access_token: str, page_id: str) -> str:
    """Get the HTML content of a page.

    Returns the page body as an HTML string. Empty string on a Graph
    404 (the page might have been deleted between list and fetch).
    """
    return _make_request(
        access_token,
        f"/me/onenote/pages/{page_id}/content",
        timeout=CONTENT_FETCH_TIMEOUT,
        accept="text/html",
    )
