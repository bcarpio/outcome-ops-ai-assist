"""
Salesforce REST + SOQL client.

Wraps the subset of the Salesforce REST API used to enumerate Accounts,
Contacts, and Opportunities (rendered to markdown), their recent activities
(Task/Event embedded in the record doc), and attached files (ContentVersion
ingested as binaries).

All calls are issued against the connection's own ``instance_url`` (the
customer's My Domain host, returned in the OAuth token response). Bearer
auth uses an access token minted by ``shared.oauth.salesforce``.

Because SOQL runs as the connected OAuth user, field-level security and
sharing rules are applied by Salesforce automatically -- the sync sees
only the fields and records that user can see.

429 / 403+``REQUEST_LIMIT_EXCEEDED`` responses log the exact phrase
``Salesforce rate limited`` so a CloudWatch metric filter can match on it
and alarm.

Ported from ``../app.retrieveit.ai/lambda/salesforce-integration/
salesforce_api.py`` and adapted to OutcomeOps conventions:
- Lives under ``shared/`` so integration + sync + file-ingestion share
  one canonical module.
- ``urllib.request`` with ``timeout=30`` instead of ``requests``.
- Same error/retry contract as the source.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


API_VERSION = "v60.0"
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
PAGE_LIMIT = 200
ACTIVITY_LIMIT = 20
DEFAULT_TIMEOUT_SECONDS = 30

# Field lists (no SELECT *). Relationship fields use dot notation;
# missing/inaccessible fields simply come back absent under FLS.
ACCOUNT_FIELDS = (
    "Id, Name, Type, Industry, Website, Phone, Description, "
    "BillingCity, BillingState, BillingCountry, "
    "NumberOfEmployees, AnnualRevenue, Owner.Name, SystemModstamp"
)
CONTACT_FIELDS = (
    "Id, Name, Title, Department, Email, Phone, "
    "Account.Name, Description, Owner.Name, SystemModstamp"
)
OPPORTUNITY_FIELDS = (
    "Id, Name, Account.Name, StageName, Amount, CloseDate, Probability, "
    "Type, LeadSource, NextStep, Description, Owner.Name, SystemModstamp"
)

OBJECT_FIELDS = {
    "Account": ACCOUNT_FIELDS,
    "Contact": CONTACT_FIELDS,
    "Opportunity": OPPORTUNITY_FIELDS,
}

# Contacts relate to activities via WhoId; Account/Opportunity via WhatId.
ACTIVITY_PARENT_FIELD = {
    "Account": "WhatId",
    "Opportunity": "WhatId",
    "Contact": "WhoId",
}


class SalesforceRateLimitedError(Exception):
    """Raised when Salesforce rate-limits and we've exhausted MAX_RETRIES."""

    def __init__(self, endpoint: str, retry_after: Optional[float] = None):
        self.endpoint = endpoint
        self.retry_after = retry_after
        super().__init__(f"Salesforce rate limited on {endpoint}")


def _request(
    method: str,
    instance_url: str,
    endpoint: str,
    access_token: str,
    params: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    binary: bool = False,
) -> Tuple[int, bytes]:
    """Make one Salesforce API call. Retries on 429 / 5xx.

    Returns ``(status_code, body_bytes)``. Callers parse JSON or read
    bytes as needed.

    Raises:
      ``ValueError`` on 401 / 403 (perms) / non-retryable 4xx / exhausted 5xx.
      ``SalesforceRateLimitedError`` after ``MAX_RETRIES`` of rate limiting.
    """
    if endpoint.startswith("http"):
        url = endpoint
    else:
        url = f"{instance_url}{endpoint}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/octet-stream" if binary else "application/json",
        "User-Agent": "outcome-ops-ai-assist/salesforce-api",
    }

    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.getcode(), response.read() or b""
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read() or b""
            except Exception:
                raw = b""

        if status == 401:
            raise ValueError("Access token expired or invalid")

        # Salesforce uses 403 for BOTH real perms errors AND daily API
        # quota (REQUEST_LIMIT_EXCEEDED). Distinguish by peeking at body.
        if status == 403 and b"REQUEST_LIMIT_EXCEEDED" not in raw:
            raise ValueError("Access denied - insufficient permissions")

        if status == 404:
            return status, raw

        is_rate_limit = status == 429 or status == 403
        is_server_error = 500 <= status < 600
        if not (is_rate_limit or is_server_error):
            logger.error(
                f"Salesforce API error ({status}) on {endpoint}: "
                f"{raw[:500].decode('utf-8', errors='replace')}"
            )
            raise ValueError(f"Salesforce API error: {status}")

        if attempt >= MAX_RETRIES:
            if is_rate_limit:
                raise SalesforceRateLimitedError(endpoint)
            logger.error(
                f"Salesforce {status} after {attempt} attempts: {endpoint}"
            )
            raise ValueError(f"Salesforce server error: {status}")

        if is_rate_limit:
            delay = BACKOFF_BASE_SECONDS
            logger.warning(
                f"Salesforce rate limited on {endpoint}; sleeping {delay}s"
            )
        else:
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(f"Salesforce {status} on {endpoint}; backoff {delay}s")
        time.sleep(delay)


def _soql(instance_url: str, access_token: str, query: str) -> Dict[str, Any]:
    status, body = _request(
        "GET", instance_url, f"/services/data/{API_VERSION}/query",
        access_token, params={"q": query},
    )
    if status == 404 or not body:
        return {}
    return json.loads(body)


def _soql_more(instance_url: str, access_token: str, next_url: str) -> Dict[str, Any]:
    status, body = _request("GET", instance_url, next_url, access_token)
    if status == 404 or not body:
        return {}
    return json.loads(body)


def _escape_soql_literal(value: str) -> str:
    """Escape a value for single-quoted SOQL string literals."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _iterate_query(
    instance_url: str, access_token: str, query: str,
) -> Iterator[Dict[str, Any]]:
    """Run a SOQL query and yield every record, following nextRecordsUrl."""
    data = _soql(instance_url, access_token, query)
    while True:
        for record in data.get("records", []):
            yield record
        if data.get("done", True):
            return
        next_url = data.get("nextRecordsUrl")
        if not next_url:
            return
        data = _soql_more(instance_url, access_token, next_url)


def _chunk(items: List[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_userinfo(instance_url: str, access_token: str) -> Dict[str, Any]:
    """Return the connected user's OpenID userinfo.

    Used to populate salesforce_user_id / salesforce_user_login on the
    connection row (identity capture for multi-account guard).
    """
    status, body = _request(
        "GET", instance_url, "/services/oauth2/userinfo", access_token,
    )
    if status == 404 or not body:
        return {}
    return json.loads(body)


def iterate_records(
    instance_url: str,
    access_token: str,
    object_type: str,
    cursor: str,
) -> Iterator[Dict[str, Any]]:
    """Yield records of ``object_type`` modified after ``cursor``, oldest first.

    ``cursor`` is a SOQL datetime literal (e.g. 2026-05-20T14:30:00Z) or
    "" for a full sync. Pagination follows nextRecordsUrl internally.
    Raises SalesforceRateLimitedError / ValueError on hard failure.
    """
    fields = OBJECT_FIELDS[object_type]
    where = f"WHERE SystemModstamp > {cursor} " if cursor else ""
    query = (
        f"SELECT {fields} FROM {object_type} "
        f"{where}ORDER BY SystemModstamp ASC LIMIT {PAGE_LIMIT * 50}"
    )
    yield from _iterate_query(instance_url, access_token, query)


def fetch_records_by_ids(
    instance_url: str,
    access_token: str,
    object_type: str,
    record_ids: List[str],
) -> List[Dict[str, Any]]:
    """Fetch full records of ``object_type`` by Id (chunked into IN-lists).

    Used to re-render parents whose own SystemModstamp didn't move but
    whose child activities/files changed (incremental completeness).
    """
    fields = OBJECT_FIELDS[object_type]
    out: List[Dict[str, Any]] = []
    for chunk in _chunk(list(record_ids), 200):
        quoted = ", ".join(f"'{_escape_soql_literal(i)}'" for i in chunk)
        query = f"SELECT {fields} FROM {object_type} WHERE Id IN ({quoted})"
        out.extend(_iterate_query(instance_url, access_token, query))
    return out


def latest_modified_date(
    instance_url: str, access_token: str, object_type: str,
) -> str:
    """Return the most recent LastModifiedDate for ``object_type``, or "".

    Used on full sync to baseline child cursors so the next incremental
    sync only looks at activities/files changed after the initial walk.
    """
    query = (
        f"SELECT LastModifiedDate FROM {object_type} "
        f"ORDER BY LastModifiedDate DESC LIMIT 1"
    )
    for row in _iterate_query(instance_url, access_token, query):
        return row.get("LastModifiedDate") or ""
    return ""


def changed_activity_parent_ids(
    instance_url: str,
    access_token: str,
    activity_object: str,
    cursor: str,
) -> Tuple[Set[str], str]:
    """Find parent record IDs of Tasks/Events changed after ``cursor``.

    ``activity_object`` is "Task" or "Event". Returns (parent_ids,
    new_cursor) where parent_ids is the union of WhatId/WhoId and
    new_cursor is the max LastModifiedDate seen.
    """
    where = f"WHERE LastModifiedDate > {cursor} " if cursor else ""
    query = (
        f"SELECT WhatId, WhoId, LastModifiedDate FROM {activity_object} "
        f"{where}ORDER BY LastModifiedDate ASC LIMIT {PAGE_LIMIT * 50}"
    )
    parent_ids: Set[str] = set()
    new_cursor = cursor
    for row in _iterate_query(instance_url, access_token, query):
        for field in ("WhatId", "WhoId"):
            value = row.get(field)
            if value:
                parent_ids.add(value)
        last_mod = row.get("LastModifiedDate") or ""
        if last_mod > new_cursor:
            new_cursor = last_mod
    return parent_ids, new_cursor


def changed_file_parent_ids(
    instance_url: str,
    access_token: str,
    cursor: str,
) -> Tuple[Set[str], str]:
    """Find record IDs that had a ContentVersion changed after ``cursor``.

    Two-step: ContentVersion (by LastModifiedDate) -> ContentDocumentLink
    -> LinkedEntityId. Returns (parent_ids, new_cursor).
    """
    where = (
        f"WHERE LastModifiedDate > {cursor} AND IsLatest = true "
        if cursor else "WHERE IsLatest = true "
    )
    query = (
        f"SELECT ContentDocumentId, LastModifiedDate FROM ContentVersion "
        f"{where}ORDER BY LastModifiedDate ASC LIMIT {PAGE_LIMIT * 50}"
    )
    doc_ids: Set[str] = set()
    new_cursor = cursor
    for row in _iterate_query(instance_url, access_token, query):
        doc_id = row.get("ContentDocumentId")
        if doc_id:
            doc_ids.add(doc_id)
        last_mod = row.get("LastModifiedDate") or ""
        if last_mod > new_cursor:
            new_cursor = last_mod
    if not doc_ids:
        return set(), new_cursor

    parent_ids: Set[str] = set()
    for chunk in _chunk(list(doc_ids), 200):
        quoted = ", ".join(f"'{_escape_soql_literal(d)}'" for d in chunk)
        link_q = (
            f"SELECT LinkedEntityId FROM ContentDocumentLink "
            f"WHERE ContentDocumentId IN ({quoted})"
        )
        for row in _iterate_query(instance_url, access_token, link_q):
            value = row.get("LinkedEntityId")
            if value:
                parent_ids.add(value)
    return parent_ids, new_cursor


def list_recent_activities(
    instance_url: str,
    access_token: str,
    object_type: str,
    record_id: str,
) -> List[Dict[str, Any]]:
    """Return up to ACTIVITY_LIMIT recent Tasks + Events for a record.

    Combined + sorted newest-first. Feeds the "Recent activity" section
    embedded in the record's markdown doc.
    """
    parent_field = ACTIVITY_PARENT_FIELD[object_type]
    rid = _escape_soql_literal(record_id)
    activities: List[Dict[str, Any]] = []

    task_q = (
        f"SELECT Subject, Status, Priority, ActivityDate, Description, "
        f"Owner.Name, LastModifiedDate FROM Task "
        f"WHERE {parent_field} = '{rid}' "
        f"ORDER BY LastModifiedDate DESC LIMIT {ACTIVITY_LIMIT}"
    )
    event_q = (
        f"SELECT Subject, ActivityDate, ActivityDateTime, Description, "
        f"Owner.Name, LastModifiedDate FROM Event "
        f"WHERE {parent_field} = '{rid}' "
        f"ORDER BY LastModifiedDate DESC LIMIT {ACTIVITY_LIMIT}"
    )

    for kind, q in (("Task", task_q), ("Event", event_q)):
        try:
            data = _soql(instance_url, access_token, q)
        except ValueError as e:
            # Activities are best-effort context; one kind failing shouldn't
            # sink the whole record's ingest.
            logger.warning(f"Activity query ({kind}) failed for {record_id}: {e}")
            continue
        for row in data.get("records", []):
            row["_activity_kind"] = kind
            activities.append(row)

    activities.sort(key=lambda a: a.get("LastModifiedDate") or "", reverse=True)
    return activities[:ACTIVITY_LIMIT]


def list_content_versions(
    instance_url: str,
    access_token: str,
    record_id: str,
) -> List[Dict[str, Any]]:
    """Return latest ContentVersion rows for files attached to a record."""
    rid = _escape_soql_literal(record_id)
    link_q = (
        f"SELECT ContentDocumentId FROM ContentDocumentLink "
        f"WHERE LinkedEntityId = '{rid}'"
    )
    link_data = _soql(instance_url, access_token, link_q)
    doc_ids = [
        r["ContentDocumentId"]
        for r in link_data.get("records", [])
        if r.get("ContentDocumentId")
    ]
    if not doc_ids:
        return []

    quoted = ", ".join(f"'{_escape_soql_literal(d)}'" for d in doc_ids)
    ver_q = (
        f"SELECT Id, Title, FileExtension, ContentSize, ContentDocumentId "
        f"FROM ContentVersion "
        f"WHERE ContentDocumentId IN ({quoted}) AND IsLatest = true"
    )
    ver_data = _soql(instance_url, access_token, ver_q)
    return ver_data.get("records", []) or []


def download_content_version(
    instance_url: str,
    access_token: str,
    content_version_id: str,
) -> bytes:
    """Download the binary body of a ContentVersion."""
    _, body = _request(
        "GET", instance_url,
        f"/services/data/{API_VERSION}/sobjects/ContentVersion/"
        f"{content_version_id}/VersionData",
        access_token, timeout=60, binary=True,
    )
    return body
