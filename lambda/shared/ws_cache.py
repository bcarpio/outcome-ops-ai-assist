# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Per-instance TTL cache for workspaces-table queries (fable-scale chunk 03).

The chat and MCP hot paths were doing full workspaces-table Scans on every
message / tool call:

  chat.handler._list_all_workspace_metadata:
      Scan filtered to METADATA rows — fires per chat message when the
      Rule-3 broaden triggers (Global Workspace member at default scope).

  mcp_server.handler.get_user_workspace_ids:
      Scan filtered to METADATA rows on both branches (org-admin lists all
      workspaces; regular user resolves shares against every METADATA row).

At 2000-workspace enterprise scale + hundreds of concurrent users, both
paths hit throughput ceilings inside minutes. Cheap fix: cache the results
per Fargate/Lambda instance for a short TTL (60s default). The workspace
list is enterprise-provisioning-slow; a 60s stale window is invisible.

The cache is intentionally per-instance (module-globals) and lock-guarded so
ThreadingHTTPServer request threads don't race the refresh. Instances refresh
independently — no coordination — because DDB scan cost, not consistency, is
the pressure.

All entries are keyed by `(table_name, kind, extra_key)` so a single Lambda
package that serves multiple tables (or a chat handler + MCP handler
co-located in a container) can share the same store without collision.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("outcomeops.ws_cache")

# 60s default TTL. Low enough that a new workspace shows up within a minute;
# high enough that per-instance scan cost drops ~99% at steady state
# (chat message rate >> 1/min per instance).
CACHE_TTL_SECONDS = int(os.environ.get("WORKSPACE_CACHE_TTL_SECONDS", "60"))

_lock = threading.Lock()
_entries: dict[tuple, tuple[Any, float]] = {}


def get_or_compute(
    key: tuple,
    compute: Callable[[], Any],
    ttl: Optional[int] = None,
) -> Any:
    """Return cached value for `key`; call `compute()` and cache on miss.

    Cache miss races (two threads with the same key at once) may both call
    `compute` — that's fine, both writes land the same value and the last
    writer wins. Better than holding the lock across a potentially slow
    Scan.
    """
    ttl_seconds = ttl if ttl is not None else CACHE_TTL_SECONDS
    now = time.time()

    with _lock:
        entry = _entries.get(key)
        if entry is not None:
            value, expires_at = entry
            if expires_at > now:
                return value

    value = compute()

    with _lock:
        _entries[key] = (value, now + ttl_seconds)

    return value


def invalidate(key: tuple) -> None:
    """Drop a specific cache entry. Used by write-path code that just mutated
    the underlying data (e.g., a workspace create) and knows the cached
    value is stale."""
    with _lock:
        _entries.pop(key, None)


def clear() -> None:
    """Drop the entire cache. Test helper."""
    with _lock:
        _entries.clear()
