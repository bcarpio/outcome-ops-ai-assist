# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``shared.audit_writer.log_routing_decision``.

Covers:

- Happy path: a fully-populated routing-decision call lands a single
  DynamoDB ``put_item`` row with the expected admin-action shape +
  the routing-decision details payload (``intent``, ``retrievals_fired``,
  ``graph_*``, ``rag_chunk_count``, ``manifest_hit_count``,
  ``question_hash``).
- PII discipline: the helper never stores the plaintext question; it
  hashes via SHA256 and the resulting digest appears under
  ``details.question_hash``. ``_hash_question`` is exercised directly
  to confirm it returns a hex digest, not the input.
- OCSF mapping: the row uses ``action="CHAT_ROUTING_DECISION"`` so
  the audit-stream-publisher's mapper resolves to
  ``CLASS_APPLICATION_LIFECYCLE`` (6002) with the routing-decision
  ``activity_id`` (99) -> ``type_uid = 600299``.
- Optional-args handling: when graph / rag / manifest counts are
  ``None`` they're omitted from ``details`` rather than serialized as
  literal nulls.
- Fire-and-forget exception path: a DDB ``put_item`` failure is
  swallowed and the function returns ``None`` without raising.
- Empty table-name short-circuits without a put_item attempt.

Test isolation mirrors ``test_shared_audit_writer.py`` — the module
is loaded via ``importlib`` and its ``_dynamodb_resource`` is pinned
to a MagicMock so no boto3 / moto plumbing is required (the helper
is a pure DDB ``put_item`` wrapper).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Module loading -- same shape as test_shared_audit_writer.py so the two
# files don't fight over sys.modules pinning.
# ---------------------------------------------------------------------------

mock_dynamodb_resource = MagicMock()


def _load_audit_writer_module():
    if "shared_audit_writer" in sys.modules:
        del sys.modules["shared_audit_writer"]

    shared_dir = os.path.join(os.path.dirname(__file__), "../../shared")
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)

    audit_path = os.path.join(shared_dir, "audit_writer.py")
    spec = importlib.util.spec_from_file_location(
        "shared_audit_writer", audit_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["shared_audit_writer"] = module
    spec.loader.exec_module(module)

    module._dynamodb_resource = mock_dynamodb_resource
    return module


audit_writer = _load_audit_writer_module()

log_routing_decision = audit_writer.log_routing_decision
_hash_question = audit_writer._hash_question
ACTION_CHAT_ROUTING_DECISION = audit_writer.ACTION_CHAT_ROUTING_DECISION
HIDDEN_DASHBOARD_ACTIONS = audit_writer.HIDDEN_DASHBOARD_ACTIONS
ADMIN_ACTIONS = audit_writer.ADMIN_ACTIONS
CATEGORY_ADMIN = audit_writer.CATEGORY_ADMIN


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mocks():
    """Reset DynamoDB mocks between tests; pin the mock on dual_write.

    audit_writer routes writes through ``shared.dual_write`` post-HA
    refactor; injecting the mock on dual_write._dynamodb covers the
    swapped-in code path. The legacy audit_writer._dynamodb_resource
    attribute is set too for older tests that touch it directly.
    """
    mock_dynamodb_resource.reset_mock()
    audit_writer._dynamodb_resource = mock_dynamodb_resource

    from shared import dual_write  # type: ignore

    dual_write._set_clients_for_tests(dynamodb=mock_dynamodb_resource)
    yield
    dual_write._reset_clients_for_tests()


@pytest.fixture
def mock_table():
    """Mock DynamoDB Table for put_item assertions."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


# ===========================================================================
# _hash_question -- PII discipline
# ===========================================================================


class TestHashQuestion:
    def test_hash_returns_hex_sha256_of_input(self):
        text = "Which Lambda functions call log_audit_entry?"
        digest = _hash_question(text)
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert digest == expected
        # 64-char hex (SHA256).
        assert len(digest) == 64
        # The plaintext question must not be a substring of the hash.
        assert text not in digest
        assert "log_audit_entry" not in digest

    def test_hash_empty_string_is_deterministic(self):
        # Empty input still produces a well-formed hash so the audit
        # row is never malformed (caller can pass _hash_question("")).
        assert _hash_question("") == hashlib.sha256(b"").hexdigest()

    def test_hash_none_input_treated_as_empty(self):
        # Defensive: callers occasionally pass ``None`` for an
        # unparsed question. The helper must not crash.
        assert _hash_question(None) == hashlib.sha256(b"").hexdigest()

    def test_hash_is_stable_across_calls(self):
        text = "Who calls validate_input?"
        assert _hash_question(text) == _hash_question(text)


# ===========================================================================
# log_routing_decision -- happy path
# ===========================================================================


class TestLogRoutingDecisionHappyPath:
    def test_full_payload_lands_in_ddb_item(self, mock_table):
        question = "Which Lambda functions call log_audit_entry?"
        digest = _hash_question(question)

        log_routing_decision(
            audit_table_name="audit-table",
            workspace_id="ws_engineering",
            user_email="brian@example.com",
            question_hash=digest,
            intent="structural",
            retrievals_fired=["rag", "graph"],
            graph_sha="sha-abc123def456",
            graph_staleness_seconds=42,
            graph_result_count=9,
            rag_chunk_count=15,
            manifest_hit_count=0,
            source_ip="203.0.113.42",
            user_agent="Mozilla/5.0",
        )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args.kwargs["Item"]

        # Admin-shape keys land via log_admin_action delegation.
        assert item["PK"] == "WS#ws_engineering"
        assert item["GSI1PK"] == "USER#brian@example.com"
        # fable-scale chunk 04: sharded shape CAT#admin#NN.
        assert item["GSI2PK"].startswith("CAT#admin")
        assert item["category"] == CATEGORY_ADMIN
        assert item["action"] == ACTION_CHAT_ROUTING_DECISION
        assert item["workspace_id"] == "ws_engineering"
        assert item["user_email"] == "brian@example.com"
        assert item["source_ip"] == "203.0.113.42"
        assert item["user_agent"] == "Mozilla/5.0"

        # Routing payload lives entirely under ``details``.
        details = item["details"]
        assert details["question_hash"] == digest
        assert details["intent"] == "structural"
        assert details["retrievals_fired"] == ["rag", "graph"]
        assert details["graph_sha"] == "sha-abc123def456"
        assert details["graph_staleness_seconds"] == 42
        assert details["graph_result_count"] == 9
        assert details["rag_chunk_count"] == 15
        assert details["manifest_hit_count"] == 0

    def test_action_is_in_admin_action_set(self):
        # The audit-query API validates filters against ADMIN_ACTIONS;
        # ``CHAT_ROUTING_DECISION`` must be in that frozenset so
        # operators can filter ``?action=CHAT_ROUTING_DECISION`` to
        # surface the rows hidden from the dashboard by default.
        assert ACTION_CHAT_ROUTING_DECISION in ADMIN_ACTIONS

    def test_action_is_hidden_from_dashboard_by_default(self):
        # Per-chat-turn volume would dominate the dashboard cards.
        # The forensic value is preserved (rows still land in DDB,
        # ?action= filter surfaces them) but the default view is
        # filtered.
        assert ACTION_CHAT_ROUTING_DECISION in HIDDEN_DASHBOARD_ACTIONS


# ===========================================================================
# log_routing_decision -- PII discipline (question text never stored)
# ===========================================================================


class TestLogRoutingDecisionPii:
    def test_plaintext_question_never_appears_in_item(self, mock_table):
        question = (
            "SSN 123-45-6789 belongs to alice@example.com -- "
            "which Lambda owns this user record?"
        )
        log_routing_decision(
            audit_table_name="audit-table",
            workspace_id="ws1",
            user_email="brian@example.com",
            question_hash=_hash_question(question),
            intent="structural",
            retrievals_fired=["rag", "graph"],
        )

        item = mock_table.put_item.call_args.kwargs["Item"]
        # The full item, serialized, must not leak any PII fragment
        # from the original question.
        flat = repr(item)
        assert "123-45-6789" not in flat
        assert "alice@example.com" not in flat
        # The hash IS in the item.
        assert _hash_question(question) in flat


# ===========================================================================
# log_routing_decision -- optional args omitted from details
# ===========================================================================


class TestLogRoutingDecisionOptional:
    def test_none_optionals_omitted_from_details(self, mock_table):
        # When the graph didn't fire (pattern intent), graph_* and
        # manifest_hit_count come through as None and must not appear
        # in details.
        log_routing_decision(
            audit_table_name="audit-table",
            workspace_id="ws1",
            user_email="brian@example.com",
            question_hash=_hash_question("What's our analytics pattern?"),
            intent="pattern",
            retrievals_fired=["rag"],
            # All optionals default to None.
        )

        details = mock_table.put_item.call_args.kwargs["Item"]["details"]
        # Required fields always present.
        assert details["intent"] == "pattern"
        assert details["retrievals_fired"] == ["rag"]
        assert "question_hash" in details
        # Optionals omitted.
        assert "graph_sha" not in details
        assert "graph_staleness_seconds" not in details
        assert "graph_result_count" not in details
        assert "rag_chunk_count" not in details
        assert "manifest_hit_count" not in details

    def test_zero_counts_preserved_not_dropped(self, mock_table):
        # A literal zero result-count is a meaningful audit finding
        # (the "empty set" disproof case). It must survive into the
        # row, not be elided as falsy.
        log_routing_decision(
            audit_table_name="audit-table",
            workspace_id="ws1",
            user_email="brian@example.com",
            question_hash=_hash_question("Anything"),
            intent="structural",
            retrievals_fired=["rag", "graph"],
            graph_result_count=0,
            rag_chunk_count=0,
            manifest_hit_count=0,
        )

        details = mock_table.put_item.call_args.kwargs["Item"]["details"]
        assert details["graph_result_count"] == 0
        assert details["rag_chunk_count"] == 0
        assert details["manifest_hit_count"] == 0

    def test_empty_retrievals_list_serialized_as_empty_list(self, mock_table):
        log_routing_decision(
            audit_table_name="audit-table",
            workspace_id="ws1",
            user_email="brian@example.com",
            question_hash=_hash_question("Anything"),
            intent="unknown",
            retrievals_fired=[],
        )

        details = mock_table.put_item.call_args.kwargs["Item"]["details"]
        assert details["retrievals_fired"] == []

    def test_none_retrievals_coerced_to_empty_list(self, mock_table):
        # Defensive: a caller passing ``None`` must not crash. The
        # helper coerces to an empty list (matching the
        # ``retrievals_fired or []`` defensive pattern in the impl).
        log_routing_decision(
            audit_table_name="audit-table",
            workspace_id="ws1",
            user_email="brian@example.com",
            question_hash=_hash_question("Anything"),
            intent="unknown",
            retrievals_fired=None,  # type: ignore[arg-type]
        )
        details = mock_table.put_item.call_args.kwargs["Item"]["details"]
        assert details["retrievals_fired"] == []


# ===========================================================================
# log_routing_decision -- fire-and-forget on DDB failure
# ===========================================================================


class TestLogRoutingDecisionFireAndForget:
    def test_ddb_exception_swallowed_returns_none(self, mock_table):
        mock_table.put_item.side_effect = Exception("DDB throttled")
        # Must not raise -- audit-write failure cannot break a chat
        # turn.
        rv = log_routing_decision(
            audit_table_name="audit-table",
            workspace_id="ws1",
            user_email="brian@example.com",
            question_hash=_hash_question("Anything"),
            intent="structural",
            retrievals_fired=["graph"],
        )
        assert rv is None

    def test_empty_audit_table_name_short_circuits(self, mock_table):
        # Test environments pass AUDIT_TABLE="" so the helper must
        # silently no-op without invoking DDB.
        log_routing_decision(
            audit_table_name="",
            workspace_id="ws1",
            user_email="brian@example.com",
            question_hash=_hash_question("Anything"),
            intent="structural",
            retrievals_fired=["graph"],
        )
        mock_table.put_item.assert_not_called()
