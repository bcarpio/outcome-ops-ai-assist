"""
Shared audit writer for admin-action and auth audit rows.

Companion to ``shared/audit.py`` (Bedrock-call rows). Splits admin/auth audit
emission into a narrow helper so the Bedrock writer stays focused on
model/token/cost concerns. All rows land in the same DynamoDB ``AUDIT_TABLE``
with key shapes compatible across categories.

Schema (admin row):
    PK         = WS#<workspace_id>   | WS#SYSTEM
    SK         = <iso_timestamp>#<request_id>
    GSI1PK     = USER#<email>        | LAMBDA#<name>
    GSI1SK     = same as SK
    category   = "admin" | "auth"
    action     = SCREAMING_SNAKE_CASE constant (see ACTION_* below)
    workspace_id, user_email, source_ip, user_agent, request_id, timestamp
    details    = free-form dict (action-specific)
    ttl        = epoch_seconds + 365d

See plan/01-dynamodb-schema.md for the canonical wire format.
"""

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import boto3

logger = logging.getLogger(__name__)

# Lazy-initialized DynamoDB resource (one per container)
_dynamodb_resource = None


def _get_dynamodb_table(table_name: str):
    """Get or create DynamoDB Table resource."""
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource.Table(table_name)


# ============================================================================
# Action constants (canonical list - mirrors plan/01-dynamodb-schema.md)
# ============================================================================

ACTION_WORKSPACE_CREATED = "WORKSPACE_CREATED"
ACTION_WORKSPACE_UPDATED = "WORKSPACE_UPDATED"
ACTION_WORKSPACE_DELETED = "WORKSPACE_DELETED"
ACTION_WORKSPACE_BUDGET_UPDATED = "WORKSPACE_BUDGET_UPDATED"
ACTION_WORKSPACE_MEMBER_ADDED = "WORKSPACE_MEMBER_ADDED"
ACTION_WORKSPACE_MEMBER_REMOVED = "WORKSPACE_MEMBER_REMOVED"
ACTION_WORKSPACE_REPO_ADDED = "WORKSPACE_REPO_ADDED"
ACTION_WORKSPACE_REPO_REMOVED = "WORKSPACE_REPO_REMOVED"
ACTION_ORG_MEMBER_ADDED = "ORG_MEMBER_ADDED"
ACTION_ORG_MEMBER_REMOVED = "ORG_MEMBER_REMOVED"
ACTION_MCP_SERVER_CREATED = "MCP_SERVER_CREATED"
ACTION_MCP_SERVER_UPDATED = "MCP_SERVER_UPDATED"
ACTION_MCP_SERVER_DELETED = "MCP_SERVER_DELETED"
ACTION_API_KEY_CREATED = "API_KEY_CREATED"
ACTION_API_KEY_REVOKED = "API_KEY_REVOKED"
ACTION_SYSTEM_PROMPT_UPDATED = "SYSTEM_PROMPT_UPDATED"
ACTION_SYSTEM_PROMPT_RESET = "SYSTEM_PROMPT_RESET"
ACTION_PRIVACY_MODE_UPDATED = "PRIVACY_MODE_UPDATED"
ACTION_DOCUMENT_INGESTED = "DOCUMENT_INGESTED"

# ----------------------------------------------------------------------------
# Audit-completeness expansion (plan/audit-completeness):
# new actions added to close gaps a regulated-buyer InfoSec review will probe.
# ----------------------------------------------------------------------------

# Auth (CATEGORY_AUTH) — first time the auth category is populated.
ACTION_LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
ACTION_API_KEY_USED = "API_KEY_USED"
ACTION_MCP_JWT_VALIDATED = "MCP_JWT_VALIDATED"

# Source-system sync triggers — one row per Lambda invocation, parent
# event for the per-file DOCUMENT_INGESTED rows that follow.
ACTION_REPO_SYNCED = "REPO_SYNCED"
ACTION_CONFLUENCE_SYNCED = "CONFLUENCE_SYNCED"
ACTION_JIRA_SYNCED = "JIRA_SYNCED"
ACTION_OUTLOOK_SYNCED = "OUTLOOK_SYNCED"
ACTION_TEAMS_SYNCED = "TEAMS_SYNCED"
ACTION_SHAREPOINT_SYNCED = "SHAREPOINT_SYNCED"

# OAuth lifecycle — credential establishment + rotation evidence.
ACTION_OAUTH_CONNECTION_ESTABLISHED = "OAUTH_CONNECTION_ESTABLISHED"
ACTION_OAUTH_TOKEN_REFRESHED = "OAUTH_TOKEN_REFRESHED"

# Data-deletion events — right-to-deletion compliance.
ACTION_DOCUMENT_DELETED = "DOCUMENT_DELETED"
ACTION_CONVERSATION_DELETED = "CONVERSATION_DELETED"

# Configuration-change events.
ACTION_WORKSPACE_SHARING_UPDATED = "WORKSPACE_SHARING_UPDATED"
ACTION_MCP_SERVER_ASSIGNED = "MCP_SERVER_ASSIGNED_TO_WORKSPACE"
ACTION_MCP_SERVER_UNASSIGNED = "MCP_SERVER_UNASSIGNED_FROM_WORKSPACE"

# Code-generation top-level events — parent rows for the per-Bedrock-call
# rows already captured by the underlying check pipelines.
ACTION_PR_ANALYZED = "PR_ANALYZED"
ACTION_CODE_GENERATED = "CODE_GENERATED"
ACTION_CODE_GEN_DLQ_SALVAGE_PR_CREATED = "CODE_GEN_DLQ_SALVAGE_PR_CREATED"

# Tier 2 — nice-to-haves.
ACTION_BUDGET_THRESHOLD_CROSSED = "BUDGET_THRESHOLD_CROSSED"
ACTION_DOCUMENT_DOWNLOADED = "DOCUMENT_DOWNLOADED"
ACTION_MCP_TOOL_INVOKED = "MCP_TOOL_INVOKED"
ACTION_TESTS_RAN = "TESTS_RAN"

# Set of all known admin actions; used by the audit-query API to validate filters.
ADMIN_ACTIONS = frozenset(
    {
        ACTION_WORKSPACE_CREATED,
        ACTION_WORKSPACE_UPDATED,
        ACTION_WORKSPACE_DELETED,
        ACTION_WORKSPACE_BUDGET_UPDATED,
        ACTION_WORKSPACE_MEMBER_ADDED,
        ACTION_WORKSPACE_MEMBER_REMOVED,
        ACTION_WORKSPACE_REPO_ADDED,
        ACTION_WORKSPACE_REPO_REMOVED,
        ACTION_ORG_MEMBER_ADDED,
        ACTION_ORG_MEMBER_REMOVED,
        ACTION_MCP_SERVER_CREATED,
        ACTION_MCP_SERVER_UPDATED,
        ACTION_MCP_SERVER_DELETED,
        ACTION_API_KEY_CREATED,
        ACTION_API_KEY_REVOKED,
        ACTION_SYSTEM_PROMPT_UPDATED,
        ACTION_SYSTEM_PROMPT_RESET,
        ACTION_PRIVACY_MODE_UPDATED,
        ACTION_DOCUMENT_INGESTED,
        # Audit-completeness expansion:
        ACTION_LOGIN_SUCCEEDED,
        ACTION_API_KEY_USED,
        ACTION_MCP_JWT_VALIDATED,
        ACTION_REPO_SYNCED,
        ACTION_CONFLUENCE_SYNCED,
        ACTION_JIRA_SYNCED,
        ACTION_OUTLOOK_SYNCED,
        ACTION_TEAMS_SYNCED,
        ACTION_SHAREPOINT_SYNCED,
        ACTION_OAUTH_CONNECTION_ESTABLISHED,
        ACTION_OAUTH_TOKEN_REFRESHED,
        ACTION_DOCUMENT_DELETED,
        ACTION_CONVERSATION_DELETED,
        ACTION_WORKSPACE_SHARING_UPDATED,
        ACTION_MCP_SERVER_ASSIGNED,
        ACTION_MCP_SERVER_UNASSIGNED,
        ACTION_PR_ANALYZED,
        ACTION_CODE_GENERATED,
        ACTION_CODE_GEN_DLQ_SALVAGE_PR_CREATED,
        ACTION_BUDGET_THRESHOLD_CROSSED,
        ACTION_DOCUMENT_DOWNLOADED,
        ACTION_MCP_TOOL_INVOKED,
        ACTION_TESTS_RAN,
    }
)

CATEGORY_BEDROCK = "bedrock"
CATEGORY_ADMIN = "admin"
CATEGORY_AUTH = "auth"


# Map source names to the corresponding sync-trigger action constant.
# Used by log_source_sync() so each *-sync Lambda doesn't have to know
# the constant name itself - just pass its source.
_SOURCE_SYNC_ACTIONS = {
    "github":     "REPO_SYNCED",
    "confluence": "CONFLUENCE_SYNCED",
    "jira":       "JIRA_SYNCED",
    "outlook":    "OUTLOOK_SYNCED",
    "teams":      "TEAMS_SYNCED",
    "sharepoint": "SHAREPOINT_SYNCED",
}


# Admin actions that are written to the audit table for forensic /
# per-document provenance, but are hidden from the audit dashboard UI
# and excluded from analytics rollups so they don't dominate the
# "most common action" / "total actions" cards. DOCUMENT_INGESTED
# fires once per ingested document - in a workspace with even a
# moderate sync cadence, it would drown out every other admin action.
# Operators who need the underlying rows can query the audit table
# directly via DynamoDB.
HIDDEN_DASHBOARD_ACTIONS = frozenset({
    ACTION_DOCUMENT_INGESTED,
})


# ============================================================================
# Request-context extraction
# ============================================================================


def extract_request_context(event: Dict) -> Dict[str, str]:
    """
    Pull source_ip and user_agent from API Gateway / Function URL events.

    Handles three event shapes:
      1. API Gateway v2 (HTTP API):  requestContext.http.sourceIp / userAgent
      2. API Gateway v1 (REST):      requestContext.identity.sourceIp / userAgent
      3. Lambda Function URL fall-through: top-level headers map.

    Returns a dict with keys ``source_ip`` and ``user_agent``. Empty strings
    on lookup miss. Never raises; audit attribution must not break a request.
    """
    if not isinstance(event, dict):
        return {"source_ip": "", "user_agent": ""}

    rc = event.get("requestContext") or {}
    if not isinstance(rc, dict):
        rc = {}

    http = rc.get("http") if isinstance(rc.get("http"), dict) else {}
    identity = rc.get("identity") if isinstance(rc.get("identity"), dict) else {}

    source_ip = http.get("sourceIp") or identity.get("sourceIp") or ""

    user_agent = http.get("userAgent") or identity.get("userAgent") or ""
    if not user_agent:
        headers = event.get("headers") or {}
        if isinstance(headers, dict):
            user_agent = headers.get("user-agent") or headers.get("User-Agent") or ""

    return {"source_ip": source_ip or "", "user_agent": user_agent or ""}


# ============================================================================
# Admin audit row writer
# ============================================================================


def log_admin_action(
    audit_table_name: str,
    action: str,
    workspace_id: str,
    user_email: str,
    source_ip: str = "",
    user_agent: str = "",
    details: Optional[Dict] = None,
    category: str = CATEGORY_ADMIN,
) -> None:
    """
    Write a single admin or auth audit row.

    Non-fatal: catches all exceptions, logs a warning, never raises. An
    audit-write failure must not break the user-facing request.

    Args:
        audit_table_name: DynamoDB table name (typically ``AUDIT_TABLE`` env).
        action: One of ``ACTION_*`` constants above.
        workspace_id: Workspace context. Empty string is treated as a
            system-level event (PK becomes ``WS#SYSTEM``).
        user_email: Acting user's email. Empty falls back to
            ``LAMBDA#workspace-management`` so GSI1 still has a partition.
        source_ip: Captured by ``extract_request_context``; pass empty
            string for non-HTTP call paths.
        user_agent: Same.
        details: Action-specific dict; intentionally untyped.
        category: Default ``"admin"``. Pass ``"auth"`` for SSO-style events.
    """
    if not audit_table_name:
        return

    try:
        request_id = secrets.token_hex(8)
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()

        ws_key = f"WS#{workspace_id}" if workspace_id else "WS#SYSTEM"
        user_key = (
            f"USER#{user_email}" if user_email else "LAMBDA#workspace-management"
        )
        sort_key = f"{timestamp}#{request_id}"

        ttl = int(time.time()) + (365 * 24 * 60 * 60)

        item = {
            "PK": ws_key,
            "SK": sort_key,
            "GSI1PK": user_key,
            "GSI1SK": sort_key,
            # GSI2: category-partitioned index that lets the audit dashboard
            # answer "all admin actions, newest first, across workspaces"
            # via a single Query (vs. Scan + FilterExpression). See
            # plan/01-dynamodb-schema.md and terraform/dynamodb-audit.tf.
            "GSI2PK": f"CAT#{category}",
            "GSI2SK": sort_key,
            "category": category,
            "action": action,
            "workspace_id": workspace_id or "",
            "user_email": user_email or "",
            "source_ip": source_ip or "",
            "user_agent": user_agent or "",
            "request_id": request_id,
            "timestamp": timestamp,
            "ttl": ttl,
        }

        if details:
            item["details"] = details

        table = _get_dynamodb_table(audit_table_name)
        table.put_item(Item=item)

        logger.debug(
            f"[audit_writer] Logged {category}/{action} ws={workspace_id} user={user_email}"
        )

    except Exception as e:
        logger.warning(
            f"[audit_writer] Failed to log admin action {action}: {e}"
        )


def log_source_sync(
    audit_table_name: str,
    source: str,
    workspace_id: str,
    target: str,
    trigger: str,
    files_synced: int = 0,
    skipped_no_change: bool = False,
    duration_ms: int = 0,
    success: bool = True,
    error: str = "",
    extra_details: Optional[Dict] = None,
) -> None:
    """Convenience wrapper that emits a per-source SYNC audit row.

    Each *-sync Lambda calls this at the end of a sync run (success or
    failure). The corresponding ACTION_* constant is looked up from the
    source name, so the Lambdas don't have to import 6 constants - they
    just pass ``source="github"`` etc.

    Non-fatal: catches all exceptions, logs a warning, never raises.

    Args:
        audit_table_name: DynamoDB table name (typically ``AUDIT_TABLE``).
        source: One of ``"github"``, ``"confluence"``, ``"jira"``,
            ``"outlook"``, ``"teams"``, ``"sharepoint"``.
        workspace_id: Workspace this sync was scoped to.
        target: Source-specific target identifier (repo name, space
            key, project key, mailbox, channel, site, etc.).
        trigger: ``"scheduled"`` (cron), ``"webhook"``, or ``"manual"``.
        files_synced: Count of items pulled.
        skipped_no_change: True when a no-op sync (e.g., commit SHA
            unchanged for github).
        duration_ms: Wall-clock time for the sync run.
        success: True for successful runs; False writes the same row
            with ``"success": False`` plus an ``"error"`` field.
        error: Error message when ``success=False``.
        extra_details: Optional source-specific extra fields merged
            into ``details`` (e.g., commit_sha for github).
    """
    action = _SOURCE_SYNC_ACTIONS.get(source)
    if not action:
        logger.warning(f"[audit_writer] Unknown source for sync audit: {source}")
        return

    details: Dict = {
        "source": source,
        "target": target,
        "trigger": trigger,
        "files_synced": files_synced,
        "skipped_no_change": skipped_no_change,
        "duration_ms": duration_ms,
        "success": success,
    }
    if error:
        details["error"] = error
    if extra_details:
        details.update(extra_details)

    log_admin_action(
        audit_table_name,
        action,
        workspace_id=workspace_id,
        user_email="",  # Sync Lambdas are SQS / cron driven, no human caller.
        source_ip="",
        user_agent="",
        details=details,
    )
