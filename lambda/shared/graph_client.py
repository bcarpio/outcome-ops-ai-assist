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
from typing import Dict, List, Optional

from shared import group_patterns

_GRAPH = "https://graph.microsoft.com/v1.0"
_TOKEN_SKEW = 300  # refresh 5 min before expiry

# process-lifetime caches
_token_cache: Dict[str, object] = {"value": None, "exp": 0.0}
_secret_cache: Dict[str, Optional[str]] = {"value": None}


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
