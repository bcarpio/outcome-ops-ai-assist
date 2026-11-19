# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""GitLab API v4 client (ADR-050) -- raw ``urllib``, no SDK.

Every call takes a user-scoped access token (Model A delegated OAuth), so results
are already limited to what the authorizing user can see. Projects are addressed
by URL-encoded full path (``group%2Fsubgroup%2Fproject``) so slugs stay
human-readable and nested subgroups work.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

_DEFAULT_BASE_URL = "https://gitlab.com"


def base_url() -> str:
    """Legacy single-instance base URL (env). Prefer passing an explicit
    ``base_url`` from the connection's instance -- see _resolve_base."""
    return (os.environ.get("GITLAB_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _resolve_base(base_url_arg: Optional[str]) -> str:
    """Resolve which GitLab instance this call targets.

    An explicit ``base_url`` (from the connection's instance) always wins.
    Falling back is legal ONLY when the deploy has exactly one instance
    configured; on a multi-instance deploy an un-threaded call site raises
    instead of silently hitting whichever instance the legacy env var
    happens to name. That makes a missed call site a loud failure during
    multi-instance rollout rather than a wrong-instance read.
    """
    if base_url_arg:
        return base_url_arg.rstrip("/")
    from shared.gitlab_instances import get_instance
    return get_instance(None).base_url


def _api(path: str, base: str) -> str:
    return f"{base}/api/v4{path}"


def _enc(project_path: str) -> str:
    """URL-encode a full project path for the /projects/{id} form."""
    return urllib.parse.quote(project_path, safe="")


# GitLab rate limits (429) are short-lived; retry in place rather than failing
# the whole caller. Without this a 429 on a PR-check note post failed the SQS
# message, which re-ran the entire check (LLM analysis included) 12 minutes
# later just to post a comment. A 429 means GitLab rejected the request
# without acting on it, so retrying a POST/PUT/DELETE is safe.
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_MAX_SLEEP_S = 10.0


def _retry_after_seconds(err: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before retrying a 429: GitLab's ``Retry-After`` when
    present and numeric, else exponential backoff (1, 2, 4 ...). Capped so a
    long reset window cannot eat the Lambda's timeout; the caller's own retry
    (SQS redelivery) covers anything longer."""
    header = err.headers.get("Retry-After") if err.headers else None
    try:
        wait = float(header) if header is not None else float(2 ** attempt)
    except ValueError:
        wait = float(2 ** attempt)
    return max(0.0, min(wait, _RATE_LIMIT_MAX_SLEEP_S))


def _urlopen(req: urllib.request.Request):
    """``urllib.request.urlopen`` with in-place retry on HTTP 429.

    Returns the open response (use it as a context manager). Any other error,
    or a 429 that persists past ``_RATE_LIMIT_RETRIES``, propagates.
    """
    attempt = 0
    while True:
        try:
            return urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as err:
            if err.code != 429 or attempt >= _RATE_LIMIT_RETRIES:
                raise
            time.sleep(_retry_after_seconds(err, attempt))
            attempt += 1


def _request(access_token: str, url: str, raw: bool = False):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"})
    with _urlopen(req) as resp:
        body = resp.read()
        headers = resp.headers
    if raw:
        return body, headers
    return json.loads(body), headers


def _get(access_token: str, path: str, params: Optional[Dict] = None,
         *, base: str):
    url = _api(path, base)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data, _ = _request(access_token, url)
    return data


def _get_paginated(access_token: str, path: str,
                   params: Optional[Dict] = None, *, base: str) -> List[Dict]:
    """Offset-paginate a list endpoint until a short/empty page (per_page=100)."""
    out: List[Dict] = []
    page = 1
    params = dict(params or {})
    params["per_page"] = 100
    while True:
        params["page"] = page
        url = _api(path, base) + "?" + urllib.parse.urlencode(params)
        items, headers = _request(access_token, url)
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        nxt = headers.get("X-Next-Page")
        if not nxt:
            break
        page = int(nxt)
    return out


def list_groups(access_token: str, *, base_url: Optional[str] = None) -> List[Dict]:
    """Groups the user belongs to with at least REPORTER access.

    min_access_level=20 (Reporter) is the correct floor for a read-only
    integration: Reporter can read a project's code, so a Reporter-access group
    is enrollable. A higher floor (e.g. Developer=30) would wrongly hide groups
    a user can legitimately read + ingest.
    """
    return [
        {"id": g["id"], "full_path": g["full_path"], "name": g["name"]}
        for g in _get_paginated(
            access_token, "/groups",
            {"min_access_level": 20, "all_available": "false"},
            base=_resolve_base(base_url))
    ]


def list_group_projects(access_token: str, group_id, *,
                        base_url: Optional[str] = None) -> List[Dict]:
    """Projects in a group INCLUDING nested subgroups (the slug can have >2
    segments). ``path_with_namespace`` is the enrollable slug."""
    return [
        {"id": p["id"],
         "slug": p["path_with_namespace"],
         "name": p["name"],
         "default_branch": p.get("default_branch")}
        for p in _get_paginated(
            access_token, f"/groups/{group_id}/projects",
            {"include_subgroups": "true", "with_shared": "false",
             "archived": "false"},
            base=_resolve_base(base_url))
    ]


def get_project(access_token: str, project_path: str, *,
                base_url: Optional[str] = None) -> Dict:
    return _get(access_token, f"/projects/{_enc(project_path)}",
                base=_resolve_base(base_url))


def get_default_branch(access_token: str, project_path: str, *,
                       base_url: Optional[str] = None) -> Optional[str]:
    return get_project(access_token, project_path,
                       base_url=base_url).get("default_branch")


def get_latest_commit_sha(access_token: str, project_path: str,
                          ref: str, *,
                          base_url: Optional[str] = None) -> Optional[str]:
    commits = _get(
        access_token,
        f"/projects/{_enc(project_path)}/repository/commits",
        {"ref_name": ref, "per_page": 1},
        base=_resolve_base(base_url))
    return commits[0]["id"] if commits else None


def get_tree(access_token: str, project_path: str, ref: str, *,
             base_url: Optional[str] = None) -> List[str]:
    """Recursive tree -> list of blob (file) paths."""
    entries = _get_paginated(
        access_token,
        f"/projects/{_enc(project_path)}/repository/tree",
        {"ref": ref, "recursive": "true"},
        base=_resolve_base(base_url))
    return [e["path"] for e in entries if e.get("type") == "blob"]


def get_file_content(access_token: str, project_path: str, file_path: str,
                     ref: str, *, base_url: Optional[str] = None) -> bytes:
    """Raw file bytes at ``ref``."""
    url = _api(
        f"/projects/{_enc(project_path)}/repository/files/"
        f"{_enc(file_path)}/raw",
        _resolve_base(base_url)) + "?" + urllib.parse.urlencode({"ref": ref})
    body, _ = _request(access_token, url, raw=True)
    return body


# =============================================================================
# Merge requests
# =============================================================================


def _send(access_token: str, path: str, payload: Dict, method: str,
          *, base: str) -> Dict:
    """JSON-body POST/PUT returning parsed JSON."""
    req = urllib.request.Request(
        _api(path, base),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with _urlopen(req) as resp:
        return json.loads(resp.read())


def get_merge_request(access_token: str, project_path: str,
                      mr_iid: int, *,
                      base_url: Optional[str] = None) -> Dict:
    """One MR by project-scoped iid (the number shown in the GitLab UI).

    Callers rely on: ``diff_refs.base_sha`` / ``diff_refs.head_sha``,
    ``target_branch``, ``author.username``, ``title``.
    """
    return _get(
        access_token, f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}",
        base=_resolve_base(base_url))


def get_merge_request_diffs(access_token: str, project_path: str,
                            mr_iid: int, *,
                            base_url: Optional[str] = None) -> List[Dict]:
    """The MR's changed files (paginated).

    Each entry carries ``new_path`` / ``old_path`` plus EXPLICIT booleans
    ``new_file`` / ``deleted_file`` / ``renamed_file`` -- unlike ADO's
    changeType string vocabulary there is nothing to parse (the ADO
    iteration-changes endpoint live-burned on a missing field; GitLab's
    booleans are documented and always present).
    """
    return _get_paginated(
        access_token,
        f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}/diffs",
        base=_resolve_base(base_url))


def get_merge_request_commits(access_token: str, project_path: str,
                              mr_iid: int, *,
                              base_url: Optional[str] = None) -> List[Dict]:
    """The MR's commits. ``message`` is the FULL commit message (GitLab does
    not truncate like ADO's pull-request-commits ``comment`` field)."""
    return _get_paginated(
        access_token,
        f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}/commits",
        base=_resolve_base(base_url))


def post_merge_request_note(access_token: str, project_path: str,
                            mr_iid: int, body: str, *,
                            base_url: Optional[str] = None) -> Dict:
    """Create an MR note (comment). Returns the note object; callers store
    ``id`` to target update_merge_request_note on later runs."""
    return _send(
        access_token,
        f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}/notes",
        {"body": body}, "POST", base=_resolve_base(base_url))


def update_merge_request_note(access_token: str, project_path: str,
                              mr_iid: int, note_id: int, body: str, *,
                              base_url: Optional[str] = None) -> Dict:
    """Update an existing MR note in place (the upsert-comment UX)."""
    return _send(
        access_token,
        f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}"
        f"/notes/{note_id}",
        {"body": body}, "PUT", base=_resolve_base(base_url))


def post_merge_request_discussion(access_token: str, project_path: str,
                                  mr_iid: int, body: str, position: Dict, *,
                                  base_url: Optional[str] = None) -> Dict:
    """Create a line-anchored MR discussion (inline comment).

    ``position`` is GitLab's diff-position object, built from the MR's
    ``diff_refs`` (see get_merge_request)::

        {"position_type": "text",
         "base_sha": <diff_refs.base_sha>,
         "start_sha": <diff_refs.start_sha>,
         "head_sha": <diff_refs.head_sha>,
         "new_path": "<file>", "new_line": <int>}

    Returns the discussion object; callers store its ``id`` plus the first
    note's ``id`` (``notes[0].id``) to target
    update_merge_request_discussion_note on later runs (inline upsert; plan
    ).
    """
    return _send(
        access_token,
        f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}/discussions",
        {"body": body, "position": position}, "POST",
        base=_resolve_base(base_url))


def update_merge_request_discussion_note(access_token: str, project_path: str,
                                         mr_iid: int, discussion_id: str,
                                         note_id: int, body: str, *,
                                         base_url: Optional[str] = None) -> Dict:
    """Update a note inside an MR discussion in place (inline upsert UX)."""
    return _send(
        access_token,
        f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}"
        f"/discussions/{discussion_id}/notes/{note_id}",
        {"body": body}, "PUT", base=_resolve_base(base_url))


def delete_merge_request_discussion_note(access_token: str, project_path: str,
                                         mr_iid: int, discussion_id: str,
                                         note_id: int, *,
                                         base_url: Optional[str] = None) -> None:
    """Delete a note inside an MR discussion (stale inline-finding cleanup).

    GitLab returns 204 No Content for this endpoint, so unlike ``_send`` this
    does not attempt to parse a response body.
    """
    req = urllib.request.Request(
        _api(f"/projects/{_enc(project_path)}/merge_requests/{mr_iid}"
             f"/discussions/{discussion_id}/notes/{note_id}",
             _resolve_base(base_url)),
        headers={"Authorization": f"Bearer {access_token}"},
        method="DELETE",
    )
    with _urlopen(req):
        return None
