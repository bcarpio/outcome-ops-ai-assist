# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared/audit_shard (fable-scale chunk 04).

Covers:
- Shard suffix determinism (same row_id -> same shard).
- Sharded vs legacy GSI2PK shape.
- Reader fan-out including legacy key.
- Kill-switch semantics (AUDIT_SHARD_ENABLED=false -> writes go to legacy key,
  reads still cover both shard set + legacy).
- read_by_category cross-shard merge + cursor.
"""
import importlib
import importlib.util
import os
import sys

import boto3
import pytest
from moto import mock_aws


def _load(monkeypatch=None):
    """Load shared.audit_shard (re-reads env at import time)."""
    if monkeypatch:
        # Callers can pass a monkeypatch fixture to inject env before load.
        pass
    if "shared.audit_shard" in sys.modules:
        del sys.modules["shared.audit_shard"]
    # Add lambda/ to sys.path so `import shared.audit_shard` works.
    lambda_root = os.path.join(os.path.dirname(__file__), "../..")
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    return importlib.import_module("shared.audit_shard")


class TestShardSuffix:
    def test_same_row_id_produces_same_shard(self):
        mod = _load()
        assert mod.gsi2_shard_suffix("audit_abc") == mod.gsi2_shard_suffix("audit_abc")

    def test_shard_is_within_range(self):
        mod = _load()
        for i in range(200):
            suffix = mod.gsi2_shard_suffix(f"audit_{i:04x}")
            if not suffix:
                continue
            # Suffix is "#NN" with NN in [00, AUDIT_GSI2_SHARDS).
            shard_num = int(suffix[1:])
            assert 0 <= shard_num < mod.AUDIT_GSI2_SHARDS

    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "false")
        mod = _load()
        assert mod.gsi2_shard_suffix("anything") == ""

    def test_single_shard_returns_empty(self, monkeypatch):
        # Sharding to 1 partition is the pre-shard shape. Keeps legacy write
        # path when the operator drops shards back to 1 (unlikely but valid).
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "1")
        mod = _load()
        assert mod.gsi2_shard_suffix("anything") == ""


class TestGsi2pkForCategory:
    def test_sharded_shape(self, monkeypatch):
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "true")
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()
        pk = mod.gsi2pk_for_category("bedrock", "audit_deadbeef")
        assert pk.startswith("CAT#bedrock#")
        # Suffix format: "CAT#bedrock#NN"
        assert len(pk.split("#")[-1]) == 2

    def test_legacy_shape_when_disabled(self, monkeypatch):
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "false")
        mod = _load()
        assert mod.gsi2pk_for_category("bedrock", "audit_deadbeef") == "CAT#bedrock"

    def test_hash_distributes_across_shards(self, monkeypatch):
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "true")
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()
        seen = set()
        for i in range(500):
            pk = mod.gsi2pk_for_category("bedrock", f"audit_{i:04x}")
            seen.add(pk.split("#")[-1])
        # Expect coverage of most/all 16 shards over 500 random-ish row_ids.
        assert len(seen) >= 12, f"only saw {len(seen)} shards: {sorted(seen)}"


class TestShardsForRead:
    def test_includes_all_shards_plus_legacy(self, monkeypatch):
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()
        shards = mod.gsi2pk_shards_for_read("bedrock")
        assert len(shards) == 17
        assert "CAT#bedrock" in shards  # legacy
        assert "CAT#bedrock#00" in shards
        assert "CAT#bedrock#15" in shards

    def test_can_exclude_legacy(self, monkeypatch):
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()
        shards = mod.gsi2pk_shards_for_read("bedrock", include_legacy=False)
        assert len(shards) == 16
        assert "CAT#bedrock" not in shards


# ---------------------------------------------------------------------------
# read_by_category (moto-backed integration)
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName="audit-test",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI2PK", "AttributeType": "S"},
                {"AttributeName": "GSI2SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI2",
                    "KeySchema": [
                        {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        yield boto3.resource("dynamodb", region_name="us-west-2").Table("audit-test")


def _seed_rows(table, count: int, category: str, mod):
    """Seed `count` rows across the sharded set for `category`."""
    for i in range(count):
        row_id = f"audit_{i:06x}"
        sk = f"2026-07-10T00:00:{i:02d}.000000+00:00#{row_id}"
        table.put_item(Item={
            "PK": "WS#SYSTEM",
            "SK": sk,
            "GSI2PK": mod.gsi2pk_for_category(category, row_id),
            "GSI2SK": sk,
            "category": category,
            "request_id": row_id,
        })


class TestReadByCategory:
    def test_reads_all_shards_and_returns_desc_by_sk(self, audit_ddb, monkeypatch):
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "true")
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()
        _seed_rows(audit_ddb, 20, "bedrock", mod)

        items, cursor = mod.read_by_category(
            audit_ddb,
            category="bedrock",
            sk_start="2026-07-10T00:00:00.000000+00:00",
            sk_end="2026-07-10T00:00:59.999999+00:00~",
            limit=20,
        )
        assert len(items) == 20
        # Desc by GSI2SK.
        sks = [i["GSI2SK"] for i in items]
        assert sks == sorted(sks, reverse=True)
        assert cursor is None  # exactly limit returned; nothing left

    def test_returns_cursor_when_more_rows_exist(self, audit_ddb, monkeypatch):
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "true")
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()
        _seed_rows(audit_ddb, 50, "bedrock", mod)

        items, cursor = mod.read_by_category(
            audit_ddb,
            category="bedrock",
            sk_start="2026-07-10T00:00:00.000000+00:00",
            sk_end="2026-07-10T00:00:59.999999+00:00~",
            limit=10,
        )
        assert len(items) == 10
        assert cursor is not None
        # Cursor is the smallest SK returned.
        assert cursor == items[-1]["GSI2SK"]

    def test_cursor_advances_and_deduplicates_boundary(self, audit_ddb, monkeypatch):
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "true")
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()
        _seed_rows(audit_ddb, 30, "bedrock", mod)

        page1, cursor = mod.read_by_category(
            audit_ddb, category="bedrock",
            sk_start="2026-07-10T00:00:00.000000+00:00",
            sk_end="2026-07-10T00:00:59.999999+00:00~",
            limit=10,
        )
        page2, cursor2 = mod.read_by_category(
            audit_ddb, category="bedrock",
            sk_start="2026-07-10T00:00:00.000000+00:00",
            sk_end="2026-07-10T00:00:59.999999+00:00~",
            limit=10,
            cursor_sk=cursor,
        )
        # No overlap between pages.
        sks1 = {i["GSI2SK"] for i in page1}
        sks2 = {i["GSI2SK"] for i in page2}
        assert sks1.isdisjoint(sks2)
        # Combined pages are still desc-sorted.
        all_sks = [i["GSI2SK"] for i in page1 + page2]
        assert all_sks == sorted(all_sks, reverse=True)

    def test_covers_legacy_rows(self, audit_ddb, monkeypatch):
        # Seed legacy-shaped rows (no shard suffix) alongside sharded ones.
        monkeypatch.setenv("AUDIT_SHARD_ENABLED", "true")
        monkeypatch.setenv("AUDIT_GSI2_SHARDS", "16")
        mod = _load()

        # 3 legacy rows on GSI2PK = "CAT#bedrock"
        for i in range(3):
            sk = f"2026-07-09T00:00:{i:02d}.000000+00:00#legacy_{i}"
            audit_ddb.put_item(Item={
                "PK": "WS#SYSTEM",
                "SK": sk,
                "GSI2PK": "CAT#bedrock",  # legacy shape (no shard)
                "GSI2SK": sk,
                "category": "bedrock",
                "request_id": f"legacy_{i}",
            })
        # 3 sharded rows on 2026-07-10 (newer)
        _seed_rows(audit_ddb, 3, "bedrock", mod)

        items, _ = mod.read_by_category(
            audit_ddb, category="bedrock",
            sk_start="2026-07-09T00:00:00.000000+00:00",
            sk_end="2026-07-10T23:59:59.999999+00:00~",
            limit=20,
        )
        request_ids = {i["request_id"] for i in items}
        # Both legacy and sharded rows appear.
        assert {"legacy_0", "legacy_1", "legacy_2"}.issubset(request_ids)
        assert any(r.startswith("audit_") for r in request_ids)
