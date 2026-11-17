# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Jira Cloud REST API wrapper for OutcomeOps AI Assist.

Provides functions to interact with Jira Cloud via the REST API.
All API calls require a valid access token obtained via OAuth 2.0 (3LO).

Uses API v3 endpoints.
"""

import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Jira API base URL template
JIRA_API_BASE = "https://api.atlassian.com/ex/jira"

# Atlassian accessible resources URL
ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"


class JiraAPIError(Exception):
    """Jira API error with status code."""

    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


class JiraAuthError(JiraAPIError):
    """Authentication error (token expired/revoked)."""

    pass


def _make_request(
    method: str,
    url: str,
    access_token: str,
    params: Optional[Dict] = None,
    timeout: int = 60,
    stream: bool = False,
) -> requests.Response:
    """
    Make an authenticated request to Jira API.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Full URL to request
        access_token: OAuth access token
        params: Optional query parameters
        timeout: Request timeout in seconds
        stream: Whether to stream the response

    Returns:
        Response object

    Raises:
        JiraAuthError: If authentication fails (401)
        JiraAPIError: If request fails
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        timeout=timeout,
        stream=stream,
    )

    if response.status_code == 401:
        raise JiraAuthError("Authentication failed - token may be expired", 401)

    if not response.ok:
        try:
            error_body = response.text
            logger.error(f"Jira API error {response.status_code}: {error_body}")
        except Exception:
            pass
        raise JiraAPIError(
            f"Jira API error: {response.status_code}", response.status_code
        )

    return response


def get_accessible_resources(access_token: str) -> List[Dict]:
    """
    Get list of Atlassian sites the user has access to.

    Args:
        access_token: Valid access token

    Returns:
        List of site dicts: [{"id": "cloud_id", "name": "Site Name", "url": "https://xxx.atlassian.net"}]

    Raises:
        JiraAPIError: If API call fails
    """
    response = requests.get(
        ATLASSIAN_RESOURCES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )

    if not response.ok:
        logger.error(f"Failed to get accessible resources: {response.text}")
        raise JiraAPIError(f"Failed to get accessible resources: {response.status_code}")

    return response.json()


def list_all_projects(access_token: str, cloud_id: str) -> List[Dict]:
    """
    List all projects in a Jira site.

    Uses pagination to retrieve all projects.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID

    Returns:
        List of project dicts with id, key, name fields
    """
    base_url = f"{JIRA_API_BASE}/{cloud_id}/rest/api/3/project/search"

    projects = []
    start_at = 0
    max_results = 50

    while True:
        params = {"startAt": start_at, "maxResults": max_results}

        response = _make_request("GET", base_url, access_token, params=params)
        data = response.json()

        projects.extend(data.get("values", []))

        # Check for more pages
        if data.get("isLast", True) or len(data.get("values", [])) < max_results:
            break

        start_at += max_results

    logger.info(f"Retrieved {len(projects)} projects from cloud_id {cloud_id}")
    return projects


def search_issues(
    access_token: str,
    cloud_id: str,
    jql: str,
    fields: Optional[List[str]] = None,
    expand: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Search for issues using JQL.

    Uses pagination to retrieve all matching issues.
    Uses the /rest/api/3/search/jql endpoint.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        jql: JQL query string
        fields: Optional list of fields to return
        expand: Optional list of fields to expand

    Returns:
        List of issue dicts
    """
    base_url = f"{JIRA_API_BASE}/{cloud_id}/rest/api/3/search/jql"

    if fields is None:
        fields = [
            "summary",
            "description",
            "status",
            "assignee",
            "reporter",
            "created",
            "updated",
            "attachment",
            "comment",
            "priority",
            "issuetype",
            "labels",
        ]

    issues = []
    start_at = 0
    max_results = 100

    while True:
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": ",".join(fields),
        }
        if expand:
            params["expand"] = ",".join(expand)

        response = _make_request("GET", base_url, access_token, params=params)
        data = response.json()

        issues.extend(data.get("issues", []))

        total = data.get("total", 0)
        if start_at + max_results >= total:
            break

        start_at += max_results

    logger.info(f"Found {len(issues)} issues matching JQL: {jql[:100]}")
    return issues


def list_all_issues_in_project(
    access_token: str,
    cloud_id: str,
    project_key: str,
) -> List[Dict]:
    """
    List all issues in a project.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        project_key: Project key to list issues from

    Returns:
        List of issue dicts
    """
    jql = f'project = "{project_key}" ORDER BY updated DESC'
    return search_issues(access_token, cloud_id, jql)


def search_issues_modified_since(
    access_token: str,
    cloud_id: str,
    project_key: str,
    modified_since: str,
) -> List[Dict]:
    """
    Search for issues modified since a specific date using JQL.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        project_key: Project key to search in
        modified_since: ISO date string (e.g., "2024-01-09T12:00:00Z")

    Returns:
        List of issue dicts
    """
    from datetime import datetime, timezone

    # Parse the ISO timestamp and calculate minutes ago
    try:
        if "." in modified_since:
            dt = datetime.fromisoformat(modified_since.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(modified_since.replace("Z", "+00:00"))

        now = datetime.now(timezone.utc)
        delta = now - dt
        minutes_ago = max(1, int(delta.total_seconds() / 60))

        # Use relative time format which avoids timezone issues
        # Minimum 65 minutes to cover hourly scheduler interval plus buffer
        minutes_ago = max(minutes_ago, 65)
        jql = f'project = "{project_key}" AND updated >= -{minutes_ago}m ORDER BY updated DESC'
    except Exception as e:
        logger.warning(f"Failed to parse date {modified_since}: {e}, using -65m fallback")
        jql = f'project = "{project_key}" AND updated >= -65m ORDER BY updated DESC'

    return search_issues(access_token, cloud_id, jql)


def get_issue_comments(
    access_token: str,
    cloud_id: str,
    issue_key: str,
) -> List[Dict]:
    """
    Get all comments for an issue.

    Uses pagination to retrieve all comments.

    Args:
        access_token: OAuth access token
        cloud_id: Atlassian Cloud ID
        issue_key: Issue key (e.g., "PROJ-123")

    Returns:
        List of comment dicts
    """
    base_url = f"{JIRA_API_BASE}/{cloud_id}/rest/api/3/issue/{issue_key}/comment"

    comments = []
    start_at = 0
    max_results = 100

    while True:
        params = {"startAt": start_at, "maxResults": max_results}

        response = _make_request("GET", base_url, access_token, params=params)
        data = response.json()

        comments.extend(data.get("comments", []))

        total = data.get("total", 0)
        if start_at + max_results >= total:
            break

        start_at += max_results

    logger.info(f"Retrieved {len(comments)} comments from issue {issue_key}")
    return comments


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
        attachment_id: Attachment ID to download

    Returns:
        Attachment content as bytes
    """
    url = f"{JIRA_API_BASE}/{cloud_id}/rest/api/3/attachment/content/{attachment_id}"

    response = _make_request(
        "GET",
        url,
        access_token,
        timeout=300,  # 5 minutes for large files
        stream=True,
    )

    # Read content from stream
    content = b""
    for chunk in response.iter_content(chunk_size=8192):
        content += chunk

    logger.info(f"Downloaded attachment {attachment_id}, size: {len(content)} bytes")
    return content
