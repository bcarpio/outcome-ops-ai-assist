# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the MCP session store abstraction (fable-scale chunk 02).

Covers the SessionStore contract on both implementations:
  - MemorySessionStore (dict-backed; test/rollback path)
  - DdbSessionStore   (production path with DynamoDB TTL)

The DDB tests use moto to spin an in-process DynamoDB with a real TTL
configuration. The store's ConditionalCheckFailedException handling on
update_field is exercised explicitly — that's the "no-op if session
missing" contract the pre-refactor dict code relied on.
"""

import importlib.util
import os
import sys
import time

import boto3
import pytest
from moto import mock_aws


def _load_session_store():
    if "outcomeops_mcp_session_store" in sys.modules:
        del sys.modules["outcomeops_mcp_session_store"]
    path = os.path.join(
        os.path.dirname(__file__), "../../mcp-server/session_store.py"
    )
    spec = importlib.util.spec_from_file_location(
        "outcomeops_mcp_session_store", path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["outcomeops_mcp_session_store"] = mod
    spec.loader.exec_module(mod)
    return mod


session_store = _load_session_store()


# ---------------------------------------------------------------------------
# MemorySessionStore
# ---------------------------------------------------------------------------


class TestMemorySessionStore:
    def _store(self, user=None, upstream=None):
        return session_store.MemorySessionStore(user, upstream)

    def test_put_then_get_roundtrip(self):
        s = self._store()
        s.put("sess-1", {"email": "u@example.com", "last_active": 100.0})
        got = s.get("sess-1")
        assert got == {"email": "u@example.com", "last_active": 100.0}

    def test_get_missing_returns_none(self):
        assert self._store().get("nope") is None

    def test_update_field_on_existing_session(self):
        s = self._store()
        s.put("sess-1", {"email": "u@example.com", "last_active": 100.0})
        s.update_field("sess-1", "tool_routing", {"foo": "bar"})
        got = s.get("sess-1")
        assert got["tool_routing"] == {"foo": "bar"}
        assert got["email"] == "u@example.com"

    def test_update_field_on_missing_session_is_noop(self):
        s = self._store()
        s.update_field("sess-1", "tool_routing", {"foo": "bar"})
        assert s.get("sess-1") is None

    def test_delete(self):
        s = self._store()
        s.put("sess-1", {"email": "u@example.com", "last_active": 100.0})
        s.delete("sess-1")
        assert s.get("sess-1") is None

    def test_delete_missing_is_noop(self):
        self._store().delete("nope")  # must not raise

    def test_upstream_prefix_routes_to_upstream_bucket(self):
        user_bucket, upstream_bucket = {}, {}
        s = self._store(user_bucket, upstream_bucket)
        s.put("upstream#mcp-abc", {"session_id": "x", "created_at": 1.0})
        assert "mcp-abc" in upstream_bucket
        assert "mcp-abc" not in user_bucket

    def test_user_key_routes_to_user_bucket(self):
        user_bucket, upstream_bucket = {}, {}
        s = self._store(user_bucket, upstream_bucket)
        s.put("sess-1", {"email": "u@example.com", "last_active": 1.0})
        assert "sess-1" in user_bucket
        assert not upstream_bucket

    def test_cleanup_expired_removes_stale_only(self, monkeypatch):
        s = self._store()
        # Two rows, one fresh, one stale.
        now = time.time()
        s.put("fresh", {"email": "a@x", "last_active": now})
        s.put("stale", {"email": "b@x", "last_active": now - 999999})
        removed = s.cleanup_expired()
        assert removed == 1
        assert s.get("fresh") is not None
        assert s.get("stale") is None

    def test_shared_backing_dicts_reflect_writes(self):
        # This is the compat-with-legacy-tests behavior: the module-level
        # _sessions/_mcp_sessions dicts in handler.py must observe writes
        # made through the store, or the seed-and-assert tests break.
        user_bucket, upstream_bucket = {}, {}
        s = self._store(user_bucket, upstream_bucket)
        s.put("sess-1", {"email": "u@example.com", "last_active": 1.0})
        assert user_bucket["sess-1"]["email"] == "u@example.com"


# ---------------------------------------------------------------------------
# DdbSessionStore (moto-backed)
# ---------------------------------------------------------------------------


@pytest.fixture
def ddb_table_name():
    return "mcp-sessions-test"


@pytest.fixture
def ddb_store(ddb_table_name):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=ddb_table_name,
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.update_time_to_live(
            TableName=ddb_table_name,
            TimeToLiveSpecification={
                "AttributeName": "expires_at", "Enabled": True,
            },
        )
        store = session_store.DdbSessionStore(
            ddb_table_name,
            client_factory=lambda: boto3.resource("dynamodb", region_name="us-west-2"),
        )
        yield store


class TestDdbSessionStore:
    def test_put_then_get_roundtrip(self, ddb_store):
        ddb_store.put("sess-1", {"email": "u@example.com", "last_active": 100.0})
        got = ddb_store.get("sess-1")
        # Session shape preserved; DDB-internal PK/expires_at fields stripped.
        assert got == {"email": "u@example.com", "last_active": 100.0}

    def test_put_sets_expires_at_ttl(self, ddb_store, ddb_table_name):
        ddb_store.put("sess-1", {"email": "u@example.com", "last_active": 100.0})
        # Peek at the raw item to confirm the TTL attribute landed.
        client = boto3.client("dynamodb", region_name="us-west-2")
        item = client.get_item(
            TableName=ddb_table_name,
            Key={"PK": {"S": "sess-1"}},
        )["Item"]
        assert "expires_at" in item
        assert int(item["expires_at"]["N"]) > int(time.time())

    def test_get_returns_none_for_missing(self, ddb_store):
        assert ddb_store.get("nope") is None

    def test_get_filters_application_side_when_expires_at_past(self, ddb_store):
        # Force-write a stale row via raw DDB so we can bypass the store's
        # put() which always refreshes the TTL.
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.put_item(
            TableName="mcp-sessions-test",
            Item={
                "PK": {"S": "stale"},
                "email": {"S": "u@example.com"},
                "last_active": {"N": "0"},
                "expires_at": {"N": "1"},  # 1970
            },
        )
        # DDB TTL eviction is best-effort; the store MUST filter itself.
        assert ddb_store.get("stale") is None

    def test_update_field_on_existing_session(self, ddb_store):
        ddb_store.put("sess-1", {"email": "u@example.com", "last_active": 100.0})
        ddb_store.update_field("sess-1", "tool_routing", {"foo": "bar"})
        got = ddb_store.get("sess-1")
        assert got["tool_routing"] == {"foo": "bar"}
        assert got["email"] == "u@example.com"

    def test_update_field_on_missing_session_is_noop(self, ddb_store):
        # No exception; row remains absent.
        ddb_store.update_field("nope", "tool_routing", {"foo": "bar"})
        assert ddb_store.get("nope") is None

    def test_delete_and_reread(self, ddb_store):
        ddb_store.put("sess-1", {"email": "u@example.com", "last_active": 100.0})
        ddb_store.delete("sess-1")
        assert ddb_store.get("sess-1") is None

    def test_cleanup_expired_is_noop_on_ddb_store(self, ddb_store):
        # DDB TTL owns eviction; store's cleanup_expired reports 0.
        assert ddb_store.cleanup_expired() == 0


# ---------------------------------------------------------------------------
# Factory (env-driven)
# ---------------------------------------------------------------------------


class TestBuildSessionStore:
    def test_default_returns_memory_store(self, monkeypatch):
        monkeypatch.delenv("MCP_SESSION_STORE", raising=False)
        store = session_store.build_session_store()
        assert isinstance(store, session_store.MemorySessionStore)

    def test_memory_kind_returns_memory_store(self, monkeypatch):
        monkeypatch.setenv("MCP_SESSION_STORE", "memory")
        store = session_store.build_session_store()
        assert isinstance(store, session_store.MemorySessionStore)

    def test_memory_store_shares_provided_backing_dicts(self, monkeypatch):
        monkeypatch.setenv("MCP_SESSION_STORE", "memory")
        user, upstream = {}, {}
        store = session_store.build_session_store(user, upstream)
        store.put("sess-1", {"email": "u@example.com", "last_active": 1.0})
        assert "sess-1" in user

    def test_ddb_kind_without_table_falls_back_to_memory(self, monkeypatch):
        monkeypatch.setenv("MCP_SESSION_STORE", "ddb")
        monkeypatch.delenv("MCP_SESSIONS_TABLE", raising=False)
        store = session_store.build_session_store()
        assert isinstance(store, session_store.MemorySessionStore)

    def test_ddb_kind_with_table_returns_ddb_store(self, monkeypatch):
        monkeypatch.setenv("MCP_SESSION_STORE", "ddb")
        monkeypatch.setenv("MCP_SESSIONS_TABLE", "some-table")
        store = session_store.build_session_store()
        assert isinstance(store, session_store.DdbSessionStore)
