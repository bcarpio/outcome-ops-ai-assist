"""
Hybrid OCSF mapper for audit-stream-export.

Transforms an OutcomeOps audit row (DynamoDB NEW_IMAGE shape) into an OCSF
v1.3.0 envelope. The envelope carries the standard OCSF fields a SIEM /
Security Lake / Splunk OCSF app expects (time, metadata, category_uid,
class_uid, activity_id, severity_id, actor, src_endpoint, status). The full
OutcomeOps payload is preserved under ``unmapped`` so nothing is lost.

V1 mapping coverage:

  * bedrock category    -> 6003 API Activity (status_id reflects refusal)
  * auth category       -> 3002 Authentication
  * admin (workspace)   -> 3001 Account Change
  * admin (membership)  -> 3001 Account Change
  * admin (code-gen)    -> 6005 Application Activity
  * admin (OAuth)       -> 3002 Authentication
  * admin (budget)      -> 6005 Application Activity (severity Medium)
  * admin (everything else) -> 6005 Application Activity (default)

Specific OCSF class mappings for the long tail of admin actions can be added
later without breaking the wire format - consumers just see a more specific
class_uid for events that previously fell through to the default.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

OCSF_VERSION = "1.3.0"
PRODUCT_NAME = "OutcomeOps AI Assist"
VENDOR_NAME = "OutcomeOps"

# OCSF class UIDs (see https://schema.ocsf.io/)
CLASS_ACCOUNT_CHANGE = 3001
CLASS_AUTHENTICATION = 3002
# Chat routing-decision audit lands on OCSF "Application Lifecycle"
# so a SIEM consumer can distinguish it from generic 6005 "Application
# Activity" rows.
CLASS_APPLICATION_LIFECYCLE = 6002
CLASS_API_ACTIVITY = 6003
CLASS_APPLICATION_ACTIVITY = 6005

CLASS_NAMES = {
    CLASS_ACCOUNT_CHANGE: "Account Change",
    CLASS_AUTHENTICATION: "Authentication",
    CLASS_APPLICATION_LIFECYCLE: "Application Lifecycle",
    CLASS_API_ACTIVITY: "API Activity",
    CLASS_APPLICATION_ACTIVITY: "Application Activity",
}

# OCSF category UIDs
CATEGORY_IAM = 3
CATEGORY_APPLICATION = 6

CATEGORY_NAMES = {
    CATEGORY_IAM: "Identity & Access Management",
    CATEGORY_APPLICATION: "Application Activity",
}

# Severity per OCSF (1=Informational, 2=Low, 3=Medium, 4=High, 5=Critical)
SEV_INFO = 1
SEV_LOW = 2
SEV_MEDIUM = 3
SEV_HIGH = 4

SEV_NAMES = {
    SEV_INFO: "Informational",
    SEV_LOW: "Low",
    SEV_MEDIUM: "Medium",
    SEV_HIGH: "High",
}

# Activity IDs differ per class. For 3001 (Account Change) and 6003/6005:
#   1 = Create, 2 = Read, 3 = Update, 4 = Delete, 5 = Other
# For 3002 (Authentication):
#   1 = Logon, 2 = Logoff, 3 = Authentication Ticket, 4 = Service Ticket
ACT_CREATE = 1
ACT_READ = 2
ACT_UPDATE = 3
ACT_DELETE = 4
ACT_OTHER = 5

ACT_LOGON = 1

# OCSF class 6002 "Application Lifecycle" reserves 1-8 for
# install/remove/start/stop/restart/enable/disable/update. We use 99
# ("Other") for the retrieval-routing extension - well outside the
# reserved range, distinct from every other activity_id currently in
# use by OutcomeOps (ACT_CREATE/READ/UPDATE/DELETE/OTHER + ACT_LOGON
# are all <= 5).
ACT_ROUTING_DECISION = 99

# Mapping: admin action -> (class_uid, activity_id, severity_id)
ADMIN_ACTION_MAP: Dict[str, tuple] = {
    # Workspace lifecycle (3001 Account Change)
    "WORKSPACE_CREATED": (CLASS_ACCOUNT_CHANGE, ACT_CREATE, SEV_INFO),
    "WORKSPACE_UPDATED": (CLASS_ACCOUNT_CHANGE, ACT_UPDATE, SEV_INFO),
    "WORKSPACE_DELETED": (CLASS_ACCOUNT_CHANGE, ACT_DELETE, SEV_MEDIUM),
    "WORKSPACE_BUDGET_UPDATED": (CLASS_ACCOUNT_CHANGE, ACT_UPDATE, SEV_INFO),
    "WORKSPACE_SHARING_UPDATED": (CLASS_ACCOUNT_CHANGE, ACT_UPDATE, SEV_LOW),
    # Membership (3001 Account Change)
    "WORKSPACE_MEMBER_ADDED": (CLASS_ACCOUNT_CHANGE, ACT_CREATE, SEV_INFO),
    "WORKSPACE_MEMBER_REMOVED": (CLASS_ACCOUNT_CHANGE, ACT_DELETE, SEV_INFO),
    "ORG_MEMBER_ADDED": (CLASS_ACCOUNT_CHANGE, ACT_CREATE, SEV_LOW),
    "ORG_MEMBER_REMOVED": (CLASS_ACCOUNT_CHANGE, ACT_DELETE, SEV_LOW),
    # Code-gen (6005 Application Activity)
    "CODE_GENERATED": (CLASS_APPLICATION_ACTIVITY, ACT_CREATE, SEV_INFO),
    "PR_ANALYZED": (CLASS_APPLICATION_ACTIVITY, ACT_OTHER, SEV_INFO),
    "CODE_GEN_DLQ_SALVAGE_PR_CREATED": (
        CLASS_APPLICATION_ACTIVITY,
        ACT_CREATE,
        SEV_MEDIUM,
    ),
    "TESTS_RAN": (CLASS_APPLICATION_ACTIVITY, ACT_OTHER, SEV_INFO),
    # OAuth (3002 Authentication)
    "OAUTH_CONNECTION_ESTABLISHED": (CLASS_AUTHENTICATION, ACT_LOGON, SEV_INFO),
    # Budget (6005 elevated severity - operators want to see these)
    "BUDGET_THRESHOLD_CROSSED": (
        CLASS_APPLICATION_ACTIVITY,
        ACT_OTHER,
        SEV_MEDIUM,
    ),
    # Chat routing decision (which retrievals fired, graph staleness,
    # result counts). Lands on OCSF Application Lifecycle 6002 with the
    # routing-decision activity_id so SIEM consumers can filter on
    # type_uid=600299 without conflating it with generic 6005
    # Application Activity events.
    "CHAT_ROUTING_DECISION": (
        CLASS_APPLICATION_LIFECYCLE,
        ACT_ROUTING_DECISION,
        SEV_INFO,
    ),
}

# Auth-category mapping
AUTH_ACTION_MAP: Dict[str, tuple] = {
    "LOGIN_SUCCEEDED": (CLASS_AUTHENTICATION, ACT_LOGON, SEV_INFO),
    "API_KEY_USED": (CLASS_AUTHENTICATION, ACT_LOGON, SEV_INFO),
    "MCP_JWT_VALIDATED": (CLASS_AUTHENTICATION, ACT_LOGON, SEV_INFO),
}

# Default for unmapped admin actions: 6005 Application Activity, "Other" verb,
# Informational. Specific classes can be promoted from the default later.
DEFAULT_ADMIN: tuple = (CLASS_APPLICATION_ACTIVITY, ACT_OTHER, SEV_INFO)

# OCSF status_id: 0=Unknown, 1=Success, 2=Failure, 99=Other
STATUS_UNKNOWN = 0
STATUS_SUCCESS = 1
STATUS_FAILURE = 2

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")


def _category_uid_for_class(class_uid: int) -> int:
    if class_uid in (CLASS_ACCOUNT_CHANGE, CLASS_AUTHENTICATION):
        return CATEGORY_IAM
    return CATEGORY_APPLICATION


def _iso_to_epoch_ms(iso_ts: str) -> Optional[int]:
    """Best-effort ISO-8601 -> unix epoch ms. Returns None on parse failure
    so the caller can fall back to ``time.time()`` rather than crash a batch."""
    if not iso_ts:
        return None
    m = _ISO_RE.match(iso_ts)
    if not m:
        return None
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _ddb_str(image: Dict[str, Any], key: str, default: str = "") -> str:
    return image.get(key, {}).get("S", default)


def _ddb_num(image: Dict[str, Any], key: str) -> Optional[float]:
    raw = image.get(key, {}).get("N")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _flatten_unmapped(image: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort flatten of a DynamoDB Streams NEW_IMAGE into a plain
    dict for the OCSF ``unmapped`` field. Strings/numbers/bools/null become
    native types; nested maps/lists recurse; binary fields are skipped."""
    out: Dict[str, Any] = {}
    for k, v in image.items():
        if not isinstance(v, dict) or len(v) != 1:
            continue
        type_tag, raw = next(iter(v.items()))
        if type_tag == "S":
            out[k] = raw
        elif type_tag == "N":
            try:
                if "." in raw:
                    out[k] = float(raw)
                else:
                    out[k] = int(raw)
            except (TypeError, ValueError):
                out[k] = raw
        elif type_tag == "BOOL":
            out[k] = bool(raw)
        elif type_tag == "NULL":
            out[k] = None
        elif type_tag == "L":
            out[k] = [_unwrap_ddb_value(item) for item in raw]
        elif type_tag == "M":
            out[k] = _flatten_unmapped(raw)
        # Skip B (binary), SS/NS/BS sets - rare in our schema.
    return out


def _unwrap_ddb_value(v: Any) -> Any:
    if not isinstance(v, dict) or len(v) != 1:
        return v
    type_tag, raw = next(iter(v.items()))
    if type_tag == "S":
        return raw
    if type_tag == "N":
        try:
            return float(raw) if "." in raw else int(raw)
        except (TypeError, ValueError):
            return raw
    if type_tag == "BOOL":
        return bool(raw)
    if type_tag == "NULL":
        return None
    if type_tag == "L":
        return [_unwrap_ddb_value(i) for i in raw]
    if type_tag == "M":
        return _flatten_unmapped(raw)
    return raw


def map_audit_row_to_ocsf(image: Dict[str, Any]) -> Dict[str, Any]:
    """Transform a DynamoDB Streams ``NEW_IMAGE`` audit row into an OCSF
    envelope. Returns the OCSF event as a plain dict ready to JSON-encode."""
    category = _ddb_str(image, "category", "admin")
    action = _ddb_str(image, "action")
    timestamp = _ddb_str(image, "timestamp")
    user_email = _ddb_str(image, "user_email")
    source_ip = _ddb_str(image, "source_ip")
    user_agent = _ddb_str(image, "user_agent")
    request_id = _ddb_str(image, "request_id")
    workspace_id = _ddb_str(image, "workspace_id")
    lambda_name = _ddb_str(image, "lambda_name")
    status = _ddb_str(image, "status")
    flagged_reason = _ddb_str(image, "flagged_reason")

    # Decide class / activity / severity by category + action.
    if category == "bedrock":
        class_uid = CLASS_API_ACTIVITY
        activity_id = ACT_READ
        if status == "refusal":
            severity_id = SEV_MEDIUM
            status_id = STATUS_FAILURE
        elif status == "error":
            severity_id = SEV_LOW
            status_id = STATUS_FAILURE
        else:
            severity_id = SEV_INFO
            status_id = STATUS_SUCCESS
    elif category == "auth":
        class_uid, activity_id, severity_id = AUTH_ACTION_MAP.get(
            action, (CLASS_AUTHENTICATION, ACT_LOGON, SEV_INFO)
        )
        status_id = STATUS_SUCCESS
    else:  # admin (default)
        class_uid, activity_id, severity_id = ADMIN_ACTION_MAP.get(
            action, DEFAULT_ADMIN
        )
        status_id = STATUS_SUCCESS

    epoch_ms = _iso_to_epoch_ms(timestamp)

    envelope: Dict[str, Any] = {
        "metadata": {
            "version": OCSF_VERSION,
            "product": {
                "name": PRODUCT_NAME,
                "vendor_name": VENDOR_NAME,
                "feature": {"name": lambda_name} if lambda_name else None,
            },
            "uid": request_id,
            "log_name": category,
        },
        "category_uid": _category_uid_for_class(class_uid),
        "category_name": CATEGORY_NAMES.get(_category_uid_for_class(class_uid)),
        "class_uid": class_uid,
        "class_name": CLASS_NAMES.get(class_uid),
        "activity_id": activity_id,
        "type_uid": class_uid * 100 + activity_id,
        "severity_id": severity_id,
        "severity": SEV_NAMES.get(severity_id),
        "status_id": status_id,
        "actor": {
            "user": {
                "email_addr": user_email or None,
                "name": user_email or None,
            }
        },
        "src_endpoint": {
            "ip": source_ip or None,
            "user_agent": user_agent or None,
        },
        "unmapped": _flatten_unmapped(image),
    }

    if epoch_ms is not None:
        envelope["time"] = epoch_ms

    if flagged_reason:
        envelope["status_detail"] = flagged_reason

    if workspace_id:
        envelope.setdefault("resources", []).append(
            {"type": "workspace", "uid": workspace_id}
        )

    if action and category != "bedrock":
        envelope["activity_name"] = action

    # Strip metadata.product.feature if no lambda_name (OCSF doesn't allow
    # an empty feature object).
    if envelope["metadata"]["product"]["feature"] is None:
        envelope["metadata"]["product"].pop("feature")

    return envelope


def partition_key_for(image: Dict[str, Any]) -> str:
    """Kinesis partition key derivation. We hash on workspace_id when present
    so a workspace's events stay in-order on a single shard, falling back to
    request_id when workspace_id is empty (rare - SYSTEM rows)."""
    ws = _ddb_str(image, "workspace_id")
    if ws:
        return ws
    rid = _ddb_str(image, "request_id")
    return rid or "SYSTEM"
