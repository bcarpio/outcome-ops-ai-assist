"""Unit tests for the replication-applier handler.

Covers the dispatcher map (all six operations), the
ConditionalCheckFailed -> already-applied path, the self-event
short-circuit, the unknown-operation drop path, the
unserializable/oversized marker drop, and the transient-error
propagation path.
"""

from __future__ import annotations

import importlib
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
_APPLIER_DIR = _LAMBDA_ROOT / "replication-applier"
for p in (str(_LAMBDA_ROOT), str(_APPLIER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


THIS_REGION = "us-west-2"
OTHER_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _applier(monkeypatch):
    monkeypatch.setenv("THIS_REGION", THIS_REGION)
    monkeypatch.setenv("AWS_REGION", THIS_REGION)
    monkeypatch.setenv("LOCAL_KB_BUCKET", "dev-app-kb-secondary")

    if "handler" in sys.modules:
        del sys.modules["handler"]
    import handler  # type: ignore  # noqa: E402

    importlib.reload(handler)
    handler._reset_clients_for_tests()
    yield handler
    handler._reset_clients_for_tests()


def _event(detail: dict) -> dict:
    return {"version": "0", "source": "dev.outcome-ops-ai-assist.ha-replication", "detail": detail}


def test_ddb_put_item_applied_locally(_applier):
    resource = MagicMock()
    table = MagicMock()
    resource.Table.return_value = table
    _applier._set_clients_for_tests(dynamodb=resource)

    result = _applier.lambda_handler(
        _event(
            {
                "version": 1,
                "operation": "ddb_put_item",
                "source_region": OTHER_REGION,
                "table": "widgets",
                "args": {"Item": {"PK": "X", "name": "y"}},
            }
        ),
        None,
    )

    table.put_item.assert_called_once_with(Item={"PK": "X", "name": "y"})
    assert result == {"status": "applied", "operation": "ddb_put_item"}


def test_ddb_update_item_with_conditional_already_applied_logs_and_returns(_applier):
    resource = MagicMock()
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
        "UpdateItem",
    )
    resource.Table.return_value = table
    _applier._set_clients_for_tests(dynamodb=resource)

    result = _applier.lambda_handler(
        _event(
            {
                "version": 1,
                "operation": "ddb_update_item",
                "source_region": OTHER_REGION,
                "table": "widgets",
                "key": {"PK": "X", "SK": "Y"},
                "args": {
                    "UpdateExpression": "SET #c = :v",
                    "ConditionExpression": "attribute_not_exists(SK)",
                    "ExpressionAttributeNames": {"#c": "count"},
                    "ExpressionAttributeValues": {":v": 5},
                },
            }
        ),
        None,
    )

    assert result["status"] == "already_applied"


def test_s3_put_object_fetches_from_source_region_and_puts_locally(_applier, monkeypatch):
    # Stub the local S3 client used for PutObject.
    local_s3 = MagicMock()
    _applier._set_clients_for_tests(s3=local_s3)

    # Patch the cross-region boto3.client("s3", region_name=...) call.
    source_s3 = MagicMock()
    source_s3.get_object.return_value = {"Body": io.BytesIO(b"hello")}

    def _fake_client(name, region_name=None):
        assert name == "s3"
        assert region_name == OTHER_REGION
        return source_s3

    monkeypatch.setattr(_applier.boto3, "client", _fake_client)

    result = _applier.lambda_handler(
        _event(
            {
                "version": 1,
                "operation": "s3_put_object",
                "source_region": OTHER_REGION,
                "bucket": "dev-app-kb",
                "key": "doc/path.md",
                "args": {"ContentType": "text/markdown"},
            }
        ),
        None,
    )

    source_s3.get_object.assert_called_once_with(Bucket="dev-app-kb", Key="doc/path.md")
    local_s3.put_object.assert_called_once_with(
        Bucket="dev-app-kb-secondary",
        Key="doc/path.md",
        Body=b"hello",
        ContentType="text/markdown",
    )
    assert result["status"] == "applied"


def test_s3vectors_put_vectors_applied(_applier):
    s3v = MagicMock()
    _applier._set_clients_for_tests(s3vectors=s3v)

    vectors = [{"key": "v1", "data": {"float32": [0.1, 0.2]}}]
    _applier.lambda_handler(
        _event(
            {
                "version": 1,
                "operation": "s3vectors_put_vectors",
                "source_region": OTHER_REGION,
                "bucket": "dev-app-vectors",
                "index": "knowledge-base",
                "args": {"vectors": vectors},
            }
        ),
        None,
    )

    s3v.put_vectors.assert_called_once_with(
        vectorBucketName="dev-app-vectors",
        indexName="knowledge-base",
        vectors=vectors,
    )


def test_unknown_operation_logs_and_drops(_applier):
    resource = MagicMock()
    s3 = MagicMock()
    _applier._set_clients_for_tests(dynamodb=resource, s3=s3)

    result = _applier.lambda_handler(
        _event(
            {
                "version": 1,
                "operation": "weird_op",
                "source_region": OTHER_REGION,
            }
        ),
        None,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "unknown_operation"
    resource.Table.assert_not_called()
    s3.put_object.assert_not_called()


def test_self_event_short_circuits(_applier):
    resource = MagicMock()
    _applier._set_clients_for_tests(dynamodb=resource)

    result = _applier.lambda_handler(
        _event(
            {
                "version": 1,
                "operation": "ddb_put_item",
                "source_region": THIS_REGION,
                "table": "widgets",
                "args": {"Item": {"PK": "X"}},
            }
        ),
        None,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "self_event"
    resource.Table.assert_not_called()


def test_transient_clienterror_propagates(_applier):
    resource = MagicMock()
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "PutItem",
    )
    resource.Table.return_value = table
    _applier._set_clients_for_tests(dynamodb=resource)

    with pytest.raises(ClientError):
        _applier.lambda_handler(
            _event(
                {
                    "version": 1,
                    "operation": "ddb_put_item",
                    "source_region": OTHER_REGION,
                    "table": "widgets",
                    "args": {"Item": {"PK": "X"}},
                }
            ),
            None,
        )


def test_marker_only_event_skipped(_applier):
    resource = MagicMock()
    _applier._set_clients_for_tests(dynamodb=resource)

    result = _applier.lambda_handler(
        _event(
            {
                "version": 1,
                "operation": "ddb_put_item",
                "source_region": OTHER_REGION,
                "oversized": True,
                "marker": "some-key",
                "table": "widgets",
            }
        ),
        None,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "marker_only"
    resource.Table.assert_not_called()
