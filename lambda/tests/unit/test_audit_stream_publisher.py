"""Unit tests for the audit-stream-publisher Lambda."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock


def _load_handler():
    if "audit_stream_publisher_handler" in sys.modules:
        del sys.modules["audit_stream_publisher_handler"]

    handler_path = Path(__file__).resolve().parents[2] / "audit-stream-publisher" / "handler.py"
    # Ensure shared/ resolves at import time. Production zip puts it at
    # task_root/shared/; here we put the real path on sys.path under a 'shared'
    # alias by adding lambda/ to sys.path.
    lambda_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(lambda_root))
    sys.path.insert(0, str(lambda_root / "shared"))

    # Honour an already-set env var (tests that flip it to "" rely on this);
    # default to "test-stream" otherwise.
    os.environ.setdefault("AUDIT_EXPORT_STREAM_NAME", "test-stream")

    spec = importlib.util.spec_from_file_location(
        "audit_stream_publisher_handler", handler_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_stream_publisher_handler"] = module
    spec.loader.exec_module(module)
    return module


def _ddb_str(s):
    return {"S": s}


def _make_record(action="WORKSPACE_CREATED", category="admin", request_id="r-1"):
    return {
        "eventID": f"event-{request_id}",
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "category": _ddb_str(category),
                "action": _ddb_str(action),
                "timestamp": _ddb_str("2026-05-08T10:00:00Z"),
                "user_email": _ddb_str("admin@example.com"),
                "request_id": _ddb_str(request_id),
                "workspace_id": _ddb_str("ws_test"),
                "lambda_name": _ddb_str("workspace-management"),
            }
        },
    }


def test_publishes_records_to_kinesis():
    h = _load_handler()
    mock_kinesis = MagicMock()
    mock_kinesis.put_records.return_value = {"FailedRecordCount": 0, "Records": []}
    h._kinesis = mock_kinesis

    event = {"Records": [_make_record(request_id="r-1"), _make_record(request_id="r-2")]}
    result = h.handler(event, None)

    assert mock_kinesis.put_records.called
    call = mock_kinesis.put_records.call_args
    assert call.kwargs["StreamName"] == "test-stream"
    assert len(call.kwargs["Records"]) == 2
    payload = json.loads(call.kwargs["Records"][0]["Data"].decode("utf-8"))
    assert payload["class_uid"] == 3001  # Account Change
    assert payload["actor"]["user"]["email_addr"] == "admin@example.com"
    assert result["batchItemFailures"] == []


def test_skips_modify_and_remove_events():
    h = _load_handler()
    mock_kinesis = MagicMock()
    h._kinesis = mock_kinesis

    event = {
        "Records": [
            {**_make_record(), "eventName": "MODIFY"},
            {**_make_record(), "eventName": "REMOVE"},
        ]
    }
    result = h.handler(event, None)

    mock_kinesis.put_records.assert_not_called()
    assert result["batchItemFailures"] == []


def test_partial_failure_returns_failed_event_ids():
    h = _load_handler()
    mock_kinesis = MagicMock()
    # First call: 2 records in, record 2 fails.
    # Subsequent retries: 1 record in (just the failed one), still fails.
    mock_kinesis.put_records.side_effect = [
        {
            "FailedRecordCount": 1,
            "Records": [
                {"SequenceNumber": "1"},
                {"ErrorCode": "ProvisionedThroughputExceededException"},
            ],
        },
        {
            "FailedRecordCount": 1,
            "Records": [{"ErrorCode": "ProvisionedThroughputExceededException"}],
        },
        {
            "FailedRecordCount": 1,
            "Records": [{"ErrorCode": "ProvisionedThroughputExceededException"}],
        },
    ]
    h._kinesis = mock_kinesis
    h.RETRY_BASE_DELAY = 0  # don't actually sleep

    rec1 = _make_record(request_id="ok")
    rec2 = _make_record(request_id="fail")
    event = {"Records": [rec1, rec2]}
    result = h.handler(event, None)

    failed_ids = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    assert "event-fail" in failed_ids
    assert "event-ok" not in failed_ids


def test_handles_record_with_no_new_image():
    h = _load_handler()
    mock_kinesis = MagicMock()
    h._kinesis = mock_kinesis

    event = {
        "Records": [
            {"eventID": "x", "eventName": "INSERT", "dynamodb": {}},
        ]
    }
    result = h.handler(event, None)

    mock_kinesis.put_records.assert_not_called()
    assert result["batchItemFailures"] == []


def test_dropping_batch_when_stream_unset(monkeypatch):
    monkeypatch.setenv("AUDIT_EXPORT_STREAM_NAME", "")
    h = _load_handler()
    result = h.handler({"Records": [_make_record()]}, None)
    assert result["batchItemFailures"] == []
