# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Azure DevOps REST API client.

Thin function-style wrappers over ADO REST v7.0. Raw ``urllib.request``
per the SharePoint/Outlook pattern -- no SDK, no transitive deps.

Two host families are in play:

- ``app.vssps.visualstudio.com`` -- the legacy identity host. Used for
  ``/_apis/accounts`` (which orgs the signed-in user can see). Microsoft
  has not yet migrated this surface to ``dev.azure.com``; don't try to
  consolidate.
- ``dev.azure.com`` -- everything else (projects, repos, items,
  commits, pull-request threads).

Pagination is split too:

- ``/_apis/accounts`` and ``/_apis/projects`` use ``$top`` + a
  ``X-MS-ContinuationToken`` response header. The cursor is returned
  as a header, not a body field.
- ``/_apis/git/repositories`` and ``/items`` return everything in one
  body page (no continuation token) -- the result set is bounded by
  the project/repo size.

``_paged_get`` handles both shapes.

Errors map to one of:
- ``AzureDevOpsAuthError`` (401)  -- caller should refresh and retry once
- ``AzureDevOpsScopeError`` (403) -- caller should surface to UI
- ``AzureDevOpsNotFoundError`` (404)
- ``AzureDevOpsServerError`` (5xx) -- caller decides whether to retry
- ``AzureDevOpsAPIError`` -- network / timeout / unmapped
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


VSSPS_HOST = "https://app.vssps.visualstudio.com"
DEV_AZURE_HOST = "https://dev.azure.com"
API_VERSION = "7.0"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
USER_AGENT = f"OutcomeOps-AIAssist/{APP_NAME}/{ENV}"


class AzureDevOpsAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class AzureDevOpsAuthError(AzureDevOpsAPIError):
    """401 -- access token expired/invalid; caller refreshes and retries once."""
    pass


class AzureDevOpsScopeError(AzureDevOpsAPIError):
    """403 -- token lacks the required ADO scope (e.g. vso.code_write missing
    when posting a PR comment). Caller surfaces to UI with a hint."""
    pass


class AzureDevOpsNotFoundError(AzureDevOpsAPIError):
    """404 -- org/project/repo/branch/path/PR doesn't exist."""
    pass


class AzureDevOpsServerError(AzureDevOpsAPIError):
    """5xx -- ADO is down or transiently broken. Caller decides retry policy."""
    pass


def _request(
    method: str,
    url: str,
    access_token: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = REQUEST_TIMEOUT,
    parse_json: bool = True,
    accept: str = "application/json",
):
    """Single HTTP entry point. Returns (parsed_body, response_headers).

    If ``parse_json`` is False, returns (raw_bytes, response_headers)
    instead -- used by ``get_file_content`` which is binary.

    ``accept`` overrides the Accept header. Default is JSON for the
    metadata endpoints. ``get_file_content`` passes ``*/*`` so ADO's
    ``download=true`` actually returns the raw blob bytes -- otherwise
    ADO honors ``Accept: application/json`` and returns the JSON
    object descriptor (objectId / url / blob hrefs) instead of the
    file. Live-burned 2026-06-05 on first ADO sync: all 131 files
    landed in S3 as ~900-byte JSON metadata wrappers instead of the
    actual source.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": accept,
        "User-Agent": USER_AGENT,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            resp_headers = dict(response.headers)
            raw = response.read()
            if not parse_json:
                return raw, resp_headers
            if not raw:
                return {}, resp_headers
            return json.loads(raw.decode("utf-8")), resp_headers
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8") if e.fp else ""
        except Exception:
            pass
        path_for_log = urllib.parse.urlsplit(url).path
        logger.error(f"ADO API {method} {path_for_log} -> {e.code}: {error_body}")
        if e.code == 401:
            raise AzureDevOpsAuthError(f"401 on {path_for_log}", status_code=401, response=error_body)
        if e.code == 403:
            raise AzureDevOpsScopeError(f"403 on {path_for_log}", status_code=403, response=error_body)
        if e.code == 404:
            raise AzureDevOpsNotFoundError(f"404 on {path_for_log}", status_code=404, response=error_body)
        if 500 <= e.code < 600:
            raise AzureDevOpsServerError(f"{e.code} on {path_for_log}", status_code=e.code, response=error_body)
        raise AzureDevOpsAPIError(f"{e.code} on {path_for_log}", status_code=e.code, response=error_body)
    except urllib.error.URLError as e:
        raise AzureDevOpsAPIError(f"Network error on {url}: {e.reason}")


def _add_query(url: str, params: Dict[str, Any]) -> str:
    """Append query params to a URL, preserving any already present."""
    parts = urllib.parse.urlsplit(url)
    existing = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    existing.update({k: str(v) for k, v in params.items() if v is not None})
    new_query = urllib.parse.urlencode(existing)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _paged_get(
    url: str,
    access_token: str,
    page_size: int = PAGE_SIZE,
) -> Iterator[Dict[str, Any]]:
    """Yield items across all pages.

    Handles both ADO pagination styles:

    1. **Continuation-token style** (``/_apis/accounts``,
       ``/_apis/projects``, commits). ADO returns
       ``X-MS-ContinuationToken`` as a response header. We thread that
       back as ``continuationToken=...`` on the next request.
    2. **Skip-pagination style** (``/_apis/git/repositories``, items).
       ADO returns the full result set in ``value``; no continuation.
       We yield once and stop.

    The caller doesn't care which is which.
    """
    next_url: Optional[str] = _add_query(url, {"$top": page_size})
    while next_url:
        body, headers = _request("GET", next_url, access_token)
        for item in body.get("value", []) or []:
            yield item
        token = headers.get("X-MS-ContinuationToken") or headers.get("x-ms-continuationtoken")
        if not token:
            return
        next_url = _add_query(url, {"$top": page_size, "continuationToken": token})


# ---------------------------------------------------------------------------
# Discovery / listing
# ---------------------------------------------------------------------------


def list_accessible_orgs(access_token: str, member_id: str) -> List[Dict[str, Any]]:
    """List ADO organizations the signed-in user has access to.

    ``member_id`` comes from ``token_manager.get_user_profile(...)['id']``
    -- it's the Entra ID user identifier as ADO sees it.
    """
    url = _add_query(
        f"{VSSPS_HOST}/_apis/accounts",
        {"memberId": member_id, "api-version": API_VERSION},
    )
    return list(_paged_get(url, access_token))


def list_projects(access_token: str, org: str) -> List[Dict[str, Any]]:
    """List projects in an org. Continuation-token paginated."""
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/_apis/projects",
        {"api-version": API_VERSION},
    )
    return list(_paged_get(url, access_token))


def list_repos(access_token: str, org: str, project: str) -> List[Dict[str, Any]]:
    """List Git repos in a project. Single-page response."""
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories",
        {"api-version": API_VERSION},
    )
    body, _ = _request("GET", url, access_token)
    return body.get("value", []) or []


def get_repo(access_token: str, org: str, project: str, repo: str) -> Dict[str, Any]:
    """Repo metadata (includes ``defaultBranch``)."""
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}",
        {"api-version": API_VERSION},
    )
    body, _ = _request("GET", url, access_token)
    return body


# ---------------------------------------------------------------------------
# Repo content
# ---------------------------------------------------------------------------


def get_default_branch(access_token: str, org: str, project: str, repo: str) -> Optional[str]:
    """Return the repo's default branch as a short ref (``main``, not
    ``refs/heads/main``). ADO returns the full ref; we strip the prefix
    for consistency with the GitHub integration's representation."""
    meta = get_repo(access_token, org, project, repo)
    default = meta.get("defaultBranch")
    if default and default.startswith("refs/heads/"):
        return default[len("refs/heads/"):]
    return default


def list_branches(
    access_token: str, org: str, project: str, repo: str,
) -> List[Dict[str, Any]]:
    """List branches for a repo.

    Uses ``GET /_apis/git/repositories/{repo}/refs?filter=heads/`` to
    scope to branch refs (skip tags, notes). Returns
    ``[{"name": "main"}]`` -- short ref names, no ``refs/heads/``
    prefix (matches the vocabulary get_default_branch already speaks).
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/refs",
        {"api-version": API_VERSION, "filter": "heads/"},
    )
    body, _ = _request("GET", url, access_token)
    refs = body.get("value", []) or []
    branches = []
    for ref in refs:
        raw = ref.get("name", "") or ""
        # ADO returns full ref: "refs/heads/main". Strip the prefix so
        # every consumer speaks the same short-ref form GitHub uses.
        if raw.startswith("refs/heads/"):
            branches.append({"name": raw[len("refs/heads/"):]})
        elif raw:
            branches.append({"name": raw})
    return branches


def get_latest_commit_sha(
    access_token: str,
    org: str,
    project: str,
    repo: str,
    branch: str,
) -> Optional[str]:
    """Newest commit SHA on a branch."""
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/commits",
        {
            "searchCriteria.itemVersion.version": branch,
            "$top": 1,
            "api-version": API_VERSION,
        },
    )
    body, _ = _request("GET", url, access_token)
    commits = body.get("value", []) or []
    if not commits:
        return None
    return commits[0].get("commitId")


def get_tree_recursive(
    access_token: str,
    org: str,
    project: str,
    repo: str,
    branch: str,
) -> List[Dict[str, Any]]:
    """Full file tree at the branch tip. Each item is
    ``{path, gitObjectType, objectId, ...}``."""
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/items",
        {
            "scopePath": "/",
            "recursionLevel": "Full",
            "versionDescriptor.version": branch,
            "api-version": API_VERSION,
        },
    )
    body, _ = _request("GET", url, access_token)
    return body.get("value", []) or []


def get_file_content(
    access_token: str,
    org: str,
    project: str,
    repo: str,
    path: str,
    branch: str,
) -> bytes:
    """Raw file bytes.

    Two parameters are load-bearing on this call:
    - ``download=true`` tells ADO to return the blob content (vs the
      JSON object descriptor).
    - ``Accept: */*`` overrides the client's default
      ``Accept: application/json`` -- otherwise ADO honors the JSON
      Accept and returns the JSON wrapper instead of the file bytes.
      Burned 2026-06-05 (see _request docstring).
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/items",
        {
            "path": path,
            "download": "true",
            "versionDescriptor.version": branch,
            "api-version": API_VERSION,
        },
    )
    raw, _ = _request(
        "GET", url, access_token,
        timeout=DOWNLOAD_TIMEOUT,
        parse_json=False,
        accept="*/*",
    )
    return raw


# ---------------------------------------------------------------------------
# PR-check (used by )
# ---------------------------------------------------------------------------


def list_open_pulls(
    access_token: str,
    org: str,
    project: str,
    repo: str,
) -> List[Dict[str, Any]]:
    """List active (open) pull requests on a repo."""
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/pullrequests",
        {
            "searchCriteria.status": "active",
            "api-version": API_VERSION,
        },
    )
    body, _ = _request("GET", url, access_token)
    return body.get("value", []) or []


def post_pull_request_thread(
    access_token: str,
    org: str,
    project: str,
    repo: str,
    pull_request_id: int,
    comment_markdown: str,
) -> Dict[str, Any]:
    """Create a top-level PR comment thread on an open PR.

    ADO's thread shape:
        ``{"comments": [{"content": str, "commentType": 1}], "status": 1}``
    where ``commentType=1`` is "text" and ``status=1`` is "active".
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/pullRequests/{pull_request_id}/threads",
        {"api-version": API_VERSION},
    )
    payload = {
        "comments": [
            {
                "content": comment_markdown,
                "commentType": 1,
            }
        ],
        "status": 1,
    }
    body, _ = _request("POST", url, access_token, body=payload)
    return body


def post_pull_request_inline_thread(
    access_token: str,
    org: str,
    project: str,
    repo: str,
    pull_request_id: int,
    comment_markdown: str,
    *,
    file_path: str,
    line: int,
) -> Dict[str, Any]:
    """Create a line-anchored PR comment thread via ``threadContext``.

    Like post_pull_request_thread, but anchors the thread to a file+line so it
    renders inline on the diff. ADO line anchors are 1-based and ``filePath``
    must be repo-absolute (leading ``/``), which this normalizes.

    ADO's line-anchored thread shape adds ``threadContext``::

        {"filePath": "/path", "rightFileStart": {"line": N, "offset": 1},
         "rightFileEnd": {"line": N, "offset": 1}}

    Updates reuse update_pull_request_thread_comment (thread id from the
    returned body's ``id``; comment id from ``comments[0].id``).
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/pullRequests/{pull_request_id}/threads",
        {"api-version": API_VERSION},
    )
    fp = file_path if file_path.startswith("/") else f"/{file_path}"
    payload = {
        "comments": [{"content": comment_markdown, "commentType": 1}],
        "status": 1,
        "threadContext": {
            "filePath": fp,
            "rightFileStart": {"line": line, "offset": 1},
            "rightFileEnd": {"line": line, "offset": 1},
        },
    }
    body, _ = _request("POST", url, access_token, body=payload)
    return body


def delete_pull_request_thread_comment(
    access_token: str,
    org: str,
    project: str,
    repo: str,
    pull_request_id: int,
    thread_id: int,
    comment_id: int,
) -> None:
    """Delete a comment from a PR thread (stale inline-finding cleanup).

    Deleting the sole comment in an inline thread removes that thread, so a
    fixed line stops showing an orphaned finding. ADO returns 204; ``_request``
    tolerates the empty body.
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/"
        f"pullRequests/{pull_request_id}/threads/{thread_id}/comments/{comment_id}",
        {"api-version": API_VERSION},
    )
    _request("DELETE", url, access_token)


def update_pull_request_thread_comment(
    access_token: str,
    org: str,
    project: str,
    repo: str,
    pull_request_id: int,
    thread_id: int,
    comment_id: int,
    comment_markdown: str,
) -> Dict[str, Any]:
    """Update the content of an existing PR-thread comment.

    Used by the update-in-place PR-check UX so subsequent runs edit
    the existing comment rather than posting a new one.

    ADO's PATCH shape for a thread comment:
        ``{"content": str, "commentType": 1, "parentCommentId": 0}``
    ``parentCommentId=0`` marks this as a top-level thread comment
    (not a reply). Same commentType=1 (text) as the POST.

    Args:
        thread_id: The thread's numeric ID (from post_pull_request_thread's
            ``id`` field on the returned body).
        comment_id: The comment's numeric ID within the thread. For threads
            created by ``post_pull_request_thread`` (single-comment threads),
            this is always ``1`` -- but we pass it explicitly so future
            multi-comment thread edits work.

    Raises AzureDevOpsAPIError on non-2xx (Auth/Scope/NotFound mapped
    like the existing GET/POST wrappers via ``_request``).
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{org}/{project}/_apis/git/repositories/{repo}/"
        f"pullRequests/{pull_request_id}/threads/{thread_id}/comments/{comment_id}",
        {"api-version": API_VERSION},
    )
    payload = {
        "content": comment_markdown,
        "commentType": 1,
        "parentCommentId": 0,
    }
    body, _ = _request("PATCH", url, access_token, body=payload)
    return body
