# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Region-guard tests for chat handler mutation routes.

After plan/05, chat mutation routes are gated by
`shared.region_guard.guard_active`. The three gates:

- PUT /conversations/{id}/share (lambda_handler)
- DELETE /conversations/{id}    (lambda_handler)
- chat streaming entry point    (handle_chat_stream)

Each route returns / yields a 403 when the local region is passive
and proceeds normally when active. The audit row is emitted by
`guard_active` itself; we only verify the HTTP surface here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loading -- mirrors test_chat_sharing.py
# ---------------------------------------------------------------------------

mock_dynamodb_resource = MagicMock()


def _load_handler_module():
    if "chat_handler_region_guard" in sys.modules:
        del sys.modules["chat_handler_region_guard"]

    chat_dir = os.path.join(os.path.dirname(__file__), "../../chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)

    handler_path = os.path.join(chat_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "chat_handler_region_guard", handler_path
    )
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["chat_handler_region_guard"] = handler_module
    spec.loader.exec_module(handler_module)

    handler_module._dynamodb = mock_dynamodb_resource
    handler_module._chat_table = "test-chat-table"
    handler_module._workspaces_table = "test-workspaces-table"
    return handler_module


handler_module = _load_handler_module()

lambda_handler = handler_module.lambda_handler
handle_chat_stream = handler_module.handle_chat_stream


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _event(method: str, path: str, body: dict | None = None) -> dict:
    return {
        "httpMethod": method,
        "path": path,
        "headers": {"x-user-email": "user@example.com"},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"http": {"method": method, "path": path}},
    }


# ---------------------------------------------------------------------------
# PUT /conversations/{id}/share
# ---------------------------------------------------------------------------


class TestSharePassive:
    def test_passive_returns_403_region_passive(self):
        with patch.object(handler_module, "guard_active") as mock_guard:
            mock_guard.side_effect = handler_module.RegionPassiveError("chat_share_conversation")
            result = lambda_handler(
                _event("PUT", "/conversations/abc/share", {"workspace_ids": ["ws_1"]}),
                None,
            )
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "region_passive"

    def test_active_proceeds_to_handler(self):
        # `handle_share_conversation` is hit only when the gate lets us
        # through; stub it so we don't depend on DDB plumbing.
        with patch.object(handler_module, "guard_active"), patch.object(
            handler_module, "handle_share_conversation", return_value={"statusCode": 200, "body": "{}"}
        ) as mock_share:
            result = lambda_handler(
                _event("PUT", "/conversations/abc/share", {"workspace_ids": ["ws_1"]}),
                None,
            )
        assert result["statusCode"] == 200
        mock_share.assert_called_once()


# ---------------------------------------------------------------------------
# DELETE /conversations/{id}
# ---------------------------------------------------------------------------


class TestDeletePassive:
    def test_passive_returns_403_region_passive(self):
        with patch.object(handler_module, "guard_active") as mock_guard:
            mock_guard.side_effect = handler_module.RegionPassiveError("chat_delete_conversation")
            result = lambda_handler(_event("DELETE", "/conversations/abc"), None)
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "region_passive"

    def test_active_proceeds_to_handler(self):
        with patch.object(handler_module, "guard_active"), patch.object(
            handler_module, "delete_conversation", return_value={"statusCode": 200, "body": "{}"}
        ) as mock_delete:
            result = lambda_handler(_event("DELETE", "/conversations/abc"), None)
        assert result["statusCode"] == 200
        mock_delete.assert_called_once()


# ---------------------------------------------------------------------------
# GET /conversations  (read path; must NOT be gated)
# ---------------------------------------------------------------------------


class TestReadPathsUngated:
    def test_get_conversations_passive_still_succeeds(self):
        # Even if guard_active would raise, the GET route never calls it.
        with patch.object(handler_module, "guard_active") as mock_guard, patch.object(
            handler_module, "list_conversations", return_value={"statusCode": 200, "body": "{}"}
        ) as mock_list:
            mock_guard.side_effect = handler_module.RegionPassiveError("would-not-fire")
            result = lambda_handler(_event("GET", "/conversations"), None)
        assert result["statusCode"] == 200
        mock_guard.assert_not_called()
        mock_list.assert_called_once()

    def test_get_single_conversation_passive_still_succeeds(self):
        with patch.object(handler_module, "guard_active") as mock_guard, patch.object(
            handler_module,
            "get_conversation_with_messages",
            return_value={"statusCode": 200, "body": "{}"},
        ) as mock_get:
            mock_guard.side_effect = handler_module.RegionPassiveError("would-not-fire")
            result = lambda_handler(_event("GET", "/conversations/abc"), None)
        assert result["statusCode"] == 200
        mock_guard.assert_not_called()
        mock_get.assert_called_once()


# ---------------------------------------------------------------------------
# handle_chat_stream (NDJSON yield path)
# ---------------------------------------------------------------------------


class TestStreamPassive:
    def test_stream_passive_yields_region_passive_error(self):
        body = {
            "message": "hi",
            "user": {"email": "user@example.com"},
            "workspace_ids": ["ws_1"],
        }
        event = {
            "body": json.dumps(body),
            "requestContext": {"http": {"sourceIp": "1.2.3.4", "userAgent": "test"}},
        }

        with patch.object(handler_module, "guard_active") as mock_guard:
            mock_guard.side_effect = handler_module.RegionPassiveError("chat_stream")
            chunks = list(handle_chat_stream(event))

        # First chunk is the NDJSON error event. _stream_error embeds the
        # status_code; assert via substring rather than re-parsing the
        # error shape because the function builds a JSON envelope.
        assert chunks
        first = chunks[0]
        assert "403" in first
        assert "region_passive" in first
