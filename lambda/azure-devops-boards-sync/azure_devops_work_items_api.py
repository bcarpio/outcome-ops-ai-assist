# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Azure DevOps Work Items REST API client.

Thin function-style wrappers over ADO REST v7.0 for the Work Items
+ WIQL surface. Independent module from the Repos client
(``lambda/azure-devops-sync/azure_devops_api.py``) so the two
endpoint surfaces don't grow into each other -- one is source-code,
the other is work-item content.

Raw ``urllib.request`` per the SharePoint/Outlook pattern -- no SDK,
no transitive deps beyond stdlib + boto3.

Host + API version:
- ``dev.azure.com`` -- work items, WIQL, projects, teams.
- ``API_VERSION = "7.0"`` for stable endpoints.
- Comments endpoint uses ``7.0-preview.3`` -- Microsoft's own
  labeling; the ``-preview.3`` sub-version is stable for
  production use and the only version that returns comment
  bodies.

Errors:
- ``AzureDevOpsBoardsAuthError`` (401)  -- caller refreshes token, retries once
- ``AzureDevOpsBoardsScopeError`` (403) -- token lacks vso.work; admin consent likely missing
- ``AzureDevOpsBoardsNotFoundError`` (404)
- ``AzureDevOpsBoardsServerError`` (5xx)
- ``AzureDevOpsBoardsAPIError`` -- network / timeout / unmapped
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


DEV_AZURE_HOST = "https://dev.azure.com"
API_VERSION = "7.0"
COMMENTS_API_VERSION = "7.0-preview.3"

REQUEST_TIMEOUT = 60

# ADO's documented cap on the number of work-item IDs per
# `GET /wit/workitems` batch call. The sync worker splits larger
# lists into 200-at-a-time chunks.
MAX_WORK_ITEMS_PER_BATCH = 200

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
USER_AGENT = f"OutcomeOps-AIAssist/{APP_NAME}/{ENV}"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AzureDevOpsBoardsAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class AzureDevOpsBoardsAuthError(AzureDevOpsBoardsAPIError):
    """401 -- access token expired/invalid; caller refreshes and retries once."""
    pass


class AzureDevOpsBoardsScopeError(AzureDevOpsBoardsAPIError):
    """403 -- token lacks vso.work scope. Admin likely hasn't granted
    consent for the Boards permission on the shared Entra ID app."""
    pass


class AzureDevOpsBoardsNotFoundError(AzureDevOpsBoardsAPIError):
    """404 -- org/project/work-item id doesn't exist or isn't visible
    to the connected user."""
    pass


class AzureDevOpsBoardsServerError(AzureDevOpsBoardsAPIError):
    """5xx -- ADO is down or transiently broken. Caller decides retry policy."""
    pass


# ---------------------------------------------------------------------------
# Standard field selection (open question 3 -- v1 fixed list)
# ---------------------------------------------------------------------------


STANDARD_WORK_ITEM_FIELDS: List[str] = [
    "System.Id",
    "System.Title",
    "System.WorkItemType",
    "System.State",
    "System.Reason",
    "System.AssignedTo",
    "System.CreatedDate",
    "System.ChangedDate",
    "System.Tags",
    "System.Description",
    "Microsoft.VSTS.TCM.ReproSteps",
    "Microsoft.VSTS.Common.AcceptanceCriteria",
    "Microsoft.VSTS.Common.Priority",
    "Microsoft.VSTS.Common.Severity",
    "System.AreaPath",
    "System.IterationPath",
]


# ---------------------------------------------------------------------------
# HTTP entry point (mirrors azure-devops-sync/azure_devops_api.py::_request)
# ---------------------------------------------------------------------------


def _request(
    method: str,
    url: str,
    access_token: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = REQUEST_TIMEOUT,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Single HTTP entry point. Returns (parsed_body, response_headers).

    JSON in, JSON out. Boards has no binary-content path so we don't
    need the Repos client's ``parse_json=False`` branch.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(
        url, data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            resp_headers = dict(response.headers)
            raw = response.read()
            if not raw:
                return {}, resp_headers
            return json.loads(raw.decode("utf-8")), resp_headers
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8") if e.fp else ""
        except Exception:  # noqa: BLE001
            pass
        path_for_log = urllib.parse.urlsplit(url).path
        logger.error(
            f"ADO Boards API {method} {path_for_log} -> {e.code}: {error_body}"
        )
        # ADO returns JSON errors like
        # {"message": "TF...: friendly text", "typeName": "..."}.
        # Bubble the ``message`` field up in the exception string so
        # the caller (sync worker) can render something diagnostic
        # instead of just "400 on {path}".
        ado_msg = _extract_ado_error_message(error_body)
        suffix = f": {ado_msg}" if ado_msg else ""
        if e.code == 401:
            raise AzureDevOpsBoardsAuthError(
                f"401 on {path_for_log}{suffix}",
                status_code=401, response=error_body,
            )
        if e.code == 403:
            raise AzureDevOpsBoardsScopeError(
                f"403 on {path_for_log}{suffix}",
                status_code=403, response=error_body,
            )
        if e.code == 404:
            raise AzureDevOpsBoardsNotFoundError(
                f"404 on {path_for_log}{suffix}",
                status_code=404, response=error_body,
            )
        if 500 <= e.code < 600:
            raise AzureDevOpsBoardsServerError(
                f"{e.code} on {path_for_log}",
                status_code=e.code, response=error_body,
            )
        raise AzureDevOpsBoardsAPIError(
            f"{e.code} on {path_for_log}{suffix}",
            status_code=e.code, response=error_body,
        )
    except urllib.error.URLError as e:
        raise AzureDevOpsBoardsAPIError(f"Network error on {url}: {e.reason}")


def _extract_ado_error_message(error_body: str) -> str:
    """Pull the human-readable message field out of ADO's JSON error body.

    ADO returns errors as
    ``{"message": "TF...: ...", "typeName": "...", "typeKey": "...",
    "innerException": null|{...}, ...}``. We surface ``message`` (and
    inner ``message`` if present) so the caller sees exactly what ADO
    complained about instead of just an HTTP code.
    """
    if not error_body:
        return ""
    try:
        parsed = json.loads(error_body)
    except (ValueError, TypeError):
        # Non-JSON body -- return the first line, capped.
        return error_body.strip().splitlines()[0][:400]
    if not isinstance(parsed, dict):
        return ""
    msg = parsed.get("message") or ""
    inner = parsed.get("innerException")
    if isinstance(inner, dict) and inner.get("message"):
        msg = f"{msg} -- {inner['message']}" if msg else inner["message"]
    return str(msg)[:400] if msg else ""


def _add_query(url: str, params: Dict[str, Any]) -> str:
    """Append query params to a URL, preserving any already present.

    Mirrors the Repos client helper. Kept local to this module so the
    two clients can evolve independently.
    """
    parts = urllib.parse.urlsplit(url)
    existing = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    existing.update({k: str(v) for k, v in params.items() if v is not None})
    new_query = urllib.parse.urlencode(existing)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment),
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _q(segment: str) -> str:
    """URL-encode a path segment. ADO org / project names can contain
    spaces and other reserved characters -- urllib.request rejects a
    URL with control chars, so every user-supplied path segment gets
    quoted before it lands in the URL. ``safe=''`` is intentional: no
    reserved characters are allowed to pass through raw.
    """
    return urllib.parse.quote(segment, safe="")


def list_projects(access_token: str, org: str) -> List[Dict[str, Any]]:
    """List projects in an org. Same endpoint as the Repos client, but
    kept local so Boards doesn't depend on the Repos module's import path.
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{_q(org)}/_apis/projects",
        {"api-version": API_VERSION},
    )
    body, _ = _request("GET", url, access_token)
    return body.get("value", []) or []


def list_teams(
    access_token: str,
    org: str,
    project: str,
) -> List[Dict[str, Any]]:
    """List teams in a project. Exposed for v2 per-team filtering;
    v1 syncs whole projects and does not consume this endpoint yet.
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{_q(org)}/_apis/projects/{_q(project)}/teams",
        {"api-version": API_VERSION},
    )
    body, _ = _request("GET", url, access_token)
    return body.get("value", []) or []


# ---------------------------------------------------------------------------
# WIQL -- Work Item Query Language
# ---------------------------------------------------------------------------


def build_incremental_wiql(
    project: str,
    changed_since: Optional[str] = None,
    open_only: bool = True,
) -> str:
    """Build the incremental-sync WIQL query for one project.

    - Filters to the given project (System.TeamProject).
    - Excludes closed / done / removed items when ``open_only`` is
      True (the v1 default per the plan's open question 2 answer).
      Closed items already ingested stay in the KB; they just stop
      being re-synced once they transition out of an active state.
    - Applies the ``ChangedDate > changed_since`` watermark when
      supplied. When ``changed_since`` is None (first sync) the
      query returns every currently-open item ordered by
      ChangedDate DESC -- the sync worker takes the top N per
      the plan's initial-sync cap.

    Ordering by ChangedDate DESC + Id DESC guarantees stable
    pagination: two items with the same ChangedDate sort
    deterministically.
    """
    conditions = [f"[System.TeamProject] = '{_escape_wiql_string(project)}'"]
    if open_only:
        # ADO ships several closed-state names depending on process
        # template (Agile/Scrum/CMMI/Basic). Enumerate the common ones
        # so a customer using any of the standard templates gets the
        # right filter without configuration.
        closed_states = ["Closed", "Done", "Removed", "Resolved"]
        quoted = ", ".join(
            f"'{_escape_wiql_string(s)}'" for s in closed_states
        )
        conditions.append(f"[System.State] NOT IN ({quoted})")
    if changed_since:
        # WIQL treats [System.ChangedDate] as date-precision: passing
        # a time component ("2026-04-07T08:04:35Z") triggers
        # TF-style parser error "You cannot supply a time with the
        # date when running a query using date precision." Strip to
        # YYYY-MM-DD and use >= (inclusive) instead of >, so items
        # that changed later on the watermark day still come along.
        # Sub-day accuracy is a nice-to-have; deterministic
        # document_id + idempotent S3 puts absorb the re-fetch cost
        # of items that were already ingested earlier the same day.
        date_only = changed_since.split("T", 1)[0]
        conditions.append(
            f"[System.ChangedDate] >= '{_escape_wiql_string(date_only)}'"
        )
    where_clause = "\n  AND ".join(conditions)
    return (
        "SELECT [System.Id]\n"
        "FROM workitems\n"
        f"WHERE {where_clause}\n"
        "ORDER BY [System.ChangedDate] DESC, [System.Id] DESC"
    )


def _escape_wiql_string(value: str) -> str:
    """Escape single quotes for WIQL string literals.

    WIQL uses single-quoted string literals with '' as the escape for
    a literal single quote (SQL-like). No other escaping is required
    for the field values we pass -- project names, ISO timestamps,
    and state names never contain backslashes or control chars in ADO.
    """
    return value.replace("'", "''")


def run_wiql(
    access_token: str,
    org: str,
    project: str,
    wiql_query: str,
) -> List[int]:
    """Run a WIQL query. Returns a list of matching work-item IDs.

    WIQL returns IDs only -- callers pass the returned list to
    ``get_work_items`` to fetch the actual field values. This
    two-step is Microsoft's own separation; ADO caps the ID list
    at 20,000 per call, so the sync worker's WIQL query should
    always be tight (project + changed-since + open-only).
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{_q(org)}/{_q(project)}/_apis/wit/wiql",
        {"api-version": API_VERSION},
    )
    body, _ = _request("POST", url, access_token, body={"query": wiql_query})
    return [
        item["id"] for item in body.get("workItems", []) or []
        if isinstance(item, dict) and "id" in item
    ]


# ---------------------------------------------------------------------------
# Work-item content fetch
# ---------------------------------------------------------------------------


def get_work_items(
    access_token: str,
    org: str,
    ids: Iterable[int],
    fields: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch a batch of work items by ID.

    ADO caps this at ``MAX_WORK_ITEMS_PER_BATCH`` (200) IDs per
    request. Callers batch larger ID lists into 200-at-a-time
    chunks. This function does NOT batch; it validates the
    caller passed <=200 IDs and raises on overflow.

    ``fields`` defaults to ``STANDARD_WORK_ITEM_FIELDS`` -- the v1
    fixed field list. Custom-field expansion is a v2 follow-up per
    open question 3.
    """
    id_list = list(ids)
    if not id_list:
        return []
    if len(id_list) > MAX_WORK_ITEMS_PER_BATCH:
        raise ValueError(
            f"get_work_items called with {len(id_list)} IDs; ADO caps at "
            f"{MAX_WORK_ITEMS_PER_BATCH} per batch. Callers must chunk."
        )
    fields_list = fields if fields is not None else STANDARD_WORK_ITEM_FIELDS
    url = _add_query(
        f"{DEV_AZURE_HOST}/{_q(org)}/_apis/wit/workitems",
        {
            "ids": ",".join(str(i) for i in id_list),
            "fields": ",".join(fields_list),
            "api-version": API_VERSION,
        },
    )
    body, _ = _request("GET", url, access_token)
    return body.get("value", []) or []


def get_work_item_comments(
    access_token: str,
    org: str,
    project: str,
    work_item_id: int,
) -> List[Dict[str, Any]]:
    """Fetch all comments on a work item.

    Uses the ``7.0-preview.3`` API version -- Microsoft's own
    labeling on the comments endpoint. It's the stable production
    endpoint for comment bodies; the non-preview ``7.0`` version
    returns comment metadata only.
    """
    url = _add_query(
        f"{DEV_AZURE_HOST}/{_q(org)}/{_q(project)}/_apis/wit/workitems/{work_item_id}/comments",
        {"api-version": COMMENTS_API_VERSION},
    )
    body, _ = _request("GET", url, access_token)
    return body.get("comments", []) or []
