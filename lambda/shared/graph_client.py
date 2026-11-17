# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""App-only Microsoft Graph client for the group binding picker (ADR-048 v1.1).

Client-credentials (app-only) access to Graph, used ONLY when a deployment
enables ``azure_ad_group_graph_fallback_enabled``. Two calls:

  * ``search_groups(q)`` -- populate the full binding picker, scoped to the
    ``azure_ad_group_patterns`` allow-list (prefix globs become a server-side
    ``startswith(displayName,…)`` filter; embedded-star patterns fetch a page
    and filter locally via ``group_patterns.filter_groups``).
  * ``member_of(user_id)`` -- resolve a user's true group set for the
    >200-group ``_claim_names``/``hasGroups`` overage.

Credentials: app (client) ID from ``AZURE_AD_GRAPH_CLIENT_ID``; client secret
read once from SSM SecureString at ``AZURE_AD_GRAPH_SECRET_SSM_PATH`` (never
logged); tenant from ``AZURE_AD_TENANT_ID``. The access token is cached in
process for its lifetime (minus a skew). Disabled/unconfigured -> callers get
an empty result and fall back to the credential-free path.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Set

from shared import group_patterns

_GRAPH = "https://graph.microsoft.com/v1.0"
_TOKEN_SKEW = 300  # refresh 5 min before expiry

# process-lifetime caches
_token_cache: Dict[str, object] = {"value": None, "exp": 0.0}
_secret_cache: Dict[str, Optional[str]] = {"value": None}
# group-centric membership: gid -> {"exp": float, "ids": Set[str]}
_members_cache: Dict[str, Dict[str, object]] = {}
_MEMBERS_TTL = int(os.environ.get("AZURE_AD_GRAPH_MEMBERS_TTL_SECONDS", "300"))


def enabled() -> bool:
    """Public: whether app-only Graph group resolution is usable on this deploy."""
    return _enabled()


def _enabled() -> bool:
    return (
        os.environ.get("AZURE_AD_GROUP_GRAPH_FALLBACK_ENABLED", "false").lower()
        == "true"
        and bool(os.environ.get("AZURE_AD_GRAPH_CLIENT_ID"))
        and bool(os.environ.get("AZURE_AD_TENANT_ID"))
    )


def _patterns() -> List[str]:
    raw = os.environ.get("AZURE_AD_GROUP_PATTERNS", "")
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(p) for p in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _secret() -> Optional[str]:
    if _secret_cache["value"] is not None:
        return _secret_cache["value"]
    path = os.environ.get("AZURE_AD_GRAPH_SECRET_SSM_PATH")
    if not path:
        return None
    import boto3  # lazy: only when the fallback is actually used

    val = boto3.client("ssm").get_parameter(
        Name=path, WithDecryption=True
    )["Parameter"]["Value"]
    _secret_cache["value"] = val
    return val


def _token() -> Optional[str]:
    now = time.time()
    if _token_cache["value"] and float(_token_cache["exp"]) > now:
        return str(_token_cache["value"])
    secret = _secret()
    if not secret:
        return None
    tenant = os.environ["AZURE_AD_TENANT_ID"]
    client_id = os.environ["AZURE_AD_GRAPH_CLIENT_ID"]
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    with urllib.request.urlopen(
        urllib.request.Request(url, data=data), timeout=15
    ) as resp:
        body = json.loads(resp.read())
    _token_cache["value"] = body["access_token"]
    _token_cache["exp"] = now + int(body.get("expires_in", 3600)) - _TOKEN_SKEW
    return str(_token_cache["value"])


def _get(path: str, tok: str, extra_headers: Optional[Dict] = None) -> Dict:
    url = path if path.startswith("http") else f"{_GRAPH}{path}"
    headers = {"Authorization": f"Bearer {tok}"}
    if extra_headers:
        headers.update(extra_headers)
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=15
    ) as resp:
        return json.loads(resp.read())


def _norm(items: List[Dict]) -> List[Dict]:
    return [
        {"id": g.get("id"), "display_name": g.get("displayName", "")}
        for g in items
        if g.get("id")
    ]


def search_groups(q: str = "", limit: int = 50) -> List[Dict]:
    """Return ``[{id, display_name}]`` of eligible groups matching ``q``.

    Scoped to ``azure_ad_group_patterns``. Returns [] when the fallback is
    disabled/unconfigured (caller uses the credential-free picker).
    """
    if not _enabled():
        return []
    tok = _token()
    if not tok:
        return []
    patterns = _patterns()
    prefixes = group_patterns.graph_prefix_filters(patterns)
    q = (q or "").strip()

    filters: List[str] = []
    if prefixes:
        # OR the clean prefixes into one $filter; user query narrows further.
        ors = [f"startswith(displayName,'{p}')" for p in prefixes]
        filters.append("(" + " or ".join(ors) + ")")
    if q:
        safe = q.replace("'", "''")
        filters.append(f"startswith(displayName,'{safe}')")

    params = {
        "$select": "id,displayName",
        "$top": str(min(max(limit, 1), 999)),
    }
    if filters:
        params["$filter"] = " and ".join(filters)
    path = "/groups?" + urllib.parse.urlencode(params, safe="(),' ")
    try:
        # $filter on displayName needs the eventual-consistency header.
        body = _get(path, tok, {"ConsistencyLevel": "eventual"})
    except urllib.error.HTTPError:
        return []
    groups = _norm(body.get("value", []))
    # embedded-star patterns (e.g. *-outcomeops-*) aren't a startswith -> apply
    # the full pattern set locally so eligibility is exact.
    groups = group_patterns.filter_groups(groups, patterns)
    if q:
        ql = q.lower()
        groups = [g for g in groups if ql in g["display_name"].lower()]
    return groups[:limit]


def member_of(user_id: str) -> List[str]:
    """Return the user's group object IDs (>200-group overage resolution).

    Empty list when disabled/unconfigured (caller then skips the sync safely).
    NOTE: this reads ``/users/{id}/memberOf`` and therefore needs a directory/
    user-read grant (User.Read.All or Directory.Read.All), which is broader than
    the group-scoped path below. Group-centric ``groups_containing_user`` is the
    least-privilege resolution and is preferred; see ADR-048.
    """
    if not _enabled() or not user_id:
        return []
    tok = _token()
    if not tok:
        return []
    ids: List[str] = []
    path = (
        f"/users/{urllib.parse.quote(user_id)}/memberOf/microsoft.graph.group"
        "?$select=id&$top=999"
    )
    try:
        while path:
            body = _get(path, tok, {"ConsistencyLevel": "eventual"})
            ids.extend(g["id"] for g in body.get("value", []) if g.get("id"))
            path = body.get("@odata.nextLink")
    except urllib.error.HTTPError:
        return ids
    return ids


def group_member_ids(group_id: str) -> Set[str]:
    """Return the object IDs of the group's direct user members.

    Group-scoped read (``Group.Read.All`` / ``GroupMember.Read.All``) -- does NOT
    require directory/user-read. Cached per group for
    ``AZURE_AD_GRAPH_MEMBERS_TTL_SECONDS``. RAISES on a Graph error (or missing
    token) so callers can tell 'genuinely no members' from 'lookup failed' and
    never revoke memberships against a partial result.
    """
    now = time.time()
    ent = _members_cache.get(group_id)
    if ent and float(ent["exp"]) > now:
        return ent["ids"]  # type: ignore[return-value]
    tok = _token()
    if not tok:
        raise RuntimeError("graph token unavailable")
    ids: Set[str] = set()
    # cast to microsoft.graph.user so only user members come back (not nested
    # groups / service principals); id only, paginated.
    path = (
        f"/groups/{urllib.parse.quote(group_id)}/members/microsoft.graph.user"
        "?$select=id&$top=999"
    )
    while path:
        body = _get(path, tok, {"ConsistencyLevel": "eventual"})
        ids.update(m["id"] for m in body.get("value", []) if m.get("id"))
        path = body.get("@odata.nextLink")
    _members_cache[group_id] = {"exp": now + _MEMBERS_TTL, "ids": ids}
    return ids


def groups_containing_user(
    user_oid: str, candidate_group_ids: Iterable[str]
) -> List[str]:
    """Return the subset of ``candidate_group_ids`` that ``user_oid`` is a direct
    member of, resolved group-centrically (no user/directory read).

    Used when the OIDC groups claim can't be delivered (ALB + Azure AD). RAISES
    if Graph is disabled or any member lookup fails, so the caller skips the sync
    rather than revoking every group-derived membership against a partial result.
    """
    if not _enabled():
        raise RuntimeError("graph fallback disabled")
    if not user_oid:
        return []
    return [
        gid
        for gid in candidate_group_ids
        if gid and user_oid in group_member_ids(gid)
    ]
