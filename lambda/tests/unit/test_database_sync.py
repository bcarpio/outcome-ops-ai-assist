"""Unit tests for database-sync Lambda (handler + license-layer integration)."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

mock_dynamodb_resource = MagicMock()
mock_sqs_client = MagicMock()


def _load_module(mod_name, filename, directory, also_register_as=None):
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    mod_dir = os.path.join(os.path.dirname(__file__), directory)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)

    mod_path = os.path.join(mod_dir, filename)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    if also_register_as:
        sys.modules[also_register_as] = module
    spec.loader.exec_module(module)
    return module


credential_module = _load_module(
    "db_sync_credential_manager",
    "credential_manager.py",
    "../../database-sync",
    also_register_as="credential_manager",
)
handler_module = _load_module(
    "db_sync_handler", "handler.py", "../../database-sync"
)
handler_module.dynamodb = mock_dynamodb_resource
handler_module.sqs_client = mock_sqs_client


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks(monkeypatch):
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_sqs_client.send_message.side_effect = None
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
    handler_module.DATABASE_SYNC_QUEUE = "https://sqs.example.com/db-sync"
    handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"

    # ADR-019: stub the license layer for default-pass behavior.
    monkeypatch.setattr(
        handler_module,
        "validate_license",
        lambda *a, **kw: {"sub": "test-customer", "license": {"tier": "team"}},
    )

    def _allow(*a, **kw):
        result = MagicMock()
        result.allowed = True
        result.reason = None
        result.current = 0
        result.limit = 100
        return result

    monkeypatch.setattr(handler_module, "check_repo_limit", _allow)
    monkeypatch.setattr(handler_module, "track_repo_active", lambda *a, **kw: True)
    yield


@pytest.fixture
def mock_table():
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_connection():
    return {
        "PK": "WS#ws_test",
        "SK": "DATABASE_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "engine": "postgres",
        "host": "db.example.com",
        "port": 5432,
        "database_name": "analytics",
        "host_hash": "deadbeefcafebabe",
        "display_label": "Test DW",
        "scope_config": {
            "introspection": {"include": ["public.users"], "exclude": []},
            "sampling": {"enabled": False, "include": [], "exclude": []},
            "profiling": {"enabled": True},
        },
        "known_table_ids": [],
        "tables_count": 0,
    }


@pytest.fixture
def mock_credential():
    return {
        "engine": "postgres",
        "username": "u",
        "password": "p",
        "ssl_mode": "require",
    }


# ============================================================================
# sanitize_filename - must match file-ingestion exactly
# ============================================================================


class TestSanitizeFilename:

    def test_strips_unsafe_characters(self):
        assert handler_module.sanitize_filename("a<b>c") == "a_b_c"
        assert handler_module.sanitize_filename('a"b/c\\d|e?f*g:h') == "a_b_c_d_e_f_g_h"

    def test_collapses_whitespace(self):
        assert handler_module.sanitize_filename("hello   world") == "hello_world"

    def test_strips_leading_trailing_dots_underscores(self):
        assert handler_module.sanitize_filename("._abc._") == "abc"

    def test_truncates_to_200(self):
        assert len(handler_module.sanitize_filename("a" * 500)) == 200


# ============================================================================
# strip_top_values - defensive top-values strip on parsed YAML doc
# ============================================================================


class TestStripTopValues:

    def test_strips_top_level(self):
        doc = {"top_values": [1, 2, 3], "name": "foo"}
        handler_module.strip_top_values(doc)
        assert "top_values" not in doc
        assert doc["name"] == "foo"

    def test_strips_nested(self):
        doc = {
            "tables": [
                {
                    "name": "users",
                    "columns": [
                        {"name": "email", "top_values": ["a@x.com", "b@x.com"]},
                        {"name": "id"},
                    ],
                }
            ]
        }
        handler_module.strip_top_values(doc)
        cols = doc["tables"][0]["columns"]
        assert "top_values" not in cols[0]
        assert cols[0]["name"] == "email"  # sibling fields preserved
        assert "top_values" not in cols[1]

    def test_handles_lists_and_dicts(self):
        doc = [
            {"top_values": [1, 2]},
            {"nested": {"top_values": ["x"]}},
        ]
        handler_module.strip_top_values(doc)
        assert doc == [{}, {"nested": {}}]

    def test_handles_scalars_and_none(self):
        # Should not crash on non-container inputs
        assert handler_module.strip_top_values("string") == "string"
        assert handler_module.strip_top_values(42) == 42
        assert handler_module.strip_top_values(None) is None


# ============================================================================
# get_connection / update_connection_status
# ============================================================================


class TestConnectionReadWrite:

    def test_get_connection_returns_item(self, mock_table, sample_connection):
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.get_connection("ws_test", "conn_test")
        assert result == sample_connection
        call_key = mock_table.get_item.call_args[1]["Key"]
        assert call_key["SK"] == "DATABASE_CONNECTION#conn_test"

    def test_get_connection_not_found(self, mock_table):
        mock_table.get_item.return_value = {}
        assert handler_module.get_connection("ws", "c") is None

    def test_get_connection_dynamodb_error(self, mock_table):
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "GetItem"
        )
        assert handler_module.get_connection("ws", "c") is None

    def test_update_status_includes_table_state(self, mock_table):
        handler_module.update_connection_status(
            "ws", "c", "completed",
            tables_count=5,
            known_table_ids=["public.users", "public.orders"],
        )
        call_args = mock_table.update_item.call_args[1]
        expr = call_args["UpdateExpression"]
        assert "tables_count" in expr
        assert "known_table_ids" in expr
        values = call_args["ExpressionAttributeValues"]
        assert values[":kti"] == ["public.users", "public.orders"]


# ============================================================================
# queue_table_ingestion
# ============================================================================


class TestQueueTableIngestion:

    def test_queue_payload_shape(self):
        handler_module.queue_table_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            engine="postgres",
            host_hash="deadbeefcafebabe",
            catalog="analytics",
            schema="public",
            table="users",
            table_yaml="name: users\ncolumns:\n  - name: id\n",
            sync_id="sync_x",
        )
        sent = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        assert sent["source"] == "database"
        assert sent["workspace_id"] == "ws_test"
        assert sent["connection_id"] == "conn_test"
        f = sent["file"]
        assert f["engine"] == "postgres"
        assert f["host_hash"] == "deadbeefcafebabe"
        assert f["catalog"] == "analytics"
        assert f["schema"] == "public"
        assert f["table"] == "users"
        # table_id is sanitized form of "catalog.schema.table"
        assert f["table_id"] == "analytics.public.users"
        assert "name: users" in f["yaml_content"]
        # FIFO ordering by workspace+connection
        assert mock_sqs_client.send_message.call_args[1]["MessageGroupId"] == "ws_test-conn_test"

    def test_queue_swallows_sqs_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "SendMessage"
        )
        # Must not raise
        handler_module.queue_table_ingestion(
            workspace_id="ws", connection_id="c",
            engine="postgres", host_hash="h",
            catalog="cat", schema="s", table="t",
            table_yaml="x", sync_id="sx",
        )


# ============================================================================
# process_sync_request - happy paths and gating
# ============================================================================


class TestProcessSyncRequest:

    def test_no_connection_returns_error(self, mock_table):
        mock_table.get_item.return_value = {}
        result = handler_module.process_sync_request({
            "workspace_id": "ws", "connection_id": "c",
        })
        assert result["status"] == "error"

    def test_no_scope_selected_skips(self, mock_table, sample_connection):
        sample_connection["scope_config"]["introspection"]["include"] = []
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        assert result["status"] == "skipped"
        assert result["reason"] == "No schemas selected"

    def test_missing_engine_errors(self, mock_table, sample_connection):
        sample_connection["engine"] = ""
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        assert result["status"] == "error"

    @patch.object(handler_module, "get_credential", return_value=None)
    def test_missing_credential_marks_auth_error(
        self, _gc, mock_table, sample_connection,
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        assert result["status"] == "error"
        assert "Credential" in result["error"]

    @patch.object(handler_module, "log_admin_action")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_credential_used_audit_row_emitted(
        self, mock_get_cred, mock_build, mock_audit,
        mock_table, sample_connection, mock_credential,
    ):
        """The forensic audit row for credential use fires on decrypt,
        not on engine success - the customer's data was touched the
        moment we decrypted, regardless of what happens next."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential
        mock_build.return_value = {"tables": [], "columns_count": 0, "had_top_values": False}

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        # DB_CREDENTIAL_USED audit row
        mock_audit.assert_called_once()
        action = mock_audit.call_args[0][1]
        assert action == "DB_CREDENTIAL_USED"
        details = mock_audit.call_args[1]["details"]
        assert details["provider"] == "database"
        assert details["engine"] == "postgres"
        assert details["host_hash"] == "deadbeefcafebabe"

    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_repo_limit_block_uses_engine_host_hash_identity(
        self, mock_get_cred, mock_build,
        mock_table, sample_connection, mock_credential, monkeypatch,
    ):
        """Repo identity is `database:{engine}:{host_hash}` so the same
        physical DB across many workspace connections counts once."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential

        seen_repos = []

        def _block(*a, **kw):
            seen_repos.append(kw.get("repo"))
            r = MagicMock()
            r.allowed = False
            r.reason = "tier limit"
            r.current = 100
            r.limit = 100
            return r

        monkeypatch.setattr(handler_module, "check_repo_limit", _block)

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        assert result["status"] == "skipped"
        assert result["reason"] == "license_limit"
        assert seen_repos == ["database:postgres:deadbeefcafebabe"]
        # Engine not called when limit hit
        mock_build.assert_not_called()

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_full_success_queues_tables_and_emits_audit(
        self, mock_get_cred, mock_build, mock_audit, mock_queue_table,
        _cleanup, mock_table, sample_connection, mock_credential,
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential
        mock_build.return_value = {
            "tables": [
                {
                    "catalog": "analytics", "schema": "public", "table": "users",
                    "yaml": "name: users\n",
                },
                {
                    "catalog": "analytics", "schema": "public", "table": "orders",
                    "yaml": "name: orders\n",
                },
            ],
            "columns_count": 12,
            "had_top_values": False,
        }

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        assert result["status"] == "success"
        assert result["tables_queued"] == 2
        assert result["tables_count"] == 2

        # Both tables queued
        assert mock_queue_table.call_count == 2

        # log_source_sync row carries the compliance posture in
        # extra_details so auditors can see at-a-glance whether
        # top_values was stripped + whether sampling was on.
        audit_kwargs = mock_audit.call_args[1]
        assert audit_kwargs["source"] == "database"
        assert audit_kwargs["files_synced"] == 2
        assert audit_kwargs["extra_details"]["engine"] == "postgres"
        assert audit_kwargs["extra_details"]["host_hash"] == "deadbeefcafebabe"
        assert audit_kwargs["extra_details"]["tables_count"] == 2
        assert audit_kwargs["extra_details"]["columns_count"] == 12
        assert audit_kwargs["extra_details"]["top_values_stripped"] is True
        assert audit_kwargs["extra_details"]["sampling_enabled"] is False


# ============================================================================
# known_table_ids tracking + orphan cleanup
# ============================================================================


class TestKnownTableIdsTracking:

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_table_ids_sanitized_before_storage(
        self, mock_get_cred, mock_build, _audit, _queue, mock_cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        """Table IDs go through sanitize_filename so the form matches
        what file-ingestion writes to S3 (and what delete-worker reads)."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential
        # A table name with characters that sanitize_filename rewrites
        mock_build.return_value = {
            "tables": [
                {
                    "catalog": "an/al", "schema": "pub lic", "table": "users",
                    "yaml": "name: users\n",
                },
            ],
            "columns_count": 1,
        }

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        _ws, _conn, current_ids = mock_cleanup.call_args[0]
        # Expected form: sanitize("an/al.pub lic.users")
        expected = handler_module.sanitize_filename("an/al.pub lic.users")
        assert current_ids == [expected]

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_known_table_ids_persisted_to_ddb(
        self, mock_get_cred, mock_build, _audit, _queue, _cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential
        mock_build.return_value = {
            "tables": [
                {"catalog": "c", "schema": "s", "table": "users", "yaml": "x"},
            ],
            "columns_count": 0,
        }

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        update_calls = mock_table.update_item.call_args_list
        for call in update_calls:
            expr_values = call[1].get("ExpressionAttributeValues", {})
            if expr_values.get(":status") == "completed":
                assert ":kti" in expr_values
                # Sanitized "c.s.users"
                assert expr_values[":kti"] == [handler_module.sanitize_filename("c.s.users")]
                return
        raise AssertionError("No completed-status update with known_table_ids")


class TestQueueOrphanCleanup:

    def test_sends_correct_payload(self):
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["tid1", "tid2"])
        sent = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        assert sent == {
            "action": "delete_orphans",
            "workspace_id": "ws1",
            "source": "database",
            "prefix": "conn1/",
            "current_ids": ["tid1", "tid2"],
        }
        assert mock_sqs_client.send_message.call_args[1]["QueueUrl"] == "https://sqs.example.com/delete"

    def test_refuses_empty_current_ids(self):
        """Defensive: empty set would orphan-delete everything for this
        connection. Skip the SQS send."""
        handler_module.queue_orphan_cleanup("ws1", "conn1", [])
        mock_sqs_client.send_message.assert_not_called()

    def test_skips_when_delete_queue_unconfigured(self, monkeypatch):
        monkeypatch.setattr(handler_module, "DELETE_QUEUE", None)
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["tid1"])
        mock_sqs_client.send_message.assert_not_called()

    def test_swallows_sqs_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "SendMessage"
        )
        # Must not raise
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["tid1"])


# ============================================================================
# lambda_handler - SQS dispatch + license-layer regression detection
# ============================================================================


class TestLambdaHandlerLicenseLayer:

    def test_license_layer_absent_emits_audit_and_raises(self, monkeypatch):
        """validate_license=None means the license layer wasn't attached
        at deploy. Per ADR-019, emit LICENSE_FAIL_OPEN audit + raise so
        SQS retries / DLQs the message rather than silently dropping it."""
        monkeypatch.setattr(handler_module, "validate_license", None)

        with patch.object(handler_module, "log_license_fail_open") as mock_log:
            with pytest.raises(ImportError):
                handler_module.lambda_handler({"Records": []}, None)
        mock_log.assert_called_once()
        # Lambda name passed as positional arg
        assert mock_log.call_args[0][1] == "database-sync"

    def test_license_validation_failure_raises(self, monkeypatch):
        def _raise(*a, **kw):
            raise Exception("license expired")
        monkeypatch.setattr(handler_module, "validate_license", _raise)

        with pytest.raises(Exception, match="license expired"):
            handler_module.lambda_handler({"Records": []}, None)

    def test_no_records_returns_400(self):
        result = handler_module.lambda_handler({}, None)
        assert result["statusCode"] == 400


class TestLambdaHandlerDispatch:

    @patch.object(handler_module, "process_sync_request")
    def test_processes_sqs_records(self, mock_process):
        mock_process.return_value = {"status": "success", "tables_queued": 5}
        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws", "connection_id": "c1"})},
                {"body": json.dumps({"workspace_id": "ws", "connection_id": "c2"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert mock_process.call_count == 2

    @patch.object(handler_module, "process_sync_request")
    def test_invalid_json_records_continue(self, mock_process):
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"body": "{not valid json"},
                {"body": json.dumps({"workspace_id": "ws", "connection_id": "c"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        body = json.loads(result["body"])
        assert any(r.get("status") == "error" for r in body["results"])
        assert any(r.get("status") == "success" for r in body["results"])
