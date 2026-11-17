# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Backend guard: reject mutating admin operations when the local
region is passive.

Used by workspace-management (chunk 9d) and the 9 integration handlers
(chunk 9c) to refuse admin / state-changing operations from the
secondary region while it's passive. Reads from shared.active_region.

The guard records an audit row with action
``ADMIN_MUTATION_BLOCKED_PASSIVE`` for every blocked attempt so
operators can see what users tried to do during a failover window.
Reads are NEVER gated through this module — they always serve from
the local region per ADR-022.

Pattern at the call site::

    from shared.region_guard import (
        RegionPassiveError,
        guard_active,
        passive_response,
    )

    def handle_create_workspace(event, context):
        try:
            guard_active("create_workspace", user_email=user_email)
        except RegionPassiveError:
            return passive_response("create_workspace")
        # ... existing handler body
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from shared.active_region import is_active_region

logger = logging.getLogger(__name__)


class RegionPassiveError(Exception):
    """Raised by ``guard_active`` when the local region is passive."""

    def __init__(self, operation: str = ""):
        super().__init__(operation)
        self.operation = operation


def guard_active(
    operation: str = "",
    *,
    user_email: str = "",
    workspace_id: str = "",
    audit_table_name: Optional[str] = None,
) -> None:
    """Raise ``RegionPassiveError`` if the local region is passive.

    On a block, also emits an audit row with action
    ``ADMIN_MUTATION_BLOCKED_PASSIVE``. The audit emit is fire-and-
    forget — its failure must not prevent the 403 from reaching the
    user.

    ``audit_table_name`` defaults to the ``AUDIT_TABLE`` env var.
    ``user_email`` and ``workspace_id`` are best-effort attribution
    for the audit row.
    """

    if is_active_region():
        return

    # Region is passive — write the audit row before raising. Import
    # here to avoid a circular import with shared.audit_writer (which
    # uses shared.dual_write, which has no dependency back here).
    try:
        from shared.audit_writer import (  # type: ignore
            ACTION_ADMIN_MUTATION_BLOCKED_PASSIVE,
            CATEGORY_ADMIN,
            log_admin_action,
        )

        table_name = audit_table_name or os.environ.get("AUDIT_TABLE", "")
        if table_name:
            log_admin_action(
                audit_table_name=table_name,
                action=ACTION_ADMIN_MUTATION_BLOCKED_PASSIVE,
                workspace_id=workspace_id,
                user_email=user_email,
                details={"operation": operation},
                category=CATEGORY_ADMIN,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[region_guard] audit emit failed for blocked operation %s: %s",
            operation,
            exc,
        )

    logger.info(
        "[region_guard] blocked mutation %s in passive region (user=%s ws=%s)",
        operation,
        user_email or "-",
        workspace_id or "-",
    )
    raise RegionPassiveError(operation)


def passive_response(operation: str = "") -> dict:
    """Standard HTTP 403 body for the rejected-because-passive case.

    Shape::

        {
          "statusCode": 403,
          "headers": {"Content-Type": "application/json"},
          "body": "{\\"error\\": \\"region_passive\\", ...}"
        }

    Front-end UIs catch the ``"error": "region_passive"`` value to
    surface a region-specific toast or to refresh their cached role
    state.
    """

    return {
        "statusCode": 403,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "error": "region_passive",
                "message": (
                    "This region is currently passive. Admin operations "
                    "must be performed on the active region."
                ),
                "operation": operation or "",
            }
        ),
    }
