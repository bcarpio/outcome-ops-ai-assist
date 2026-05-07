"""Unit tests for shared/audit_writer.py module."""

import importlib.util
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Module loading
# ============================================================================

mock_dynamodb_resource = MagicMock()


def _load_audit_writer_module():
    """Load or reload the shared audit_writer module from its exact file path."""
    if "shared_audit_writer" in sys.modules:
        del sys.modules["shared_audit_writer"]

    shared_dir = os.path.join(os.path.dirname(__file__), "../../shared")
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)

    audit_path = os.path.join(shared_dir, "audit_writer.py")
    spec = importlib.util.spec_from_file_location("shared_audit_writer", audit_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["shared_audit_writer"] = module
    spec.loader.exec_module(module)

    module._dynamodb_resource = mock_dynamodb_resource
    return module


audit_writer = _load_audit_writer_module()

extract_request_context = audit_writer.extract_request_context
log_admin_action = audit_writer.log_admin_action
ADMIN_ACTIONS = audit_writer.ADMIN_ACTIONS
ACTION_API_KEY_CREATED = audit_writer.ACTION_API_KEY_CREATED
ACTION_SYSTEM_PROMPT_UPDATED = audit_writer.ACTION_SYSTEM_PROMPT_UPDATED
ACTION_PRIVACY_MODE_UPDATED = audit_writer.ACTION_PRIVACY_MODE_UPDATED
ACTION_DOCUMENT_INGESTED = audit_writer.ACTION_DOCUMENT_INGESTED
HIDDEN_DASHBOARD_ACTIONS = audit_writer.HIDDEN_DASHBOARD_ACTIONS
CATEGORY_ADMIN = audit_writer.CATEGORY_ADMIN
CATEGORY_AUTH = audit_writer.CATEGORY_AUTH


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks between tests; keep module pinned to our mock resource."""
    mock_dynamodb_resource.reset_mock()
    audit_writer._dynamodb_resource = mock_dynamodb_resource
    yield


@pytest.fixture
def mock_table():
    """Provide a mock DynamoDB table for put_item assertions."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


# ============================================================================
# extract_request_context
# ============================================================================


class TestExtractRequestContext:
    """Pull source_ip / user_agent from various event shapes."""

    def test_api_gw_v2_http_api(self):
        event = {
            "requestContext": {
                "http": {
                    "sourceIp": "203.0.113.42",
                    "userAgent": "Mozilla/5.0 v2",
                }
            }
        }
        result = extract_request_context(event)
        assert result == {
            "source_ip": "203.0.113.42",
            "user_agent": "Mozilla/5.0 v2",
        }

    def test_api_gw_v1_rest_identity(self):
        event = {
            "requestContext": {
                "identity": {
                    "sourceIp": "198.51.100.7",
                    "userAgent": "curl/7.88.1",
                }
            }
        }
        result = extract_request_context(event)
        assert result == {"source_ip": "198.51.100.7", "user_agent": "curl/7.88.1"}

    def test_lambda_function_url_header_fallback(self):
        event = {
            "requestContext": {"http": {"sourceIp": "192.0.2.5"}},
            "headers": {"user-agent": "Mozilla/5.0 LFU"},
        }
        result = extract_request_context(event)
        assert result == {"source_ip": "192.0.2.5", "user_agent": "Mozilla/5.0 LFU"}

    def test_lambda_function_url_capitalized_header(self):
        event = {
            "requestContext": {},
            "headers": {"User-Agent": "MyApp/1.0"},
        }
        result = extract_request_context(event)
        assert result["user_agent"] == "MyApp/1.0"

    def test_missing_request_context(self):
        result = extract_request_context({})
        assert result == {"source_ip": "", "user_agent": ""}

    def test_request_context_not_dict(self):
        # Defensive: malformed event should not raise.
        result = extract_request_context({"requestContext": "broken"})
        assert result == {"source_ip": "", "user_agent": ""}

    def test_event_not_dict(self):
        result = extract_request_context(None)
        assert result == {"source_ip": "", "user_agent": ""}

    def test_v2_takes_precedence_over_v1(self):
        event = {
            "requestContext": {
                "http": {"sourceIp": "1.1.1.1", "userAgent": "v2"},
                "identity": {"sourceIp": "9.9.9.9", "userAgent": "v1"},
            }
        }
        assert extract_request_context(event) == {
            "source_ip": "1.1.1.1",
            "user_agent": "v2",
        }


# ============================================================================
# log_admin_action
# ============================================================================


class TestLogAdminAction:
    """Write a single admin row and verify shape + non-fatal behavior."""

    def test_happy_path_writes_full_row(self, mock_table):
        log_admin_action(
            audit_table_name="audit-table",
            action=ACTION_API_KEY_CREATED,
            workspace_id="ws_abc",
            user_email="brian@example.com",
            source_ip="203.0.113.42",
            user_agent="Mozilla/5.0",
            details={"key_id": "kid_xyz", "name": "ci-bot"},
        )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]

        assert item["PK"] == "WS#ws_abc"
        assert item["GSI1PK"] == "USER#brian@example.com"
        assert item["SK"].endswith(item["request_id"])
        assert item["GSI1SK"] == item["SK"]
        # GSI2: category-partitioned index for org-wide audit-dashboard
        # queries. Without this, /audit/events would have to Scan + Filter
        # past every bedrock row to find admin matches.
        assert item["GSI2PK"] == "CAT#admin"
        assert item["GSI2SK"] == item["SK"]
        assert item["category"] == CATEGORY_ADMIN
        assert item["action"] == "API_KEY_CREATED"
        assert item["workspace_id"] == "ws_abc"
        assert item["user_email"] == "brian@example.com"
        assert item["source_ip"] == "203.0.113.42"
        assert item["user_agent"] == "Mozilla/5.0"
        assert item["details"] == {"key_id": "kid_xyz", "name": "ci-bot"}
        # TTL ~ 365 days from now (allow some skew)
        assert item["ttl"] > int(time.time()) + (364 * 86400)
        assert item["ttl"] < int(time.time()) + (366 * 86400)

    def test_no_workspace_uses_system_partition(self, mock_table):
        log_admin_action(
            audit_table_name="audit-table",
            action="ORG_MEMBER_ADDED",
            workspace_id="",
            user_email="org-admin@example.com",
        )

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["PK"] == "WS#SYSTEM"
        assert item["workspace_id"] == ""

    def test_no_user_email_uses_lambda_partition(self, mock_table):
        log_admin_action(
            audit_table_name="audit-table",
            action="WORKSPACE_DELETED",
            workspace_id="ws1",
            user_email="",
        )

        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["GSI1PK"].startswith("LAMBDA#")
        assert item["user_email"] == ""

    def test_no_table_skips_write(self, mock_table):
        log_admin_action(
            audit_table_name="",
            action="API_KEY_REVOKED",
            workspace_id="ws1",
            user_email="brian@example.com",
        )
        mock_table.put_item.assert_not_called()

    def test_put_item_exception_is_swallowed(self, mock_table):
        mock_table.put_item.side_effect = Exception("DDB throttled")

        # Must not raise.
        log_admin_action(
            audit_table_name="audit-table",
            action="API_KEY_CREATED",
            workspace_id="ws1",
            user_email="brian@example.com",
        )

    def test_empty_ip_and_agent_default_to_empty_string(self, mock_table):
        log_admin_action(
            audit_table_name="audit-table",
            action=ACTION_SYSTEM_PROMPT_UPDATED,
            workspace_id="ws1",
            user_email="brian@example.com",
        )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["source_ip"] == ""
        assert item["user_agent"] == ""

    def test_details_omitted_when_none(self, mock_table):
        log_admin_action(
            audit_table_name="audit-table",
            action="WORKSPACE_DELETED",
            workspace_id="ws1",
            user_email="brian@example.com",
        )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert "details" not in item

    def test_auth_category_passes_through(self, mock_table):
        log_admin_action(
            audit_table_name="audit-table",
            action="LOGIN",
            workspace_id="",
            user_email="brian@example.com",
            category=CATEGORY_AUTH,
        )
        item = mock_table.put_item.call_args.kwargs["Item"]
        assert item["category"] == "auth"
        # GSI2 partition follows the category, so a future auth dashboard
        # can Query GSI2 with PK="CAT#auth" the same way the admin one does.
        assert item["GSI2PK"] == "CAT#auth"


# ============================================================================
# Action constants invariants
# ============================================================================


class TestActionConstants:
    def test_all_actions_screaming_snake_case(self):
        for action in ADMIN_ACTIONS:
            assert action == action.upper(), action
            assert " " not in action

    def test_action_count_after_audit_completeness_expansion(self):
        # Original 17 (plan/01-dynamodb-schema.md) + 2 from PII redaction
        # (PRIVACY_MODE_UPDATED, DOCUMENT_INGESTED) + 23 from
        # plan/audit-completeness Tier 1 + Tier 2 = 42.
        assert len(ADMIN_ACTIONS) == 42

    def test_known_action_in_set(self):
        assert ACTION_API_KEY_CREATED in ADMIN_ACTIONS
        assert ACTION_SYSTEM_PROMPT_UPDATED in ADMIN_ACTIONS
        assert ACTION_PRIVACY_MODE_UPDATED in ADMIN_ACTIONS
        assert ACTION_DOCUMENT_INGESTED in ADMIN_ACTIONS

    def test_log_source_sync_helper_exists_and_dispatches(self):
        """The log_source_sync helper maps source name -> action constant
        and forwards to log_admin_action with the right payload shape."""
        with patch.object(audit_writer, "log_admin_action") as mock_admin:
            audit_writer.log_source_sync(
                audit_table_name="test-audit",
                source="github",
                workspace_id="ws-1",
                target="bcarpio/test-repo",
                trigger="scheduled",
                files_synced=42,
                duration_ms=1234,
                extra_details={"commit_sha": "abc123"},
            )
        mock_admin.assert_called_once()
        args, kwargs = mock_admin.call_args
        assert args[1] == "REPO_SYNCED"
        assert kwargs["workspace_id"] == "ws-1"
        details = kwargs["details"]
        assert details["source"] == "github"
        assert details["target"] == "bcarpio/test-repo"
        assert details["trigger"] == "scheduled"
        assert details["files_synced"] == 42
        assert details["success"] is True
        assert details["duration_ms"] == 1234
        assert details["commit_sha"] == "abc123"

    def test_log_source_sync_dispatches_each_source(self):
        cases = [
            ("github", "REPO_SYNCED"),
            ("confluence", "CONFLUENCE_SYNCED"),
            ("jira", "JIRA_SYNCED"),
            ("outlook", "OUTLOOK_SYNCED"),
            ("teams", "TEAMS_SYNCED"),
            ("sharepoint", "SHAREPOINT_SYNCED"),
        ]
        for source, expected_action in cases:
            with patch.object(audit_writer, "log_admin_action") as mock_admin:
                audit_writer.log_source_sync(
                    audit_table_name="test-audit",
                    source=source,
                    workspace_id="ws-1",
                    target="t",
                    trigger="webhook",
                )
            assert mock_admin.call_args.args[1] == expected_action, source

    def test_log_source_sync_unknown_source_is_noop(self):
        with patch.object(audit_writer, "log_admin_action") as mock_admin:
            audit_writer.log_source_sync(
                audit_table_name="test-audit",
                source="bitbucket",  # not in the map
                workspace_id="ws-1",
                target="t",
                trigger="webhook",
            )
        mock_admin.assert_not_called()

    def test_audit_completeness_actions_in_set(self):
        # Spot-check one constant from each grouping in
        # plan/audit-completeness so a future ADMIN_ACTIONS edit that
        # accidentally drops a category gets caught.
        assert audit_writer.ACTION_LOGIN_SUCCEEDED in ADMIN_ACTIONS
        assert audit_writer.ACTION_REPO_SYNCED in ADMIN_ACTIONS
        assert audit_writer.ACTION_OAUTH_CONNECTION_ESTABLISHED in ADMIN_ACTIONS
        assert audit_writer.ACTION_DOCUMENT_DELETED in ADMIN_ACTIONS
        assert audit_writer.ACTION_WORKSPACE_SHARING_UPDATED in ADMIN_ACTIONS
        assert audit_writer.ACTION_PR_ANALYZED in ADMIN_ACTIONS
        assert audit_writer.ACTION_BUDGET_THRESHOLD_CROSSED in ADMIN_ACTIONS

    def test_hidden_dashboard_actions_includes_document_ingested(self):
        # DOCUMENT_INGESTED audit rows are written for forensic provenance
        # but hidden from the dashboard so they don't dominate the
        # "most common action" / "total actions" cards.
        assert ACTION_DOCUMENT_INGESTED in HIDDEN_DASHBOARD_ACTIONS

    def test_hidden_dashboard_actions_subset_of_admin_actions(self):
        # Hidden actions must be valid admin actions (otherwise the hide
        # rule references a typo).
        assert HIDDEN_DASHBOARD_ACTIONS.issubset(ADMIN_ACTIONS)
