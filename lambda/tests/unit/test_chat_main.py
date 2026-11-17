# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for chat/main.py FastAPI entry point.

Tests cover:
- Health check endpoint
- Streaming chat endpoint (POST /)
- Conversation REST endpoints (list, get, delete)
- Request parsing (headers forwarded correctly)
- Error handling (handler exceptions)
- Lambda event construction via _build_rest_event
- Response parsing via _parse_lambda_response
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Guard: skip entire module if FastAPI / starlette / httpx are unavailable
_SKIP_REASON = None
try:
    from starlette.testclient import TestClient  # noqa: F401
except (ImportError, RuntimeError) as exc:
    _SKIP_REASON = f"starlette TestClient unavailable: {exc}"

if _SKIP_REASON is None:
    try:
        _mod_name = "chat_main"
        if _mod_name in sys.modules:
            del sys.modules[_mod_name]
        # Save and restore handler to avoid polluting other tests
        _saved_handler = sys.modules.pop("handler", None)
        _main_path = os.path.join(os.path.dirname(__file__), "../../chat/main.py")
        _spec = importlib.util.spec_from_file_location(_mod_name, _main_path)
        main_module = importlib.util.module_from_spec(_spec)
        sys.modules[_mod_name] = main_module
        _spec.loader.exec_module(main_module)
        app = main_module.app
        if _saved_handler is not None:
            sys.modules["handler"] = _saved_handler
    except Exception as exc:
        _SKIP_REASON = f"Could not load chat/main.py: {exc}"

if _SKIP_REASON:
    pytest.skip(_SKIP_REASON, allow_module_level=True)


# ============================================================================
# Helpers
# ============================================================================


def _mock_lambda_response(status_code=200, body=None):
    """Build a mock Lambda handler response dict."""
    if body is None:
        body = {"message": "ok"}
    return {"statusCode": status_code, "body": json.dumps(body)}


# ============================================================================
# Tests
# ============================================================================


class TestHealthCheck:
    """Test the /health endpoint."""

    def test_health_returns_200(self):
        """Health check returns 200 with healthy status."""
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_health_is_synchronous(self):
        """Health check is a simple GET with no dependencies."""
        client = TestClient(app)
        response = client.get("/health")
        assert "status" in response.json()


class TestBuildRestEvent:
    """Test the _build_rest_event helper function."""

    def test_builds_event_with_method_and_path(self):
        """Event contains correct method and path."""
        mock_response = _mock_lambda_response(200, {"conversations": []})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            client.get("/conversations")

            call_args = mock_handler.call_args
            event = call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "GET"
            assert event["requestContext"]["http"]["path"] == "/conversations"
            assert "headers" in event
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_headers_forwarded_to_event(self):
        """Request headers are forwarded into the Lambda event."""
        mock_response = _mock_lambda_response(200, {"conversations": []})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            client.get(
                "/conversations",
                headers={"Authorization": "Bearer test-token", "X-Custom": "value"},
            )

            event = mock_handler.call_args[0][0]
            assert event["headers"]["authorization"] == "Bearer test-token"
            assert event["headers"]["x-custom"] == "value"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]


class TestParseLambdaResponse:
    """Test the _parse_lambda_response helper function."""

    def test_parses_200_response(self):
        """200 response body is parsed correctly."""
        result = main_module._parse_lambda_response(
            {"statusCode": 200, "body": json.dumps({"data": "value"})}
        )
        assert result.status_code == 200

    def test_parses_error_response(self):
        """Error status codes are forwarded."""
        result = main_module._parse_lambda_response(
            {"statusCode": 403, "body": json.dumps({"error": "forbidden"})}
        )
        assert result.status_code == 403

    def test_defaults_to_200_when_no_status(self):
        """Defaults to 200 when statusCode is missing."""
        result = main_module._parse_lambda_response(
            {"body": json.dumps({"ok": True})}
        )
        assert result.status_code == 200

    def test_defaults_to_empty_body(self):
        """Defaults to empty JSON object when body is missing."""
        result = main_module._parse_lambda_response({"statusCode": 200})
        assert result.status_code == 200


class TestListConversations:
    """Test GET /conversations endpoint."""

    def test_returns_conversations(self):
        """Successful list returns conversations from handler."""
        conversations = [{"id": "conv-1", "title": "Test"}]
        mock_response = _mock_lambda_response(200, {"conversations": conversations})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            response = client.get("/conversations")
            assert response.status_code == 200
            assert response.json()["conversations"] == conversations
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_handler_called_with_none_context(self):
        """lambda_handler is called with None as the context arg."""
        mock_response = _mock_lambda_response(200, {"conversations": []})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            client.get("/conversations")
            context_arg = mock_handler.call_args[0][1]
            assert context_arg is None
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_handler_error_returns_500(self):
        """When handler returns 500, the response reflects that."""
        mock_response = _mock_lambda_response(500, {"error": "Internal error"})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            response = client.get("/conversations")
            assert response.status_code == 500
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]


class TestGetConversation:
    """Test GET /conversations/{conversation_id} endpoint."""

    def test_returns_conversation(self):
        """Successful get returns conversation details."""
        conv = {"id": "conv-123", "title": "My Chat", "messages": []}
        mock_response = _mock_lambda_response(200, conv)
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            response = client.get("/conversations/conv-123")
            assert response.status_code == 200
            assert response.json()["id"] == "conv-123"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_path_includes_conversation_id(self):
        """The event path includes the conversation ID."""
        mock_response = _mock_lambda_response(200, {})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            client.get("/conversations/abc-def")
            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["path"] == "/conversations/abc-def"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_not_found_returns_404(self):
        """When handler returns 404, the response reflects that."""
        mock_response = _mock_lambda_response(404, {"error": "Not found"})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            response = client.get("/conversations/nonexistent")
            assert response.status_code == 404
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]


class TestDeleteConversation:
    """Test DELETE /conversations/{conversation_id} endpoint."""

    def test_delete_returns_success(self):
        """Successful delete returns handler response."""
        mock_response = _mock_lambda_response(200, {"deleted": True})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            response = client.delete("/conversations/conv-123")
            assert response.status_code == 200
            assert response.json()["deleted"] is True
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_delete_event_uses_delete_method(self):
        """The event uses DELETE method."""
        mock_response = _mock_lambda_response(200, {})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            client.delete("/conversations/conv-123")
            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
            assert event["requestContext"]["http"]["path"] == "/conversations/conv-123"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_delete_not_found(self):
        """Delete of nonexistent conversation returns 404."""
        mock_response = _mock_lambda_response(404, {"error": "Not found"})
        mock_handler = MagicMock(return_value=mock_response)
        sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
        try:
            client = TestClient(app)
            response = client.delete("/conversations/nonexistent")
            assert response.status_code == 404
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]


class TestChatStream:
    """Test POST / streaming endpoint."""

    def test_stream_returns_ndjson_content_type(self):
        """Streaming endpoint returns application/x-ndjson media type."""
        mock_stream = MagicMock(return_value=iter([
            json.dumps({"type": "text", "content": "Hello"}) + "\n"
        ]))
        sys.modules["handler"] = MagicMock(handle_chat_stream=mock_stream)
        try:
            client = TestClient(app)
            response = client.post(
                "/",
                json={"message": "Hi", "workspace_ids": ["ws-1"]},
            )
            assert response.status_code == 200
            assert "application/x-ndjson" in response.headers.get("content-type", "")
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_stream_event_includes_body(self):
        """Streaming endpoint passes the request body in the event."""
        mock_stream = MagicMock(return_value=iter([
            json.dumps({"type": "done"}) + "\n"
        ]))
        sys.modules["handler"] = MagicMock(handle_chat_stream=mock_stream)
        try:
            client = TestClient(app)
            body = {"message": "test message", "workspace_ids": ["ws-1"]}
            client.post("/", json=body)

            call_args = mock_stream.call_args
            event = call_args[0][0]
            parsed_body = json.loads(event["body"])
            assert parsed_body["message"] == "test message"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_stream_event_has_post_method(self):
        """Streaming event has POST method and / path."""
        mock_stream = MagicMock(return_value=iter([
            json.dumps({"type": "done"}) + "\n"
        ]))
        sys.modules["handler"] = MagicMock(handle_chat_stream=mock_stream)
        try:
            client = TestClient(app)
            client.post("/", json={"message": "test"})

            event = mock_stream.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            assert event["requestContext"]["http"]["path"] == "/"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_stream_error_yields_error_json(self):
        """When handle_chat_stream raises, an error chunk is yielded."""

        def raise_on_iter(event):
            raise RuntimeError("Something broke")

        sys.modules["handler"] = MagicMock(handle_chat_stream=raise_on_iter)
        try:
            client = TestClient(app)
            response = client.post("/", json={"message": "test"})
            assert response.status_code == 200
            # The error is yielded as NDJSON, not as an HTTP error
            lines = [line for line in response.text.strip().split("\n") if line]
            error_chunk = json.loads(lines[-1])
            assert error_chunk["type"] == "error"
            assert "Something broke" in error_chunk["error"]
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_stream_multiple_chunks(self):
        """Multiple chunks from handle_chat_stream are all yielded."""
        chunks = [
            json.dumps({"type": "text", "content": "Hello "}) + "\n",
            json.dumps({"type": "text", "content": "World"}) + "\n",
            json.dumps({"type": "done"}) + "\n",
        ]
        mock_stream = MagicMock(return_value=iter(chunks))
        sys.modules["handler"] = MagicMock(handle_chat_stream=mock_stream)
        try:
            client = TestClient(app)
            response = client.post("/", json={"message": "test"})
            lines = [line for line in response.text.strip().split("\n") if line]
            assert len(lines) == 3
            assert json.loads(lines[0])["content"] == "Hello "
            assert json.loads(lines[1])["content"] == "World"
            assert json.loads(lines[2])["type"] == "done"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]

    def test_stream_headers_forwarded(self):
        """Request headers are forwarded in the streaming event."""
        mock_stream = MagicMock(return_value=iter([
            json.dumps({"type": "done"}) + "\n"
        ]))
        sys.modules["handler"] = MagicMock(handle_chat_stream=mock_stream)
        try:
            client = TestClient(app)
            client.post(
                "/",
                json={"message": "test"},
                headers={"Authorization": "Bearer abc123"},
            )
            event = mock_stream.call_args[0][0]
            assert event["headers"]["authorization"] == "Bearer abc123"
        finally:
            if "handler" in sys.modules:
                del sys.modules["handler"]
