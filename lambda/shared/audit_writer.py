# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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

import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from shared import dual_write

# — GSI2 partition sharding.
from shared import audit_shard as _audit_shard

logger = logging.getLogger(__name__)


# ============================================================================
# Action constants (canonical list - mirrors plan/01-dynamodb-schema.md)
# ============================================================================

ACTION_WORKSPACE_CREATED = "WORKSPACE_CREATED"
ACTION_WORKSPACE_UPDATED = "WORKSPACE_UPDATED"
ACTION_WORKSPACE_DELETED = "WORKSPACE_DELETED"
ACTION_WORKSPACE_BUDGET_UPDATED = "WORKSPACE_BUDGET_UPDATED"
# Bidirectional-scope flag change. Emitted separately from the plain
# WORKSPACE_UPDATED action because the change materially affects who
# can read what across the deployment; compliance auditors want to
# see these flips in a distinct feed rather than buried in a
# generic-metadata-change row.
ACTION_WORKSPACE_GLOBAL_FLAG_SET = "WORKSPACE_GLOBAL_FLAG_SET"
ACTION_WORKSPACE_MEMBER_ADDED = "WORKSPACE_MEMBER_ADDED"
ACTION_WORKSPACE_MEMBER_REMOVED = "WORKSPACE_MEMBER_REMOVED"
ACTION_WORKSPACE_REPO_ADDED = "WORKSPACE_REPO_ADDED"
ACTION_WORKSPACE_REPO_REMOVED = "WORKSPACE_REPO_REMOVED"
ACTION_GROUP_BINDING_ADDED = "GROUP_BINDING_ADDED"
ACTION_GROUP_BINDING_REMOVED = "GROUP_BINDING_REMOVED"
ACTION_GROUP_MEMBERSHIPS_SYNCED = "GROUP_MEMBERSHIPS_SYNCED"
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

# HA passive-mode block. Steady-state count is zero in
# active region; non-zero in passive region during a failover drill
# or post-failover until the operator flips. Operators WANT these on
# the audit dashboard — NOT in HIDDEN_DASHBOARD_ACTIONS.
ACTION_ADMIN_MUTATION_BLOCKED_PASSIVE = "ADMIN_MUTATION_BLOCKED_PASSIVE"

# Source-system sync triggers — one row per Lambda invocation, parent
# event for the per-file DOCUMENT_INGESTED rows that follow.
ACTION_REPO_SYNCED = "REPO_SYNCED"
ACTION_CONFLUENCE_SYNCED = "CONFLUENCE_SYNCED"
ACTION_JIRA_SYNCED = "JIRA_SYNCED"
ACTION_OUTLOOK_SYNCED = "OUTLOOK_SYNCED"
ACTION_TEAMS_SYNCED = "TEAMS_SYNCED"
ACTION_SHAREPOINT_SYNCED = "SHAREPOINT_SYNCED"
ACTION_ONEDRIVE_SYNCED = "ONEDRIVE_SYNCED"
ACTION_ONENOTE_SYNCED = "ONENOTE_SYNCED"
ACTION_DATABASE_SYNCED = "DATABASE_SYNCED"

# OAuth lifecycle — credential establishment + rotation evidence.
ACTION_OAUTH_CONNECTION_ESTABLISHED = "OAUTH_CONNECTION_ESTABLISHED"
ACTION_OAUTH_TOKEN_REFRESHED = "OAUTH_TOKEN_REFRESHED"

# Database connection lifecycle — non-OAuth credential storage.
# DB_CREDENTIAL_USED fires on every sync read of the stored credential;
# keeps secret-access events in the unified audit table instead of
# splitting between audit + CloudTrail.
ACTION_DB_CONNECTION_ESTABLISHED = "DB_CONNECTION_ESTABLISHED"
ACTION_DB_CREDENTIAL_USED = "DB_CREDENTIAL_USED"
ACTION_DB_CONNECTION_DELETED = "DB_CONNECTION_DELETED"

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

# Every chat turn fires a routing decision (intent + retrievals fired
# + graph staleness). Cost of the Haiku classification itself still
# lands via ``log_audit_entry(lambda_name="chat:router")`` in
# ``shared.audit``; this constant tags the separate "what surfaces did
# we hit" event that ``log_routing_decision`` writes.
ACTION_CHAT_ROUTING_DECISION = "CHAT_ROUTING_DECISION"

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
        ACTION_ADMIN_MUTATION_BLOCKED_PASSIVE,
        ACTION_REPO_SYNCED,
        ACTION_CONFLUENCE_SYNCED,
        ACTION_JIRA_SYNCED,
        ACTION_OUTLOOK_SYNCED,
        ACTION_TEAMS_SYNCED,
        ACTION_SHAREPOINT_SYNCED,
        ACTION_ONEDRIVE_SYNCED,
        ACTION_ONENOTE_SYNCED,
        ACTION_DATABASE_SYNCED,
        ACTION_OAUTH_CONNECTION_ESTABLISHED,
        ACTION_OAUTH_TOKEN_REFRESHED,
        ACTION_DB_CONNECTION_ESTABLISHED,
        ACTION_DB_CREDENTIAL_USED,
        ACTION_DB_CONNECTION_DELETED,
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
        ACTION_CHAT_ROUTING_DECISION,
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
    "onedrive":   "ONEDRIVE_SYNCED",
    "onenote":    "ONENOTE_SYNCED",
    "database":   "DATABASE_SYNCED",
}


# Admin actions that are written to the audit table for forensic /
# per-document provenance, but are hidden from the audit dashboard UI
# and excluded from analytics rollups so they don't dominate the
# "most common action" / "total actions" cards. These fire on routine
# automation and would drown out every other admin action - operators
# who need them can filter by ?action=... explicitly to bypass the hide
# rule, or query DynamoDB directly. Order: high-frequency first.
HIDDEN_DASHBOARD_ACTIONS = frozenset({
    # Per-document ingest - one row per file synced.
    ACTION_DOCUMENT_INGESTED,
    # Per-API-call - hundreds per MCP session.
    ACTION_API_KEY_USED,
    ACTION_MCP_TOOL_INVOKED,
    # Per-active-user-per-day - moderate but adds up.
    ACTION_LOGIN_SUCCEEDED,
    # Hourly cron drumbeat × N connections per source.
    ACTION_REPO_SYNCED,
    ACTION_CONFLUENCE_SYNCED,
    ACTION_JIRA_SYNCED,
    ACTION_OUTLOOK_SYNCED,
    ACTION_TEAMS_SYNCED,
    ACTION_SHAREPOINT_SYNCED,
    ACTION_ONEDRIVE_SYNCED,
    ACTION_ONENOTE_SYNCED,
    ACTION_DATABASE_SYNCED,
    # Per-credential-decrypt during database sync — one row per active
    # connection per scheduler tick. Forensically essential, dashboard
    # noise. Operators filter ?action=DB_CREDENTIAL_USED explicitly.
    ACTION_DB_CREDENTIAL_USED,
    # Operator preference: keep these queryable but off the dashboard
    # cards. They write the audit row for forensic / right-to-deletion
    # / per-PR provenance, but the volume + nature isn't dashboard-
    # interesting at a glance. Operators filter ?action=... explicitly
    # to drill in.
    ACTION_DOCUMENT_DELETED,
    ACTION_CONVERSATION_DELETED,
    ACTION_PR_ANALYZED,
    ACTION_TESTS_RAN,
    # Per-chat-turn — would dominate the "actions" cards and drown
    # out everything operators actually want to skim. Forensically
    # essential (routing decision is part of the audit story), but
    # filtered out of the dashboard by default; operators query with
    # ?action=CHAT_ROUTING_DECISION when investigating.
    ACTION_CHAT_ROUTING_DECISION,
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
            # GSI2: category-partitioned index. shards
            # this key by a hash of request_id so a hot category (e.g.
            # CAT#admin during org-scale onboarding) doesn't hammer a single
            # DDB partition. Reader must fan out across shards + the legacy
            # unsharded key. See shared/audit_shard.py.
            "GSI2PK": _audit_shard.gsi2pk_for_category(category, request_id),
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

        # ConditionExpression="attribute_not_exists(SK)" makes the write
        # safely idempotent under EventBridge replay: the passive region's
        # applier replays the same put-item, the SK collision triggers
        # CCF, and the applier swallows it as "already applied." SK
        # carries an 8-byte random request_id so legitimate distinct
        # rows never collide.
        try:
            dual_write.ddb_put_item(
                audit_table_name,
                item,
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as ce:
            if ce.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Replay collision — the row already exists. No-op.
                return
            raise

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
            ``"outlook"``, ``"teams"``, ``"sharepoint"``, ``"onedrive"``,
            ``"onenote"``, ``"database"``.
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


# ============================================================================
# Chat routing-decision audit
# ============================================================================


def _hash_question(question_text: str) -> str:
    """SHA256 hash of a chat-turn question text.

    The router invocation itself is logged via
    ``shared.audit.log_audit_entry()`` with ``flagged_reason`` already
    carrying ``q_sha256=<hex>``. ``log_routing_decision`` uses this same
    helper so the routing-decision audit row and the Bedrock cost row
    correlate on a stable hash without ever storing the question text
    in either row. Empty input is hashed deterministically too so the
    audit row is well-formed even on the empty / whitespace edge case.
    """

    raw = question_text or ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def log_routing_decision(
    audit_table_name: str,
    workspace_id: str,
    user_email: str,
    question_hash: str,
    intent: str,
    retrievals_fired: List[str],
    graph_sha: Optional[str] = None,
    graph_staleness_seconds: Optional[int] = None,
    graph_result_count: Optional[int] = None,
    graph_skip_reason: Optional[str] = None,
    rag_chunk_count: Optional[int] = None,
    manifest_hit_count: Optional[int] = None,
    source_ip: str = "",
    user_agent: str = "",
) -> None:
    """Emit one ``CHAT_ROUTING_DECISION`` audit row per chat turn.

    The **Haiku cost** of the classifier call is logged separately via
    ``log_audit_entry(lambda_name="chat:router")`` in
    ``shared.audit``. This function logs the **routing decision itself**
    — which retrieval surfaces fired, the graph anchor SHA + staleness,
    and the result counts each surface returned. Two distinct events,
    one auditable record per turn.

    Question text is hashed (SHA256), never stored in plaintext, so PII
    in user questions does not land in the audit stream. The hash lets
    investigators correlate routing decisions for identical questions
    without storing the question itself.

    Mapped to OCSF ``Application Lifecycle (6002)`` with activity_id 99
    ("Other" — class-specific retrieval routing extension; see
    ``shared.ocsf_mapper`` for the type_uid). The OCSF envelope is
    composed at audit-stream-export time; ``map_audit_row_to_ocsf``
    picks the class/activity from the action constant.

    Non-fatal: catches all exceptions, logs a warning, never raises.
    A routing-decision-audit failure must not break a chat turn.

    Args:
        audit_table_name: DynamoDB table name (typically ``AUDIT_TABLE``).
        workspace_id: Workspace this chat turn ran against. Empty
            string is system-level (rare for chat).
        user_email: Acting user's email; empty if unauthenticated.
        question_hash: SHA256 hex of the question text. Callers pass
            ``_hash_question(question)`` to get the canonical form.
        intent: One of ``pattern | dependency | structural | hybrid |
            unknown`` (the router's ``VALID_INTENTS``).
        retrievals_fired: Ordered list of retrieval surfaces that ran
            for this turn, e.g. ``["rag", "graph"]`` or
            ``["rag", "manifest", "graph"]``.
        graph_sha: Symbol-graph anchor SHA, or ``None`` when the graph
            didn't fire.
        graph_staleness_seconds: Seconds since ``graph_sha`` was
            committed; ``None`` when the graph didn't fire.
        graph_result_count: Number of sites returned by the graph
            query (zero is a finding — empty-set discipline).
        rag_chunk_count: Number of RAG chunks returned, or ``None``
            when RAG didn't fire.
        manifest_hit_count: Number of manifest matches, or ``None``
            when the manifest layer didn't fire.
        source_ip: HTTP source IP if available.
        user_agent: HTTP user agent if available.
    """
    details: Dict = {
        "question_hash": question_hash or "",
        "intent": intent or "unknown",
        "retrievals_fired": list(retrievals_fired or []),
    }
    if graph_sha is not None:
        details["graph_sha"] = graph_sha
    if graph_staleness_seconds is not None:
        details["graph_staleness_seconds"] = int(graph_staleness_seconds)
    if graph_result_count is not None:
        details["graph_result_count"] = int(graph_result_count)
    if graph_skip_reason is not None:
        details["graph_skip_reason"] = graph_skip_reason
    if rag_chunk_count is not None:
        details["rag_chunk_count"] = int(rag_chunk_count)
    if manifest_hit_count is not None:
        details["manifest_hit_count"] = int(manifest_hit_count)

    log_admin_action(
        audit_table_name,
        ACTION_CHAT_ROUTING_DECISION,
        workspace_id=workspace_id,
        user_email=user_email,
        source_ip=source_ip,
        user_agent=user_agent,
        details=details,
        category=CATEGORY_ADMIN,
    )
