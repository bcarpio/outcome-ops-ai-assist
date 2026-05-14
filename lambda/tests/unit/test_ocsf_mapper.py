"""Unit tests for the OCSF mapper used by the audit-stream-publisher."""

import sys
from pathlib import Path

# Ensure the shared/ module is importable.
SHARED = Path(__file__).resolve().parents[2] / "shared"
sys.path.insert(0, str(SHARED))

from ocsf_mapper import (  # noqa: E402
    CLASS_ACCOUNT_CHANGE,
    CLASS_API_ACTIVITY,
    CLASS_APPLICATION_ACTIVITY,
    CLASS_AUTHENTICATION,
    SEV_INFO,
    SEV_LOW,
    SEV_MEDIUM,
    map_audit_row_to_ocsf,
    partition_key_for,
)


def _ddb(d):
    """Convenience: wrap a flat dict into a DynamoDB Streams NEW_IMAGE shape."""
    out = {}
    for k, v in d.items():
        if isinstance(v, bool):
            out[k] = {"BOOL": v}
        elif isinstance(v, (int, float)):
            out[k] = {"N": str(v)}
        elif v is None:
            out[k] = {"NULL": True}
        else:
            out[k] = {"S": str(v)}
    return out


def test_bedrock_success_maps_to_api_activity_class():
    image = _ddb(
        {
            "category": "bedrock",
            "timestamp": "2026-05-08T14:30:00Z",
            "user_email": "alice@example.com",
            "source_ip": "10.0.0.1",
            "user_agent": "Mozilla/5.0",
            "request_id": "req-123",
            "workspace_id": "ws_abc",
            "lambda_name": "chat",
            "model_id": "anthropic.claude-haiku-4-5",
            "status": "success",
            "input_tokens": 100,
            "output_tokens": 200,
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["class_uid"] == CLASS_API_ACTIVITY
    assert out["activity_id"] == 2  # Read
    assert out["status_id"] == 1  # Success
    assert out["severity_id"] == SEV_INFO
    assert out["actor"]["user"]["email_addr"] == "alice@example.com"
    assert out["src_endpoint"]["ip"] == "10.0.0.1"
    assert out["unmapped"]["model_id"] == "anthropic.claude-haiku-4-5"
    assert out["unmapped"]["input_tokens"] == 100
    assert out["metadata"]["product"]["name"] == "OutcomeOps AI Assist"
    assert out["metadata"]["uid"] == "req-123"


def test_bedrock_refusal_elevates_severity_and_sets_status_detail():
    image = _ddb(
        {
            "category": "bedrock",
            "timestamp": "2026-05-08T14:30:00Z",
            "user_email": "alice@example.com",
            "request_id": "req-456",
            "workspace_id": "ws_abc",
            "status": "refusal",
            "flagged_reason": "tos_violation",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["status_id"] == 2  # Failure
    assert out["severity_id"] == SEV_MEDIUM
    assert out["status_detail"] == "tos_violation"


def test_login_succeeded_maps_to_authentication_class():
    image = _ddb(
        {
            "category": "auth",
            "timestamp": "2026-05-08T08:00:00Z",
            "user_email": "bob@example.com",
            "request_id": "req-7",
            "workspace_id": "ws_abc",
            "action": "LOGIN_SUCCEEDED",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["class_uid"] == CLASS_AUTHENTICATION
    assert out["activity_id"] == 1  # Logon
    assert out["activity_name"] == "LOGIN_SUCCEEDED"
    assert out["category_uid"] == 3  # IAM


def test_workspace_created_maps_to_account_change_create():
    image = _ddb(
        {
            "category": "admin",
            "timestamp": "2026-05-08T10:00:00Z",
            "user_email": "admin@example.com",
            "request_id": "req-8",
            "workspace_id": "ws_new",
            "action": "WORKSPACE_CREATED",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["class_uid"] == CLASS_ACCOUNT_CHANGE
    assert out["activity_id"] == 1  # Create
    assert out["category_uid"] == 3  # IAM


def test_workspace_deleted_elevates_severity_to_medium():
    image = _ddb(
        {
            "category": "admin",
            "action": "WORKSPACE_DELETED",
            "user_email": "admin@example.com",
            "request_id": "r",
            "workspace_id": "ws_x",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["severity_id"] == SEV_MEDIUM
    assert out["activity_id"] == 4  # Delete


def test_oauth_connection_maps_to_authentication_class():
    image = _ddb(
        {
            "category": "admin",
            "action": "OAUTH_CONNECTION_ESTABLISHED",
            "user_email": "admin@example.com",
            "request_id": "r",
            "workspace_id": "ws_x",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["class_uid"] == CLASS_AUTHENTICATION


def test_budget_threshold_maps_to_application_activity_medium():
    image = _ddb(
        {
            "category": "admin",
            "action": "BUDGET_THRESHOLD_CROSSED",
            "user_email": "system@outcomeops",
            "request_id": "r",
            "workspace_id": "ws_x",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["class_uid"] == CLASS_APPLICATION_ACTIVITY
    assert out["severity_id"] == SEV_MEDIUM


def test_unknown_admin_action_falls_through_to_application_activity():
    image = _ddb(
        {
            "category": "admin",
            "action": "DOCUMENT_INGESTED",  # not in the v1 explicit map
            "user_email": "system@outcomeops",
            "request_id": "r",
            "workspace_id": "ws_x",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["class_uid"] == CLASS_APPLICATION_ACTIVITY
    assert out["activity_id"] == 5  # Other
    assert out["severity_id"] == SEV_INFO
    # Action name still preserved on the envelope and inside unmapped.
    assert out["activity_name"] == "DOCUMENT_INGESTED"
    assert out["unmapped"]["action"] == "DOCUMENT_INGESTED"


def test_workspace_resource_recorded_when_present():
    image = _ddb(
        {
            "category": "admin",
            "action": "WORKSPACE_UPDATED",
            "user_email": "admin@example.com",
            "request_id": "r",
            "workspace_id": "ws_target",
        }
    )
    out = map_audit_row_to_ocsf(image)
    resources = out.get("resources", [])
    assert any(
        r.get("type") == "workspace" and r.get("uid") == "ws_target"
        for r in resources
    )


def test_iso_timestamp_converted_to_epoch_ms():
    image = _ddb(
        {
            "category": "admin",
            "action": "WORKSPACE_CREATED",
            "timestamp": "2026-05-08T14:30:00Z",
            "user_email": "admin@example.com",
            "request_id": "r",
            "workspace_id": "ws",
        }
    )
    out = map_audit_row_to_ocsf(image)
    # 2026-05-08T14:30:00 UTC -> epoch ms positive integer
    assert isinstance(out["time"], int)
    assert out["time"] > 1_700_000_000_000


def test_partition_key_uses_workspace_when_present():
    image = _ddb({"workspace_id": "ws_42", "request_id": "req-1"})
    assert partition_key_for(image) == "ws_42"


def test_partition_key_falls_back_to_request_id():
    image = _ddb({"request_id": "req-system"})
    assert partition_key_for(image) == "req-system"


def test_unmapped_preserves_full_payload():
    image = _ddb(
        {
            "category": "bedrock",
            "model_id": "haiku",
            "input_tokens": 50,
            "output_tokens": 75,
            "cost_usd": 0.001234,
            "user_email": "u@x",
            "request_id": "r",
            "workspace_id": "w",
        }
    )
    out = map_audit_row_to_ocsf(image)
    unmapped = out["unmapped"]
    assert unmapped["model_id"] == "haiku"
    assert unmapped["input_tokens"] == 50
    assert unmapped["output_tokens"] == 75
    assert unmapped["cost_usd"] == 0.001234


def test_envelope_omits_feature_when_lambda_name_blank():
    image = _ddb(
        {
            "category": "admin",
            "action": "WORKSPACE_CREATED",
            "user_email": "u@x",
            "request_id": "r",
            "workspace_id": "w",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert "feature" not in out["metadata"]["product"]


def test_chat_routing_decision_maps_to_application_lifecycle_class():
    """plan-gkg/09: CHAT_ROUTING_DECISION rows must map to OCSF
    Application Lifecycle (6002) with the routing-decision activity_id
    so SIEM consumers can filter on type_uid=600299 without conflating
    with generic 6005 Application Activity events."""
    from ocsf_mapper import CLASS_APPLICATION_LIFECYCLE  # noqa: PLC0415

    image = _ddb(
        {
            "category": "admin",
            "action": "CHAT_ROUTING_DECISION",
            "user_email": "alice@example.com",
            "request_id": "r-routing",
            "workspace_id": "ws_engineering",
            "timestamp": "2026-05-14T10:00:00Z",
        }
    )
    out = map_audit_row_to_ocsf(image)
    assert out["class_uid"] == CLASS_APPLICATION_LIFECYCLE
    assert out["class_name"] == "Application Lifecycle"
    assert out["activity_id"] == 99  # ACT_ROUTING_DECISION
    # type_uid = class * 100 + activity_id
    assert out["type_uid"] == 600299
    assert out["severity_id"] == SEV_INFO
    # Falls under the Application category (6), not IAM.
    assert out["category_uid"] == 6
    assert out["activity_name"] == "CHAT_ROUTING_DECISION"
