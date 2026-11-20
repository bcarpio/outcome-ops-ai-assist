# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``shared.notifications``.

moto-backed (ADR-005). ``dual_write`` is pointed at the same moto
resource so writes land in the mocked notifications table; with no
replication bus configured its emit step is a no-op.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


NOTIF_TABLE = "test-notifications"
FAILED_TABLE = "test-notifications-failed-files"

WORKSPACE_ID = "ws_test"
REPO = "owner/test-repo"
SHA_A = "a" * 40
SHA_B = "b" * 40


def _kwargs(**overrides):
    base = {
        "type": "repo_stalled",
        "severity": "warning",
        "title": "Repo stalled",
        "message": "Ingest for owner/test-repo is stalled.",
        "repo_name": REPO,
        "added_by": "user@example.com",
        "reason": "code_map_stalled",
        "target_sha": SHA_A,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _aws_mocks(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("NOTIFICATIONS_TABLE", NOTIF_TABLE)
    monkeypatch.setenv("FAILED_FILES_TABLE", FAILED_TABLE)
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _reset_module_clients():
    from shared import dual_write, notifications

    notifications._reset_clients_for_tests()
    dual_write._reset_clients_for_tests()
    real = boto3.resource("dynamodb", region_name="us-west-2")
    notifications._set_resource_for_tests(real)
    dual_write._set_clients_for_tests(dynamodb=real)
    yield
    notifications._reset_clients_for_tests()
    dual_write._reset_clients_for_tests()


@pytest.fixture
def tables():
    client = boto3.client("dynamodb", region_name="us-west-2")
    for name in (NOTIF_TABLE, FAILED_TABLE):
        client.create_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    resource = boto3.resource("dynamodb", region_name="us-west-2")
    return resource.Table(NOTIF_TABLE), resource.Table(FAILED_TABLE)


def _get_notif(table, nid):
    return table.get_item(Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"NOTIF#{nid}"}).get(
        "Item"
    )


# ---------------------------------------------------------------------------
# notify_workspace
# ---------------------------------------------------------------------------


def test_first_write_creates_row_with_created_at_and_ttl(tables):
    from shared import notifications

    notif_table, _ = tables
    before = int(time.time())
    assert notifications.notify_workspace(WORKSPACE_ID, **_kwargs()) is True

    nid = notifications.notification_id_for(
        WORKSPACE_ID, REPO, SHA_A, "code_map_stalled"
    )
    assert nid.startswith("notif_") and len(nid) == len("notif_") + 16
    item = _get_notif(notif_table, nid)
    assert item is not None
    assert item["notification_id"] == nid
    assert item["workspace_id"] == WORKSPACE_ID
    assert item["type"] == "repo_stalled"
    assert item["severity"] == "warning"
    assert item["repo_name"] == REPO
    assert item["added_by"] == "user@example.com"
    assert item["target_sha"] == SHA_A
    assert item["failed_files"] == []
    assert int(item["failed_files_total"]) == 0
    assert item["created_at"] == item["updated_at"]
    datetime.fromisoformat(item["created_at"])
    expected_ttl = before + 7 * 86400
    assert expected_ttl <= int(item["ttl"]) <= expected_ttl + 5


def test_second_write_same_key_updates_and_keeps_created_at(tables):
    from shared import notifications

    notif_table, _ = tables
    assert notifications.notify_workspace(WORKSPACE_ID, **_kwargs())
    nid = notifications.notification_id_for(
        WORKSPACE_ID, REPO, SHA_A, "code_map_stalled"
    )
    original_created = "2020-01-01T00:00:00+00:00"
    notif_table.update_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"NOTIF#{nid}"},
        UpdateExpression="SET created_at = :c",
        ExpressionAttributeValues={":c": original_created},
    )

    assert notifications.notify_workspace(
        WORKSPACE_ID, **_kwargs(severity="error", message="Still stalled.")
    )

    items = notif_table.scan()["Items"]
    assert len(items) == 1
    item = items[0]
    assert item["severity"] == "error"
    assert item["message"] == "Still stalled."
    assert item["created_at"] == original_created
    assert item["updated_at"] != original_created


def test_different_reason_gives_different_notification_id(tables):
    from shared import notifications

    notif_table, _ = tables
    assert notifications.notify_workspace(WORKSPACE_ID, **_kwargs())
    assert notifications.notify_workspace(
        WORKSPACE_ID, **_kwargs(reason="gkg_stalled")
    )

    a = notifications.notification_id_for(WORKSPACE_ID, REPO, SHA_A, "code_map_stalled")
    b = notifications.notification_id_for(WORKSPACE_ID, REPO, SHA_A, "gkg_stalled")
    assert a != b
    assert {i["notification_id"] for i in notif_table.scan()["Items"]} == {a, b}


def test_failed_files_and_total_round_trip(tables):
    from shared import notifications

    notif_table, _ = tables
    files = [f"src/file_{i}.py" for i in range(20)]
    assert notifications.notify_workspace(
        WORKSPACE_ID, **_kwargs(), failed_files=files, failed_files_total=57
    )
    nid = notifications.notification_id_for(
        WORKSPACE_ID, REPO, SHA_A, "code_map_stalled"
    )
    item = _get_notif(notif_table, nid)
    assert item["failed_files"] == files
    assert int(item["failed_files_total"]) == 57


def test_failed_files_over_limit_are_truncated(tables):
    from shared import notifications

    notif_table, _ = tables
    files = [f"src/file_{i}.py" for i in range(25)]
    assert notifications.notify_workspace(
        WORKSPACE_ID, **_kwargs(), failed_files=files, failed_files_total=25
    )
    item = notif_table.scan()["Items"][0]
    assert item["failed_files"] == files[:20]
    assert int(item["failed_files_total"]) == 25


def test_missing_table_env_returns_false_and_warns_once(tables, monkeypatch, caplog):
    from shared import notifications

    monkeypatch.delenv("NOTIFICATIONS_TABLE")
    with caplog.at_level(logging.WARNING, logger="shared.notifications"):
        assert notifications.notify_workspace(WORKSPACE_ID, **_kwargs()) is False
        assert notifications.notify_workspace(WORKSPACE_ID, **_kwargs()) is False

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "NOTIFICATIONS_TABLE" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert tables[0].scan()["Items"] == []


def test_dynamodb_error_returns_false_and_logs_error(monkeypatch, caplog):
    """No table exists, so the moto UpdateItem raises ResourceNotFound."""

    from shared import notifications

    with caplog.at_level(logging.ERROR, logger="shared.notifications"):
        result = notifications.notify_workspace(WORKSPACE_ID, **_kwargs())

    assert result is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert WORKSPACE_ID in errors[0].getMessage()
    assert REPO in errors[0].getMessage()


# ---------------------------------------------------------------------------
# list_failed_files
# ---------------------------------------------------------------------------


def _put_failed(table, sha, pipeline, path):
    table.put_item(
        Item={
            "PK": f"WS#{WORKSPACE_ID}#GITHUB_REPO#{REPO}",
            "SK": f"FAILED#{sha}#{pipeline}#{path}",
            "file_path": path,
            "target_sha": sha,
            "pipeline": pipeline,
        }
    )


def test_list_failed_files_only_target_sha(tables):
    from shared import notifications

    _, failed = tables
    _put_failed(failed, SHA_A, "code_map_units", "a.py")
    _put_failed(failed, SHA_A, "gkg", "b.py")
    _put_failed(failed, SHA_B, "gkg", "other.py")

    paths, total = notifications.list_failed_files(WORKSPACE_ID, REPO, SHA_A)
    assert sorted(paths) == ["a.py", "b.py"]
    assert total == 2


def test_list_failed_files_limit_and_total(tables):
    from shared import notifications

    _, failed = tables
    for i in range(30):
        _put_failed(failed, SHA_A, "code_map_units", f"src/f{i:02d}.py")
    # Same path failing in a second pipeline counts once.
    _put_failed(failed, SHA_A, "gkg", "src/f00.py")

    paths, total = notifications.list_failed_files(WORKSPACE_ID, REPO, SHA_A)
    assert len(paths) == 20
    assert len(set(paths)) == 20
    assert total == 30

    paths, total = notifications.list_failed_files(
        WORKSPACE_ID, REPO, SHA_A, limit=5
    )
    assert len(paths) == 5
    assert total == 30


def test_list_failed_files_empty(tables):
    from shared import notifications

    assert notifications.list_failed_files(WORKSPACE_ID, REPO, SHA_A) == ([], 0)


class _PagingTable:
    """Wraps a real moto Table and forces small pages on query()."""

    def __init__(self, table, page_size):
        self._table = table
        self._page_size = page_size
        self.calls = 0

    def query(self, **kwargs):
        self.calls += 1
        return self._table.query(Limit=self._page_size, **kwargs)


class _PagingResource:
    def __init__(self, resource, page_size):
        self._resource = resource
        self._page_size = page_size
        self.tables: list[_PagingTable] = []

    def Table(self, name):  # noqa: N802 - mirrors boto3 resource API
        wrapped = _PagingTable(self._resource.Table(name), self._page_size)
        self.tables.append(wrapped)
        return wrapped


def test_list_failed_files_paginates_two_pages(tables):
    from shared import notifications

    _, failed = tables
    for i in range(5):
        _put_failed(failed, SHA_A, "code_map_units", f"f{i}.py")

    paging = _PagingResource(boto3.resource("dynamodb", region_name="us-west-2"), 3)
    notifications._set_resource_for_tests(paging)

    paths, total = notifications.list_failed_files(WORKSPACE_ID, REPO, SHA_A)
    assert total == 5
    assert sorted(paths) == [f"f{i}.py" for i in range(5)]
    assert paging.tables[0].calls == 2
