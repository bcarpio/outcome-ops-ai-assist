"""Unit tests for chat conversation sharing.

Tests cover:
- Share/unshare conversation (PUT /conversations/{id}/share)
- Shared access via GET /conversations/{id} (is_owner fallback)
- List shared conversations (GET /shared)
- Conversation lifecycle (delete/update syncs share items)
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading (mirrors test_chat.py pattern)
# ============================================================================

mock_dynamodb_resource = MagicMock()
mock_ssm_client = MagicMock()
mock_s3vectors_client = MagicMock()
mock_bedrock_client = MagicMock()


def _load_handler_module():
    """Load the chat handler module with mocked dependencies."""
    if "chat_handler_sharing" in sys.modules:
        del sys.modules["chat_handler_sharing"]

    chat_dir = os.path.join(os.path.dirname(__file__), "../../chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)

    handler_path = os.path.join(chat_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("chat_handler_sharing", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["chat_handler_sharing"] = handler_module
    spec.loader.exec_module(handler_module)

    handler_module._dynamodb = mock_dynamodb_resource
    handler_module._ssm_client = mock_ssm_client
    handler_module._s3vectors_client = mock_s3vectors_client
    handler_module._bedrock_client = mock_bedrock_client
    handler_module._chat_table = "test-chat-table"
    handler_module._vector_bucket = "test-vector-bucket"
    handler_module._vector_index = "test-vector-index"
    handler_module._workspaces_table = "test-workspaces-table"

    return handler_module


handler_module = _load_handler_module()

lambda_handler = handler_module.lambda_handler
handle_share_conversation = handler_module.handle_share_conversation
list_shared_conversations = handler_module.list_shared_conversations
get_conversation = handler_module.get_conversation
_sync_share_items = handler_module._sync_share_items


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_ssm_client.reset_mock()
    mock_s3vectors_client.reset_mock()
    mock_bedrock_client.reset_mock()
    mock_dynamodb_resource.Table.side_effect = None


@pytest.fixture(autouse=True)
def stub_license_layer():
    """Inject a passing outcomeops_license stub so the ADR-019 license
    guard at the top of lambda_handler / handle_chat_stream succeeds by
    default. Mirrors the fixture in test_chat.py.
    """
    license_mock = MagicMock()
    license_mock.validate_license.return_value = {"sub": "test-customer"}
    with patch.dict(sys.modules, {"outcomeops_license": license_mock}):
        yield license_mock


@pytest.fixture
def mock_table():
    """Create mock DynamoDB table."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_conversation():
    """Sample conversation metadata owned by user@example.com."""
    return {
        "PK": "CONV#conv_test123",
        "SK": "METADATA",
        "conversation_id": "conv_test123",
        "title": "Test Conversation",
        "message_count": 4,
        "created_at": "2026-03-20T10:00:00+00:00",
        "updated_at": "2026-03-22T10:00:00+00:00",
        "user_id": "owner@example.com",
        "workspace_id": "ws_test",
    }


@pytest.fixture
def shared_conversation(sample_conversation):
    """Conversation that is already shared to ws-1 and ws-2."""
    sample_conversation["shared_workspace_ids"] = ["ws-1", "ws-2"]
    return sample_conversation


@pytest.fixture
def sample_messages():
    """Sample conversation messages."""
    return [
        {
            "PK": "CONV#conv_test123",
            "SK": "MSG#2026-03-20T10:00:00+00:00#msg_001",
            "message_id": "msg_001",
            "role": "user",
            "content": "How should Lambda handlers be structured?",
            "created_at": "2026-03-20T10:00:00+00:00",
        },
        {
            "PK": "CONV#conv_test123",
            "SK": "MSG#2026-03-20T10:01:00+00:00#msg_002",
            "message_id": "msg_002",
            "role": "assistant",
            "content": "According to ADR-004, Lambda handlers should follow...",
            "created_at": "2026-03-20T10:01:00+00:00",
        },
    ]


def _make_event(method, path, user_email="owner@example.com", body=None, query_params=None):
    """Build a Lambda Function URL event."""
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"x-user-email": user_email},
        "queryStringParameters": query_params or {},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _setup_workspace_access(table, user_email, workspace_ids, org_admin=False):
    """Configure mock to grant workspace access for a user.

    Since the handler module is loaded fresh (not bypassed), we need to
    mock the workspaces table responses properly.
    """
    def get_item_side_effect(**kwargs):
        key = kwargs.get("Key", {})
        pk = key.get("PK", "")
        sk = key.get("SK", "")

        # Chat table: conversation metadata
        if pk.startswith("CONV#") and sk == "METADATA":
            # Let the caller set this via table.get_item for chat table
            return table._chat_get_item(**kwargs)

        # Workspaces table: org admin check
        if pk == f"USER#{user_email}" and sk == "ORG_MEMBER":
            if org_admin:
                return {"Item": {"PK": pk, "SK": sk, "org_role": "org_admin"}}
            return {}

        # Workspaces table: membership check
        if pk.startswith("USER#") and sk.startswith("WS_MEMBER#"):
            ws_id = sk.replace("WS_MEMBER#", "")
            if ws_id in workspace_ids:
                return {"Item": {"PK": pk, "SK": sk}}
            return {}

        return {}

    return get_item_side_effect


# ============================================================================
# Share Conversation Tests (PUT /conversations/{id}/share with non-empty workspace_ids)
# ============================================================================


class TestShareConversation:
    """PUT /conversations/{id}/share with non-empty workspace_ids"""

    def test_share_to_accessible_workspaces(self, mock_table, sample_conversation):
        """Owner shares conversation to workspaces they have access to."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )

        chat_table.get_item.return_value = {"Item": sample_conversation}
        batch_ctx = MagicMock()
        chat_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        chat_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#owner@example.com", "SK": "WS_MEMBER#ws-1", "workspace_id": "ws-1"},
            ]
        }

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           body={"workspace_ids": ["ws-1"]})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "ws-1" in body["shared_workspace_ids"]
        assert body["share_url"] == "/chat/conv_test123"

    def test_share_empty_workspace_ids_unshares(self, mock_table, shared_conversation):
        """PUT with empty workspace_ids unshares all (returns 200 with empty list)."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        batch_ctx = MagicMock()
        chat_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        chat_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           body={"workspace_ids": []})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["shared_workspace_ids"] == []

    def test_share_nonexistent_conversation(self, mock_table):
        """Share a conversation that doesn't exist returns 404."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {}

        event = _make_event("PUT", "/conversations/nonexistent/share",
                           body={"workspace_ids": ["ws-1"]})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_share_conversation_not_owned(self, mock_table, sample_conversation):
        """Non-owner tries to share returns 404."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": sample_conversation}

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           user_email="other@example.com",
                           body={"workspace_ids": ["ws-1"]})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_share_to_inaccessible_workspace(self, mock_table, sample_conversation):
        """Share to workspace user doesn't belong to returns 403."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": sample_conversation}
        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {"Items": []}  # No memberships

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           body={"workspace_ids": ["ws-no-access"]})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 403

    def test_share_replaces_not_additive(self, mock_table, shared_conversation):
        """PUT replaces sharing entirely (ws-3 only, not ws-1+ws-2+ws-3)."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        batch_ctx = MagicMock()
        chat_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        chat_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#owner@example.com", "SK": "WS_MEMBER#ws-3", "workspace_id": "ws-3"},
            ]
        }

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           body={"workspace_ids": ["ws-3"]})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # Should only contain ws-3 (replace semantics, not additive)
        assert body["shared_workspace_ids"] == ["ws-3"]

    def test_org_admin_can_share_to_any_workspace(self, mock_table, sample_conversation):
        """org_admin can share to any workspace."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": sample_conversation}
        batch_ctx = MagicMock()
        chat_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        chat_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        def ws_get_item(**kwargs):
            sk = kwargs["Key"]["SK"]
            if sk == "ORG_MEMBER":
                return {"Item": {"PK": "USER#owner@example.com", "SK": "ORG_MEMBER", "org_role": "org_admin"}}
            return {}
        ws_table.get_item.side_effect = ws_get_item

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           body={"workspace_ids": ["ws-any"]})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200


# ============================================================================
# Unshare Tests (PUT /conversations/{id}/share with empty workspace_ids)
# ============================================================================


class TestUnshareConversation:
    """PUT /conversations/{id}/share with empty workspace_ids"""

    def test_unshare_all(self, mock_table, shared_conversation):
        """Empty workspace_ids removes all sharing."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        batch_ctx = MagicMock()
        chat_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        chat_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           body={"workspace_ids": []})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["shared_workspace_ids"] == []

    def test_unshare_not_owned(self, mock_table, shared_conversation):
        """Non-owner tries to unshare returns 404."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}

        event = _make_event("PUT", "/conversations/conv_test123/share",
                           user_email="other@example.com",
                           body={"workspace_ids": []})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_unshare_nonexistent_conversation(self, mock_table):
        """Unshare nonexistent conversation returns 404."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {}

        event = _make_event("PUT", "/conversations/nonexistent/share",
                           body={"workspace_ids": []})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404


# ============================================================================
# Shared Access via GET /conversations/{id} (is_owner fallback)
# ============================================================================


class TestSharedConversationAccess:
    """GET /conversations/{id} with shared access fallback"""

    def test_recipient_with_workspace_access(self, mock_table, shared_conversation, sample_messages):
        """User in shared workspace can view conversation via GET /conversations/{id}."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        chat_table.query.return_value = {"Items": sample_messages}

        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#reader@example.com", "SK": "WS_MEMBER#ws-1", "workspace_id": "ws-1"},
            ]
        }

        event = _make_event("GET", "/conversations/conv_test123", user_email="reader@example.com")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["conversation"]["conversation_id"] == "conv_test123"
        assert body["conversation"]["is_owner"] is False
        assert body["conversation"]["owner_email"] == "owner@example.com"
        assert len(body["messages"]) == 2

    def test_recipient_without_workspace_access(self, mock_table, shared_conversation):
        """User not in any shared workspace returns 404."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {"Items": []}  # No memberships

        event = _make_event("GET", "/conversations/conv_test123", user_email="noaccess@example.com")
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_conversation_not_shared(self, mock_table, sample_conversation):
        """Non-owner accessing unshared conversation returns 404."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": sample_conversation}

        event = _make_event("GET", "/conversations/conv_test123", user_email="reader@example.com")
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_conversation_not_found(self, mock_table):
        """Conversation doesn't exist returns 404."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {}

        event = _make_event("GET", "/conversations/nonexistent", user_email="reader@example.com")
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_owner_sees_is_owner_true(self, mock_table, shared_conversation, sample_messages):
        """Owner viewing their own conversation sees is_owner=True."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        chat_table.query.return_value = {"Items": sample_messages}

        event = _make_event("GET", "/conversations/conv_test123", user_email="owner@example.com")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["conversation"]["is_owner"] is True
        assert "shared_workspace_ids" in body["conversation"]
        # owner_email should NOT be included for the owner
        assert "owner_email" not in body["conversation"]

    def test_recipient_sees_shared_workspace_ids(
        self, mock_table, shared_conversation, sample_messages
    ):
        """Recipients can see shared_workspace_ids in the merged response."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        chat_table.query.return_value = {"Items": sample_messages}

        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#reader@example.com", "SK": "WS_MEMBER#ws-1", "workspace_id": "ws-1"},
            ]
        }

        event = _make_event("GET", "/conversations/conv_test123", user_email="reader@example.com")
        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert body["conversation"]["shared_workspace_ids"] == ["ws-1", "ws-2"]
        assert body["conversation"]["owner_email"] == "owner@example.com"

    def test_org_admin_can_view(self, mock_table, shared_conversation, sample_messages):
        """org_admin can view any shared conversation."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        chat_table.query.return_value = {"Items": sample_messages}

        def ws_get_item(**kwargs):
            sk = kwargs["Key"]["SK"]
            if sk == "ORG_MEMBER":
                return {"Item": {"PK": "USER#admin@example.com", "SK": "ORG_MEMBER", "org_role": "org_admin"}}
            return {}
        ws_table.get_item.side_effect = ws_get_item

        event = _make_event("GET", "/conversations/conv_test123", user_email="admin@example.com")
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200


# ============================================================================
# List Shared Conversations Tests (GET /shared)
# ============================================================================


class TestListSharedConversations:
    """GET /shared"""

    def test_list_with_workspace_filter(self, mock_table):
        """workspace_id param filters to single workspace."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )

        # User has access to ws-1
        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#user@example.com", "SK": "WS_MEMBER#ws-1", "workspace_id": "ws-1"},
            ]
        }

        # GSI3 returns one conversation
        chat_table.query.return_value = {
            "Items": [
                {
                    "conversation_id": "conv-1",
                    "title": "Shared Conv 1",
                    "message_count": 5,
                    "user_id": "sharer@example.com",
                    "updated_at": "2026-03-22T10:00:00+00:00",
                }
            ]
        }

        event = _make_event("GET", "/shared", user_email="user@example.com",
                           query_params={"workspace_id": "ws-1"})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["conversations"]) == 1
        assert body["conversations"][0]["conversation_id"] == "conv-1"
        assert body["has_more"] is False

    def test_list_inaccessible_workspace(self, mock_table):
        """workspace_id user doesn't have access to returns 403."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {"Items": []}  # No memberships

        event = _make_event("GET", "/shared", user_email="user@example.com",
                           query_params={"workspace_id": "ws-noaccess"})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 403

    def test_list_empty_result(self, mock_table):
        """No shared conversations returns empty list."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )

        # User has access to ws-1
        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#user@example.com", "SK": "WS_MEMBER#ws-1", "workspace_id": "ws-1"},
            ]
        }

        chat_table.query.return_value = {"Items": []}

        event = _make_event("GET", "/shared", user_email="user@example.com",
                           query_params={"workspace_id": "ws-1"})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["conversations"] == []
        assert body["has_more"] is False

    def test_list_no_workspaces(self, mock_table):
        """User with no workspace memberships returns empty list."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )

        # No workspace filter, user has no memberships
        ws_table.query.return_value = {"Items": []}

        event = _make_event("GET", "/shared", user_email="user@example.com")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["conversations"] == []

    def test_list_deduplicates_across_workspaces(self, mock_table):
        """Conversation shared to multiple of user's workspaces appears once."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )

        # User has access to ws-1 and ws-2
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#user@example.com", "SK": "WS_MEMBER#ws-1"},
                {"PK": "USER#user@example.com", "SK": "WS_MEMBER#ws-2"},
            ]
        }

        # Same conversation appears in both workspace queries
        same_conv = {
            "conversation_id": "conv-dup",
            "title": "Duplicated",
            "message_count": 3,
            "user_id": "owner@example.com",
            "updated_at": "2026-03-22T10:00:00+00:00",
        }
        chat_table.query.return_value = {"Items": [same_conv]}

        event = _make_event("GET", "/shared", user_email="user@example.com")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # Should be deduplicated to 1
        assert len(body["conversations"]) == 1

    def test_list_sorted_by_recency(self, mock_table):
        """Most recently updated conversations come first."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )

        ws_table.get_item.return_value = {}  # Not org_admin
        ws_table.query.return_value = {
            "Items": [
                {"PK": "USER#user@example.com", "SK": "WS_MEMBER#ws-1", "workspace_id": "ws-1"},
            ]
        }

        chat_table.query.return_value = {
            "Items": [
                {"conversation_id": "conv-old", "updated_at": "2026-03-20T10:00:00+00:00",
                 "title": "Old", "message_count": 1, "user_id": "a@b.com"},
                {"conversation_id": "conv-new", "updated_at": "2026-03-22T10:00:00+00:00",
                 "title": "New", "message_count": 2, "user_id": "a@b.com"},
            ]
        }

        event = _make_event("GET", "/shared", user_email="user@example.com",
                           query_params={"workspace_id": "ws-1"})
        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert body["conversations"][0]["conversation_id"] == "conv-new"
        assert body["conversations"][1]["conversation_id"] == "conv-old"


# ============================================================================
# Lambda Handler Route Tests
# ============================================================================


class TestSharingRoutes:
    """Test lambda_handler correctly routes sharing endpoints."""

    def test_put_share_route(self, mock_table, sample_conversation):
        """PUT /conversations/{id}/share routes correctly."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": sample_conversation}
        batch_ctx = MagicMock()
        chat_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        chat_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        # Grant access via org_admin
        ws_table.get_item.return_value = {
            "Item": {"PK": "USER#owner@example.com", "SK": "ORG_MEMBER", "org_role": "org_admin"}
        }

        event = {
            "requestContext": {"http": {"method": "PUT", "path": "/conversations/conv_test123/share"}},
            "headers": {"x-user-email": "owner@example.com"},
            "body": json.dumps({"workspace_ids": ["ws-1"]}),
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_put_unshare_route(self, mock_table, shared_conversation):
        """PUT /conversations/{id}/share with empty workspace_ids routes correctly."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        chat_table.get_item.return_value = {"Item": shared_conversation}
        batch_ctx = MagicMock()
        chat_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        chat_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        event = {
            "requestContext": {"http": {"method": "PUT", "path": "/conversations/conv_test123/share"}},
            "headers": {"x-user-email": "owner@example.com"},
            "body": json.dumps({"workspace_ids": []}),
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_old_post_share_returns_404(self, mock_table, sample_conversation):
        """POST /conversations/{id}/share no longer routes (returns 404)."""
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/conversations/conv_test123/share"}},
            "headers": {"x-user-email": "owner@example.com"},
            "body": json.dumps({"workspace_ids": ["ws-1"]}),
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_old_delete_share_returns_404(self, mock_table, shared_conversation):
        """DELETE /conversations/{id}/share no longer routes (returns 404)."""
        event = {
            "requestContext": {"http": {"method": "DELETE", "path": "/conversations/conv_test123/share"}},
            "headers": {"x-user-email": "owner@example.com"},
            "body": json.dumps({}),
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_old_get_share_status_returns_404(self, mock_table, sample_conversation):
        """GET /conversations/{id}/share no longer routes (returns 404)."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/conversations/conv_test123/share"}},
            "headers": {"x-user-email": "owner@example.com"},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_old_get_shared_id_returns_404(self, mock_table, shared_conversation):
        """GET /shared/{id} no longer routes (returns 404)."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/shared/conv_test123"}},
            "headers": {"x-user-email": "owner@example.com"},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_list_shared_route(self, mock_table):
        """GET /shared routes correctly."""
        chat_table = MagicMock()
        ws_table = MagicMock()
        mock_dynamodb_resource.Table.side_effect = lambda name: (
            chat_table if name == "test-chat-table" else ws_table
        )
        ws_table.query.return_value = {"Items": []}

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/shared"}},
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
