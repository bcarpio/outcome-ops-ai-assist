# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for database-sync Lambda handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# `databao_context_engine` is a proprietary package not shipped with the OSS
# release; every import of it in the platform code is deferred so the module is
# optional at runtime. Tests that exercise it run only where it is installed --
# a clean clone or CI runner skips them instead of erroring on the import.
databao_required = pytest.mark.skipif(
    importlib.util.find_spec("databao_context_engine") is None,
    reason="requires proprietary databao_context_engine (not in OSS release)",
)


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
            # 2-segment "catalog.schema" keys, sampling.include
            # replaces sampling.enabled bool.
            "introspection": {"include": ["analytics.public"], "exclude": []},
            "sampling":      {"include": [], "exclude": []},
            "profiling":     {"enabled": True},
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
# Schema introspection (introspect_database path)
# ============================================================================


class TestToYamlSafe:
    """Recursive dataclass/enum unwrap used to serialize introspector
    results into YAML chunks."""

    def test_unwraps_simple_dataclass(self):
        from dataclasses import dataclass as _dc

        @_dc
        class Inner:
            x: int
            y: str

        assert handler_module._to_yaml_safe(Inner(x=1, y="a")) == {"x": 1, "y": "a"}

    def test_unwraps_enum_to_value(self):
        from enum import Enum

        class Kind(Enum):
            TABLE = "table"
            VIEW = "view"

        assert handler_module._to_yaml_safe(Kind.TABLE) == "table"

    def test_unwraps_nested_dataclass_and_list(self):
        from dataclasses import dataclass as _dc, field

        @_dc
        class Col:
            name: str

        @_dc
        class Tbl:
            name: str
            columns: list = field(default_factory=list)

        result = handler_module._to_yaml_safe(
            Tbl(name="t", columns=[Col(name="a"), Col(name="b")])
        )
        assert result == {"name": "t", "columns": [{"name": "a"}, {"name": "b"}]}

    def test_passes_through_primitives(self):
        assert handler_module._to_yaml_safe(None) is None
        assert handler_module._to_yaml_safe("x") == "x"
        assert handler_module._to_yaml_safe(7) == 7
        assert handler_module._to_yaml_safe({"a": 1}) == {"a": 1}


class TestBuildTableChunkYaml:
    """The per-table YAML envelope file-ingestion expects."""

    def _make_stub_table(self, name="users", with_top_values=False):
        # Mirrors databao_context_engine's DatabaseTable shape via a
        # plain dataclass so the unwrapper handles it identically.
        from dataclasses import dataclass as _dc, field
        from enum import Enum

        class Kind(Enum):
            TABLE = "table"

        @_dc
        class StubColumn:
            name: str
            type: str
            top_values: list | None = None

        @_dc
        class StubTable:
            name: str
            kind: Kind = Kind.TABLE
            columns: list = field(default_factory=list)

        cols = [
            StubColumn(name="id", type="int"),
            StubColumn(
                name="email", type="varchar",
                top_values=["a@x.com"] if with_top_values else None,
            ),
        ]
        return StubTable(name=name, columns=cols)

    def test_envelope_shape(self):
        import yaml

        yaml_str, col_count, had_tv = handler_module._build_table_chunk_yaml(
            self._make_stub_table(),
            catalog_name="analytics",
            schema_name="healthcare",
            engine="mssql",
            datasource_id="ooat-sync:mssql:conn123",
        )
        doc = yaml.safe_load(yaml_str)
        assert doc["datasource_id"] == "ooat-sync:mssql:conn123"
        assert doc["datasource_type"] == "mssql"
        assert doc["context"]["catalogs"][0]["name"] == "analytics"
        assert doc["context"]["catalogs"][0]["schemas"][0]["name"] == "healthcare"
        tbl = doc["context"]["catalogs"][0]["schemas"][0]["tables"][0]
        assert tbl["name"] == "users"
        assert tbl["kind"] == "table"
        assert len(tbl["columns"]) == 2
        assert col_count == 2

    def test_strips_top_values_from_emitted_yaml(self):
        import yaml

        yaml_str, _, had_tv = handler_module._build_table_chunk_yaml(
            self._make_stub_table(with_top_values=True),
            catalog_name="c", schema_name="s",
            engine="mssql", datasource_id="d",
        )
        # The detection flag fires because top_values was present
        # pre-strip; the emitted YAML has been stripped.
        assert had_tv is True
        doc = yaml.safe_load(yaml_str)
        for col in doc["context"]["catalogs"][0]["schemas"][0]["tables"][0]["columns"]:
            assert "top_values" not in col


@databao_required
class TestRunSchemaIntrospection:
    """End-to-end behavior of _run_schema_introspection with the
    introspector mocked. The function should filter to include_table_keys,
    serialize via _build_table_chunk_yaml, and return aggregated counts."""

    @pytest.fixture
    def stub_introspect_result(self):
        from dataclasses import dataclass as _dc, field

        @_dc
        class StubCol:
            name: str
            type: str

        @_dc
        class StubTable:
            name: str
            columns: list = field(default_factory=list)

        @_dc
        class StubSchema:
            name: str
            tables: list = field(default_factory=list)

        @_dc
        class StubCatalog:
            name: str
            schemas: list = field(default_factory=list)

        @_dc
        class StubResult:
            catalogs: list = field(default_factory=list)

        return StubResult(catalogs=[
            StubCatalog(name="analytics", schemas=[
                StubSchema(name="healthcare", tables=[
                    StubTable(name="patients", columns=[StubCol(name="id", type="int")]),
                    StubTable(name="excluded", columns=[StubCol(name="id", type="int")]),
                ]),
                StubSchema(name="billing", tables=[
                    StubTable(name="claims", columns=[StubCol(name="id", type="int"), StubCol(name="amount", type="decimal")]),
                ]),
            ]),
        ])

    def test_emits_every_table_in_selected_schemas(
        self, stub_introspect_result, sample_connection, mock_credential
    ):
        """Schemas-only scope: every table the upstream introspector
        returns is emitted, no per-table filter."""
        import yaml

        connection = {
            **sample_connection,
            "engine": "mssql",
            "scope_config": {
                # Schema-level include (2 segments) — shape.
                "introspection": {"include": [
                    "analytics.healthcare",
                    "analytics.billing",
                ]},
                "sampling": {"include": []},  # sampling off
            },
        }
        fake_introspector = MagicMock()
        fake_introspector.introspect_database.return_value = stub_introspect_result

        with patch.object(
            handler_module, "_get_introspector_for_sync",
            return_value=fake_introspector,
        ), patch.object(
            handler_module, "_build_sync_config",
            return_value=MagicMock(),
        ):
            result = handler_module._run_schema_introspection(connection, mock_credential)

        # ALL tables in result (patients, excluded, claims) get emitted -
        # no per-table filter.
        assert len(result["tables"]) == 3
        emitted = {(t["catalog"], t["schema"], t["table"]) for t in result["tables"]}
        assert emitted == {
            ("analytics", "healthcare", "patients"),
            ("analytics", "healthcare", "excluded"),
            ("analytics", "billing", "claims"),
        }
        # columns: patients=1 + excluded=1 + claims=2 = 4
        assert result["columns_count"] == 4
        assert result["had_top_values"] is False

        doc = yaml.safe_load(result["tables"][0]["yaml"])
        assert doc["datasource_type"] == "mssql"
        assert doc["context"]["catalogs"][0]["name"] == "analytics"

    def test_rejects_legacy_three_segment_include(
        self, sample_connection, mock_credential
    ):
        """Legacy 'catalog.schema.table' shape is rejected. Connections
        were disconnected pre-deploy; no legacy records survive."""
        connection = {
            **sample_connection,
            "engine": "mssql",
            "scope_config": {
                "introspection": {"include": ["analytics.healthcare.patients"]},
                "sampling": {"include": []},
            },
        }
        result = handler_module._run_schema_introspection(connection, mock_credential)
        # Strict-validation failure -> empty result, error logged.
        assert result == {"tables": [], "columns_count": 0, "had_top_values": False}

    def test_no_include_keys_skips_introspect(
        self, sample_connection, mock_credential
    ):
        connection = {
            **sample_connection,
            "scope_config": {"introspection": {"include": []}},
        }
        result = handler_module._run_schema_introspection(connection, mock_credential)
        assert result == {"tables": [], "columns_count": 0, "had_top_values": False}

    def test_introspector_exception_returns_empty(
        self, sample_connection, mock_credential
    ):
        connection = {
            **sample_connection,
            "engine": "mssql",
            "scope_config": {
                "introspection": {"include": ["analytics.healthcare"]},
                "sampling": {"include": []},
            },
        }
        fake = MagicMock()
        fake.introspect_database.side_effect = RuntimeError("upstream boom")
        with patch.object(
            handler_module, "_get_introspector_for_sync", return_value=fake,
        ), patch.object(
            handler_module, "_build_sync_config", return_value=MagicMock(),
        ):
            result = handler_module._run_schema_introspection(connection, mock_credential)
        assert result == {"tables": [], "columns_count": 0, "had_top_values": False}

    def test_sampling_include_drives_sampling_scope(
        self, stub_introspect_result, sample_connection, mock_credential
    ):
        """sampling.include of schema keys produces a SamplingConfig
        with SamplingScope containing per-schema SamplingIncludeRules."""
        connection = {
            **sample_connection,
            "engine": "mssql",
            "scope_config": {
                "introspection": {"include": ["analytics.healthcare", "analytics.billing"]},
                # sampling on for healthcare ONLY
                "sampling": {"include": ["analytics.healthcare"]},
            },
        }
        fake_introspector = MagicMock()
        fake_introspector.introspect_database.return_value = stub_introspect_result

        captured = {}

        def capture_build_sync_config(*args, **kw):
            captured["sampling_schemas_by_catalog"] = kw.get("sampling_schemas_by_catalog")
            captured["schemas_by_catalog"] = kw.get("schemas_by_catalog")
            return MagicMock()

        with patch.object(
            handler_module, "_get_introspector_for_sync",
            return_value=fake_introspector,
        ), patch.object(
            handler_module, "_build_sync_config",
            side_effect=capture_build_sync_config,
        ):
            handler_module._run_schema_introspection(connection, mock_credential)

        assert captured["sampling_schemas_by_catalog"] == {
            "analytics": {"healthcare"},
        }
        # Introspection still covers both schemas - sampling scope is
        # a subset, not the introspection set.
        assert captured["schemas_by_catalog"] == {
            "analytics": {"healthcare", "billing"},
        }

    def test_build_sync_config_sampling_off_when_include_empty(self):
        """Empty sampling.include -> SamplingConfig(enabled=False)."""
        import databao_context_engine as dce
        cfg = handler_module._build_sync_config(
            dce, "mssql", "host", 1433, "analytics",
            credential={"username": "u", "password": "p"},
            schemas_by_catalog={"analytics": {"healthcare"}},
            sampling_schemas_by_catalog={},
        )
        assert cfg.sampling.enabled is False
        # No scope set when sampling is off.
        assert cfg.sampling.scope is None

    def test_build_sync_config_sampling_on_with_scope(self):
        """Non-empty sampling.include -> SamplingConfig.enabled=True with
        SamplingScope.include populated per-schema."""
        import databao_context_engine as dce
        cfg = handler_module._build_sync_config(
            dce, "mssql", "host", 1433, "analytics",
            credential={"username": "u", "password": "p"},
            schemas_by_catalog={"analytics": {"healthcare", "billing"}},
            sampling_schemas_by_catalog={"analytics": {"healthcare"}},
        )
        assert cfg.sampling.enabled is True
        assert cfg.sampling.scope is not None
        assert len(cfg.sampling.scope.include) == 1
        rule = cfg.sampling.scope.include[0]
        assert rule.catalog == "analytics"
        assert rule.schemas == ["healthcare"]


class TestParseSchemaKeys:
    """The strict-validation helper used by both _run_schema_introspection
    paths to reject legacy 3-segment include entries."""

    def test_accepts_two_segment(self):
        assert handler_module._parse_schema_keys(
            ["analytics.healthcare", "analytics.billing"],
            field="introspection.include",
        ) == {"analytics": {"healthcare", "billing"}}

    def test_dedupes_across_repeats(self):
        assert handler_module._parse_schema_keys(
            ["analytics.healthcare", "analytics.healthcare"],
            field="introspection.include",
        ) == {"analytics": {"healthcare"}}

    def test_rejects_three_segment_legacy_shape(self):
        with pytest.raises(ValueError, match="2 segments"):
            handler_module._parse_schema_keys(
                ["analytics.healthcare.patients"],
                field="introspection.include",
            )

    def test_rejects_one_segment(self):
        with pytest.raises(ValueError, match="2 segments"):
            handler_module._parse_schema_keys(
                ["analytics"], field="introspection.include",
            )

    def test_rejects_empty_segments(self):
        with pytest.raises(ValueError, match="2 segments"):
            handler_module._parse_schema_keys(
                ["analytics."], field="introspection.include",
            )

    def test_empty_list_returns_empty_map(self):
        assert handler_module._parse_schema_keys(
            [], field="sampling.include",
        ) == {}


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
        # extra_details so auditors can see at-a-glance what was
        # processed: top_values stripped, sampling schemas, and the
        # per-sync schema-drift delta (tables_added/removed) for
        # downstream alerting.
        audit_kwargs = mock_audit.call_args[1]
        assert audit_kwargs["source"] == "database"
        assert audit_kwargs["files_synced"] == 2
        details = audit_kwargs["extra_details"]
        assert details["engine"] == "postgres"
        assert details["host_hash"] == "deadbeefcafebabe"
        assert details["tables_count"] == 2
        assert details["columns_count"] == 12
        assert details["top_values_stripped"] is True
        # New: sampling_schemas replaces sampling_enabled.
        # The fixture has scope_config.sampling.include = [] so sampling
        # is off and the list is empty.
        assert details["sampling_schemas"] == []
        # New: schema-drift delta. The sample_connection
        # fixture has known_table_ids=[] so every current table is
        # "added" on this sync.
        assert sorted(details["tables_added"]) == [
            "analytics.public.orders", "analytics.public.users",
        ]
        assert details["tables_removed"] == []
        assert details["table_count_delta"] == 2

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_audit_row_surfaces_tables_added_and_removed_delta(
        self, mock_get_cred, mock_build, mock_audit, mock_queue_table,
        _cleanup, mock_table, sample_connection, mock_credential,
    ):
        """Second-sync semantics: connection.known_table_ids reflects
        the prior sync's table set. New tables show up in tables_added,
        dropped tables in tables_removed."""
        connection_with_prior_state = {
            **sample_connection,
            # Previous sync had users + payments (no orders).
            "known_table_ids": ["analytics.public.users", "analytics.public.payments"],
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior_state}
        mock_get_cred.return_value = mock_credential
        mock_build.return_value = {
            "tables": [
                # users carries over from the previous sync
                {"catalog": "analytics", "schema": "public", "table": "users",
                 "yaml": "name: users\n"},
                # orders is new
                {"catalog": "analytics", "schema": "public", "table": "orders",
                 "yaml": "name: orders\n"},
                # payments is gone (was in known_table_ids, not in current)
            ],
            "columns_count": 8,
            "had_top_values": False,
        }
        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        details = mock_audit.call_args[1]["extra_details"]
        assert details["tables_added"] == ["analytics.public.orders"]
        assert details["tables_removed"] == ["analytics.public.payments"]
        assert details["table_count_delta"] == 0   # +1 added, -1 removed


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
# lambda_handler - SQS dispatch
# ============================================================================


class TestLambdaHandlerDispatch:
    """SQS partial-batch-failure contract."""

    def test_no_records_returns_400(self):
        result = handler_module.lambda_handler({}, None)
        assert result["statusCode"] == 400

    @patch.object(handler_module, "process_sync_request")
    def test_processes_sqs_records(self, mock_process):
        mock_process.return_value = {"status": "success", "tables_queued": 5}
        event = {
            "Records": [
                {"messageId": "m1", "body": json.dumps({"workspace_id": "ws", "connection_id": "c1"})},
                {"messageId": "m2", "body": json.dumps({"workspace_id": "ws", "connection_id": "c2"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": []}
        assert mock_process.call_count == 2

    @patch.object(handler_module, "process_sync_request")
    def test_invalid_json_records_continue(self, mock_process):
        """Poison-pill routes to DLQ; sibling success message ack'd."""
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"messageId": "bad", "body": "{not valid json"},
                {"messageId": "good", "body": json.dumps({"workspace_id": "ws", "connection_id": "c"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


# ============================================================================
# _list_visible_schemas - unrestricted enumeration
# ============================================================================


@databao_required
class TestListVisibleSchemas:
    """The cheap unrestricted introspect that powers schema-drift
    detection. Profiling + sampling are forced off; no IntrospectionScope
    is passed - we want every schema the RO user can see."""

    @pytest.fixture
    def stub_introspect_result(self):
        from dataclasses import dataclass as _dc, field

        @_dc
        class StubSchema:
            name: str

        @_dc
        class StubCatalog:
            name: str
            schemas: list = field(default_factory=list)

        @_dc
        class StubResult:
            catalogs: list = field(default_factory=list)

        return StubResult(catalogs=[
            StubCatalog(name="analytics", schemas=[
                StubSchema(name="public"),
                StubSchema(name="healthcare"),
                StubSchema(name="imaging"),
            ]),
            StubCatalog(name="archive", schemas=[
                StubSchema(name="legacy"),
            ]),
        ])

    def test_returns_sorted_two_segment_keys(self, stub_introspect_result, mock_credential):
        import databao_context_engine as dce

        fake_introspector = MagicMock()
        fake_introspector.introspect_database.return_value = stub_introspect_result
        with patch.object(
            handler_module, "_get_introspector_for_sync",
            return_value=fake_introspector,
        ):
            keys = handler_module._list_visible_schemas(
                dce, "mssql", "host", 1433, "analytics", mock_credential,
            )
        # Sorted catalog.schema keys across both catalogs
        assert keys == [
            "analytics.healthcare",
            "analytics.imaging",
            "analytics.public",
            "archive.legacy",
        ]

    def test_postgres_config_has_no_introspection_scope(self, stub_introspect_result):
        """Postgres path: build config with profiling+sampling off and
        no IntrospectionScope, then walk every catalog/schema the user
        can see."""
        import databao_context_engine as dce

        fake_introspector = MagicMock()
        fake_introspector.introspect_database.return_value = stub_introspect_result
        captured = {}

        def _capture_introspect(cfg):
            captured["config"] = cfg
            return stub_introspect_result

        fake_introspector.introspect_database.side_effect = _capture_introspect

        with patch.object(
            handler_module, "_get_introspector_for_sync",
            return_value=fake_introspector,
        ):
            handler_module._list_visible_schemas(
                dce, "postgres", "h", 5432, "analytics",
                {"username": "u", "password": "p", "ssl_mode": "require"},
            )
        cfg = captured["config"]
        # Profiling + sampling both off (metadata-only call).
        assert cfg.profiling.enabled is False
        assert cfg.sampling.enabled is False
        # No IntrospectionScope = enumerate everything.
        assert getattr(cfg, "introspection_scope", None) is None

    def test_mysql_config_path(self, stub_introspect_result):
        import databao_context_engine as dce

        fake_introspector = MagicMock()
        captured = {}

        def _capture(cfg):
            captured["config"] = cfg
            return stub_introspect_result

        fake_introspector.introspect_database.side_effect = _capture
        with patch.object(
            handler_module, "_get_introspector_for_sync",
            return_value=fake_introspector,
        ):
            handler_module._list_visible_schemas(
                dce, "mysql", "h", 3306, "analytics",
                {"username": "u", "password": "p"},
            )
        cfg = captured["config"]
        assert cfg.profiling.enabled is False
        assert cfg.sampling.enabled is False

    def test_mssql_config_path(self, stub_introspect_result):
        import databao_context_engine as dce

        fake_introspector = MagicMock()
        captured = {}

        def _capture(cfg):
            captured["config"] = cfg
            return stub_introspect_result

        fake_introspector.introspect_database.side_effect = _capture
        with patch.object(
            handler_module, "_get_introspector_for_sync",
            return_value=fake_introspector,
        ):
            handler_module._list_visible_schemas(
                dce, "mssql", "h", 1433, "analytics",
                {"username": "u", "password": "p", "encrypt": True},
            )
        cfg = captured["config"]
        assert cfg.profiling.enabled is False
        assert cfg.sampling.enabled is False

    def test_unsupported_engine_raises(self, mock_credential):
        import databao_context_engine as dce

        with pytest.raises(ValueError, match="Unsupported engine"):
            handler_module._list_visible_schemas(
                dce, "oracle", "h", 1521, "db", mock_credential,
            )


# ============================================================================
# schema-drift delta computation + audit-row + DDB persist
# ============================================================================


@databao_required
class TestSchemaDriftDelta:
    """diff current available_schemas against the connection
    record's prior snapshot to produce schemas_added / schemas_removed
    on the DATABASE_SYNCED audit row's extra_details."""

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_added_schemas_surface_in_audit(
        self, mock_get_cred, mock_intro, mock_list_schemas, mock_audit,
        mock_queue_table, _cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        """Prior snapshot had [analytics.public]; current enumeration
        finds [analytics.public, analytics.imaging] -> imaging is added."""
        connection_with_prior_schemas = {
            **sample_connection,
            "available_schemas": ["analytics.public"],
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior_schemas}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.imaging", "analytics.public"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        details = mock_audit.call_args[1]["extra_details"]
        assert details["schemas_added"] == ["analytics.imaging"]
        assert details["schemas_removed"] == []
        assert details["schema_count_delta"] == 1

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_removed_schemas_surface_in_audit(
        self, mock_get_cred, mock_intro, mock_list_schemas, mock_audit,
        mock_queue_table, _cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        """DBA dropped a schema between syncs."""
        connection_with_prior_schemas = {
            **sample_connection,
            "available_schemas": ["analytics.public", "analytics.legacy"],
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior_schemas}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.public"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        details = mock_audit.call_args[1]["extra_details"]
        assert details["schemas_added"] == []
        assert details["schemas_removed"] == ["analytics.legacy"]
        assert details["schema_count_delta"] == -1

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_no_change_yields_empty_delta(
        self, mock_get_cred, mock_intro, mock_list_schemas, mock_audit,
        mock_queue_table, _cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        """Steady state: prior == current -> both deltas empty."""
        connection_with_prior_schemas = {
            **sample_connection,
            "available_schemas": ["analytics.public", "analytics.billing"],
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior_schemas}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.billing", "analytics.public"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        details = mock_audit.call_args[1]["extra_details"]
        assert details["schemas_added"] == []
        assert details["schemas_removed"] == []
        assert details["schema_count_delta"] == 0

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_first_sync_treats_everything_as_added(
        self, mock_get_cred, mock_intro, mock_list_schemas, mock_audit,
        mock_queue_table, _cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        """First sync after this chunk lands (or after fresh connect):
        connection has no available_schemas yet, so every visible schema
        is 'added'."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.public", "analytics.healthcare"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        details = mock_audit.call_args[1]["extra_details"]
        assert details["schemas_added"] == sorted(
            ["analytics.public", "analytics.healthcare"]
        )
        assert details["schemas_removed"] == []
        assert details["schema_count_delta"] == 2

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_enumeration_failure_is_graceful(
        self, mock_get_cred, mock_intro, mock_list_schemas, mock_audit,
        mock_queue_table, _cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        """If _list_visible_schemas raises, the sync still completes -
        we treat the enumeration result as empty and just log."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.side_effect = RuntimeError("transient introspect blip")

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        # Sync still succeeded - drift detection is best-effort.
        assert result["status"] == "success"
        details = mock_audit.call_args[1]["extra_details"]
        # Empty enumeration -> nothing added (we don't know what's
        # actually missing, so we don't claim removals either).
        assert details["schemas_added"] == []

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_available_schemas_persisted_to_ddb(
        self, mock_get_cred, mock_intro, mock_list_schemas, _audit,
        _queue, _cleanup,
        mock_table, sample_connection, mock_credential,
    ):
        """The completed-status update writes available_schemas via the
        new UpdateExpression kwarg."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.public", "analytics.imaging"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        for call in mock_table.update_item.call_args_list:
            expr_values = call[1].get("ExpressionAttributeValues", {})
            if expr_values.get(":status") == "completed":
                assert ":as" in expr_values
                assert expr_values[":as"] == [
                    "analytics.public", "analytics.imaging",
                ]
                assert "available_schemas" in call[1]["UpdateExpression"]
                return
        raise AssertionError("No completed-status update with available_schemas")


# ============================================================================
# SNS drift-notification publish
# ============================================================================


@databao_required
class TestDriftNotificationPublish:
    """Push-notification path. Topic ARN comes from env. Empty/unset
    arn = audit-only (no publish). Non-empty deltas trigger publish;
    all-empty deltas skip publish (steady-state syncs send nothing)."""

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_publish_fires_on_schema_drift(
        self, mock_get_cred, mock_intro, mock_list_schemas, _audit,
        _queue, _cleanup,
        mock_table, sample_connection, mock_credential, monkeypatch,
    ):
        """Schema added -> publish fires with subject+body containing
        the connection label + engine."""
        monkeypatch.setenv("DATABASE_DRIFT_TOPIC_ARN", "arn:aws:sns:us-east-1:111:db-drift")
        mock_publish = MagicMock()
        monkeypatch.setattr(handler_module.sns_client, "publish", mock_publish)

        connection_with_prior = {
            **sample_connection,
            "available_schemas": ["analytics.public"],
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.public", "analytics.imaging"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        mock_publish.assert_called_once()
        call_kwargs = mock_publish.call_args[1]
        assert call_kwargs["TopicArn"] == "arn:aws:sns:us-east-1:111:db-drift"
        # Subject capped at 99 chars
        assert len(call_kwargs["Subject"]) <= 99
        assert "Database drift" in call_kwargs["Subject"]
        assert "Test DW" in call_kwargs["Subject"]
        # Body lists the actual schema drift
        assert "analytics.imaging" in call_kwargs["Message"]
        # Body should NOT carry the table-level data lines (admin has
        # no action to take on auto-discovered tables; only the audit
        # row carries those fields for downstream consumers). Match
        # against the data-formatted prefix (with colon) so the
        # explanatory parenthetical that mentions "Tables added or
        # removed" without a colon doesn't trip the assertion.
        assert "Tables added:" not in call_kwargs["Message"]
        assert "Tables removed:" not in call_kwargs["Message"]

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_publish_skipped_on_table_drift_only(
        self, mock_get_cred, mock_intro, mock_list_schemas, _audit,
        mock_queue_table, _cleanup,
        mock_table, sample_connection, mock_credential, monkeypatch,
    ):
        """Table-level drift inside already-selected schemas is handled
        automatically by auto-discover / orphan-cleanup
        paths. Admin has nothing to act on, so we deliberately do NOT
        email about it. The DATABASE_SYNCED audit row still carries
        tables_added / tables_removed for downstream consumers."""
        monkeypatch.setenv("DATABASE_DRIFT_TOPIC_ARN", "arn:aws:sns:us-east-1:111:db-drift")
        mock_publish = MagicMock()
        monkeypatch.setattr(handler_module.sns_client, "publish", mock_publish)

        connection_with_prior = {
            **sample_connection,
            "available_schemas": ["analytics.public"],
            # known_table_ids is empty so any current tables count as added
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [
                {"catalog": "analytics", "schema": "public", "table": "orders",
                 "yaml": "name: orders\n"},
            ],
            "columns_count": 1,
            "had_top_values": False,
        }
        # No schema drift — table only
        mock_list_schemas.return_value = ["analytics.public"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        mock_publish.assert_not_called()

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_publish_skipped_when_no_drift(
        self, mock_get_cred, mock_intro, mock_list_schemas, _audit,
        _queue, _cleanup,
        mock_table, sample_connection, mock_credential, monkeypatch,
    ):
        """Steady state: every delta list empty -> SNS publish skipped.
        Quiet hourly tick. No email noise."""
        monkeypatch.setenv("DATABASE_DRIFT_TOPIC_ARN", "arn:aws:sns:us-east-1:111:db-drift")
        mock_publish = MagicMock()
        monkeypatch.setattr(handler_module.sns_client, "publish", mock_publish)

        connection_steady = {
            **sample_connection,
            "available_schemas": ["analytics.public"],
            "known_table_ids": [],   # current also empty -> no table drift
        }
        mock_table.get_item.return_value = {"Item": connection_steady}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.public"]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        mock_publish.assert_not_called()

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_publish_noop_when_topic_arn_unset(
        self, mock_get_cred, mock_intro, mock_list_schemas, _audit,
        _queue, _cleanup,
        mock_table, sample_connection, mock_credential, monkeypatch,
    ):
        """No topic-arn env var (empty list of tfvar emails -> no topic)
        means publish is a no-op even if drift is non-empty. Audit row
        still fires."""
        monkeypatch.delenv("DATABASE_DRIFT_TOPIC_ARN", raising=False)
        mock_publish = MagicMock()
        monkeypatch.setattr(handler_module.sns_client, "publish", mock_publish)

        connection_with_prior = {
            **sample_connection,
            "available_schemas": ["analytics.public"],
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.public", "analytics.imaging"]

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        # Sync still succeeded - audit row already wrote the drift.
        assert result["status"] == "success"
        mock_publish.assert_not_called()

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_table_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "_list_visible_schemas")
    @patch.object(handler_module, "_run_schema_introspection")
    @patch.object(handler_module, "get_credential")
    def test_publish_clienterror_is_swallowed(
        self, mock_get_cred, mock_intro, mock_list_schemas, _audit,
        _queue, _cleanup,
        mock_table, sample_connection, mock_credential, monkeypatch,
    ):
        """SNS ClientError must not block the sync — audit row already
        has the data. Just warning-log."""
        monkeypatch.setenv("DATABASE_DRIFT_TOPIC_ARN", "arn:aws:sns:us-east-1:111:db-drift")
        mock_publish = MagicMock(side_effect=ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "Publish",
        ))
        monkeypatch.setattr(handler_module.sns_client, "publish", mock_publish)

        connection_with_prior = {
            **sample_connection,
            "available_schemas": ["analytics.public"],
        }
        mock_table.get_item.return_value = {"Item": connection_with_prior}
        mock_get_cred.return_value = mock_credential
        mock_intro.return_value = {
            "tables": [], "columns_count": 0, "had_top_values": False,
        }
        mock_list_schemas.return_value = ["analytics.public", "analytics.imaging"]

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })
        assert result["status"] == "success"


class TestUpdateConnectionStatusAvailableSchemas:
    """The new available_schemas kwarg writes to DDB only when provided."""

    def test_available_schemas_kwarg_writes_to_ddb(self, mock_table):
        handler_module.update_connection_status(
            "ws", "c", "completed",
            tables_count=1,
            known_table_ids=["x"],
            available_schemas=["analytics.public", "analytics.imaging"],
        )
        call_args = mock_table.update_item.call_args[1]
        expr = call_args["UpdateExpression"]
        assert "available_schemas" in expr
        values = call_args["ExpressionAttributeValues"]
        assert values[":as"] == ["analytics.public", "analytics.imaging"]

    def test_available_schemas_omitted_when_not_provided(self, mock_table):
        handler_module.update_connection_status(
            "ws", "c", "syncing",
        )
        call_args = mock_table.update_item.call_args[1]
        expr = call_args["UpdateExpression"]
        # No :as placeholder when the kwarg wasn't passed
        assert ":as" not in call_args["ExpressionAttributeValues"]
        assert "available_schemas" not in expr
