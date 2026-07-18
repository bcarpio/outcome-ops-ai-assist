"""GitLab API v4 client (ADR-050) -- raw ``urllib``, no SDK.

Every call takes a user-scoped access token (Model A delegated OAuth), so results
are already limited to what the authorizing user can see. Projects are addressed
by URL-encoded full path (``group%2Fsubgroup%2Fproject``) so slugs stay
human-readable and nested subgroups work.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

_DEFAULT_BASE_URL = "https://gitlab.com"


def base_url() -> str:
    return (os.environ.get("GITLAB_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _api(path: str) -> str:
    return f"{base_url()}/api/v4{path}"


def _enc(project_path: str) -> str:
    """URL-encode a full project path for the /projects/{id} form."""
    return urllib.parse.quote(project_path, safe="")


def _request(access_token: str, url: str, raw: bool = False):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read()
        headers = resp.headers
    if raw:
        return body, headers
    return json.loads(body), headers


def _get(access_token: str, path: str, params: Optional[Dict] = None):
    url = _api(path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data, _ = _request(access_token, url)
    return data


def _get_paginated(access_token: str, path: str,
                   params: Optional[Dict] = None) -> List[Dict]:
    """Offset-paginate a list endpoint until a short/empty page (per_page=100)."""
    out: List[Dict] = []
    page = 1
    params = dict(params or {})
    params["per_page"] = 100
    while True:
        params["page"] = page
        url = _api(path) + "?" + urllib.parse.urlencode(params)
        items, headers = _request(access_token, url)
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        nxt = headers.get("X-Next-Page")
        if not nxt:
            break
        page = int(nxt)
    return out


def list_groups(access_token: str) -> List[Dict]:
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
            {"min_access_level": 20, "all_available": "false"})
    ]


def list_group_projects(access_token: str, group_id) -> List[Dict]:
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
             "archived": "false"})
    ]


def get_project(access_token: str, project_path: str) -> Dict:
    return _get(access_token, f"/projects/{_enc(project_path)}")


def get_default_branch(access_token: str, project_path: str) -> Optional[str]:
    return get_project(access_token, project_path).get("default_branch")


def get_latest_commit_sha(access_token: str, project_path: str,
                          ref: str) -> Optional[str]:
    commits = _get(
        access_token,
        f"/projects/{_enc(project_path)}/repository/commits",
        {"ref_name": ref, "per_page": 1})
    return commits[0]["id"] if commits else None


def get_tree(access_token: str, project_path: str, ref: str) -> List[str]:
    """Recursive tree -> list of blob (file) paths."""
    entries = _get_paginated(
        access_token,
        f"/projects/{_enc(project_path)}/repository/tree",
        {"ref": ref, "recursive": "true"})
    return [e["path"] for e in entries if e.get("type") == "blob"]


def get_file_content(access_token: str, project_path: str, file_path: str,
                     ref: str) -> bytes:
    """Raw file bytes at ``ref``."""
    url = _api(
        f"/projects/{_enc(project_path)}/repository/files/"
        f"{_enc(file_path)}/raw") + "?" + urllib.parse.urlencode({"ref": ref})
    body, _ = _request(access_token, url, raw=True)
    return body
