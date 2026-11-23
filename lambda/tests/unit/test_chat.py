# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for chat Lambda handler.

Tests cover:
- Conversation management (create, get, update, delete, list)
- Message handling (save, retrieve)
- Title generation
- RAG pipeline (embedding, vector search, context formatting)
- Contextual search query building
- Conversation memory (create chunks, search, delete, format)
- Answer generation stream
- handle_chat_stream orchestration
- lambda_handler REST routing
- Audit logging integration
- Error handling across all layers
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
# Module loading
# ============================================================================

# Pre-mock boto3 clients before module import
mock_dynamodb_resource = MagicMock()
mock_ssm_client = MagicMock()
mock_s3vectors_client = MagicMock()
mock_bedrock_client = MagicMock()


def _load_handler_module():
    """Load or reload the chat handler module."""
    if "chat_handler" in sys.modules:
        del sys.modules["chat_handler"]

    # Add chat dir to path so handler can resolve its sibling imports
    chat_dir = os.path.join(os.path.dirname(__file__), "../../chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)

    handler_path = os.path.join(chat_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("chat_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["chat_handler"] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace cached boto3 clients with mocks
    handler_module._dynamodb = mock_dynamodb_resource
    handler_module._ssm_client = mock_ssm_client
    handler_module._s3vectors_client = mock_s3vectors_client
    handler_module._bedrock_client = mock_bedrock_client
    handler_module._chat_table = "test-chat-table"
    handler_module._vector_bucket = "test-vector-bucket"
    handler_module._vector_index = "test-vector-index"
    handler_module._workspaces_table = "test-workspaces-table"
    # Bypass workspace access validation in tests
    handler_module._validate_workspace_access = lambda user_email, workspace_ids: workspace_ids
    # Bypass scope resolution too.
    # Save a reference to the real resolver so the dedicated resolver
    # tests can reach past the stub.
    handler_module._real_resolve_chat_workspace_scope = (
        handler_module.resolve_chat_workspace_scope
    )
    handler_module.resolve_chat_workspace_scope = (
        lambda user_email, workspace_ids: workspace_ids
    )

    return handler_module


handler_module = _load_handler_module()

# Import functions under test
generate_id = handler_module.generate_id
generate_title = handler_module.generate_title
get_conversation = handler_module.get_conversation
create_conversation = handler_module.create_conversation
update_conversation = handler_module.update_conversation
get_conversation_messages = handler_module.get_conversation_messages
save_message = handler_module.save_message
generate_embedding = handler_module.generate_embedding
build_contextual_search_query = handler_module.build_contextual_search_query
format_context = handler_module.format_context
summarize_messages_for_memory = handler_module.summarize_messages_for_memory
create_conversation_memory_chunk = handler_module.create_conversation_memory_chunk
search_conversation_memory = handler_module.search_conversation_memory
delete_conversation_memory = handler_module.delete_conversation_memory
format_memory_context = handler_module.format_memory_context
update_memory_chunk_count = handler_module.update_memory_chunk_count
generate_answer_stream = handler_module.generate_answer_stream
handle_chat_stream = handler_module.handle_chat_stream
lambda_handler = handler_module.lambda_handler
streaming_handler = handler_module.streaming_handler
list_conversations = handler_module.list_conversations
get_conversation_with_messages = handler_module.get_conversation_with_messages
delete_conversation = handler_module.delete_conversation
ChatMessageRequest = handler_module.ChatMessageRequest
_stream_error = handler_module._stream_error


# ============================================================================
# Fixtures
# ============================================================================

def _legacy_converse_stream_to_unified(events):
    """Translate Converse-shape stream events (the dict shape that
    test_chat fixtures program via mock_bedrock_client.converse_stream.
    return_value) into the unified StreamEvent sequence that the new
    chat handler consumes after the ADR-035 migration.
    """
    import json as _json
    from shared.model_client_types import (
        Metadata, Stop, StopReason, TextDelta, ToolUseArgumentsDelta,
        ToolUseStart, ToolUseStop, Usage,
    )

    current_tool_id = None
    args_buf = ""
    for ev in events:
        if "messageStart" in ev:
            continue
        if "contentBlockStart" in ev:
            start = ev["contentBlockStart"].get("start", {})
            tu = start.get("toolUse")
            if tu:
                current_tool_id = tu.get("toolUseId", "")
                args_buf = ""
                yield ToolUseStart(tool_use_id=current_tool_id, name=tu.get("name", ""))
            continue
        if "contentBlockDelta" in ev:
            delta = ev["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                yield TextDelta(text=delta["text"])
            elif "toolUse" in delta and current_tool_id:
                partial = delta["toolUse"].get("input", "")
                args_buf += partial
                yield ToolUseArgumentsDelta(
                    tool_use_id=current_tool_id, arguments_partial=partial
                )
            continue
        if "contentBlockStop" in ev:
            if current_tool_id is not None:
                try:
                    args = _json.loads(args_buf or "{}")
                except _json.JSONDecodeError:
                    args = {}
                yield ToolUseStop(tool_use_id=current_tool_id, arguments=args)
                current_tool_id = None
                args_buf = ""
            continue
        if "messageStop" in ev:
            stop_raw = ev["messageStop"].get("stopReason", "end_turn")
            try:
                yield Stop(stop_reason=StopReason(stop_raw))
            except ValueError:
                yield Stop(stop_reason=StopReason.END_TURN)
            continue
        if "metadata" in ev:
            usage = ev["metadata"].get("usage", {})
            yield Metadata(
                usage=Usage(
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                )
            )


def _patched_model_invoke_stream(**kwargs):
    """Bridge: read mock_bedrock_client.converse_stream.return_value
    (set by individual tests in the legacy Converse-shape) and yield
    the equivalent unified StreamEvents."""
    raw = mock_bedrock_client.converse_stream.return_value or {}
    if isinstance(raw, Exception):
        raise raw
    stream = raw.get("stream", []) if isinstance(raw, dict) else []
    # Also propagate side_effect for tests that raise.
    side_effect = mock_bedrock_client.converse_stream.side_effect
    if side_effect is not None:
        if isinstance(side_effect, Exception):
            raise side_effect
    # Translate unified kwargs back to Converse-shape so existing
    # tests that assert call_args["modelId"] / ["messages"] / etc. on
    # mock_bedrock_client.converse_stream keep working.
    #
    # ADR-016 v1.2: callers now pass workload+tier instead of model_id.
    # Synthesize a stand-in model_id from the tier so pre-existing
    # tests that grep the modelId string for "haiku"/"sonnet" keep
    # asserting the intent. Real callers get an application inference
    # profile ARN back from shared.model_client; this shim short-
    # circuits the profile-arn/env-var lookup that isn't wired in
    # tests.
    model_id = kwargs.get("model_id")
    if model_id is None:
        tier = kwargs.get("tier")
        if tier == "advanced":
            model_id = "us.anthropic.claude-sonnet-4-6"
        elif tier == "basic":
            model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    converse_kwargs = {
        "modelId": model_id,
        "messages": kwargs.get("messages"),
        "inferenceConfig": {
            "maxTokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
        },
    }
    sys_val = kwargs.get("system")
    if sys_val is not None:
        sys_list = sys_val if isinstance(sys_val, list) else [sys_val]
        converse_kwargs["system"] = [{"text": s} for s in sys_list]
    tools = kwargs.get("tools")
    if tools:
        converse_kwargs["toolConfig"] = {
            "tools": [
                {"toolSpec": {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": {"json": t.input_schema},
                }}
                for t in tools
            ]
        }
    mock_bedrock_client.converse_stream(**converse_kwargs)
    yield from _legacy_converse_stream_to_unified(stream)


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests and clear side_effect."""
    mock_dynamodb_resource.reset_mock()
    mock_ssm_client.reset_mock()
    mock_s3vectors_client.reset_mock()
    mock_bedrock_client.reset_mock()

    # Clear any side_effect from previous tests
    mock_dynamodb_resource.Table.side_effect = None
    mock_s3vectors_client.query_vectors.side_effect = None
    mock_s3vectors_client.put_vectors.side_effect = None
    mock_s3vectors_client.list_vectors.side_effect = None
    mock_s3vectors_client.delete_vectors.side_effect = None
    mock_bedrock_client.invoke_model.side_effect = None
    mock_bedrock_client.converse_stream.side_effect = None

    # Restore cached config so SSM is not called
    handler_module._chat_table = "test-chat-table"
    handler_module._vector_bucket = "test-vector-bucket"
    handler_module._vector_index = "test-vector-index"
    handler_module._workspaces_table = "test-workspaces-table"

    # Patch the shared client alias used by the migrated chat handler
    # so existing tests that program mock_bedrock_client.converse_stream
    # keep working without rewriting every fixture.
    with patch.object(
        handler_module, "model_invoke_stream",
        side_effect=_patched_model_invoke_stream,
    ):
        yield


@pytest.fixture(autouse=True)
def stub_symbol_graph_augment():
    """Short-circuit the symbol-graph augmentation step entirely so
    handle_chat_stream tests never reach the router's bedrock.converse()
    call (moto does not implement converse). test_chat exercises the
    RAG path and does not assert on structural augmentation — the
    dedicated test_chat_symbol_graph_augment / test_chat_router /
    test_chat_context_builder suites cover that path.

    Replace the module attribute rather than the router's bedrock client
    so the substitution is robust across pytest collection orderings and
    works whether or not the router module was reloaded by a sibling
    test file mid-suite.
    """
    empty_result = {
        "decision": None,
        "graph_block": "",
        "retrievals_fired": [],
        "graph_sha": None,
        "graph_staleness_seconds": None,
        "graph_result_count": None,
    }
    original = handler_module._augment_context_with_symbol_graph
    handler_module._augment_context_with_symbol_graph = MagicMock(
        return_value=empty_result
    )
    try:
        yield handler_module._augment_context_with_symbol_graph
    finally:
        handler_module._augment_context_with_symbol_graph = original


@pytest.fixture
def mock_table():
    """Create mock DynamoDB table."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_embedding():
    """Sample embedding vector."""
    return [0.1] * 1024


@pytest.fixture
def sample_search_results():
    """Sample S3 Vectors search results."""
    return {
        "vectors": [
            {
                "key": "doc-001",
                "distance": 0.95,
                "metadata": {
                    "content": "Lambda handlers should use standard patterns...",
                    "repo": "my-repo",
                    "type": "adr",
                    "file_path": "docs/ADR-001.md",
                    "workspace_id": "ws_test",
                },
            },
            {
                "key": "doc-002",
                "distance": 0.82,
                "metadata": {
                    "content": "Use pytest for testing...",
                    "repo": "standards-repo",
                    "type": "readme",
                    "file_path": "README.md",
                    "workspace_id": "ws_test",
                },
            },
        ]
    }


@pytest.fixture
def sample_conversation():
    """Sample conversation metadata."""
    return {
        "PK": "CONV#conv_test123",
        "SK": "METADATA",
        "conversation_id": "conv_test123",
        "title": "How should Lambda handlers...",
        "message_count": 4,
        "memory_chunk_count": 0,
        "created_at": "2025-01-15T10:00:00+00:00",
        "updated_at": "2025-01-15T10:05:00+00:00",
        "user_id": "user@example.com",
        "workspace_id": "ws_test",
    }


@pytest.fixture
def sample_messages():
    """Sample conversation messages."""
    return [
        {
            "PK": "CONV#conv_test123",
            "SK": "MSG#2025-01-15T10:00:00+00:00#msg_001",
            "message_id": "msg_001",
            "role": "user",
            "content": "How should Lambda handlers be structured?",
            "created_at": "2025-01-15T10:00:00+00:00",
        },
        {
            "PK": "CONV#conv_test123",
            "SK": "MSG#2025-01-15T10:01:00+00:00#msg_002",
            "message_id": "msg_002",
            "role": "assistant",
            "content": "According to ADR-004, Lambda handlers should follow...",
            "created_at": "2025-01-15T10:01:00+00:00",
        },
    ]


class TestDdbNormalize:
    """boto3's Table resource returns every DDB Number as
    ``decimal.Decimal``. Bedrock Converse's SDK rejects Decimal on
    the ``document`` param used by tool ``inputSchema``. AWS's hosted
    MCP server publishes tool schemas with numeric ``default`` /
    ``minimum`` / ``maximum`` fields; without normalization the whole
    chat turn blows up with a Bedrock ValidationException the first
    time an admin adds one.
    """

    def test_top_level_decimal_becomes_int_for_whole_numbers(self):
        from decimal import Decimal
        assert handler_module._ddb_normalize(Decimal("100")) == 100
        assert isinstance(handler_module._ddb_normalize(Decimal("100")), int)

    def test_top_level_decimal_becomes_float_for_fractional(self):
        from decimal import Decimal
        result = handler_module._ddb_normalize(Decimal("2.5"))
        assert result == 2.5
        assert isinstance(result, float)

    def test_recurses_into_dicts_and_lists(self):
        from decimal import Decimal
        schema = {
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "integer",
                    "default": Decimal("100"),
                    "minimum": Decimal("1"),
                    "maximum": Decimal("900"),
                },
                "tags": [Decimal("1"), Decimal("2.5"), "keep"],
            },
        }
        normalized = handler_module._ddb_normalize(schema)
        timeout = normalized["properties"]["timeout"]
        assert timeout["default"] == 100
        assert isinstance(timeout["default"], int)
        assert timeout["minimum"] == 1
        assert timeout["maximum"] == 900
        assert normalized["properties"]["tags"] == [1, 2.5, "keep"]

    def test_non_decimal_values_pass_through(self):
        assert handler_module._ddb_normalize("hello") == "hello"
        assert handler_module._ddb_normalize(True) is True
        assert handler_module._ddb_normalize(None) is None
        assert handler_module._ddb_normalize(42) == 42


# ============================================================================
# of the workspace-scope plan: resolve_chat_workspace_scope
# ============================================================================


class TestResolveChatWorkspaceScope:
    """Bidirectional-read scope resolver. Rules:
    1. Standard access check passes through.
    2. is_global workspaces readable by anyone.
    3. If user is a member of an is_global workspace they've included
       in the request AND the request represents their default scope
       (contains every workspace they're a direct member of), broaden
       to every workspace in the deployment. Any subset -- even one
       that includes a global they're a member of -- is treated as
       explicit narrowing and respected. Fixed 2026-07-09 after an
       org-admin picking only the Standards global saw scope leak
       to every workspace in the deployment.
    """

    def _table_stub(self, ws_meta, all_ws_scan):
        """Build a MagicMock table whose get_item returns per-ws metadata
        and whose scan returns the all-workspaces list once (no pagination)."""
        from unittest.mock import MagicMock
        table = MagicMock()

        def get_item(Key, **_):
            ws_id = Key["PK"].replace("WS#", "")
            item = ws_meta.get(ws_id)
            return {"Item": item} if item else {}
        table.get_item.side_effect = get_item
        table.scan.return_value = {"Items": all_ws_scan}
        return table

    def _call_real(self, ws_meta, all_ws_scan, authorized, user_ws_ids, request_ids):
        """Invoke the *real* resolver (bypassing the fixture stub) with
        the given ddb + user context patched in."""
        from unittest.mock import patch, MagicMock

        table = self._table_stub(ws_meta, all_ws_scan)
        with patch.object(
            handler_module, "_get_dynamodb",
            return_value=MagicMock(Table=lambda _: table),
        ), patch.object(
            handler_module, "_validate_workspace_access",
            return_value=authorized,
        ), patch.object(
            handler_module, "_get_user_workspace_ids",
            return_value=user_ws_ids,
        ):
            return handler_module._real_resolve_chat_workspace_scope(
                "user@example.com", request_ids,
            ), table

    def test_pure_owned_workspace_passes_through(self):
        # No globals involved. User is a member of ws-eng.
        result, _ = self._call_real(
            ws_meta={"ws-eng": {"workspace_id": "ws-eng", "is_global": False}},
            all_ws_scan=[],
            authorized=["ws-eng"],
            user_ws_ids=["ws-eng"],
            request_ids=["ws-eng"],
        )
        assert result == ["ws-eng"]

    def test_global_workspace_passes_through_for_non_member(self):
        # User picked a global workspace they're NOT a member of; the
        # is_global pass-through makes it readable. Broaden should NOT
        # fire.
        result, table = self._call_real(
            ws_meta={
                "ws-eng": {"workspace_id": "ws-eng", "is_global": False},
                "ws-sec": {"workspace_id": "ws-sec", "is_global": True},
            },
            all_ws_scan=[],
            authorized=["ws-eng"],  # access check filtered ws-sec out
            user_ws_ids=["ws-eng"],
            request_ids=["ws-eng", "ws-sec"],
        )
        assert set(result) == {"ws-eng", "ws-sec"}
        table.scan.assert_not_called()

    def test_global_member_broadens_to_all_workspaces(self):
        # Sec-team member picks their own global workspace -> broaden
        # to every workspace in the deployment.
        result, _ = self._call_real(
            ws_meta={
                "ws-sec": {"workspace_id": "ws-sec", "is_global": True},
            },
            all_ws_scan=[
                {"workspace_id": "ws-sec"},
                {"workspace_id": "ws-eng"},
                {"workspace_id": "ws-payments"},
            ],
            authorized=["ws-sec"],
            user_ws_ids=["ws-sec"],
            request_ids=["ws-sec"],
        )
        assert set(result) == {"ws-sec", "ws-eng", "ws-payments"}

    def test_global_member_narrowing_to_non_global_does_not_broaden(self):
        # Sec-team member picks JUST ws-eng (excluding their global
        # workspace) -> respect the narrowing; no auto-broaden.
        # Plan open-question #4.
        result, table = self._call_real(
            ws_meta={"ws-eng": {"workspace_id": "ws-eng", "is_global": False}},
            all_ws_scan=[],
            authorized=["ws-eng"],
            user_ws_ids=["ws-sec", "ws-eng"],
            request_ids=["ws-eng"],
        )
        assert result == ["ws-eng"]
        table.scan.assert_not_called()

    def test_empty_request_returns_empty(self):
        # No table access needed for the empty case.
        result = handler_module._real_resolve_chat_workspace_scope(
            "user@example.com", [],
        )
        assert result == []

    def test_global_member_narrowing_to_only_global_does_not_broaden(self):
        # Org admin is a member of {ws-sec (global), ws-eng, ws-payments}
        # and explicitly narrows to JUST the global workspace. This is
        # NOT the default scope -- the request omits ws-eng and
        # ws-payments -- so Rule 3 must NOT fire. Regression for the
        # 2026-07-09 scope-leak report where selecting only the
        # "Standards and ADRs" global workspace returned sources from
        # every workspace in the deployment.
        result, table = self._call_real(
            ws_meta={
                "ws-sec": {"workspace_id": "ws-sec", "is_global": True},
            },
            all_ws_scan=[
                {"workspace_id": "ws-sec"},
                {"workspace_id": "ws-eng"},
                {"workspace_id": "ws-payments"},
            ],
            authorized=["ws-sec"],
            user_ws_ids=["ws-sec", "ws-eng", "ws-payments"],
            request_ids=["ws-sec"],
        )
        assert result == ["ws-sec"]
        table.scan.assert_not_called()

    def test_global_member_narrowing_to_global_plus_one_team_does_not_broaden(self):
        # Security lead is a member of {ws-sec (global), ws-eng,
        # ws-payments, ws-mobile} and narrows to "global + one team"
        # for a focused query. Respect the narrowing.
        result, table = self._call_real(
            ws_meta={
                "ws-sec": {"workspace_id": "ws-sec", "is_global": True},
                "ws-eng": {"workspace_id": "ws-eng", "is_global": False},
            },
            all_ws_scan=[
                {"workspace_id": "ws-sec"},
                {"workspace_id": "ws-eng"},
                {"workspace_id": "ws-payments"},
                {"workspace_id": "ws-mobile"},
            ],
            authorized=["ws-sec", "ws-eng"],
            user_ws_ids=["ws-sec", "ws-eng", "ws-payments", "ws-mobile"],
            request_ids=["ws-sec", "ws-eng"],
        )
        assert set(result) == {"ws-sec", "ws-eng"}
        table.scan.assert_not_called()

    def test_default_scope_including_global_still_broadens(self):
        # Same user as above, but the picker sends the *full*
        # membership set (default state, no narrowing). Rule 3 still
        # fires and broadens to every workspace in the deployment.
        result, _ = self._call_real(
            ws_meta={
                "ws-sec": {"workspace_id": "ws-sec", "is_global": True},
                "ws-eng": {"workspace_id": "ws-eng", "is_global": False},
                "ws-payments": {"workspace_id": "ws-payments", "is_global": False},
                "ws-mobile": {"workspace_id": "ws-mobile", "is_global": False},
            },
            all_ws_scan=[
                {"workspace_id": "ws-sec"},
                {"workspace_id": "ws-eng"},
                {"workspace_id": "ws-payments"},
                {"workspace_id": "ws-mobile"},
                {"workspace_id": "ws-other-team"},
            ],
            authorized=["ws-sec", "ws-eng", "ws-payments", "ws-mobile"],
            user_ws_ids=["ws-sec", "ws-eng", "ws-payments", "ws-mobile"],
            request_ids=["ws-sec", "ws-eng", "ws-payments", "ws-mobile"],
        )
        assert set(result) == {
            "ws-sec", "ws-eng", "ws-payments", "ws-mobile", "ws-other-team",
        }


# ============================================================================
# Request/Response Models
# ============================================================================

class TestChatMessageRequest:
    """Test ChatMessageRequest validation."""

    def test_valid_request_minimal(self):
        req = ChatMessageRequest(message="Hello")
        assert req.message == "Hello"
        assert req.top_k == 10
        assert req.advanced is False
        assert req.show_cost is False
        assert req.conversation_id is None

    def test_valid_request_full(self):
        req = ChatMessageRequest(
            conversation_id="conv_123",
            message="What are our standards?",
            top_k=20,
            advanced=True,
            show_cost=True,
            user={"email": "user@example.com"},
            workspace_ids=["ws_a", "ws_b"],
        )
        assert req.conversation_id == "conv_123"
        assert req.top_k == 20
        assert req.advanced is True
        assert req.workspace_ids == ["ws_a", "ws_b"]

    def test_message_stripped(self):
        req = ChatMessageRequest(message="  Hello world  ")
        assert req.message == "Hello world"

    def test_empty_message_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ChatMessageRequest(message="")

    def test_whitespace_only_message_stripped_to_empty(self):
        """Whitespace-only message is stripped by validator; min_length is
        checked before the strip validator runs in Pydantic v2, but the
        field_validator runs after, producing an empty string. The handler
        itself will still get an empty message and the stream will proceed
        (this matches the actual behavior)."""
        req = ChatMessageRequest(message="   ")
        assert req.message == ""

    def test_top_k_bounds(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ChatMessageRequest(message="test", top_k=0)
        with pytest.raises(ValidationError):
            ChatMessageRequest(message="test", top_k=51)


class TestStreamError:
    """Test _stream_error helper."""

    def test_stream_error_default_status(self):
        result = _stream_error("Bad request")
        parsed = json.loads(result)
        assert parsed["type"] == "error"
        assert parsed["error"] == "Bad request"
        assert parsed["status_code"] == 400

    def test_stream_error_custom_status(self):
        result = _stream_error("Not found", 404)
        parsed = json.loads(result)
        assert parsed["status_code"] == 404


# ============================================================================
# ID and Title Generation
# ============================================================================

class TestGenerateId:
    """Test unique ID generation."""

    def test_generate_id_returns_string(self):
        result = generate_id()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_id_is_unique(self):
        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100


class TestGenerateTitle:
    """Test title generation from messages."""

    def test_short_message_used_as_title(self):
        result = generate_title("What is Terraform?")
        assert result == "What is Terraform?"

    def test_long_message_truncated_at_word_boundary(self):
        msg = "How should Lambda handlers be structured according to the latest standards?"
        result = generate_title(msg)
        assert result.endswith("...")
        assert len(result) <= 55  # 50 + "..."

    def test_long_message_no_good_word_boundary(self):
        msg = "a" * 60
        result = generate_title(msg)
        assert result.endswith("...")

    def test_exactly_50_chars(self):
        msg = "x" * 50
        result = generate_title(msg)
        assert result == msg  # No truncation needed


# ============================================================================
# Conversation Management
# ============================================================================

class TestGetConversation:
    """Test getting conversation metadata from DynamoDB."""

    def test_get_conversation_found(self, mock_table, sample_conversation):
        mock_table.get_item.return_value = {"Item": sample_conversation}
        result = get_conversation("conv_test123")
        assert result["conversation_id"] == "conv_test123"
        mock_table.get_item.assert_called_once_with(
            Key={"PK": "CONV#conv_test123", "SK": "METADATA"}
        )

    def test_get_conversation_not_found(self, mock_table):
        mock_table.get_item.return_value = {}
        result = get_conversation("nonexistent")
        assert result is None

    def test_get_conversation_dynamodb_error(self, mock_table):
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "GetItem"
        )
        result = get_conversation("conv_test123")
        assert result is None


class TestCreateConversation:
    """Test creating new conversations."""

    def test_create_conversation_basic(self, mock_table):
        result = create_conversation("Test title")
        assert "conversation_id" in result
        assert result["title"] == "Test title"
        assert result["message_count"] == 0
        assert result["memory_chunk_count"] == 0
        mock_table.put_item.assert_called_once()

    def test_create_conversation_with_user_and_workspace(self, mock_table):
        result = create_conversation(
            "Test title",
            user_id="user@example.com",
            workspace_id="ws_test",
        )
        assert result["user_id"] == "user@example.com"
        assert result["workspace_id"] == "ws_test"

    def test_create_conversation_no_optional_fields(self, mock_table):
        result = create_conversation("Test title")
        assert "user_id" not in result
        assert "workspace_id" not in result

    def test_create_conversation_populates_gsi2(self, mock_table):
        """Verify GSI2PK and GSI2SK are set on new conversations."""
        create_conversation("Test Title", user_id="user@example.com")
        put_call = mock_table.put_item.call_args[1]
        item = put_call["Item"]
        assert item["GSI2PK"] == "USER#user@example.com"
        assert item["GSI2SK"].startswith("CONV#")

    def test_create_conversation_sets_metadata_ttl(self, mock_table):
        """ADR-060 ghost fix: the METADATA row carries expires_at so a
        conversation ages out whole instead of outliving its expired messages."""
        import time as _time
        before = int(_time.time())
        create_conversation("Test Title")
        after = int(_time.time())
        item = mock_table.put_item.call_args[1]["Item"]
        assert "expires_at" in item
        # Default chat TTL = 90d (7776000s).
        assert item["expires_at"] >= before + 7776000
        assert item["expires_at"] <= after + 7776000 + 5


class TestUpdateConversation:
    """Test updating conversation metadata."""

    def test_update_conversation_success(self, mock_table):
        update_conversation("conv_test123", 5)
        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert call_args["Key"] == {"PK": "CONV#conv_test123", "SK": "METADATA"}
        assert call_args["ExpressionAttributeValues"][":mc"] == 5

    def test_update_conversation_refreshes_ttl(self, mock_table):
        """ADR-060 ghost fix: each message refreshes the METADATA TTL so the
        conversation always outlives its newest message (never orphaned)."""
        import time as _time
        before = int(_time.time())
        update_conversation("conv_test123", 5)
        after = int(_time.time())
        call_args = mock_table.update_item.call_args[1]
        assert "expires_at = :exp" in call_args["UpdateExpression"]
        exp = call_args["ExpressionAttributeValues"][":exp"]
        assert exp >= before + 7776000
        assert exp <= after + 7776000 + 5

    def test_update_conversation_dynamodb_error(self, mock_table):
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "UpdateItem"
        )
        # Should not raise
        update_conversation("conv_test123", 5)

    def test_update_conversation_updates_gsi2sk(self, mock_table):
        """Verify GSI2SK is updated when conversation is updated."""
        update_conversation("conv_123", 5)
        update_call = mock_table.update_item.call_args[1]
        assert "GSI2SK = :gsi2sk" in update_call["UpdateExpression"]
        assert update_call["ExpressionAttributeValues"][":gsi2sk"].startswith("CONV#")


class TestGetConversationMessages:
    """Test retrieving conversation messages."""

    def test_get_messages_returns_chronological(self, mock_table, sample_messages):
        # DynamoDB returns newest first (ScanIndexForward=False), handler reverses
        mock_table.query.return_value = {"Items": list(reversed(sample_messages))}
        result = get_conversation_messages("conv_test123", limit=10)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_get_messages_empty(self, mock_table):
        mock_table.query.return_value = {"Items": []}
        result = get_conversation_messages("conv_test123")
        assert result == []

    def test_get_messages_dynamodb_error(self, mock_table):
        mock_table.query.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "Query"
        )
        result = get_conversation_messages("conv_test123")
        assert result == []


class TestSaveMessage:
    """Test saving messages to a conversation."""

    def test_save_user_message(self, mock_table):
        result = save_message("conv_test123", "user", "Hello")
        assert isinstance(result, str)
        assert len(result) > 0
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "CONV#conv_test123"
        assert item["SK"].startswith("MSG#")
        assert item["role"] == "user"
        assert item["content"] == "Hello"

    def test_save_assistant_message_with_sources(self, mock_table):
        sources = [{"repo": "my-repo", "type": "adr", "path": "docs/ADR-001.md"}]
        save_message("conv_test123", "assistant", "Answer text", sources=sources)
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["sources"] == sources

    def test_save_message_without_sources(self, mock_table):
        save_message("conv_test123", "user", "Question")
        item = mock_table.put_item.call_args[1]["Item"]
        assert "sources" not in item

    def test_save_message_sets_expires_at(self, mock_table):
        """expires_at drives TTL eviction (default 90d)."""
        import time as _time
        before = int(_time.time())
        save_message("conv_test123", "user", "Hello")
        after = int(_time.time())
        item = mock_table.put_item.call_args[1]["Item"]
        assert "expires_at" in item
        # Default TTL = 7776000s (90 days). Row's expires_at should sit
        # within the 90-day window from now, allowing for small clock drift.
        assert item["expires_at"] >= before + 7776000
        assert item["expires_at"] <= after + 7776000 + 5

    def test_save_message_caps_oversized_content(self, mock_table, monkeypatch):
        """>CHAT_MAX_MESSAGE_CONTENT_CHARS pastes get
        head+tail truncated with a middle-of-message marker."""
        # Use a realistic cap so the head+tail budget preserves both ends.
        monkeypatch.setenv("CHAT_MAX_MESSAGE_CONTENT_CHARS", "1000")
        oversized = "A" * 600 + "B" * 500 + "C" * 600  # 1700 chars > 1000
        save_message("conv_test123", "user", oversized)
        item = mock_table.put_item.call_args[1]["Item"]
        stored = item["content"]
        # Cap + marker overhead. Head budget = cap//2 = 500;
        # tail budget = cap - head - 100 = 400. Total ≤ 900 + marker.
        assert len(stored) <= 1000
        # Head + tail preserved; middle truncated.
        assert stored.startswith("A")
        assert stored.endswith("C")
        assert "truncated" in stored

    def test_save_message_leaves_small_content_intact(self, mock_table, monkeypatch):
        """Content under the cap round-trips unchanged."""
        monkeypatch.setenv("CHAT_MAX_MESSAGE_CONTENT_CHARS", "100")
        small = "Hello, world"
        save_message("conv_test123", "user", small)
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["content"] == small


# ============================================================================
# Vector Search
# ============================================================================

class TestGenerateEmbedding:
    """Test embedding generation using Bedrock Titan."""

    def test_generate_embedding_success(self, sample_embedding):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

        result = generate_embedding("test query")
        assert result == sample_embedding
        mock_bedrock_client.invoke_model.assert_called_once()
        call_args = mock_bedrock_client.invoke_model.call_args[1]
        assert call_args["modelId"] == "amazon.titan-embed-text-v2:0"
        body = json.loads(call_args["body"])
        assert body["inputText"] == "test query"
        assert body["dimensions"] == 1024

    def test_generate_embedding_empty_result(self):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

        result = generate_embedding("test query")
        assert result == []

    def test_generate_embedding_client_error(self):
        mock_bedrock_client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeModel",
        )
        with pytest.raises(ClientError):
            generate_embedding("test query")


class TestBuildContextualSearchQuery:
    """Test contextual search query augmentation."""

    def test_no_history_returns_original(self):
        result = build_contextual_search_query("What is Terraform?", [])
        assert result == "What is Terraform?"

    def test_short_message_is_follow_up(self):
        history = [
            {"role": "user", "content": "Tell me about Terraform modules"},
            {"role": "assistant", "content": "Terraform modules are reusable configurations..."},
        ]
        result = build_contextual_search_query("What about version pinning?", history)
        assert "Current question: What about version pinning?" in result
        assert "Previous context:" in result

    def test_explicit_follow_up_indicator(self):
        history = [
            {"role": "user", "content": "Tell me about testing"},
            {"role": "assistant", "content": "Testing should follow a structured approach..."},
        ]
        result = build_contextual_search_query(
            "Can you explain more about integration tests?", history
        )
        assert "Current question:" in result

    def test_long_message_not_follow_up(self):
        history = [
            {"role": "user", "content": "Previous question"},
            {"role": "assistant", "content": "Previous answer"},
        ]
        # Long message (> 80 chars) without follow-up indicators
        long_msg = "I need a completely new Terraform module that provisions an S3 bucket with versioning and lifecycle policies configured"
        result = build_contextual_search_query(long_msg, history)
        # Should not augment if not a follow-up
        assert result == long_msg

    def test_history_with_no_assistant_messages(self):
        history = [
            {"role": "user", "content": "Previous question"},
        ]
        result = build_contextual_search_query("Why?", history)
        assert "Original topic: Previous question" in result




class TestPerLaneRerank:
    """ADR-033 per-lane rerank: after dual-channel retrieval, each lane's
    hits are reranked INDEPENDENTLY so neither lane's winners are crowded
    out by the other lane's cross-rerank scores.

    The 2026-06-03 re-run confirmed the auth code-map IS retrieved by
    the code-maps lane (3rd hit, dist=0.7263) but combined-pool rerank
    dropped it -- Cohere judged it less relevant than docs-lane
    prose-shaped hits. Per-lane rerank guarantees each lane keeps its
    own top-K-after-rerank, so the code-maps slot is protected from
    doc-side competition.
    """

    def test_per_lane_rerank_called_twice_when_both_lanes_have_hits(
        self, mock_table, sample_embedding
    ):
        from unittest.mock import patch

        # Force rerank on for this test.
        with patch.object(handler_module, "RERANK_ENABLED", True), \
             patch.object(handler_module, "rerank_results") as mock_rerank:
            mock_dynamodb_resource.Table.return_value = mock_table
            mock_body = MagicMock()
            mock_body.read.return_value = json.dumps(
                {"embedding": sample_embedding}
            ).encode()
            mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
            # Both lanes return hits so per-lane branch fires.
            mock_s3vectors_client.query_vectors.return_value = {
                "vectors": [
                    {
                        "key": "doc-x",
                        "distance": 0.5,
                        "metadata": {
                            "content": "content",
                            "type": "markdown",
                            "workspace_id": "ws_test",
                            "source": "github",
                            "folder_path": "owner/repo",
                        },
                    }
                ]
            }
            # Rerank passes through; we only care about WHO calls it.
            mock_rerank.side_effect = lambda query, docs, top_n=None, **kw: list(
                docs
            )[: top_n or len(docs)]
            mock_bedrock_client.converse_stream.return_value = {
                "stream": [
                    {"contentBlockDelta": {"delta": {"text": "OK"}}},
                    {
                        "metadata": {
                            "usage": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                            }
                        }
                    },
                ]
            }
            mock_table.query.return_value = {"Items": []}

            event = {
                "body": json.dumps(
                    {"message": "Hello", "workspace_ids": ["ws_test"]}
                )
            }
            with patch.object(handler_module, "log_audit_entry"):
                list(handle_chat_stream(event))

            # rerank_results called EXACTLY TWICE -- once per lane.
            # If only called once (combined pool), per-lane fix
            # regressed.
            assert mock_rerank.call_count == 2, (
                f"Expected per-lane rerank (2 calls), got "
                f"{mock_rerank.call_count}"
            )

    def test_per_lane_rerank_splits_top_n_budget_evenly(
        self, mock_table, sample_embedding
    ):
        """request.top_k splits evenly across lanes (with docs taking the
        odd remainder). Floor of 1 per lane so a tiny budget still keeps
        both lanes represented."""
        from unittest.mock import patch

        with patch.object(handler_module, "RERANK_ENABLED", True), \
             patch.object(handler_module, "rerank_results") as mock_rerank:
            mock_dynamodb_resource.Table.return_value = mock_table
            mock_body = MagicMock()
            mock_body.read.return_value = json.dumps(
                {"embedding": sample_embedding}
            ).encode()
            mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
            mock_s3vectors_client.query_vectors.return_value = {
                "vectors": [
                    {
                        "key": "doc-x",
                        "distance": 0.5,
                        "metadata": {
                            "content": "content",
                            "type": "markdown",
                            "workspace_id": "ws_test",
                            "source": "github",
                            "folder_path": "owner/repo",
                        },
                    }
                ]
            }
            mock_rerank.side_effect = lambda query, docs, top_n=None, **kw: list(
                docs
            )[: top_n or len(docs)]
            mock_bedrock_client.converse_stream.return_value = {
                "stream": [
                    {"contentBlockDelta": {"delta": {"text": "OK"}}},
                    {
                        "metadata": {
                            "usage": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                            }
                        }
                    },
                ]
            }
            mock_table.query.return_value = {"Items": []}

            event = {
                "body": json.dumps(
                    {
                        "message": "Hello",
                        "workspace_ids": ["ws_test"],
                        "top_k": 10,
                    }
                )
            }
            with patch.object(handler_module, "log_audit_entry"):
                list(handle_chat_stream(event))

            # Two calls. Their top_n reflects the adaptive split:
            # with a 1-vector-per-lane mock, code_map has 1 hit and
            # docs has 1 hit, so each lane is capped at its actual
            # candidate count -- expected [1, 1] not [5, 5] (the
            # pre-adaptive behavior wasted the other 8 slots).
            top_ns = sorted(c.kwargs["top_n"] for c in mock_rerank.call_args_list)
            assert top_ns == [1, 1], (
                f"Adaptive split with 1 hit/lane should cap at [1, 1]; "
                f"got {top_ns}"
            )

    def test_per_lane_rerank_isolates_code_map_lane_from_doc_lane(
        self, mock_table, sample_embedding
    ):
        """Each rerank call sees ONLY its own lane's documents. A
        code-map hit must never appear in the docs lane's rerank pool
        and vice versa. This is the load-bearing isolation that
        protects the code-maps lane from being out-scored by docs."""
        from unittest.mock import patch

        with patch.object(handler_module, "RERANK_ENABLED", True), \
             patch.object(handler_module, "rerank_results") as mock_rerank:
            mock_dynamodb_resource.Table.return_value = mock_table
            mock_body = MagicMock()
            mock_body.read.return_value = json.dumps(
                {"embedding": sample_embedding}
            ).encode()
            mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

            # Different content per lane so we can verify isolation.
            def fake_query(**kwargs):
                f = kwargs.get("filter", {})
                type_clause = None
                if "$and" in f:
                    for c in f["$and"]:
                        if "type" in c:
                            type_clause = c["type"]["$in"]
                if type_clause and "handler-group-summary" in type_clause:
                    return {
                        "vectors": [
                            {
                                "key": "code-map-hit",
                                "distance": 0.7,
                                "metadata": {
                                    "content": "code-map content",
                                    "type": "handler-group-summary",
                                    "workspace_id": "ws_test",
                                    "source": "github",
                                    "folder_path": "owner/repo",
                                },
                            }
                        ]
                    }
                return {
                    "vectors": [
                        {
                            "key": "doc-hit",
                            "distance": 0.6,
                            "metadata": {
                                "content": "doc content",
                                "type": "markdown",
                                "workspace_id": "ws_test",
                                "source": "github",
                                "folder_path": "owner/repo",
                            },
                        }
                    ]
                }

            mock_s3vectors_client.query_vectors.side_effect = fake_query
            mock_rerank.side_effect = lambda query, docs, top_n=None, **kw: list(
                docs
            )[: top_n or len(docs)]
            mock_bedrock_client.converse_stream.return_value = {
                "stream": [
                    {"contentBlockDelta": {"delta": {"text": "OK"}}},
                    {
                        "metadata": {
                            "usage": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                            }
                        }
                    },
                ]
            }
            mock_table.query.return_value = {"Items": []}

            event = {
                "body": json.dumps(
                    {"message": "Hello", "workspace_ids": ["ws_test"]}
                )
            }
            with patch.object(handler_module, "log_audit_entry"):
                list(handle_chat_stream(event))

            # Inspect each rerank call's documents arg. One call's
            # documents are all code-map lane; the other call's are
            # all docs lane. No cross-contamination.
            by_lane = {}
            for c in mock_rerank.call_args_list:
                docs = c.args[1] if len(c.args) > 1 else c.kwargs.get("documents")
                if not docs:
                    continue
                lanes_in_call = {d.get("lane") for d in docs}
                assert len(lanes_in_call) == 1, (
                    f"Rerank call mixed lanes: {lanes_in_call}"
                )
                by_lane[next(iter(lanes_in_call))] = docs
            assert set(by_lane.keys()) == {"code_map", "docs"}, (
                f"Expected both lanes to rerank, got: {list(by_lane.keys())}"
            )
            assert by_lane["code_map"][0]["key"] == "code-map-hit"
            assert by_lane["docs"][0]["key"] == "doc-hit"

    def test_per_lane_rerank_falls_back_to_combined_when_one_lane_empty(
        self, mock_table, sample_embedding
    ):
        """Thin-ingest workspace (no code-maps) -> code-maps lane
        returns empty. Per-lane rerank skipped (no fairness issue);
        combined-pool rerank runs once over the docs lane only. No
        crash, no partial-failure."""
        from unittest.mock import patch

        with patch.object(handler_module, "RERANK_ENABLED", True), \
             patch.object(handler_module, "rerank_results") as mock_rerank:
            mock_dynamodb_resource.Table.return_value = mock_table
            mock_body = MagicMock()
            mock_body.read.return_value = json.dumps(
                {"embedding": sample_embedding}
            ).encode()
            mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

            def fake_query(**kwargs):
                f = kwargs.get("filter", {})
                type_clause = None
                if "$and" in f:
                    for c in f["$and"]:
                        if "type" in c:
                            type_clause = c["type"]["$in"]
                if type_clause and "handler-group-summary" in type_clause:
                    return {"vectors": []}  # thin-ingest: no code-maps
                # Return >1 doc so the rerank guard
                # (`len(search_results) > 1`) lets the call through.
                return {
                    "vectors": [
                        {
                            "key": f"doc-hit-{i}",
                            "distance": 0.6 + i * 0.01,
                            "metadata": {
                                "content": f"doc content {i}",
                                "type": "markdown",
                                "workspace_id": "ws_test",
                                "source": "github",
                                "folder_path": "owner/repo",
                            },
                        }
                        for i in range(3)
                    ]
                }

            mock_s3vectors_client.query_vectors.side_effect = fake_query
            mock_rerank.side_effect = lambda query, docs, top_n=None, **kw: list(
                docs
            )[: top_n or len(docs)]
            mock_bedrock_client.converse_stream.return_value = {
                "stream": [
                    {"contentBlockDelta": {"delta": {"text": "OK"}}},
                    {
                        "metadata": {
                            "usage": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                            }
                        }
                    },
                ]
            }
            mock_table.query.return_value = {"Items": []}

            event = {
                "body": json.dumps(
                    {"message": "Hello", "workspace_ids": ["ws_test"]}
                )
            }
            with patch.object(handler_module, "log_audit_entry"):
                list(handle_chat_stream(event))

            # Only ONE rerank call (the combined-pool fallback) when
            # one lane is empty.
            assert mock_rerank.call_count == 1, (
                f"Expected single combined-pool rerank fallback, got "
                f"{mock_rerank.call_count}"
            )


class TestRerankModelArn:
    """Bedrock Rerank rejects application inference profile ARNs, so chat
    must pass the foundation-model id even when the chat rerank profile
    env var is set (issue #57: every chat rerank failed and fell back to
    cosine order)."""

    def test_rerank_ignores_profile_arn_and_uses_model_id(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv(
            "BEDROCK_CHAT_COHERE_RERANK_PROFILE_ARN",
            "arn:aws:bedrock:us-west-2:123456789012:"
            "application-inference-profile/abc123",
        )
        client = MagicMock()
        client.meta.region_name = "us-west-2"
        client.rerank.return_value = {
            "results": [{"index": 0, "relevanceScore": 0.9}],
        }
        with patch.object(
            handler_module, "_get_bedrock_agent_runtime_client",
            return_value=client,
        ):
            handler_module.rerank_results("q", [{"text": "d", "score": 0.5}])

        model_arn = client.rerank.call_args.kwargs["rerankingConfiguration"][
            "bedrockRerankingConfiguration"
        ]["modelConfiguration"]["modelArn"]
        assert "::foundation-model/" in model_arn
        assert model_arn.endswith(handler_module.RERANK_MODEL_ID)
        assert "inference-profile" not in model_arn


class TestFormatContext:
    """Test formatting search results as prompt context."""

    def test_format_context_with_results(self):
        results = [
            {"repo": "my-repo", "type": "adr", "content": "Handler patterns..."},
            {"repo": "other-repo", "type": "readme", "content": "Testing guide..."},
        ]
        result = format_context(results)
        assert "[1] Source: my-repo (adr)" in result
        assert "Handler patterns..." in result
        assert "[2] Source: other-repo (readme)" in result
        assert "---" in result

    def test_format_context_empty(self):
        result = format_context([])
        assert result == "No relevant documents found."

    def test_format_context_truncates_long_content(self):
        results = [{"repo": "repo", "type": "doc", "content": "x" * 5000}]
        result = format_context(results)
        # Content is truncated to 3000 chars per doc
        content_part = result.split("\n", 1)[1]
        assert len(content_part) <= 3001


# ============================================================================
# Conversation Memory
# ============================================================================

class TestSummarizeMessagesForMemory:
    """Test message summarization for memory chunks."""

    def test_summarize_basic(self):
        messages = [
            {"role": "user", "content": "What is Terraform?"},
            {"role": "assistant", "content": "Terraform is an IaC tool..."},
        ]
        result = summarize_messages_for_memory(messages)
        assert "User asked: What is Terraform?" in result
        assert "Assistant answered: Terraform is an IaC tool..." in result

    def test_summarize_truncates_long_messages(self):
        messages = [
            {"role": "user", "content": "x" * 600},
        ]
        result = summarize_messages_for_memory(messages)
        assert "..." in result
        assert len(result) < 600

    def test_summarize_empty_messages(self):
        result = summarize_messages_for_memory([])
        assert result == ""


class TestCreateConversationMemoryChunk:
    """Test creating memory chunks in S3 Vectors."""

    def test_create_memory_chunk_success(self, sample_embedding):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

        messages = [
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1"},
        ]

        result = create_conversation_memory_chunk("conv_123", 1, messages)
        assert result is True
        mock_s3vectors_client.put_vectors.assert_called_once()
        call_args = mock_s3vectors_client.put_vectors.call_args[1]
        assert call_args["vectorBucketName"] == "test-vector-bucket"
        vector = call_args["vectors"][0]
        assert vector["key"] == "memory#conv_123#0001"
        assert vector["metadata"]["type"] == "conversation_memory"
        assert vector["metadata"]["conversation_id"] == "conv_123"

    def test_create_memory_chunk_embedding_fails(self):
        mock_bedrock_client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "InvokeModel"
        )
        messages = [{"role": "user", "content": "Question"}]
        result = create_conversation_memory_chunk("conv_123", 1, messages)
        assert result is False

    def test_create_memory_chunk_empty_summary(self):
        result = create_conversation_memory_chunk("conv_123", 1, [])
        assert result is False
        mock_bedrock_client.invoke_model.assert_not_called()

    def test_create_memory_chunk_s3vectors_error(self, sample_embedding):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
        mock_s3vectors_client.put_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutVectors"
        )

        messages = [{"role": "user", "content": "Question"}]
        result = create_conversation_memory_chunk("conv_123", 1, messages)
        assert result is False


class TestSearchConversationMemory:
    """Test searching conversation memory chunks."""

    def test_search_memory_success(self, sample_embedding):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
        mock_s3vectors_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "memory#conv_123#0001",
                    "distance": 0.9,
                    "metadata": {
                        "content": "User asked about Terraform...",
                        "chunk_number": 1,
                    },
                }
            ]
        }

        results = search_conversation_memory("conv_123", "question about modules")
        assert len(results) == 1
        assert results[0]["content"] == "User asked about Terraform..."
        # Verify filter uses correct format
        call_args = mock_s3vectors_client.query_vectors.call_args[1]
        assert call_args["filter"] == {
            "$and": [
                {"type": {"$eq": "conversation_memory"}},
                {"conversation_id": {"$eq": "conv_123"}},
            ]
        }

    def test_search_memory_empty_embedding(self):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

        results = search_conversation_memory("conv_123", "query")
        assert results == []

    def test_search_memory_embedding_error(self):
        mock_bedrock_client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "InvokeModel"
        )
        results = search_conversation_memory("conv_123", "query")
        assert results == []

    def test_search_memory_client_error(self, sample_embedding):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
        mock_s3vectors_client.query_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "QueryVectors"
        )

        results = search_conversation_memory("conv_123", "query")
        assert results == []

    def test_search_memory_respects_top_k(self, sample_embedding):
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
        # Return more results than top_k
        mock_s3vectors_client.query_vectors.return_value = {
            "vectors": [
                {"key": f"memory#conv_123#{i:04d}", "distance": 0.9 - i * 0.01,
                 "metadata": {"content": f"Chunk {i}", "chunk_number": i}}
                for i in range(5)
            ]
        }

        results = search_conversation_memory("conv_123", "query", top_k=2)
        assert len(results) == 2


class TestDeleteConversationMemory:
    """Test deleting conversation memory chunks."""

    def test_delete_memory_success(self):
        mock_s3vectors_client.list_vectors.return_value = {
            "vectors": [
                {"key": "memory#conv_123#0001"},
                {"key": "memory#conv_123#0002"},
                {"key": "other#doc"},  # Should be skipped
            ]
        }

        result = delete_conversation_memory("conv_123")
        assert result is True
        mock_s3vectors_client.delete_vectors.assert_called_once()
        call_args = mock_s3vectors_client.delete_vectors.call_args[1]
        assert len(call_args["keys"]) == 2
        assert "memory#conv_123#0001" in call_args["keys"]
        assert "memory#conv_123#0002" in call_args["keys"]

    def test_delete_memory_no_chunks(self):
        mock_s3vectors_client.list_vectors.return_value = {"vectors": []}

        result = delete_conversation_memory("conv_123")
        assert result is True
        mock_s3vectors_client.delete_vectors.assert_not_called()

    def test_delete_memory_client_error(self):
        mock_s3vectors_client.list_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "ListVectors"
        )

        result = delete_conversation_memory("conv_123")
        assert result is False


class TestFormatMemoryContext:
    """Test formatting memory chunks for the prompt."""

    def test_format_memory_context_basic(self):
        memory_results = [
            {"content": "User asked about modules...", "chunk_number": 1},
            {"content": "User asked about testing...", "chunk_number": 2},
        ]
        result = format_memory_context(memory_results)
        assert "CONVERSATION HISTORY SUMMARY:" in result
        assert "User asked about modules..." in result
        assert "User asked about testing..." in result

    def test_format_memory_context_sorts_by_chunk_number(self):
        memory_results = [
            {"content": "Second chunk", "chunk_number": 2},
            {"content": "First chunk", "chunk_number": 1},
        ]
        result = format_memory_context(memory_results)
        # First chunk should appear before second chunk
        assert result.index("First chunk") < result.index("Second chunk")

    def test_format_memory_context_empty(self):
        result = format_memory_context([])
        assert result == ""

    def test_format_memory_context_empty_content(self):
        memory_results = [{"content": "", "chunk_number": 1}]
        result = format_memory_context(memory_results)
        assert result == ""


class TestUpdateMemoryChunkCount:
    """Test updating memory chunk count in conversation metadata."""

    def test_update_memory_chunk_count_success(self, mock_table):
        update_memory_chunk_count("conv_123", 3)
        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":mcc"] == 3

    def test_update_memory_chunk_count_error(self, mock_table):
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "UpdateItem"
        )
        # Should not raise
        update_memory_chunk_count("conv_123", 3)


# ============================================================================
# Answer Generation
# ============================================================================

class TestGenerateAnswerStream:
    """Test streaming answer generation from Bedrock."""

    def test_generate_answer_stream_collects_text(self):
        mock_bedrock_client.converse_stream.return_value = {
            "stream": [
                {"contentBlockDelta": {"delta": {"text": "Hello "}}},
                {"contentBlockDelta": {"delta": {"text": "world"}}},
                {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 20}}},
            ]
        }

        chunks = list(generate_answer_stream("question", "context", []))
        text_chunks = [c for c in chunks if isinstance(c, str)]
        usage_chunks = [c for c in chunks if isinstance(c, dict)]
        assert text_chunks == ["Hello ", "world"]
        assert usage_chunks[0]["usage"]["inputTokens"] == 100
        assert usage_chunks[0]["usage"]["outputTokens"] == 20

    def test_generate_answer_stream_haiku_model(self):
        mock_bedrock_client.converse_stream.return_value = {"stream": []}
        list(generate_answer_stream("q", "c", [], advanced=False))
        call_args = mock_bedrock_client.converse_stream.call_args[1]
        assert "haiku" in call_args["modelId"]

    def test_generate_answer_stream_sonnet_model(self):
        mock_bedrock_client.converse_stream.return_value = {"stream": []}
        list(generate_answer_stream("q", "c", [], advanced=True))
        call_args = mock_bedrock_client.converse_stream.call_args[1]
        assert "sonnet" in call_args["modelId"]

    def test_generate_answer_stream_includes_history(self):
        mock_bedrock_client.converse_stream.return_value = {"stream": []}
        history = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
        ]
        list(generate_answer_stream("New question", "context", history))
        call_args = mock_bedrock_client.converse_stream.call_args[1]
        messages = call_args["messages"]
        # History messages + current question
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[0]["content"][0]["text"] == "First question"
        assert messages[1]["role"] == "assistant"

    def test_generate_answer_stream_includes_memory_context(self):
        mock_bedrock_client.converse_stream.return_value = {"stream": []}
        list(generate_answer_stream(
            "question", "context", [],
            memory_context="CONVERSATION HISTORY SUMMARY:\nPrevious discussion..."
        ))
        call_args = mock_bedrock_client.converse_stream.call_args[1]
        user_msg = call_args["messages"][-1]["content"][0]["text"]
        assert "CONVERSATION HISTORY SUMMARY:" in user_msg

    def test_generate_answer_stream_client_error(self):
        mock_bedrock_client.converse_stream.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Model error"}}, "ConverseStream"
        )
        with pytest.raises(ClientError):
            list(generate_answer_stream("q", "c", []))

    def test_generate_answer_stream_limits_history_to_10(self):
        mock_bedrock_client.converse_stream.return_value = {"stream": []}
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"}
            for i in range(20)
        ]
        list(generate_answer_stream("question", "context", history))
        call_args = mock_bedrock_client.converse_stream.call_args[1]
        messages = call_args["messages"]
        # 10 history messages + 1 current question
        assert len(messages) == 11

    def test_generate_answer_stream_skips_empty_history_content(self):
        mock_bedrock_client.converse_stream.return_value = {"stream": []}
        history = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "Answer"},
        ]
        list(generate_answer_stream("question", "context", history))
        call_args = mock_bedrock_client.converse_stream.call_args[1]
        messages = call_args["messages"]
        # Empty content message is skipped, only assistant + current question
        assert len(messages) == 2


# ============================================================================
# handle_chat_stream (Main Orchestration)
# ============================================================================

class TestHandleChatStream:
    """Test the main chat stream orchestration."""

    def _setup_mocks(self, mock_table, sample_embedding, conversation=None):
        """Common setup for handle_chat_stream tests."""
        # Mock DynamoDB table
        mock_dynamodb_resource.Table.return_value = mock_table

        # Mock embedding generation
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

        # Mock S3 Vectors search returning minimal results
        mock_s3vectors_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "doc-001",
                    "distance": 0.9,
                    "metadata": {
                        "content": "Some KB content",
                        "repo": "my-repo",
                        "type": "adr",
                        "file_path": "docs/ADR-001.md",
                        "workspace_id": "ws_test",
                    },
                }
            ]
        }

        # Mock Bedrock streaming response
        mock_bedrock_client.converse_stream.return_value = {
            "stream": [
                {"contentBlockDelta": {"delta": {"text": "The answer is 42."}}},
                {"metadata": {"usage": {"inputTokens": 500, "outputTokens": 50}}},
            ]
        }

        if conversation:
            mock_table.get_item.return_value = {"Item": conversation}
        else:
            mock_table.get_item.return_value = {}

        # Mock query for conversation messages (empty for new conversations)
        mock_table.query.return_value = {"Items": []}

    def test_new_conversation_flow(self, mock_table, sample_embedding):
        """Test full flow for a new conversation."""
        self._setup_mocks(mock_table, sample_embedding)

        event = {
            "body": json.dumps({
                "message": "What are our Lambda standards?",
                "workspace_id": "ws_test",
                "user": {"email": "user@example.com"},
            })
        }

        chunks = list(handle_chat_stream(event))

        # Parse NDJSON chunks
        parsed = [json.loads(c.strip()) for c in chunks]
        types = [p["type"] for p in parsed]

        # Should have metadata, at least one token, and done
        assert "metadata" in types
        assert "token" in types
        assert "done" in types

        # Metadata should include sources and conversation_id
        metadata = next(p for p in parsed if p["type"] == "metadata")
        assert "conversation_id" in metadata
        assert "sources" in metadata
        assert metadata["sources"][0]["repo"] == "my-repo"

        # Routing telemetry should always be present so the UI can render
        # a chip — even when the graph was skipped, the user should see
        # which retrievals fired. With the default stub_symbol_graph_augment
        # fixture, intent is unknown and only RAG fires.
        assert "routing" in metadata
        assert metadata["routing"]["retrievals_fired"] == ["rag"]
        assert metadata["routing"]["intent"] == "unknown"
        assert "graph_sources" in metadata
        assert metadata["graph_sources"] == []

        # Done should include message_id
        done = next(p for p in parsed if p["type"] == "done")
        assert "message_id" in done

    def test_metadata_includes_graph_sources_when_router_fires(
        self, mock_table, sample_embedding
    ):
        """When the symbol-graph augment returns sites + a decision,
        handle_chat_stream surfaces them in metadata.graph_sources and
        the routing chip reflects intent + retrievals_fired.
        """
        self._setup_mocks(mock_table, sample_embedding)

        # Override the autouse stub to simulate the graph firing.
        decision = MagicMock()
        decision.intent = "structural"
        decision.symbol_candidate = "log_audit_entry"
        populated_augment = {
            "decision": decision,
            "graph_block": "## Symbol facts\n2 callers found...",
            "retrievals_fired": ["graph"],
            "graph_sha": "abc123",
            "graph_staleness_seconds": 60,
            "graph_result_count": 2,
            "graph_relation": "callers",
            "graph_sites": [
                {
                    "file_path": "lambda/github-integration/handler.py",
                    "line": 137,
                    "repo": "owner/repo",
                    "source_symbol_id": "python:lambda.github_integration.handler::lambda_handler",
                    "language": "python",
                },
                {
                    "file_path": "lambda/jira-integration/handler.py",
                    "line": 220,
                    "repo": "owner/repo",
                    "source_symbol_id": "python:lambda.jira_integration.handler::lambda_handler",
                    "language": "python",
                },
            ],
        }
        handler_module._augment_context_with_symbol_graph = MagicMock(
            return_value=populated_augment
        )

        event = {
            "body": json.dumps({
                "message": "Which lambdas call log_audit_entry?",
                "workspace_id": "ws_test",
                "user": {"email": "user@example.com"},
            })
        }

        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        metadata = next(p for p in parsed if p["type"] == "metadata")

        # Routing reflects what the augment helper returned.
        assert metadata["routing"]["intent"] == "structural"
        assert metadata["routing"]["symbol_candidate"] == "log_audit_entry"
        assert metadata["routing"]["retrievals_fired"] == ["rag", "graph"]
        assert metadata["routing"]["graph_relation"] == "callers"
        assert metadata["routing"]["graph_result_count"] == 2
        assert metadata["routing"]["graph_sha"] == "abc123"
        assert metadata["routing"]["graph_staleness_seconds"] == 60

        # Graph sites surface in their own sources channel so the UI
        # can render them distinctly from RAG hits.
        assert len(metadata["graph_sources"]) == 2
        assert metadata["graph_sources"][0]["file_path"] == (
            "lambda/github-integration/handler.py"
        )
        assert metadata["graph_sources"][0]["line"] == 137

    def test_existing_conversation_flow(self, mock_table, sample_embedding, sample_conversation):
        """Test flow for an existing conversation."""
        self._setup_mocks(mock_table, sample_embedding, conversation=sample_conversation)

        event = {
            "body": json.dumps({
                "message": "Tell me more about testing",
                "conversation_id": "conv_test123",
            })
        }

        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        metadata = next(p for p in parsed if p["type"] == "metadata")
        assert metadata["conversation_id"] == "conv_test123"

    def test_conversation_not_found(self, mock_table, sample_embedding):
        """Test error when conversation ID does not exist."""
        self._setup_mocks(mock_table, sample_embedding)
        mock_table.get_item.return_value = {}  # Not found

        event = {
            "body": json.dumps({
                "message": "Hello",
                "conversation_id": "nonexistent",
            })
        }

        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        assert parsed[0]["type"] == "error"
        assert "not found" in parsed[0]["error"].lower()

    def test_invalid_request_body(self):
        """Test error on invalid JSON body."""
        event = {"body": "not json"}
        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        assert parsed[0]["type"] == "error"

    def test_missing_message(self):
        """Test error on missing message field."""
        event = {"body": json.dumps({})}
        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        assert parsed[0]["type"] == "error"

    def test_show_cost_included(self, mock_table, sample_embedding):
        """Test that cost info is included when show_cost=True."""
        self._setup_mocks(mock_table, sample_embedding)

        event = {
            "body": json.dumps({
                "message": "What are our standards?",
                "show_cost": True,
            })
        }

        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        done = next(p for p in parsed if p["type"] == "done")
        assert "cost" in done
        assert "input_tokens" in done["cost"]
        assert "output_tokens" in done["cost"]
        assert "cost" in done["cost"]

    def test_show_cost_not_included_by_default(self, mock_table, sample_embedding):
        """Test that cost info is not included by default."""
        self._setup_mocks(mock_table, sample_embedding)

        event = {
            "body": json.dumps({"message": "Hello"})
        }

        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        done = next(p for p in parsed if p["type"] == "done")
        assert "cost" not in done

    def test_qa_test_refusal_trigger(self, mock_table, sample_embedding):
        """Test the QA refusal test trigger (message '1234567890')."""
        self._setup_mocks(mock_table, sample_embedding)

        event = {
            "body": json.dumps({
                "message": "1234567890",
                "user": {"email": "qa@example.com"},
            })
        }

        with patch.object(handler_module, "log_audit_entry") as mock_audit:
            chunks = list(handle_chat_stream(event))

        parsed = [json.loads(c.strip()) for c in chunks]
        types = [p["type"] for p in parsed]

        assert "metadata" in types
        assert "token" in types
        assert "done" in types

        token = next(p for p in parsed if p["type"] == "token")
        assert "TEST_REFUSAL" in token["text"]

        # Bedrock should NOT be called for the refusal
        mock_bedrock_client.converse_stream.assert_not_called()

    def test_bedrock_error_during_streaming(self, mock_table, sample_embedding):
        """Test error handling when Bedrock fails during streaming."""
        self._setup_mocks(mock_table, sample_embedding)
        mock_bedrock_client.converse_stream.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}}, "ConverseStream"
        )

        event = {
            "body": json.dumps({"message": "Hello"})
        }

        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        types = [p["type"] for p in parsed]

        # Should have metadata first, then error
        assert "metadata" in types
        assert "error" in types

    def test_workspace_id_passed_to_search(self, mock_table, sample_embedding):
        """Test that workspace_id is passed through to KB search. ADR-033:
        dual-channel retrieval fires two queries per turn (code-maps lane
        + docs lane), each with the workspace_id constraint AND its own
        type filter $and-composed."""
        self._setup_mocks(mock_table, sample_embedding)

        event = {
            "body": json.dumps({
                "message": "Hello",
                "workspace_ids": ["ws_a", "ws_b"],
            })
        }

        chunks = list(handle_chat_stream(event))

        # Both lanes must apply the workspace constraint. Filters now
        # look like {"$and": [{"workspace_id": {"$in": [...]}}, {"type": {"$in": [...]}}]}
        query_calls = mock_s3vectors_client.query_vectors.call_args_list
        kb_filters = [c[1]["filter"] for c in query_calls if "filter" in c[1]]
        # At least one call carrying the multi-workspace constraint.
        ws_clause = {"workspace_id": {"$in": ["ws_a", "ws_b"]}}
        assert any(
            ws_clause in f.get("$and", [])
            for f in kb_filters
            if "$and" in f
        ), f"workspace constraint missing from dual-channel filters: {kb_filters!r}"

    def test_uses_workspace_from_existing_conversation(
        self, mock_table, sample_embedding, sample_conversation
    ):
        """Test that workspace is loaded from existing conversation when not in request."""
        self._setup_mocks(mock_table, sample_embedding, conversation=sample_conversation)

        event = {
            "body": json.dumps({
                "message": "Follow-up question",
                "conversation_id": "conv_test123",
                # No workspace_id or workspace_ids in request
            })
        }

        chunks = list(handle_chat_stream(event))
        # Should use workspace_id from conversation ("ws_test"). ADR-033:
        # filter is now $and-composed with the dual-channel type lane.
        kb_filters = [
            c[1]["filter"]
            for c in mock_s3vectors_client.query_vectors.call_args_list
            if "filter" in c[1]
        ]
        ws_clause = {"workspace_id": {"$eq": "ws_test"}}
        assert any(
            ws_clause in f.get("$and", [])
            for f in kb_filters
            if "$and" in f
        ), f"workspace constraint missing from dual-channel filters: {kb_filters!r}"

    @patch.object(handler_module, "log_audit_entry")
    def test_audit_log_called(self, mock_audit, mock_table, sample_embedding):
        """Test that audit logging is called after answer generation."""
        self._setup_mocks(mock_table, sample_embedding)

        event = {
            "body": json.dumps({
                "message": "What are our standards?",
                "user": {"email": "user@example.com"},
                "workspace_id": "ws_test",
            })
        }

        chunks = list(handle_chat_stream(event))

        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["lambda_name"] == "chat"
        assert call_kwargs["user_email"] == "user@example.com"
        assert call_kwargs["workspace_id"] == "ws_test"
        assert call_kwargs["input_text"] == "What are our standards?"

    def test_sources_deduplicated(self, mock_table, sample_embedding):
        """Test that duplicate sources are removed."""
        self._setup_mocks(mock_table, sample_embedding)
        # Return duplicate results with same file_path
        mock_s3vectors_client.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "doc-001",
                    "distance": 0.9,
                    "metadata": {
                        "content": "Content 1",
                        "repo": "repo",
                        "type": "adr",
                        "file_path": "docs/ADR-001.md",
                    },
                },
                {
                    "key": "doc-002",
                    "distance": 0.85,
                    "metadata": {
                        "content": "Content 2",
                        "repo": "repo",
                        "type": "adr",
                        "file_path": "docs/ADR-001.md",  # Same path
                    },
                },
            ]
        }

        event = {"body": json.dumps({"message": "test"})}
        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        metadata = next(p for p in parsed if p["type"] == "metadata")
        assert len(metadata["sources"]) == 1


# ============================================================================
# Lambda Handler (REST API)
# ============================================================================

class TestLambdaHandler:
    """Test REST API lambda_handler routing."""

    def test_list_conversations(self, mock_table):
        mock_table.query.return_value = {
            "Items": [
                {
                    "conversation_id": "conv_1",
                    "title": "Test",
                    "updated_at": "2025-01-15T10:00:00+00:00",
                    "SK": "METADATA",
                }
            ]
        }

        event = {
            "httpMethod": "GET",
            "path": "/conversations",
            "headers": {"x-user-email": "user@example.com"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "conversations" in body
        assert "has_more" in body

    def test_list_conversations_filters_by_user(self, mock_table):
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/conversations",
            "headers": {"x-user-email": "user@example.com"},
        }

        lambda_handler(event, None)
        call_args = mock_table.query.call_args[1]
        assert call_args["IndexName"] == "GSI2"
        assert call_args["ExpressionAttributeValues"][":pk"] == "USER#user@example.com"

    def test_list_conversations_no_user(self, mock_table):
        mock_table.scan.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/conversations",
            "headers": {},
        }

        result = lambda_handler(event, None)
        mock_table.scan.assert_called_once()
        body = json.loads(result["body"])
        assert "has_more" in body

    def test_list_conversations_dynamodb_error(self, mock_table):
        mock_table.scan.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "Scan"
        )

        event = {
            "httpMethod": "GET",
            "path": "/conversations",
            "headers": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 500

    def test_get_conversation_with_messages(self, mock_table, sample_conversation, sample_messages):
        mock_table.get_item.return_value = {"Item": sample_conversation}
        mock_table.query.return_value = {"Items": sample_messages}

        event = {
            "httpMethod": "GET",
            "path": "/conversations/conv_test123",
            "headers": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "conversation" in body
        assert "messages" in body

    def test_get_conversation_not_found(self, mock_table):
        mock_table.get_item.return_value = {}

        event = {
            "httpMethod": "GET",
            "path": "/conversations/nonexistent",
            "headers": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_delete_conversation(self, mock_table):
        mock_s3vectors_client.list_vectors.return_value = {"vectors": []}
        mock_table.query.return_value = {
            "Items": [
                {"PK": "CONV#conv_123", "SK": "METADATA"},
                {"PK": "CONV#conv_123", "SK": "MSG#2025-01-15T10:00:00#msg_001"},
            ]
        }
        batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_writer)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        event = {
            "httpMethod": "DELETE",
            "path": "/conversations/conv_123",
            "headers": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == "conv_123"

    def test_delete_conversation_dynamodb_error(self, mock_table):
        mock_s3vectors_client.list_vectors.return_value = {"vectors": []}
        mock_table.query.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "Query"
        )

        event = {
            "httpMethod": "DELETE",
            "path": "/conversations/conv_123",
            "headers": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 500

    def test_unknown_route_returns_404(self):
        event = {
            "httpMethod": "POST",
            "path": "/unknown",
            "headers": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_route_extraction_from_request_context(self, mock_table):
        """Test that routes can be extracted from requestContext (API Gateway v2)."""
        mock_table.scan.return_value = {"Items": []}

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/conversations"}
            },
            "headers": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200


# ============================================================================
# Streaming Handler
# ============================================================================

class TestStreamingHandler:
    """Test the SDK streaming handler entry point."""

    @patch.object(handler_module, "handle_chat_stream")
    def test_streaming_handler_encodes_chunks(self, mock_chat_stream):
        """Test that streaming_handler encodes string chunks to bytes."""
        mock_chat_stream.return_value = iter([
            '{"type":"metadata","conversation_id":"c1"}\n',
            '{"type":"token","text":"Hello"}\n',
            '{"type":"done","message_id":"m1"}\n',
        ])

        chunks = list(streaming_handler({"body": "{}"}, None))
        assert all(isinstance(c, bytes) for c in chunks)
        assert b"metadata" in chunks[0]

    @patch.object(handler_module, "handle_chat_stream")
    def test_streaming_handler_handles_exception(self, mock_chat_stream):
        """Test that streaming_handler catches exceptions and yields error."""
        mock_chat_stream.side_effect = Exception("Unexpected error")

        chunks = list(streaming_handler({"body": "{}"}, None))
        assert len(chunks) >= 1
        # Last chunks should contain error
        all_output = b"".join(chunks)
        assert b"error" in all_output


# ============================================================================
# Memory Chunk Creation Trigger
# ============================================================================

class TestMemoryChunkCreation:
    """Test memory chunk creation trigger during chat flow."""

    def _setup_for_memory_test(self, mock_table, sample_embedding, message_count):
        """Setup mocks for memory chunk trigger test.

        The handler calls get_conversation_messages twice:
        1. history = get_conversation_messages(conversation_id, limit=10) -- before saving
        2. chunk_messages = get_conversation_messages(conversation_id, limit=MEMORY_CHUNK_SIZE + 1)
           -- only when threshold is reached; needs to return >= MEMORY_CHUNK_SIZE + 1 items
           so that after removing the last (just-saved) message, >= MEMORY_CHUNK_SIZE remain.
        """
        mock_dynamodb_resource.Table.return_value = mock_table

        # Conversation with enough messages to trigger memory chunk creation
        conversation = {
            "PK": "CONV#conv_mem",
            "SK": "METADATA",
            "conversation_id": "conv_mem",
            "title": "Memory test",
            "message_count": message_count,
            "memory_chunk_count": 0,
            "created_at": "2025-01-15T10:00:00+00:00",
            "updated_at": "2025-01-15T10:05:00+00:00",
        }
        mock_table.get_item.return_value = {"Item": conversation}

        # Build enough messages for the second query call.
        # The handler fetches with limit=MEMORY_CHUNK_SIZE+1 (11), then drops the last one.
        # We need at least MEMORY_CHUNK_SIZE items remaining after the drop.
        chunk_messages = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"Message {i}",
             "PK": "CONV#conv_mem",
             "SK": f"MSG#2025-01-15T10:{i:02d}:00+00:00#msg_{i:03d}"}
            for i in range(handler_module.MEMORY_CHUNK_SIZE + 1)
        ]

        # First call (history): return last few messages; second call (chunking): return full set
        history_messages = chunk_messages[-10:]
        mock_table.query.side_effect = [
            {"Items": list(reversed(history_messages))},  # ScanIndexForward=False
            {"Items": list(reversed(chunk_messages))},  # ScanIndexForward=False
        ]

        # Embedding mock
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}

        # S3 Vectors search (KB results)
        mock_s3vectors_client.query_vectors.return_value = {"vectors": []}

        # Bedrock streaming
        mock_bedrock_client.converse_stream.return_value = {
            "stream": [
                {"contentBlockDelta": {"delta": {"text": "Answer"}}},
                {"metadata": {"usage": {"inputTokens": 50, "outputTokens": 10}}},
            ]
        }

    @patch.object(handler_module, "log_audit_entry")
    @patch.object(handler_module, "create_conversation_memory_chunk")
    def test_memory_chunk_created_at_threshold(
        self, mock_create_chunk, mock_audit, mock_table, sample_embedding
    ):
        """Test memory chunk is created when message_count reaches MEMORY_CHUNK_SIZE."""
        # message_count = 9 (MEMORY_CHUNK_SIZE - 1), adding one more makes 10
        self._setup_for_memory_test(mock_table, sample_embedding, message_count=9)
        mock_create_chunk.return_value = True

        event = {
            "body": json.dumps({
                "message": "Another question",
                "conversation_id": "conv_mem",
            })
        }

        chunks = list(handle_chat_stream(event))
        mock_create_chunk.assert_called_once()

    @patch.object(handler_module, "log_audit_entry")
    @patch.object(handler_module, "create_conversation_memory_chunk")
    def test_no_memory_chunk_below_threshold(
        self, mock_create_chunk, mock_audit, mock_table, sample_embedding
    ):
        """Test no memory chunk created when below threshold."""
        mock_dynamodb_resource.Table.return_value = mock_table

        # message_count = 3, well below MEMORY_CHUNK_SIZE (10)
        conversation = {
            "PK": "CONV#conv_mem",
            "SK": "METADATA",
            "conversation_id": "conv_mem",
            "title": "Memory test",
            "message_count": 3,
            "memory_chunk_count": 0,
            "created_at": "2025-01-15T10:00:00+00:00",
            "updated_at": "2025-01-15T10:05:00+00:00",
        }
        mock_table.get_item.return_value = {"Item": conversation}
        # Only one query call (history), no chunking query
        mock_table.query.return_value = {"Items": []}

        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": [0.1] * 1024}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
        mock_s3vectors_client.query_vectors.return_value = {"vectors": []}
        mock_bedrock_client.converse_stream.return_value = {
            "stream": [
                {"contentBlockDelta": {"delta": {"text": "Answer"}}},
                {"metadata": {"usage": {"inputTokens": 50, "outputTokens": 10}}},
            ]
        }

        event = {
            "body": json.dumps({
                "message": "Question",
                "conversation_id": "conv_mem",
            })
        }

        chunks = list(handle_chat_stream(event))
        mock_create_chunk.assert_not_called()


# ============================================================================
# Edge Cases and Integration Scenarios
# ============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_body_in_event(self):
        """Test handling of empty body."""
        event = {"body": None}
        chunks = list(handle_chat_stream(event))
        parsed = [json.loads(c.strip()) for c in chunks]
        assert parsed[0]["type"] == "error"

    def test_message_at_max_length(self):
        """Test that a message at max length is accepted."""
        req = ChatMessageRequest(message="x" * 10000)
        assert len(req.message) == 10000

    def test_message_exceeds_max_length(self):
        """Test that a message exceeding max length is rejected."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ChatMessageRequest(message="x" * 10001)

    def test_deprecated_workspace_id_still_works(self, mock_table, sample_embedding):
        """Test that the deprecated workspace_id field still works."""
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({"embedding": sample_embedding}).encode()
        mock_bedrock_client.invoke_model.return_value = {"body": mock_body}
        mock_s3vectors_client.query_vectors.return_value = {"vectors": []}
        mock_bedrock_client.converse_stream.return_value = {
            "stream": [
                {"contentBlockDelta": {"delta": {"text": "OK"}}},
                {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
            ]
        }
        mock_table.query.return_value = {"Items": []}

        event = {
            "body": json.dumps({
                "message": "Hello",
                "workspace_id": "ws_deprecated",  # Old field
            })
        }

        with patch.object(handler_module, "log_audit_entry"):
            chunks = list(handle_chat_stream(event))

        # Should use workspace_id for filtering. ADR-033: dual-channel
        # composes workspace + type filter via $and; at least one of
        # the two lane queries must carry the deprecated workspace_id.
        kb_filters = [
            c[1]["filter"]
            for c in mock_s3vectors_client.query_vectors.call_args_list
            if "filter" in c[1]
        ]
        ws_clause = {"workspace_id": {"$eq": "ws_deprecated"}}
        assert any(
            ws_clause in f.get("$and", [])
            for f in kb_filters
            if "$and" in f
        ), f"workspace constraint missing from dual-channel filters: {kb_filters!r}"

    def test_list_conversations_sorted_by_updated_at(self, mock_table):
        """Test conversations are returned from scan (sorting handled by GSI2 for user queries)."""
        mock_table.scan.return_value = {
            "Items": [
                {"conversation_id": "old", "updated_at": "2025-01-01T00:00:00", "SK": "METADATA"},
                {"conversation_id": "new", "updated_at": "2025-01-15T00:00:00", "SK": "METADATA"},
            ]
        }

        result = list_conversations()
        body = json.loads(result["body"])
        assert len(body["conversations"]) == 2
        assert body["has_more"] is False

    def test_list_conversations_pagination_with_cursor(self, mock_table):
        """Test cursor-based pagination passes ExclusiveStartKey."""
        import base64
        mock_table.query.return_value = {
            "Items": [{"conversation_id": "c1", "title": "Test"}],
            "LastEvaluatedKey": {"PK": "CONV#c1", "SK": "METADATA", "GSI2PK": "USER#u", "GSI2SK": "CONV#ts"},
        }

        cursor_key = {"PK": "CONV#c0", "SK": "METADATA", "GSI2PK": "USER#u", "GSI2SK": "CONV#ts0"}
        cursor = base64.b64encode(json.dumps(cursor_key).encode()).decode()

        event = {
            "httpMethod": "GET",
            "path": "/conversations",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {"limit": "10", "cursor": cursor},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["has_more"] is True
        assert "next_cursor" in body
        # Verify ExclusiveStartKey was passed
        call_args = mock_table.query.call_args[1]
        assert call_args["ExclusiveStartKey"] == cursor_key

    def test_list_conversations_invalid_cursor(self, mock_table):
        """Test invalid cursor returns 400."""
        event = {
            "httpMethod": "GET",
            "path": "/conversations",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {"cursor": "not-valid-base64!!!"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 400

    def test_list_conversations_limit_capped(self, mock_table):
        """Test limit is capped at 100."""
        mock_table.query.return_value = {"Items": []}

        event = {
            "httpMethod": "GET",
            "path": "/conversations",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {"limit": "500"},
        }

        lambda_handler(event, None)
        call_args = mock_table.query.call_args[1]
        assert call_args["Limit"] == 100

    def test_list_conversations_has_more_false_when_no_last_key(self, mock_table):
        """Test has_more is False when no LastEvaluatedKey."""
        mock_table.query.return_value = {
            "Items": [{"conversation_id": "c1"}],
        }

        result = list_conversations(user_id="user@example.com", limit=20)
        body = json.loads(result["body"])
        assert body["has_more"] is False
        assert "next_cursor" not in body

    def test_list_conversations_admin_scan_passes_limit(self, mock_table):
        """Admin (no user_id) scan must pass Limit so it doesn't read past one page."""
        mock_table.scan.return_value = {"Items": []}

        list_conversations(limit=37)
        call_args = mock_table.scan.call_args[1]
        assert call_args["Limit"] == 37

    def test_list_conversations_admin_scan_pagination_with_cursor(self, mock_table):
        """Admin scan accepts a base64 cursor and forwards it as ExclusiveStartKey,
        and returns next_cursor when LastEvaluatedKey is present."""
        import base64
        cursor_key = {"PK": "CONV#cN", "SK": "METADATA"}
        cursor = base64.b64encode(json.dumps(cursor_key).encode()).decode()

        mock_table.scan.return_value = {
            "Items": [{"conversation_id": "cN+1"}],
            "LastEvaluatedKey": {"PK": "CONV#cN+1", "SK": "METADATA"},
        }

        result = list_conversations(limit=20, cursor=cursor)
        call_args = mock_table.scan.call_args[1]
        assert call_args["ExclusiveStartKey"] == cursor_key

        body = json.loads(result["body"])
        assert body["has_more"] is True
        assert "next_cursor" in body

    def test_list_conversations_admin_invalid_cursor(self, mock_table):
        """Admin scan returns 400 on an undecodable cursor (no silent skip)."""
        result = list_conversations(cursor="not-valid-base64!!!")
        assert result["statusCode"] == 400

    def test_delete_conversation_also_deletes_memory(self, mock_table):
        """Test that deleting a conversation also deletes memory chunks."""
        mock_s3vectors_client.list_vectors.return_value = {
            "vectors": [
                {"key": "memory#conv_del#0001"},
            ]
        }
        mock_table.query.return_value = {
            "Items": [{"PK": "CONV#conv_del", "SK": "METADATA"}]
        }
        batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_writer)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        delete_conversation("conv_del")

        # Verify memory deletion was called
        mock_s3vectors_client.list_vectors.assert_called_once()
        mock_s3vectors_client.delete_vectors.assert_called_once()
        delete_call = mock_s3vectors_client.delete_vectors.call_args[1]
        assert "memory#conv_del#0001" in delete_call["keys"]

    @patch("chat_handler.log_admin_action")
    @patch("chat_handler.get_conversation")
    def test_delete_conversation_emits_audit_row(
        self, mock_get_conversation, mock_audit, mock_table,
    ):
        """plan/audit-completeness — delete_conversation emits
        ACTION_CONVERSATION_DELETED with message_count + oldest_message_at."""
        mock_s3vectors_client.list_vectors.return_value = {"vectors": []}
        # Ownership-verification probe returns the conversation row.
        mock_get_conversation.return_value = {
            "conversation_id": "conv_aud",
            "user_id": "brian@example.com",
            "workspace_id": "ws-1",
        }
        # The actual items-to-delete query.
        mock_table.query.return_value = {
            "Items": [
                {"PK": "CONV#conv_aud", "SK": "METADATA"},
                {"PK": "CONV#conv_aud", "SK": "MSG#2026-04-01T10:00:00#m1",
                 "created_at": "2026-04-01T10:00:00+00:00"},
                {"PK": "CONV#conv_aud", "SK": "MSG#2026-05-07T12:00:00#m2",
                 "created_at": "2026-05-07T12:00:00+00:00"},
            ]
        }
        batch_writer = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_writer)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"AUDIT_TABLE": "test-audit"}):
            delete_conversation(
                "conv_aud",
                user_id="brian@example.com",
                audit_ctx={"source_ip": "203.0.113.7", "user_agent": "ua"},
            )

        mock_audit.assert_called_once()
        args, kwargs = mock_audit.call_args
        assert args[1] == "CONVERSATION_DELETED"
        assert kwargs["user_email"] == "brian@example.com"
        assert kwargs["workspace_id"] == "ws-1"
        assert kwargs["details"]["conversation_id"] == "conv_aud"
        assert kwargs["details"]["message_count"] == 2
        assert kwargs["details"]["oldest_message_at"] == "2026-04-01T10:00:00+00:00"
        assert kwargs["source_ip"] == "203.0.113.7"


# ============================================================================
# Source lines for graph-matched symbols (ADR-030 v1.2)
# ============================================================================

_SOURCE_DEF = {
    "workspace_id": "ws_test",
    "symbol": "python:lambda.auth.handler::verify_magic_token",
    "repo": "outcome-ops-ai-assist",
    "file_path": "lambda/auth/handler.py",
    "line": 531,
}
_SOURCE_WINDOW = {
    "symbol": _SOURCE_DEF["symbol"],
    "repo": "owner/outcome-ops-ai-assist",
    "file_path": "lambda/auth/handler.py",
    "ref": "abcdef0123456789",
    "lines": [{"line": 531, "text": "def verify_magic_token(token):"},
              {"line": 532, "text": "    raise AuthError('TOKEN_ALREADY_USED')"}],
    "truncated": False,
}


class TestSourceLinesBlock:
    def test_no_definitions(self):
        assert handler_module._source_lines_block([]) == ""

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("CHAT_SOURCE_LINES_ENABLED", "false")
        with patch.object(handler_module.source_reader, "read_definitions") as rd:
            assert handler_module._source_lines_block([_SOURCE_DEF]) == ""
        rd.assert_not_called()

    def test_read_failure_degrades_to_no_block(self):
        with patch.object(handler_module.source_reader, "read_definitions",
                          side_effect=RuntimeError("boom")):
            assert handler_module._source_lines_block([_SOURCE_DEF]) == ""

    def test_renders_block_and_passes_config(self, monkeypatch):
        monkeypatch.setenv("CHAT_SOURCE_MAX_SYMBOLS", "2")
        monkeypatch.setenv("CHAT_SOURCE_LINES_PER_SYMBOL", "40")
        with patch.object(handler_module.source_reader, "read_definitions",
                          return_value=[_SOURCE_WINDOW, {"symbol": "x", "error": "nope"}]) as rd:
            block = handler_module._source_lines_block([_SOURCE_DEF])
        rd.assert_called_once_with([_SOURCE_DEF], max_definitions=2, max_lines=40)
        assert "TOKEN_ALREADY_USED" in block
        assert "Source could not be read: nope" in block

    def test_chat_stream_puts_source_lines_in_model_context(self, mock_table, sample_embedding):
        TestHandleChatStream._setup_mocks(None, mock_table, sample_embedding)
        augment = {
            "decision": None, "graph_block": "", "retrievals_fired": [],
            "graph_sha": None, "graph_staleness_seconds": None,
            "graph_result_count": None, "graph_definitions": [_SOURCE_DEF],
        }
        original = handler_module._augment_context_with_symbol_graph
        handler_module._augment_context_with_symbol_graph = MagicMock(return_value=augment)
        try:
            with patch.object(handler_module.source_reader, "read_definitions",
                              return_value=[_SOURCE_WINDOW]):
                event = {"body": json.dumps({
                    "message": "What errors does verify_magic_token return?",
                    "workspace_id": "ws_test",
                    "user": {"email": "user@example.com"},
                })}
                list(handle_chat_stream(event))
        finally:
            handler_module._augment_context_with_symbol_graph = original
        sent = json.dumps(mock_bedrock_client.converse_stream.call_args[1])
        assert "SOURCE LINES" in sent
        assert "TOKEN_ALREADY_USED" in sent


class TestCodeMapSourceFallback:
    """Issue #55: when the graph lane found no definitions (the question
    names no symbol), chat reads the functions the top code-map hits
    describe; when it did find some, the fallback is not consulted."""

    @staticmethod
    def _run(mock_table, sample_embedding, graph_definitions):
        TestHandleChatStream._setup_mocks(None, mock_table, sample_embedding)
        augment = {
            "decision": None, "graph_block": "", "retrievals_fired": [],
            "graph_sha": None, "graph_staleness_seconds": None,
            "graph_result_count": None, "graph_definitions": graph_definitions,
        }
        with patch.object(handler_module, "_augment_context_with_symbol_graph",
                          return_value=augment), \
             patch.object(handler_module, "code_map_definitions",
                          return_value=[_SOURCE_DEF]) as cmd, \
             patch.object(handler_module.source_reader, "read_definitions",
                          return_value=[_SOURCE_WINDOW]) as rd:
            event = {"body": json.dumps({
                "message": "How do magic links work?",
                "workspace_id": "ws_test",
                "user": {"email": "user@example.com"},
            })}
            list(handle_chat_stream(event))
        return cmd, rd

    def test_fallback_used_when_graph_found_no_definitions(self, mock_table, sample_embedding):
        cmd, rd = self._run(mock_table, sample_embedding, [])
        cmd.assert_called_once()
        assert cmd.call_args.args[0] == "How do magic links work?"
        assert cmd.call_args.kwargs["max_definitions"] == 3
        assert rd.call_args.args[0] == [_SOURCE_DEF]
        sent = json.dumps(mock_bedrock_client.converse_stream.call_args[1])
        assert "SOURCE LINES" in sent

    def test_fallback_skipped_when_graph_has_definitions(self, mock_table, sample_embedding):
        cmd, rd = self._run(mock_table, sample_embedding, [_SOURCE_DEF])
        cmd.assert_not_called()
        rd.assert_called_once()

    def test_fallback_skipped_by_kill_switch(self, mock_table, sample_embedding, monkeypatch):
        monkeypatch.setenv("CHAT_SOURCE_LINES_ENABLED", "false")
        cmd, rd = self._run(mock_table, sample_embedding, [])
        cmd.assert_not_called()
        rd.assert_not_called()
