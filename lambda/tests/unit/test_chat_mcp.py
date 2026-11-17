# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for MCP tool integration in chat Lambda."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# Pre-mock boto3 clients
mock_dynamodb_resource = MagicMock()
mock_ssm_client = MagicMock()
mock_s3vectors_client = MagicMock()
mock_bedrock_client = MagicMock()
mock_kms_client = MagicMock()


def _load_handler_module():
    """Load the chat handler module with mocked dependencies."""
    if "chat_handler_mcp" in sys.modules:
        del sys.modules["chat_handler_mcp"]

    chat_dir = os.path.join(os.path.dirname(__file__), "../../chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)

    handler_path = os.path.join(chat_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("chat_handler_mcp", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["chat_handler_mcp"] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace cached clients
    handler_module._dynamodb = mock_dynamodb_resource
    handler_module._ssm_client = mock_ssm_client
    handler_module._s3vectors_client = mock_s3vectors_client
    handler_module._bedrock_client = mock_bedrock_client
    handler_module._kms_client = mock_kms_client
    handler_module._chat_table = "test-chat-table"
    handler_module._vector_bucket = "test-vector-bucket"
    handler_module._vector_index = "test-vector-index"
    handler_module._workspaces_table = "test-workspaces-table"

    return handler_module


handler_module = _load_handler_module()

# Import functions
_load_mcp_configs = handler_module._load_mcp_configs
_build_bedrock_tool_config = handler_module._build_bedrock_tool_config
_execute_mcp_tool_call = handler_module._execute_mcp_tool_call
_decrypt_mcp_credentials = handler_module._decrypt_mcp_credentials
generate_answer_stream = handler_module.generate_answer_stream
handle_chat_stream = handler_module.handle_chat_stream
ChatMessageRequest = handler_module.ChatMessageRequest


def _make_mock_response(body_dict):
    """Create a mock HTTP response with the given JSON body."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode()
    resp.headers = MagicMock()
    resp.headers.get.return_value = None
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# Standard init success response for MCP protocol handshake
_INIT_RESPONSE = {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-03-26", "capabilities": {}}}
# Notification response (empty is fine)
_NOTIF_RESPONSE = {"jsonrpc": "2.0"}


def _mock_urlopen_with_init(*tool_responses):
    """Return a side_effect list: init response, notification response, then tool responses."""
    responses = [_make_mock_response(_INIT_RESPONSE), _make_mock_response(_NOTIF_RESPONSE)]
    for r in tool_responses:
        responses.append(_make_mock_response(r) if isinstance(r, dict) else r)
    return responses


class TestMcpToolManifestLoading:
    """Test loading MCP configs from DynamoDB."""

    def test_load_mcp_configs_success(self):
        """Test successfully loading MCP configs."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        def get_item_side_effect(**kwargs):
            key = kwargs["Key"]
            if key["SK"] == "METADATA":
                return {"Item": {
                    "mcp_id": "mcp1", "name": "Test", "url": "https://example.com",
                    "auth_type": "none", "status": "active",
                }}
            if key["SK"] == "TOOLS":
                return {"Item": {
                    "tools": [{"name": "get_data", "description": "Get data", "inputSchema": {"type": "object"}}]
                }}
            return {}
        mock_table.get_item.side_effect = get_item_side_effect

        configs = _load_mcp_configs(["mcp1"])
        assert len(configs) == 1
        assert configs[0]["mcp_id"] == "mcp1"
        assert len(configs[0]["tools"]) == 1

    def test_load_mcp_configs_missing_tools(self):
        """Test that servers without cached tools are skipped."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        def get_item_side_effect(**kwargs):
            key = kwargs["Key"]
            if key["SK"] == "METADATA":
                return {"Item": {"mcp_id": "mcp1", "status": "active"}}
            return {}  # No TOOLS item
        mock_table.get_item.side_effect = get_item_side_effect

        configs = _load_mcp_configs(["mcp1"])
        assert len(configs) == 0

    def test_load_mcp_configs_error_status(self):
        """Test that servers with error status are skipped."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        def get_item_side_effect(**kwargs):
            key = kwargs["Key"]
            if key["SK"] == "METADATA":
                return {"Item": {"mcp_id": "mcp1", "status": "error"}}
            return {}
        mock_table.get_item.side_effect = get_item_side_effect

        configs = _load_mcp_configs(["mcp1"])
        assert len(configs) == 0


class TestBedrockToolConfig:
    """Test building Bedrock toolConfig from MCP tools."""

    def test_build_tool_config_single_server(self):
        """Test building toolConfig from a single MCP server."""
        configs = [{
            "mcp_id": "abc",
            "name": "Test",
            "url": "https://example.com",
            "auth_type": "none",
            "tools": [
                {"name": "get_data", "description": "Get data", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
            ],
        }]

        tool_config, tool_mapping = _build_bedrock_tool_config(configs)

        assert tool_config is not None
        assert len(tool_config["tools"]) == 1
        assert tool_config["tools"][0]["toolSpec"]["name"] == "abc___get_data"
        assert "abc___get_data" in tool_mapping
        assert tool_mapping["abc___get_data"]["original_tool_name"] == "get_data"

    def test_build_tool_config_multiple_servers(self):
        """Test building toolConfig from multiple MCP servers."""
        configs = [
            {"mcp_id": "s1", "name": "S1", "url": "https://s1.com", "auth_type": "none",
             "tools": [{"name": "tool_a", "description": "A", "inputSchema": {"type": "object"}}]},
            {"mcp_id": "s2", "name": "S2", "url": "https://s2.com", "auth_type": "none",
             "tools": [{"name": "tool_b", "description": "B", "inputSchema": {"type": "object"}}]},
        ]

        tool_config, tool_mapping = _build_bedrock_tool_config(configs)

        assert len(tool_config["tools"]) == 2
        assert "s1___tool_a" in tool_mapping
        assert "s2___tool_b" in tool_mapping

    def test_build_tool_config_empty(self):
        """Test building toolConfig with no tools returns None."""
        tool_config, tool_mapping = _build_bedrock_tool_config([])
        assert tool_config is None


class TestMcpToolExecution:
    """Test executing MCP tool calls."""

    def test_execute_tool_call_success(self):
        """Test successfully executing a tool call."""
        mapping_entry = {
            "mcp_id": "abc",
            "original_tool_name": "get_data",
            "server_config": {
                "url": "https://mcp.example.com",
                "auth_type": "none",
                "encrypted_credentials": None,
            },
        }

        tool_resp = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Result data"}]}}
        responses = _mock_urlopen_with_init(tool_resp)

        with patch('urllib.request.urlopen', side_effect=responses):
            result = _execute_mcp_tool_call(mapping_entry, {"id": "123"})

        assert "Result data" in result

    def test_execute_tool_call_timeout(self):
        """Test tool call timeout returns error."""
        import urllib.error
        mapping_entry = {
            "original_tool_name": "get_data",
            "server_config": {"url": "https://mcp.example.com", "auth_type": "none", "encrypted_credentials": None},
        }

        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("timeout")):
            with pytest.raises(Exception, match="Failed to connect"):
                _execute_mcp_tool_call(mapping_entry, {"id": "123"})

    def test_execute_tool_call_bearer_auth(self):
        """Test tool call with bearer auth."""
        import base64
        fake_encrypted = base64.b64encode(b"encrypted-token").decode()
        mapping_entry = {
            "original_tool_name": "get_data",
            "server_config": {
                "url": "https://mcp.example.com",
                "auth_type": "bearer",
                "encrypted_credentials": fake_encrypted,
            },
        }

        mock_kms_client.decrypt.return_value = {"Plaintext": b"my-secret-token"}

        tool_resp = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "OK"}]}}
        responses = _mock_urlopen_with_init(tool_resp)

        with patch('urllib.request.urlopen', side_effect=responses) as mock_urlopen:
            result = _execute_mcp_tool_call(mapping_entry, {})

        # The last call (3rd) is the tool call - check its auth header
        call_args = mock_urlopen.call_args_list[-1]
        request_obj = call_args[0][0]
        assert request_obj.get_header("Authorization") == "Bearer my-secret-token"


class TestChatMessageRequestMcp:
    """Test ChatMessageRequest with MCP fields."""

    def test_mcp_server_ids_accepted(self):
        """Test that mcp_server_ids field is accepted."""
        req = ChatMessageRequest(
            message="Hello",
            mcp_server_ids=["mcp1", "mcp2"],
        )
        assert req.mcp_server_ids == ["mcp1", "mcp2"]

    def test_mcp_server_ids_optional(self):
        """Test that mcp_server_ids is optional."""
        req = ChatMessageRequest(message="Hello")
        assert req.mcp_server_ids is None

    def test_both_workspace_and_mcp(self):
        """Test that both workspace_ids and mcp_server_ids can be set."""
        req = ChatMessageRequest(
            message="Hello",
            workspace_ids=["ws1"],
            mcp_server_ids=["mcp1"],
        )
        assert req.workspace_ids == ["ws1"]
        assert req.mcp_server_ids == ["mcp1"]


def _legacy_converse_stream_to_unified(events):
    """Same bridge as test_chat.py — translate legacy Converse-shape
    stream events into unified StreamEvents that the migrated chat
    handler consumes."""
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
            tu = ev["contentBlockStart"].get("start", {}).get("toolUse")
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
                yield ToolUseArgumentsDelta(tool_use_id=current_tool_id, arguments_partial=partial)
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
            yield Stop(stop_reason=StopReason.END_TURN)
            continue
        if "metadata" in ev:
            usage = ev["metadata"].get("usage", {})
            yield Metadata(usage=Usage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
            ))


@pytest.fixture(autouse=True)
def patch_model_invoke_stream():
    """Bridge mock_bedrock_client.converse_stream programming into the
    new shared.model_client.invoke_stream path."""
    def _shim(**kwargs):
        raw = mock_bedrock_client.converse_stream.return_value or {}
        if isinstance(raw, Exception):
            raise raw
        stream = raw.get("stream", []) if isinstance(raw, dict) else []
        # Translate kwargs back to Converse-shape so tests that assert
        # on call_args["modelId"] / ["toolConfig"] keep working.
        converse_kwargs = {
            "modelId": kwargs.get("model_id"),
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

    with patch.object(handler_module, "model_invoke_stream", side_effect=_shim):
        yield


class TestGenerateAnswerStreamWithTools:
    """Test generate_answer_stream with tool use."""

    def test_no_tool_use_unchanged(self):
        """Test that without tool_config, behavior is unchanged."""
        mock_bedrock_client.converse_stream.return_value = {
            "stream": [
                {"contentBlockDelta": {"delta": {"text": "Hello"}}},
                {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
            ]
        }

        chunks = list(generate_answer_stream("question", "context", []))
        text_chunks = [c for c in chunks if isinstance(c, str)]
        assert "Hello" in "".join(text_chunks)

    def test_tool_config_passed_to_bedrock(self):
        """Test that tool_config is passed to converse_stream."""
        tool_config = {"tools": [{"toolSpec": {"name": "test", "description": "test", "inputSchema": {"json": {"type": "object"}}}}]}

        mock_bedrock_client.converse_stream.return_value = {
            "stream": [
                {"contentBlockDelta": {"delta": {"text": "Answer"}}},
                {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
            ]
        }

        list(generate_answer_stream("q", "ctx", [], tool_config=tool_config, tool_mapping={}))

        call_kwargs = mock_bedrock_client.converse_stream.call_args[1]
        assert "toolConfig" in call_kwargs


class TestMcpServiceErrors:
    """Test AWS service error handling in MCP functions."""

    def test_load_mcp_configs_dynamodb_client_error(self):
        """Test that a ClientError on one mcp_id skips it but loads the other."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        call_count = {"n": 0}

        def get_item_side_effect(**kwargs):
            key = kwargs["Key"]
            pk = key["PK"]
            sk = key["SK"]

            if pk == "MCP#bad_id":
                raise ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "DDB error"}},
                    "GetItem",
                )

            if pk == "MCP#good_id" and sk == "METADATA":
                return {"Item": {
                    "mcp_id": "good_id", "name": "Good", "url": "https://good.com",
                    "auth_type": "none", "status": "active",
                }}
            if pk == "MCP#good_id" and sk == "TOOLS":
                return {"Item": {
                    "tools": [{"name": "tool1", "description": "T1", "inputSchema": {"type": "object"}}]
                }}
            return {}

        mock_table.get_item.side_effect = get_item_side_effect

        configs = _load_mcp_configs(["bad_id", "good_id"])
        assert len(configs) == 1
        assert configs[0]["mcp_id"] == "good_id"

    def test_decrypt_credentials_kms_client_error(self):
        """Test that KMS ClientError is re-raised from _decrypt_mcp_credentials."""
        import base64

        mock_kms_client.decrypt.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "No access"}},
            "Decrypt",
        )

        with pytest.raises(ClientError):
            _decrypt_mcp_credentials(base64.b64encode(b"test").decode())

        # Reset side_effect
        mock_kms_client.decrypt.side_effect = None

    def test_execute_tool_call_kms_error_on_bearer(self):
        """Test that KMS error during bearer auth decrypt raises ClientError."""
        import base64

        mapping_entry = {
            "original_tool_name": "get_data",
            "server_config": {
                "url": "https://mcp.example.com",
                "auth_type": "bearer",
                "encrypted_credentials": base64.b64encode(b"encrypted").decode(),
            },
        }

        mock_kms_client.decrypt.side_effect = ClientError(
            {"Error": {"Code": "KMSInternalException", "Message": "KMS failed"}},
            "Decrypt",
        )

        with pytest.raises(ClientError):
            _execute_mcp_tool_call(mapping_entry, {"id": "123"})

        # Reset side_effect
        mock_kms_client.decrypt.side_effect = None


class TestMcpEdgeCases:
    """Test edge cases for MCP functions."""

    def test_load_mcp_configs_empty_ids(self):
        """Test that passing empty list returns empty list."""
        configs = _load_mcp_configs([])
        assert configs == []

    def test_load_mcp_configs_empty_tools_list(self):
        """Test that a server with an empty tools list is skipped."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        def get_item_side_effect(**kwargs):
            key = kwargs["Key"]
            if key["SK"] == "METADATA":
                return {"Item": {
                    "mcp_id": "mcp1", "name": "Test", "url": "https://example.com",
                    "auth_type": "none", "status": "active",
                }}
            if key["SK"] == "TOOLS":
                return {"Item": {"tools": []}}
            return {}

        mock_table.get_item.side_effect = get_item_side_effect

        configs = _load_mcp_configs(["mcp1"])
        assert len(configs) == 0

    def test_build_tool_config_long_name_truncated(self):
        """Test that prefixed tool names exceeding 64 chars are truncated."""
        long_mcp_id = "a" * 40
        long_tool_name = "b" * 30
        configs = [{
            "mcp_id": long_mcp_id,
            "name": "Long",
            "url": "https://example.com",
            "auth_type": "none",
            "tools": [{"name": long_tool_name, "description": "Long tool", "inputSchema": {"type": "object"}}],
        }]

        tool_config, tool_mapping = _build_bedrock_tool_config(configs)

        tool_name = tool_config["tools"][0]["toolSpec"]["name"]
        assert len(tool_name) == 64
        # The full prefixed name would be 40 + 3 + 30 = 73 chars
        expected_full = f"{long_mcp_id}___{long_tool_name}"
        assert len(expected_full) == 73
        assert tool_name == expected_full[:64]

    def test_build_tool_config_missing_description(self):
        """Test that a tool without description gets 'No description'."""
        configs = [{
            "mcp_id": "abc",
            "name": "Test",
            "url": "https://example.com",
            "auth_type": "none",
            "tools": [{"name": "my_tool", "inputSchema": {"type": "object"}}],
        }]

        tool_config, tool_mapping = _build_bedrock_tool_config(configs)

        assert tool_config["tools"][0]["toolSpec"]["description"] == "No description"

    def test_build_tool_config_missing_input_schema(self):
        """Test that a tool without inputSchema gets a default empty object schema."""
        configs = [{
            "mcp_id": "abc",
            "name": "Test",
            "url": "https://example.com",
            "auth_type": "none",
            "tools": [{"name": "my_tool", "description": "A tool"}],
        }]

        tool_config, tool_mapping = _build_bedrock_tool_config(configs)

        expected_schema = {"type": "object", "properties": {}}
        assert tool_config["tools"][0]["toolSpec"]["inputSchema"]["json"] == expected_schema

    def test_execute_tool_call_no_auth(self):
        """Test tool call with auth_type=none sends no auth headers."""
        mapping_entry = {
            "original_tool_name": "get_data",
            "server_config": {
                "url": "https://mcp.example.com",
                "auth_type": "none",
                "encrypted_credentials": None,
            },
        }

        tool_resp = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "OK"}]}}
        responses = _mock_urlopen_with_init(tool_resp)

        with patch('urllib.request.urlopen', side_effect=responses) as mock_urlopen:
            _execute_mcp_tool_call(mapping_entry, {})

        # Check the tool call request (last call)
        request_obj = mock_urlopen.call_args_list[-1][0][0]
        assert request_obj.get_header("Authorization") is None
        assert request_obj.get_header("X-api-key") is None

    def test_execute_tool_call_api_key_auth(self):
        """Test tool call with api_key auth sets X-API-Key header."""
        import base64

        fake_encrypted = base64.b64encode(b"encrypted-key").decode()
        mapping_entry = {
            "original_tool_name": "get_data",
            "server_config": {
                "url": "https://mcp.example.com",
                "auth_type": "api_key",
                "encrypted_credentials": fake_encrypted,
            },
        }

        mock_kms_client.decrypt.return_value = {"Plaintext": b"my-api-key-123"}

        tool_resp = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "OK"}]}}
        responses = _mock_urlopen_with_init(tool_resp)

        with patch('urllib.request.urlopen', side_effect=responses) as mock_urlopen:
            _execute_mcp_tool_call(mapping_entry, {})

        # Check the tool call request (last call)
        request_obj = mock_urlopen.call_args_list[-1][0][0]
        assert request_obj.get_header("X-api-key") == "my-api-key-123"

    def test_execute_tool_call_malformed_json_response(self):
        """Test that non-JSON response from MCP server raises Exception."""
        mapping_entry = {
            "original_tool_name": "get_data",
            "server_config": {
                "url": "https://mcp.example.com",
                "auth_type": "none",
                "encrypted_credentials": None,
            },
        }

        # Init and notif succeed, tool call returns bad JSON
        bad_resp = MagicMock()
        bad_resp.read.return_value = b"not valid json at all"
        bad_resp.headers = MagicMock()
        bad_resp.headers.get.return_value = None
        bad_resp.__enter__ = MagicMock(return_value=bad_resp)
        bad_resp.__exit__ = MagicMock(return_value=False)
        responses = _mock_urlopen_with_init(bad_resp)

        with patch('urllib.request.urlopen', side_effect=responses):
            with pytest.raises(Exception, match="Invalid JSON response"):
                _execute_mcp_tool_call(mapping_entry, {})

    def test_execute_tool_call_jsonrpc_error(self):
        """Test that JSON-RPC error response returns error text."""
        mapping_entry = {
            "original_tool_name": "get_data",
            "server_config": {
                "url": "https://mcp.example.com",
                "auth_type": "none",
                "encrypted_credentials": None,
            },
        }

        error_resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "bad"}}
        responses = _mock_urlopen_with_init(error_resp)

        with patch('urllib.request.urlopen', side_effect=responses):
            result = _execute_mcp_tool_call(mapping_entry, {})

        assert "bad" in result
        assert "Tool error" in result


class TestMcpChatLogging:
    """Test logging behavior in MCP functions."""

    def test_load_configs_logs_warning_with_prefix(self):
        """Test that missing mcp_id logs a warning with [chat_handler] prefix."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_table.get_item.return_value = {}  # No Item

        with patch.object(handler_module.logger, "warning") as mock_warn:
            _load_mcp_configs(["nonexistent_id"])

        mock_warn.assert_called()
        call_msg = mock_warn.call_args[0][0]
        assert "[chat_handler]" in call_msg

    def test_load_configs_logs_error_with_exc_info(self):
        """Test that ClientError logs error with exc_info=True."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "DDB error"}},
            "GetItem",
        )

        with patch.object(handler_module.logger, "error") as mock_err:
            _load_mcp_configs(["failing_id"])

        mock_err.assert_called()
        call_kwargs = mock_err.call_args[1]
        assert call_kwargs.get("exc_info") is True

        # Reset side_effect
        mock_table.get_item.side_effect = None
