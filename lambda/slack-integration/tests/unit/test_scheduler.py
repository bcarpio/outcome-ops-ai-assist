"""Tests for slack-integration scheduler.py."""

import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest


def _reload_module():
    if "scheduler" in sys.modules:
        del sys.modules["scheduler"]
    return importlib.import_module("scheduler")


def _reset_globals(mod):
    mod._dynamodb = None
    mod._sqs_client = None


def _mock_db_with_scan(items):
    table = MagicMock()
    table.scan.return_value = {"Items": items}
    db = MagicMock()
    db.Table.return_value = table
    return db, table


def _conn(connection_id="slack_a", status="success", channels=None, cursors=None):
    return {
        "PK": "WS#ws_1",
        "SK": f"SLACK_CONNECTION#{connection_id}",
        "entity_type": "slack_connection",
        "connection_id": connection_id,
        "workspace_id": "ws_1",
        "org_id": "org_1",
        "team_id": "T01TEST",
        "last_sync_status": status,
        "config": {
            "include_channels": channels if channels is not None else ["C0123"],
            "cursors": cursors or {},
        },
    }


class TestEnabledGate:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("TENANTS_TABLE", "test-tenants")
        monkeypatch.setenv("SLACK_SYNC_QUEUE", "https://sqs/q")
        monkeypatch.setenv("SLACK_ENABLED", "true")
        self.mod = _reload_module()
        _reset_globals(self.mod)

    def test_disabled_returns_zeros_without_scanning(self, monkeypatch):
        monkeypatch.setenv("SLACK_ENABLED", "false")
        db, table = _mock_db_with_scan([])
        with patch.object(self.mod, "_get_dynamodb", return_value=db):
            response = self.mod.handler({}, None)
        body = json.loads(response["body"])
        assert body == {"queued": 0, "skipped": 0, "total": 0}
        table.scan.assert_not_called()

    def test_enabled_proceeds_to_scan(self):
        db, table = _mock_db_with_scan([])
        with patch.object(self.mod, "_get_dynamodb", return_value=db):
            response = self.mod.handler({}, None)
        assert response["statusCode"] == 200
        table.scan.assert_called_once()


class TestQueueing:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setenv("TENANTS_TABLE", "test-tenants")
        monkeypatch.setenv("SLACK_SYNC_QUEUE", "https://sqs/q")
        monkeypatch.setenv("SLACK_ENABLED", "true")
        self.mod = _reload_module()
        _reset_globals(self.mod)

    def test_empty_scan_returns_zeros(self):
        db, _ = _mock_db_with_scan([])
        with patch.object(self.mod, "_get_dynamodb", return_value=db), \
             patch.object(self.mod, "_get_sqs_client", return_value=MagicMock()):
            response = self.mod.handler({}, None)
        body = json.loads(response["body"])
        assert body == {"queued": 0, "skipped": 0, "total": 0}

    def test_eligible_connection_is_queued(self):
        db, _ = _mock_db_with_scan([_conn(status="success")])
        sqs = MagicMock()
        with patch.object(self.mod, "_get_dynamodb", return_value=db), \
             patch.object(self.mod, "_get_sqs_client", return_value=sqs):
            response = self.mod.handler({}, None)
        body = json.loads(response["body"])
        assert body["queued"] == 1
        sqs.send_message.assert_called_once()

    def test_in_progress_connection_is_skipped(self):
        db, _ = _mock_db_with_scan([_conn(status="in_progress")])
        sqs = MagicMock()
        with patch.object(self.mod, "_get_dynamodb", return_value=db), \
             patch.object(self.mod, "_get_sqs_client", return_value=sqs):
            response = self.mod.handler({}, None)
        body = json.loads(response["body"])
        assert body["skipped"] == 1
        sqs.send_message.assert_not_called()

    def test_reauth_required_connection_is_skipped(self):
        db, _ = _mock_db_with_scan([_conn(status="reauth_required")])
        sqs = MagicMock()
        with patch.object(self.mod, "_get_dynamodb", return_value=db), \
             patch.object(self.mod, "_get_sqs_client", return_value=sqs):
            response = self.mod.handler({}, None)
        body = json.loads(response["body"])
        assert body["skipped"] == 1

    def test_unconfigured_connection_is_filtered_out(self):
        db, _ = _mock_db_with_scan([_conn(channels=[])])
        sqs = MagicMock()
        with patch.object(self.mod, "_get_dynamodb", return_value=db), \
             patch.object(self.mod, "_get_sqs_client", return_value=sqs):
            response = self.mod.handler({}, None)
        body = json.loads(response["body"])
        assert body["total"] == 0

    def test_sqs_message_contains_required_fields(self):
        db, _ = _mock_db_with_scan([_conn(cursors={"C0123": "100"})])
        sqs = MagicMock()
        with patch.object(self.mod, "_get_dynamodb", return_value=db), \
             patch.object(self.mod, "_get_sqs_client", return_value=sqs):
            self.mod.handler({}, None)
        body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["connection_id"] == "slack_a"
        assert body["workspace_id"] == "ws_1"
        assert body["org_id"] == "org_1"
        assert body["team_id"] == "T01TEST"
        assert body["cursors"] == {"C0123": "100"}
        assert "C0123" in body["include_channels"]

    def test_sqs_failure_does_not_abort_other_connections(self):
        db, _ = _mock_db_with_scan([
            _conn(connection_id="bad"),
            _conn(connection_id="good"),
        ])
        sqs = MagicMock()
        sqs.send_message.side_effect = [Exception("boom"), None]
        with patch.object(self.mod, "_get_dynamodb", return_value=db), \
             patch.object(self.mod, "_get_sqs_client", return_value=sqs):
            response = self.mod.handler({}, None)
        body = json.loads(response["body"])
        assert body["queued"] == 1
        assert body["total"] == 2
