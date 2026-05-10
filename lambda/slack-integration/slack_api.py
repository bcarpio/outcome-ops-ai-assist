"""
Slack Web API Client

Thin wrapper over Slack Web API (https://api.slack.com/methods).

Slack uses POST for almost everything, with form-encoded params or
application/json body. Responses always include `{"ok": bool, ...}`;
on `"ok": false` the response includes an `error` field (e.g.
`not_in_channel`, `channel_not_found`, `ratelimited`). This module
treats `ok: false` as an exception case via `_check_ok`.

Rate-limit handling:
- HTTP 429 with `Retry-After` header → sleep and retry up to 3 times.
- `ok: false` with `error: ratelimited` and a `Retry-After` header in
  the response (Slack sometimes returns 200 + ok=false for rate limit) →
  same behavior. Logs "Slack rate limited" so the cloudwatch-slack.tf
  log metric filter picks it up.
"""

import logging
import time
from typing import Any, Dict, Iterator, Optional

import requests

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0


def _check_ok(response_json: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
    """Raise ValueError if Slack returned ok=false. Otherwise return the body."""
    if not response_json.get("ok", False):
        err = response_json.get("error", "unknown_error")
        if err == "ratelimited":
            logger.warning(f"Slack rate limited on {endpoint} (ok=false)")
            raise SlackRateLimitedError(endpoint)
        logger.error(f"Slack API error on {endpoint}: {err} ({response_json})")
        raise ValueError(f"Slack API error on {endpoint}: {err}")
    return response_json


class SlackRateLimitedError(Exception):
    """Raised when Slack signals rate-limit (HTTP 429 or ok=false ratelimited)."""

    def __init__(self, endpoint: str, retry_after: Optional[float] = None):
        self.endpoint = endpoint
        self.retry_after = retry_after
        super().__init__(endpoint)


def _call(
    method: str,
    access_token: Optional[str],
    params: Optional[Dict] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """POST to a Slack Web API method. Retries on 429 + 5xx.

    access_token may be None for endpoints that authenticate via
    request body (e.g. oauth.v2.access uses client_id + client_secret
    in the body, not a bearer token).
    """
    url = f"{SLACK_API_BASE}/{method.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    attempt = 0
    while True:
        attempt += 1
        response = requests.post(
            url,
            data=params or {},
            headers=headers,
            timeout=timeout,
        )

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", BACKOFF_BASE_SECONDS))
            logger.warning(f"Slack rate limited on {method} (HTTP 429); sleeping {retry_after}s")
            if attempt >= MAX_RETRIES:
                raise SlackRateLimitedError(method, retry_after=retry_after)
            time.sleep(retry_after)
            continue

        if 500 <= response.status_code < 600:
            if attempt >= MAX_RETRIES:
                raise ValueError(f"Slack server error: {response.status_code}")
            backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(f"Slack {response.status_code} on {method}; backoff {backoff}s")
            time.sleep(backoff)
            continue

        if response.status_code >= 400:
            error_msg = response.text[:500]
            logger.error(f"Slack HTTP error ({response.status_code}) on {method}: {error_msg}")
            raise ValueError(f"Slack HTTP error: {response.status_code}")

        try:
            body = response.json()
        except ValueError:
            raise ValueError(f"Slack non-JSON response on {method}: {response.text[:200]}")

        # Some Slack methods return ok=false + ratelimited on 200; treat the same as 429.
        if not body.get("ok", False) and body.get("error") == "ratelimited":
            retry_after = float(response.headers.get("Retry-After", BACKOFF_BASE_SECONDS))
            logger.warning(f"Slack rate limited on {method} (ok=false); sleeping {retry_after}s")
            if attempt >= MAX_RETRIES:
                raise SlackRateLimitedError(method, retry_after=retry_after)
            time.sleep(retry_after)
            continue

        return _check_ok(body, method)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def oauth_v2_access(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> Dict[str, Any]:
    """Exchange the authorization code for a bot token via oauth.v2.access.

    Doesn't take a Bearer token — auth is via client_id + client_secret
    in the request body. Returns the full Slack response so the caller
    can extract access_token, team.id, team.name, bot_user_id, etc.
    """
    return _call(
        "oauth.v2.access",
        access_token=None,
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )


# ---------------------------------------------------------------------------
# Auth / team / users
# ---------------------------------------------------------------------------


def auth_test(access_token: str) -> Dict[str, Any]:
    """Validate the token and return {user_id, team_id, team, ...}."""
    return _call("auth.test", access_token=access_token)


def team_info(access_token: str) -> Dict[str, Any]:
    """Return {team: {id, name, domain, ...}}."""
    return _call("team.info", access_token=access_token)


def users_list(
    access_token: str,
    cursor: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """One page of users. Caller paginates via response_metadata.next_cursor."""
    params: Dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _call("users.list", access_token=access_token, params=params)


def iterate_users(access_token: str) -> Iterator[Dict[str, Any]]:
    """Generator over every user in the workspace."""
    cursor = None
    while True:
        page = users_list(access_token, cursor=cursor)
        for member in page.get("members", []):
            yield member
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return


def users_info(access_token: str, user_id: str) -> Dict[str, Any]:
    """Single-user lookup. Used as cache-miss fallback after users_list seed."""
    return _call("users.info", access_token=access_token, params={"user": user_id})


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def conversations_list(
    access_token: str,
    types: str = "public_channel,private_channel",
    cursor: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """One page of channels visible to the bot."""
    params: Dict[str, Any] = {"types": types, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _call("conversations.list", access_token=access_token, params=params)


def iterate_member_channels(access_token: str) -> Iterator[Dict[str, Any]]:
    """Generator over channels where the bot is a member.

    Filters at iteration time so the picker only ever shows actually-
    accessible channels.
    """
    cursor = None
    while True:
        page = conversations_list(access_token, cursor=cursor)
        for channel in page.get("channels", []):
            if channel.get("is_member"):
                yield channel
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def conversations_history(
    access_token: str,
    channel: str,
    oldest: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """One page of channel messages. oldest is a Slack ts cursor for delta."""
    params: Dict[str, Any] = {"channel": channel, "limit": limit}
    if oldest:
        params["oldest"] = oldest
    if cursor:
        params["cursor"] = cursor
    return _call("conversations.history", access_token=access_token, params=params)


def iterate_channel_messages(
    access_token: str,
    channel: str,
    oldest: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Generator over messages in a channel newer than the oldest cursor."""
    cursor = None
    while True:
        page = conversations_history(access_token, channel, oldest=oldest, cursor=cursor)
        for message in page.get("messages", []):
            yield message
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return


def conversations_replies(
    access_token: str,
    channel: str,
    ts: str,
    cursor: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """One page of replies on a thread. The first item in `messages` is the parent."""
    params: Dict[str, Any] = {"channel": channel, "ts": ts, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _call("conversations.replies", access_token=access_token, params=params)


def iterate_thread_replies(
    access_token: str,
    channel: str,
    ts: str,
) -> Iterator[Dict[str, Any]]:
    """Generator over thread replies. Skips the parent (first item)."""
    cursor = None
    first = True
    while True:
        page = conversations_replies(access_token, channel, ts, cursor=cursor)
        for message in page.get("messages", []):
            if first:
                first = False
                continue  # parent message, already in conversations.history result
            yield message
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return
