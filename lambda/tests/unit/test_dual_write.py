"""Unit tests for ``shared.dual_write``.

Covers the local-write-and-emit path for all six write surfaces, the
single-region fast-path (no bus configured), the emit-failure
swallow-and-log path, and the oversized-payload marker path.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


BUS_NAME = "test-dev-outcome-ops-ai-assist-replication"
THIS_REGION = "us-west-2"
ENV = "dev"
APP = "outcome-ops-ai-assist"


@pytest.fixture(autouse=True)
def _ha_module(monkeypatch):
    """Configure HA env + reload module so module-level globals refresh."""

    monkeypatch.setenv("REPLICATION_EVENT_BUS_NAME", BUS_NAME)
    monkeypatch.setenv("THIS_REGION", THIS_REGION)
    monkeypatch.setenv("AWS_REGION", THIS_REGION)
    monkeypatch.setenv("ENV", ENV)
    monkeypatch.setenv("APP_NAME", APP)

    from shared import dual_write  # type: ignore

    importlib.reload(dual_write)
    dual_write._reset_clients_for_tests()
    yield dual_write
    dual_write._reset_clients_for_tests()


def _stub_ddb_with_table() -> tuple[MagicMock, MagicMock]:
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    return resource, table


def _captured_event_detail(events_stub: MagicMock) -> dict:
    """Pull the first PutEvents call and decode its Detail JSON."""

    events_stub.put_events.assert_called_once()
    call = events_stub.put_events.call_args
    entry = call.kwargs["Entries"][0]
    return json.loads(entry["Detail"])


def test_ddb_put_item_local_write_and_emit(_ha_module):
    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    _ha_module.ddb_put_item("widgets", {"PK": "X", "name": "y"})

    table.put_item.assert_called_once_with(Item={"PK": "X", "name": "y"})
    detail = _captured_event_detail(events)
    assert detail["operation"] == "ddb_put_item"
    assert detail["table"] == "widgets"
    assert detail["args"]["Item"] == {"PK": "X", "name": "y"}
    assert detail["version"] == 1
    assert detail["source_region"] == THIS_REGION


def test_replicate_put_item_emits_without_local_write(_ha_module):
    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    _ha_module.replicate_put_item("widgets", {"PK": "X", "graph_sha": "abc"})

    # Emit-only: no local put/update, but a ddb_put_item event is sent.
    table.put_item.assert_not_called()
    table.update_item.assert_not_called()
    detail = _captured_event_detail(events)
    assert detail["operation"] == "ddb_put_item"
    assert detail["table"] == "widgets"
    assert detail["args"]["Item"] == {"PK": "X", "graph_sha": "abc"}


def test_ddb_update_item_local_write_and_emit(_ha_module):
    resource, table = _stub_ddb_with_table()
    table.update_item.return_value = {"Attributes": {"x": 1}}
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    resp = _ha_module.ddb_update_item(
        "widgets",
        {"PK": "X", "SK": "Y"},
        UpdateExpression="SET #c = :v",
        ExpressionAttributeNames={"#c": "count"},
        ExpressionAttributeValues={":v": 5},
    )

    assert resp == {"Attributes": {"x": 1}}
    table.update_item.assert_called_once()
    detail = _captured_event_detail(events)
    assert detail["operation"] == "ddb_update_item"
    assert detail["key"] == {"PK": "X", "SK": "Y"}
    assert detail["args"]["UpdateExpression"] == "SET #c = :v"


def test_ddb_delete_item_local_write_and_emit(_ha_module):
    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    _ha_module.ddb_delete_item("widgets", {"PK": "X", "SK": "Y"})

    table.delete_item.assert_called_once_with(Key={"PK": "X", "SK": "Y"})
    detail = _captured_event_detail(events)
    assert detail["operation"] == "ddb_delete_item"
    assert detail["key"] == {"PK": "X", "SK": "Y"}


def test_s3_put_object_local_write_and_emit_without_body(_ha_module):
    s3 = MagicMock()
    events = MagicMock()
    _ha_module._set_clients_for_tests(s3=s3, events=events)

    big_body = b"x" * 100_000
    _ha_module.s3_put_object(
        "my-bucket",
        "path/to/file.json",
        big_body,
        ContentType="application/json",
    )

    s3.put_object.assert_called_once_with(
        Bucket="my-bucket",
        Key="path/to/file.json",
        Body=big_body,
        ContentType="application/json",
    )
    detail = _captured_event_detail(events)
    assert detail["operation"] == "s3_put_object"
    assert detail["bucket"] == "my-bucket"
    assert detail["key"] == "path/to/file.json"
    assert "Body" not in detail["args"]
    assert detail["args"]["ContentType"] == "application/json"


def test_s3vectors_put_vectors_local_write_and_emit(_ha_module):
    s3v = MagicMock()
    events = MagicMock()
    _ha_module._set_clients_for_tests(s3vectors=s3v, events=events)

    vectors = [{"key": "vec1", "data": {"float32": [0.1, 0.2]}}]
    _ha_module.s3vectors_put_vectors("vbucket", "vindex", vectors)

    s3v.put_vectors.assert_called_once_with(
        vectorBucketName="vbucket",
        indexName="vindex",
        vectors=vectors,
    )
    detail = _captured_event_detail(events)
    assert detail["operation"] == "s3vectors_put_vectors"
    assert detail["bucket"] == "vbucket"
    assert detail["index"] == "vindex"


def test_no_bus_configured_skips_emit(_ha_module, monkeypatch):
    # Override env from the autouse fixture so the module-level capture
    # of REPLICATION_EVENT_BUS_NAME re-reads empty.
    monkeypatch.delenv("REPLICATION_EVENT_BUS_NAME", raising=False)
    importlib.reload(_ha_module)
    _ha_module._reset_clients_for_tests()

    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    _ha_module.ddb_put_item("widgets", {"PK": "X"})

    table.put_item.assert_called_once()
    events.put_events.assert_not_called()


def test_passive_region_writes_locally_but_does_not_emit(_ha_module, monkeypatch):
    """A passive region must not originate replication. The local write
    still happens (e.g. an ADMIN_MUTATION_BLOCKED_PASSIVE forensic row),
    but no EventBridge event is emitted — that would replicate the row
    back to the active region (the bidirectional-audit hazard ADR-022
    rejects)."""
    import shared.active_region as active_region

    monkeypatch.setattr(active_region, "is_active_region", lambda: False)

    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    _ha_module.ddb_put_item("audit-logs", {"PK": "X", "action": "ADMIN_MUTATION_BLOCKED_PASSIVE"})

    # Local write happened; replication emit suppressed.
    table.put_item.assert_called_once()
    events.put_events.assert_not_called()


def test_active_region_emits(_ha_module, monkeypatch):
    """Symmetry check: when the local region is active, emission proceeds."""
    import shared.active_region as active_region

    monkeypatch.setattr(active_region, "is_active_region", lambda: True)

    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    _ha_module.ddb_put_item("widgets", {"PK": "X"})

    table.put_item.assert_called_once()
    events.put_events.assert_called_once()


def test_emit_failure_does_not_raise(_ha_module):
    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    events.put_events.side_effect = ClientError(
        {"Error": {"Code": "InternalFailure", "Message": "boom"}},
        "PutEvents",
    )
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    # Should NOT raise — local write succeeded.
    _ha_module.ddb_put_item("widgets", {"PK": "X"})

    table.put_item.assert_called_once()
    events.put_events.assert_called_once()


def test_local_write_failure_raises(_ha_module):
    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
        "PutItem",
    )
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    with pytest.raises(ClientError):
        _ha_module.ddb_put_item("widgets", {"PK": "X"})

    events.put_events.assert_not_called()


def test_oversized_payload_sends_marker_event(_ha_module):
    resource, table = _stub_ddb_with_table()
    events = MagicMock()
    _ha_module._set_clients_for_tests(dynamodb=resource, events=events)

    huge_blob = "x" * 300_000  # well over the 240KB limit
    _ha_module.ddb_put_item("widgets", {"PK": "X", "blob": huge_blob})

    table.put_item.assert_called_once()
    detail = _captured_event_detail(events)
    assert detail["oversized"] is True
    assert detail["table"] == "widgets"
    assert detail["operation"] == "ddb_put_item"
    # The actual large blob is NOT in the marker payload.
    assert "args" not in detail or "blob" not in (
        detail.get("args", {}).get("Item", {})
    )
