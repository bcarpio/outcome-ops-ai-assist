"""
ServiceNow REST client (KB scope).

Thin wrapper over the ServiceNow Table API + Attachment API used to
enumerate published ``kb_knowledge`` articles and their attachments, plus
the ``sys_user`` identity endpoint for the multi-instance guard.

All calls are issued against the customer's OWN ServiceNow instance URL
(every connection has its own ``instance_url``, set during the setup-form
submission). Bearer auth uses an access token minted via the per-workspace
OAuth credentials (see ``shared.oauth.servicenow`` + ADR-044).

429 responses log the exact phrase ``ServiceNow rate limited`` so a
CloudWatch metric filter can match on it and alarm.

Ported from ``../app.retrieveit.ai/lambda/servicenow-integration/
servicenow_api.py`` and adapted to OutcomeOps conventions:
- ``urllib.request`` with ``timeout=30`` (chunk 14) instead of ``requests``.
- Same error/retry contract as the source.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
ARTICLE_LIST_LIMIT = 200
DEFAULT_TIMEOUT_SECONDS = 30


class ServiceNowRateLimitedError(Exception):
    """Raised when ServiceNow returns 429 and we've exhausted MAX_RETRIES."""

    def __init__(self, endpoint: str, retry_after: Optional[float] = None):
        self.endpoint = endpoint
        self.retry_after = retry_after
        super().__init__(f"ServiceNow rate limited on {endpoint}")


def _request(
    method: str,
    instance_url: str,
    endpoint: str,
    access_token: str,
    params: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Make one ServiceNow API call. Retries on 429 / 5xx.

    Returns the parsed JSON body, or ``{}`` for 204 / 404 / empty bodies.
    Raises:
      ``ValueError`` on 401 / 403 / non-retryable 4xx / exhausted 5xx.
      ``ServiceNowRateLimitedError`` after ``MAX_RETRIES`` of 429.
    """
    url = f"{instance_url}{endpoint}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "outcome-ops-ai-assist/servicenow-api",
    }
    data_bytes: Optional[bytes] = None
    if json_body is not None:
        data_bytes = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(
            url, data=data_bytes, headers=headers, method=method,
        )
        status: int
        raw: bytes
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.getcode()
                raw = response.read() or b""
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read() or b""
            except Exception:
                raw = b""

        if status == 401:
            raise ValueError("Access token expired or invalid")

        if status == 403:
            raise ValueError("Access denied - insufficient permissions")

        if status == 404:
            return {}

        if status == 429:
            retry_after_header = _header_from_error(raw)
            retry_after = float(retry_after_header or BACKOFF_BASE_SECONDS)
            logger.warning(
                f"ServiceNow rate limited on {endpoint}; sleeping {retry_after}s"
            )
            if attempt >= MAX_RETRIES:
                raise ServiceNowRateLimitedError(endpoint, retry_after=retry_after)
            time.sleep(retry_after)
            continue

        if 500 <= status < 600:
            if attempt >= MAX_RETRIES:
                logger.error(
                    f"ServiceNow {status} after {attempt} attempts: {endpoint}"
                )
                raise ValueError(f"ServiceNow server error: {status}")
            backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                f"ServiceNow {status} on {endpoint}; backoff {backoff}s"
            )
            time.sleep(backoff)
            continue

        if status >= 400:
            logger.error(
                f"ServiceNow API error ({status}) on {endpoint}: {raw[:500]!r}"
            )
            raise ValueError(f"ServiceNow API error: {status}")

        if status == 204 or not raw:
            return {}

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}


def _header_from_error(raw: bytes) -> Optional[str]:
    """Best-effort Retry-After parse. urllib's HTTPError doesn't expose
    the response headers cleanly; ServiceNow occasionally embeds
    ``retry-after`` in the JSON body under ``error.detail``.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if isinstance(body, dict):
        error = body.get("error") or {}
        detail = error.get("detail")
        if isinstance(detail, str) and detail.isdigit():
            return detail
    return None


# ============================================================================
# KB endpoints
# ============================================================================


def get_current_user(instance_url: str, access_token: str) -> Dict[str, Any]:
    """Return the authenticated user's ``sys_user`` record.

    ServiceNow has no ``/me`` endpoint; the standard pattern is to query
    ``sys_user`` with ``user_name=javascript:gs.getUserName()``, which the
    instance evaluates server-side as the OAuth-authenticated user.
    Populates ``servicenow_user_id`` + ``servicenow_user_login`` on the
    connection row for the multi-instance guard.
    """
    data = _request(
        "GET",
        instance_url,
        "/api/now/table/sys_user",
        access_token,
        params={
            "sysparm_query": "user_name=javascript:gs.getUserName()",
            "sysparm_limit": "1",
            "sysparm_fields": "sys_id,user_name,name,email",
        },
    )
    if not data:
        return {}
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, list):
        return result[0] if result else {}
    return result or {}


def list_articles(
    instance_url: str,
    access_token: str,
    cursor: str,
    offset: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    """``GET /api/now/table/kb_knowledge`` filtered to published articles
    updated after ``cursor``.

    Returns ``(articles, has_more)``. ``has_more`` is True when the page is
    full, indicating the caller should bump the offset and try again.
    """
    safe_cursor = cursor or "1970-01-01 00:00:00"
    params = {
        "sysparm_query": (
            f"workflow_state=published"
            f"^sys_updated_on>{safe_cursor}"
            f"^ORDERBYsys_updated_on"
        ),
        "sysparm_limit": str(ARTICLE_LIST_LIMIT),
        "sysparm_offset": str(offset),
        "sysparm_fields": (
            "sys_id,number,short_description,text,workflow_state,"
            "sys_updated_on,sys_created_on,knowledge_base,category"
        ),
        "sysparm_display_value": "false",
        "sysparm_exclude_reference_link": "true",
    }
    data = _request(
        "GET", instance_url, "/api/now/table/kb_knowledge",
        access_token, params=params,
    )
    result = data.get("result", []) if isinstance(data, dict) else []
    has_more = len(result) == ARTICLE_LIST_LIMIT
    return result, has_more


def list_attachments_for_article(
    instance_url: str,
    access_token: str,
    article_sys_id: str,
) -> List[Dict[str, Any]]:
    """``GET /api/now/attachment`` for every attachment row whose
    ``table_name=kb_knowledge`` and ``table_sys_id`` matches the given
    article.
    """
    params = {
        "sysparm_query": (
            f"table_name=kb_knowledge^table_sys_id={article_sys_id}"
        ),
        "sysparm_fields": "sys_id,file_name,size_bytes,content_type,sys_updated_on",
    }
    data = _request(
        "GET", instance_url, "/api/now/attachment",
        access_token, params=params,
    )
    if not isinstance(data, dict):
        return []
    return data.get("result", []) or []


def get_article(
    instance_url: str,
    access_token: str,
    article_sys_id: str,
) -> Optional[Dict[str, Any]]:
    """``GET /api/now/table/kb_knowledge/{sys_id}`` — used by file-ingestion
    to re-fetch the article body if the embedded payload is stale.
    Returns None on 404.
    """
    data = _request(
        "GET", instance_url,
        f"/api/now/table/kb_knowledge/{article_sys_id}",
        access_token,
    )
    if not data:
        return None
    if isinstance(data, dict):
        return data.get("result") or data
    return None


def get_attachment_bytes(
    instance_url: str,
    access_token: str,
    attachment_sys_id: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """``GET /api/now/attachment/{sys_id}/file`` -- returns raw bytes.
    Used by file-ingestion to fetch attachment payloads for S3 upload.
    """
    url = f"{instance_url}/api/now/attachment/{attachment_sys_id}/file"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "outcome-ops-ai-assist/servicenow-api",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read() or b""
