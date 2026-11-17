# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
MCP session store (fable-scale chunk 02).

The MCP server previously kept two per-container dicts:

  _sessions[session_id] = {"email", "last_active", "tool_routing"}
      (our own MCP session for authenticated clients)
  _mcp_sessions[mcp_id] = {"session_id", "created_at"}
      (upstream MCP-server sessions we've negotiated for proxying)

Both were module-global dicts mutated from ThreadingHTTPServer request
threads with no lock. That's the design constraint that prevented
horizontal scaling of the MCP Fargate service: a second task can't share
sessions, and there's no ALB stickiness.

This module provides a single abstraction, `SessionStore`, with two
implementations:

  MemorySessionStore  — dict + threading.Lock; used by tests and by any
                        operator who flips MCP_SESSION_STORE=memory as a
                        rollback.
  DdbSessionStore     — DynamoDB, TTL-driven eviction. Enables autoscaling.

Both stores use string session_ids. Upstream MCP proxied-server sessions
share the same table under keys prefixed "upstream#<mcp_id>", so we don't
need two tables.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from decimal import Decimal
from typing import Any, Callable, Optional

import boto3
from botocore.exceptions import ClientError


def _to_ddb(value: Any) -> Any:
    """Coerce Python types the boto3 DDB resource can't serialize.

    time.time() returns a float; DDB's resource-level client requires Decimal
    for numeric attributes. Handler code writes floats without knowing which
    store impl backs it, so the store absorbs the coercion here rather than
    push it up to every call site.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value


def _from_ddb(value: Any) -> Any:
    """Reverse of _to_ddb — return Decimals as float for caller convenience."""
    if isinstance(value, Decimal):
        # Session numerics are timestamps + TTL, never money — float is fine.
        return float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value

logger = logging.getLogger("outcomeops-mcp.session_store")

# TTL headroom: the item's expires_at is set to last_active + SESSION_TTL_SECONDS.
# DDB TTL is best-effort with up to 48h lag, so the application still checks
# last_active against a shorter timeout at read time (defense-in-depth).
SESSION_TTL_SECONDS = int(os.environ.get("MCP_SESSION_TTL_SECONDS", "3600"))


class SessionStore:
    """Abstract session store contract."""

    def get(self, session_id: str) -> Optional[dict]:
        raise NotImplementedError

    def put(self, session_id: str, session: dict) -> None:
        raise NotImplementedError

    def update_field(self, session_id: str, field: str, value: Any) -> None:
        """Update a single named field on an existing session row.

        No-op if the session doesn't exist (matches the pre-refactor semantics
        of the dict-based code, which guarded `if session_id in _sessions`
        before mutating).
        """
        raise NotImplementedError

    def delete(self, session_id: str) -> None:
        raise NotImplementedError

    def cleanup_expired(self) -> int:
        """Return count of sessions removed. DDB store no-ops (TTL owns it)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# In-memory implementation. Same semantics as the old module-global dicts,
# but with a lock so ThreadingHTTPServer request threads don't race.
#
# The store can be constructed with externally-owned backing dicts. handler.py
# passes the module-level `_sessions` and `_mcp_sessions` dicts in so the
# existing unit tests (which seed those dicts directly) keep working.
# `_UPSTREAM_PREFIX` routes upstream MCP-proxy sessions into the second
# dict; everything else lands in the first.
# ---------------------------------------------------------------------------


_UPSTREAM_PREFIX = "upstream#"


class MemorySessionStore(SessionStore):
    def __init__(
        self,
        user_backing: Optional[dict] = None,
        upstream_backing: Optional[dict] = None,
    ) -> None:
        self._sessions: dict[str, dict] = user_backing if user_backing is not None else {}
        self._upstream: dict[str, dict] = upstream_backing if upstream_backing is not None else {}
        self._lock = threading.Lock()

    def _bucket(self, session_id: str):
        if session_id.startswith(_UPSTREAM_PREFIX):
            return self._upstream, session_id[len(_UPSTREAM_PREFIX):]
        return self._sessions, session_id

    def get(self, session_id: str) -> Optional[dict]:
        bucket, key = self._bucket(session_id)
        with self._lock:
            entry = bucket.get(key)
            # Return a shallow copy so callers can mutate their local view
            # without racing another thread's mutation.
            return dict(entry) if entry is not None else None

    def put(self, session_id: str, session: dict) -> None:
        bucket, key = self._bucket(session_id)
        with self._lock:
            bucket[key] = dict(session)

    def update_field(self, session_id: str, field: str, value: Any) -> None:
        bucket, key = self._bucket(session_id)
        with self._lock:
            if key in bucket:
                bucket[key][field] = value

    def delete(self, session_id: str) -> None:
        bucket, key = self._bucket(session_id)
        with self._lock:
            bucket.pop(key, None)

    def cleanup_expired(self) -> int:
        cutoff = time.time() - SESSION_TTL_SECONDS
        removed = 0
        with self._lock:
            for bucket in (self._sessions, self._upstream):
                expired = [
                    sid for sid, s in bucket.items()
                    if s.get("last_active", 0) < cutoff
                ]
                for sid in expired:
                    del bucket[sid]
                removed += len(expired)
        return removed


# ---------------------------------------------------------------------------
# DynamoDB implementation. Table shape defined in
# terraform/dynamodb-mcp-sessions.tf:
#   PK (S)          session_id
#   expires_at (N)  unix seconds; TTL attribute
#   plus arbitrary session fields (email, last_active, tool_routing, ...)
# ---------------------------------------------------------------------------


class DdbSessionStore(SessionStore):
    def __init__(
        self,
        table_name: str,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._table_name = table_name
        self._client_factory = client_factory or (
            lambda: boto3.resource("dynamodb", region_name=os.environ.get(
                "AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2")
            ))
        )
        self._table = None
        self._lock = threading.Lock()

    def _get_table(self):
        # Lazy: don't touch boto until first use so unit tests can inject a
        # stub factory. Thread-safe via lock; concurrent init is fine because
        # boto3.resource is idempotent.
        if self._table is None:
            with self._lock:
                if self._table is None:
                    self._table = self._client_factory().Table(self._table_name)
        return self._table

    def get(self, session_id: str) -> Optional[dict]:
        try:
            resp = self._get_table().get_item(Key={"PK": session_id})
        except ClientError as e:
            logger.warning("session_store.get failed for %s: %s", session_id[:8], e)
            return None
        item = resp.get("Item")
        if not item:
            return None
        # DDB TTL is best-effort — an item past its expires_at may still be
        # returned. Filter application-side.
        raw_expires = item.get("expires_at", 0)
        expires_at = float(raw_expires) if raw_expires else 0
        if expires_at and expires_at < time.time():
            return None
        # Strip DDB-internal fields; callers expect the original session dict shape.
        item.pop("PK", None)
        item.pop("expires_at", None)
        return _from_ddb(item)

    def put(self, session_id: str, session: dict) -> None:
        item = {"PK": session_id}
        item.update(_to_ddb(session))
        # Refresh TTL on every put — session activity extends the lifetime.
        item["expires_at"] = int(time.time()) + SESSION_TTL_SECONDS
        try:
            self._get_table().put_item(Item=item)
        except ClientError as e:
            logger.error("session_store.put failed for %s: %s", session_id[:8], e)
            raise

    def update_field(self, session_id: str, field: str, value: Any) -> None:
        # ConditionExpression enforces the "no-op if session missing" semantics
        # from the dict-based code. attribute_exists(PK) fails cleanly with
        # ConditionalCheckFailedException; swallow it.
        try:
            self._get_table().update_item(
                Key={"PK": session_id},
                UpdateExpression="SET #f = :v, expires_at = :e",
                ExpressionAttributeNames={"#f": field},
                ExpressionAttributeValues={
                    ":v": _to_ddb(value),
                    ":e": int(time.time()) + SESSION_TTL_SECONDS,
                },
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return
            logger.warning(
                "session_store.update_field(%s) failed for %s: %s",
                field, session_id[:8], e,
            )

    def delete(self, session_id: str) -> None:
        try:
            self._get_table().delete_item(Key={"PK": session_id})
        except ClientError as e:
            logger.warning("session_store.delete failed for %s: %s", session_id[:8], e)

    def cleanup_expired(self) -> int:
        # DDB TTL is authoritative for eviction; the app has no reason to scan
        # for expired rows.
        return 0


# ---------------------------------------------------------------------------
# Factory. handler.py imports build_session_store(); the choice is env-driven
# so tests can force memory without touching boto.
# ---------------------------------------------------------------------------


def build_session_store(
    user_backing: Optional[dict] = None,
    upstream_backing: Optional[dict] = None,
) -> SessionStore:
    """Construct the store implied by env config.

    In memory mode the caller can pass externally-owned backing dicts —
    handler.py does this so its module-level `_sessions` and `_mcp_sessions`
    aliases (used by legacy unit tests) stay in sync with the store.
    """
    kind = os.environ.get("MCP_SESSION_STORE", "memory").lower()
    if kind == "ddb":
        table = os.environ.get("MCP_SESSIONS_TABLE", "")
        if not table:
            logger.warning(
                "MCP_SESSION_STORE=ddb but MCP_SESSIONS_TABLE is empty; "
                "falling back to in-memory store."
            )
            return MemorySessionStore(user_backing, upstream_backing)
        logger.info("Using DDB session store: table=%s", table)
        return DdbSessionStore(table)
    return MemorySessionStore(user_backing, upstream_backing)
