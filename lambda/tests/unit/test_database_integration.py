"""Unit tests for database-integration Lambda (handler, credential_manager,
introspection_helpers stubs)."""

import base64
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


# Load credential_manager FIRST under the name the handler imports it
# as (from credential_manager). Then introspection_helpers, then handler.
credential_module = _load_module(
    "db_int_credential_manager",
    "credential_manager.py",
    "../../database-integration",
    also_register_as="credential_manager",
)
helpers_module = _load_module(
    "db_int_introspection_helpers",
    "introspection_helpers.py",
    "../../database-integration",
    also_register_as="introspection_helpers",
)
handler_module = _load_module(
    "db_int_handler", "handler.py", "../../database-integration"
)


# ============================================================================
# Credential Manager Tests
# ============================================================================


class TestCredentialManager:

    @pytest.fixture(autouse=True)
    def reset_globals(self):
        credential_module._dynamodb = None
        credential_module._kms = None
        yield

    @patch.object(credential_module, "_get_kms")
    def test_encrypt_payload(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.encrypt.return_value = {"CiphertextBlob": b"encrypted"}
        mock_kms_fn.return_value = mock_kms
        result = credential_module._encrypt_payload({"username": "u", "password": "p"})
        # base64-encoded ciphertext
        assert base64.b64decode(result) == b"encrypted"
        # KMS got the JSON-serialized blob
        assert mock_kms.encrypt.call_args[1]["Plaintext"] == b'{"username":"u","password":"p"}'

    @patch.object(credential_module, "_get_kms")
    def test_decrypt_payload(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.decrypt.return_value = {
            "Plaintext": b'{"username":"u","password":"p"}'
        }
        mock_kms_fn.return_value = mock_kms
        result = credential_module._decrypt_payload(
            base64.b64encode(b"cipher").decode()
        )
        assert result == {"username": "u", "password": "p"}

    @patch.object(credential_module, "_encrypt_payload")
    @patch.object(credential_module, "_get_dynamodb")
    def test_save_credential_success(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "encrypted"

        result = credential_module.save_credential(
            "conn1", {"username": "u", "password": "p"}
        )
        assert result is True
        # Stored under DB_CREDENTIAL# prefix
        assert mock_table.put_item.call_args[1]["Item"]["PK"] == "DB_CREDENTIAL#conn1"

    @patch.object(credential_module, "_get_dynamodb")
    def test_save_credential_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "PutItem"
        )
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        with patch.object(credential_module, "_encrypt_payload", return_value="x"):
            assert credential_module.save_credential("c", {}) is False

    @patch.object(credential_module, "_decrypt_payload")
    @patch.object(credential_module, "_get_dynamodb")
    def test_get_credential_success(self, mock_db_fn, mock_decrypt):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"encrypted_payload": "enc"}
        }
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_decrypt.return_value = {"username": "u", "password": "p"}

        result = credential_module.get_credential("c1")
        assert result == {"username": "u", "password": "p"}

    @patch.object(credential_module, "_get_dynamodb")
    def test_get_credential_missing(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        assert credential_module.get_credential("c1") is None

    @patch.object(credential_module, "_get_dynamodb")
    def test_delete_credential(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        assert credential_module.delete_credential("c1") is True
        deleted_pk = mock_table.delete_item.call_args[1]["Key"]["PK"]
        assert deleted_pk == "DB_CREDENTIAL#c1"


# ============================================================================
# Introspection Helpers Tests
# ============================================================================


class TestIntrospectionHelpers:

    def test_validate_connection_raises_when_layer_unavailable(self):
        # Per ADR-019 / deploy-bug semantics, when the upstream package
        # isn't in the runtime image the helper raises
        # IntrospectionEngineUnavailable so callers (the integration
        # handler) can map it to a 500 "Server misconfiguration"
        # response rather than silently returning a misleading
        # success/empty result.
        with patch.object(
            helpers_module, "_import_introspection_engine",
            side_effect=helpers_module.IntrospectionEngineUnavailable("missing"),
        ):
            with pytest.raises(helpers_module.IntrospectionEngineUnavailable):
                helpers_module.validate_connection(
                    engine="postgres",
                    host="h", port=5432, database_name="d",
                    credential_payload={"username": "u", "password": "p"},
                )

    def test_list_schemas_raises_when_layer_unavailable(self):
        with patch.object(
            helpers_module, "_import_introspection_engine",
            side_effect=helpers_module.IntrospectionEngineUnavailable("missing"),
        ):
            with pytest.raises(helpers_module.IntrospectionEngineUnavailable):
                helpers_module.list_schemas(
                    engine="postgres",
                    host="h", port=5432, database_name="d",
                    credential_payload={"username": "u", "password": "p"},
                )

    def test_validation_result_dataclass_shape(self):
        # The handler tests pass ValidationResult instances around -
        # confirm the dataclass shape so any refactor breaks loudly.
        r = helpers_module.ValidationResult(valid=True, server_version="15.4")
        assert r.valid is True
        assert r.server_version == "15.4"
        assert r.error is None

    def test_list_schemas_walks_introspector_result(self):
        """list_schemas should call introspector.introspect_database()
        and flatten the catalog/schema/table tree into SchemaInfo entries.
        Mocks the upstream introspector + config builder so the test
        runs without the databao package installed."""
        from dataclasses import dataclass as _dc, field

        @_dc
        class StubTable:
            name: str

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

        stub_result = StubResult(catalogs=[
            StubCatalog(name="analytics", schemas=[
                StubSchema(name="healthcare", tables=[
                    StubTable(name="patients"), StubTable(name="encounters"),
                ]),
                StubSchema(name="billing", tables=[StubTable(name="claims")]),
            ]),
        ])

        fake_introspector = MagicMock()
        fake_introspector.introspect_database.return_value = stub_result

        with patch.object(
            helpers_module, "_import_introspection_engine", return_value=MagicMock(),
        ), patch.object(
            helpers_module, "_build_config", return_value=MagicMock(),
        ), patch.object(
            helpers_module, "_get_introspector", return_value=fake_introspector,
        ):
            schemas = helpers_module.list_schemas(
                engine="mssql", host="h", port=1433, database_name="analytics",
                credential_payload={"username": "u", "password": "p"},
            )

        triples = [(s.catalog, s.schema, s.table) for s in schemas]
        assert triples == [
            ("analytics", "healthcare", "patients"),
            ("analytics", "healthcare", "encounters"),
            ("analytics", "billing", "claims"),
        ]

    def test_list_schemas_returns_empty_on_introspector_exception(self):
        """If the introspector raises (auth failure, network blip, etc.),
        list_schemas returns [] rather than bubbling — the picker UI
        surfaces an empty-list hint."""
        with patch.object(
            helpers_module, "_import_introspection_engine", return_value=MagicMock(),
        ), patch.object(
            helpers_module, "_build_config", return_value=MagicMock(),
        ), patch.object(
            helpers_module, "_get_introspector",
            side_effect=RuntimeError("upstream boom"),
        ):
            schemas = helpers_module.list_schemas(
                engine="mssql", host="h", port=1433, database_name="analytics",
                credential_payload={"username": "u", "password": "p"},
            )
        assert schemas == []

    def test_schema_info_dataclass_shape(self):
        s = helpers_module.SchemaInfo(
            catalog="c", schema="public", table="users", row_count_estimate=100,
        )
        assert s.catalog == "c"
        assert s.table == "users"
        assert s.row_count_estimate == 100

    def test_validate_connection_unsupported_engine(self):
        # The integration handler already gates on a supported-engine
        # allowlist, but the helper also rejects unknowns so a future
        # caller (or a typo) doesn't silently mis-route to the wrong
        # engine plugin.
        fake_dce = MagicMock()
        with patch.object(helpers_module, "_import_introspection_engine", return_value=fake_dce):
            r = helpers_module.validate_connection(
                engine="oracle",
                host="h", port=1521, database_name="d",
                credential_payload={"username": "u", "password": "p"},
            )
        assert r.valid is False
        assert "oracle" in (r.error or "").lower() or "unsupported" in (r.error or "").lower()

    def test_validate_connection_returns_valid_on_engine_VALID(self):
        # Stub the engine to report VALID; the helper should return
        # ValidationResult(valid=True).
        fake_dce = MagicMock()
        fake_dce.DatasourceConnectionStatus.VALID = "VALID_SENTINEL"
        fake_dce.DatasourceType.return_value = "ds_type"

        fake_mgr = MagicMock()
        check_result = MagicMock()
        check_result.connection_status = "VALID_SENTINEL"
        check_result.summary = None
        check_result.full_message = None
        fake_mgr.check_datasource_config_connection.return_value = check_result

        fake_dce.init_dce_domain.return_value = fake_mgr

        with patch.object(helpers_module, "_import_introspection_engine", return_value=fake_dce):
            # Patch the deferred config import too
            with patch(
                "databao_context_engine.plugins.databases.postgresql.config_file."
                "PostgresConfigFile",
                create=True,
            ):
                with patch(
                    "databao_context_engine.plugins.databases.postgresql.config_file."
                    "PostgresConnectionProperties",
                    create=True,
                ):
                    r = helpers_module.validate_connection(
                        engine="postgres",
                        host="h", port=5432, database_name="d",
                        credential_payload={"username": "u", "password": "p"},
                    )
        assert r.valid is True
        assert r.error is None

    def test_validate_connection_returns_invalid_with_full_message(self):
        # Stub the engine to report INVALID with both summary +
        # full_message; helper should surface the more useful
        # full_message in error.
        fake_dce = MagicMock()
        fake_dce.DatasourceConnectionStatus.VALID = "VALID_SENTINEL"
        fake_dce.DatasourceType.return_value = "ds_type"

        fake_mgr = MagicMock()
        check_result = MagicMock()
        check_result.connection_status = "INVALID_SENTINEL"
        check_result.summary = "Connection failed"
        check_result.full_message = "Access denied for user 'u'"
        fake_mgr.check_datasource_config_connection.return_value = check_result

        fake_dce.init_dce_domain.return_value = fake_mgr

        with patch.object(helpers_module, "_import_introspection_engine", return_value=fake_dce):
            with patch(
                "databao_context_engine.plugins.databases.mysql.config_file."
                "MySQLConfigFile",
                create=True,
            ):
                with patch(
                    "databao_context_engine.plugins.databases.mysql.config_file."
                    "MySQLConnectionProperties",
                    create=True,
                ):
                    r = helpers_module.validate_connection(
                        engine="mysql",
                        host="h", port=3306, database_name="d",
                        credential_payload={"username": "u", "password": "bad"},
                    )
        assert r.valid is False
        assert "Access denied" in (r.error or "")

    def test_mssql_login_failed_with_master_succeeding_returns_db_not_found_message(self):
        # MSSQL deliberately hides "database not found" behind a
        # "Login failed for user 'X'." string. When the master-DB
        # probe succeeds, we know auth is fine and the target DB is
        # the real problem - the helper should rewrite the error to
        # the friendlier "Cannot access database 'X'" message.
        fake_dce = MagicMock()
        fake_dce.DatasourceConnectionStatus.VALID = "VALID_SENTINEL"
        fake_dce.DatasourceType.return_value = "ds_type"

        # First check (target DB): INVALID + "Login failed for user".
        first_result = MagicMock()
        first_result.connection_status = "INVALID_SENTINEL"
        first_result.summary = "Connection failed"
        first_result.full_message = "Login failed for user 'outcomeops_ro'."

        # Second check (master probe): VALID.
        second_result = MagicMock()
        second_result.connection_status = "VALID_SENTINEL"
        second_result.summary = None
        second_result.full_message = None

        fake_mgr = MagicMock()
        fake_mgr.check_datasource_config_connection.side_effect = [
            first_result, second_result,
        ]
        fake_dce.init_dce_domain.return_value = fake_mgr

        with patch.object(helpers_module, "_import_introspection_engine", return_value=fake_dce):
            with patch(
                "databao_context_engine.plugins.databases.mssql.config_file."
                "MSSQLConfigFile",
                create=True,
            ):
                with patch(
                    "databao_context_engine.plugins.databases.mssql.config_file."
                    "MSSQLConnectionProperties",
                    create=True,
                ):
                    r = helpers_module.validate_connection(
                        engine="mssql",
                        host="h", port=1433, database_name="typo_db",
                        credential_payload={"username": "u", "password": "p"},
                    )
        assert r.valid is False
        assert "Cannot access database" in (r.error or "")
        assert "typo_db" in (r.error or "")
        # Two calls: target DB then master probe.
        assert fake_mgr.check_datasource_config_connection.call_count == 2

    def test_mssql_login_failed_with_master_also_failing_returns_original_error(self):
        # When the master probe also returns INVALID, auth genuinely
        # failed - keep the original "Login failed" message rather
        # than rewriting it (the user needs to fix their password,
        # not the database name).
        fake_dce = MagicMock()
        fake_dce.DatasourceConnectionStatus.VALID = "VALID_SENTINEL"
        fake_dce.DatasourceType.return_value = "ds_type"

        first_result = MagicMock()
        first_result.connection_status = "INVALID_SENTINEL"
        first_result.summary = "Connection failed"
        first_result.full_message = "Login failed for user 'outcomeops_ro'."

        second_result = MagicMock()
        second_result.connection_status = "INVALID_SENTINEL"
        second_result.summary = "Connection failed"
        second_result.full_message = "Login failed for user 'outcomeops_ro'."

        fake_mgr = MagicMock()
        fake_mgr.check_datasource_config_connection.side_effect = [
            first_result, second_result,
        ]
        fake_dce.init_dce_domain.return_value = fake_mgr

        with patch.object(helpers_module, "_import_introspection_engine", return_value=fake_dce):
            with patch(
                "databao_context_engine.plugins.databases.mssql.config_file."
                "MSSQLConfigFile",
                create=True,
            ):
                with patch(
                    "databao_context_engine.plugins.databases.mssql.config_file."
                    "MSSQLConnectionProperties",
                    create=True,
                ):
                    r = helpers_module.validate_connection(
                        engine="mssql",
                        host="h", port=1433, database_name="d",
                        credential_payload={"username": "u", "password": "bad"},
                    )
        assert r.valid is False
        assert "Login failed for user" in (r.error or "")
        assert "Cannot access database" not in (r.error or "")

    def test_postgres_login_failed_does_not_probe_master(self):
        # Postgres distinguishes auth failures from "db not found" at
        # the wire level, so the master-probe path is MSSQL-only.
        # Even if Postgres happens to return a "Login failed for user"
        # string, no second probe should fire.
        fake_dce = MagicMock()
        fake_dce.DatasourceConnectionStatus.VALID = "VALID_SENTINEL"
        fake_dce.DatasourceType.return_value = "ds_type"

        first_result = MagicMock()
        first_result.connection_status = "INVALID_SENTINEL"
        first_result.summary = "Connection failed"
        first_result.full_message = "Login failed for user 'u'"

        fake_mgr = MagicMock()
        fake_mgr.check_datasource_config_connection.return_value = first_result
        fake_dce.init_dce_domain.return_value = fake_mgr

        with patch.object(helpers_module, "_import_introspection_engine", return_value=fake_dce):
            with patch(
                "databao_context_engine.plugins.databases.postgresql.config_file."
                "PostgresConfigFile",
                create=True,
            ):
                with patch(
                    "databao_context_engine.plugins.databases.postgresql.config_file."
                    "PostgresConnectionProperties",
                    create=True,
                ):
                    r = helpers_module.validate_connection(
                        engine="postgres",
                        host="h", port=5432, database_name="d",
                        credential_payload={"username": "u", "password": "bad"},
                    )
        assert r.valid is False
        # Exactly one call - no master-DB probe for Postgres.
        assert fake_mgr.check_datasource_config_connection.call_count == 1


# ============================================================================
# Integration Handler Tests
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_handler_state(monkeypatch):
    handler_module._dynamodb = None
    handler_module._sqs = None
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    handler_module.DATABASE_SYNC_QUEUE = "https://sqs.example.com/db-sync"
    handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
    monkeypatch.setenv("AUDIT_TABLE", "test-audit")
    yield


class TestParsePathAndAuth:

    def test_parse_path_basic(self):
        assert handler_module._parse_path("/api/database/connections") == (
            "api", "database", "connections",
        )

    def test_parse_path_workspace(self):
        path = "/workspaces/ws1/database/connections/conn1/schemas"
        assert handler_module._parse_path(path) == (
            "workspaces", "ws1", "database", "connections", "conn1", "schemas",
        )

    def test_get_user_from_request(self):
        event = {"headers": {"x-user-email": "u@x.com"}}
        assert handler_module._get_user_from_request(event) == "u@x.com"

    def test_get_user_from_request_missing(self):
        assert handler_module._get_user_from_request({}) is None

    def test_host_hash_stable(self):
        h1 = handler_module._host_hash("db.example.com")
        h2 = handler_module._host_hash("db.example.com")
        h3 = handler_module._host_hash("OTHER.example.com")
        assert h1 == h2  # deterministic
        assert h1 != h3  # different hosts -> different hashes
        assert len(h1) == 16  # short, stable


class TestValidate:

    @patch.object(handler_module, "validate_db_connection")
    def test_validate_credentials_unsupported_engine(self, mock_v):
        body = {"engine": "oracle", "host": "h", "port": 1521, "database_name": "d", "credential": {}}
        result = handler_module.validate_credentials(body)
        assert result["statusCode"] == 400
        mock_v.assert_not_called()

    @patch.object(handler_module, "validate_db_connection")
    def test_validate_credentials_missing_fields(self, mock_v):
        body = {"engine": "postgres", "host": "", "port": 0, "database_name": "", "credential": {}}
        result = handler_module.validate_credentials(body)
        assert result["statusCode"] == 400
        mock_v.assert_not_called()

    @patch.object(handler_module, "validate_db_connection")
    def test_validate_credentials_success(self, mock_v):
        mock_v.return_value = helpers_module.ValidationResult(
            valid=True, server_version="15.4"
        )
        body = {
            "engine": "postgres", "host": "h", "port": 5432,
            "database_name": "d",
            "credential": {"engine": "postgres", "username": "u", "password": "p"},
        }
        result = handler_module.validate_credentials(body)
        assert result["statusCode"] == 200
        data = json.loads(result["body"])
        assert data["valid"] is True
        assert data["server_version"] == "15.4"


class TestCreateConnection:

    @patch.object(handler_module, "log_admin_action")
    @patch.object(handler_module, "save_credential")
    @patch.object(handler_module, "validate_db_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_create_connection_success(
        self, mock_db_fn, mock_validate, mock_save, mock_audit
    ):
        mock_table = MagicMock()
        # Workspace admin lookup
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        mock_validate.return_value = helpers_module.ValidationResult(
            valid=True, server_version="15.4"
        )

        body = {
            "workspace_id": "ws1",
            "engine": "postgres",
            "host": "db.example.com",
            "port": 5432,
            "database_name": "analytics",
            "display_label": "Analytics DW",
            "credential": {"engine": "postgres", "username": "u", "password": "p"},
        }
        result = handler_module.create_connection("u@x.com", body)
        assert result["statusCode"] == 201

        # Connection record stored with the right shape
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "WS#ws1"
        assert item["SK"].startswith("DATABASE_CONNECTION#")
        assert item["engine"] == "postgres"
        assert item["host"] == "db.example.com"
        assert item["display_label"] == "Analytics DW"
        # host_hash populated and stable-length
        assert len(item["host_hash"]) == 16
        # Default scope_config (chunk 06): empty schema-level include
        # for both introspection and sampling, profiling on.
        assert item["scope_config"]["introspection"]["include"] == []
        assert item["scope_config"]["sampling"]["include"] == []
        assert "enabled" not in item["scope_config"]["sampling"]
        assert item["scope_config"]["profiling"]["enabled"] is True
        assert item["last_sync_status"] == "needs_configuration"
        assert item["known_table_ids"] == []

        # Credential saved separately under DB_CREDENTIAL#
        mock_save.assert_called_once()
        saved_conn_id, saved_payload = mock_save.call_args[0]
        assert saved_conn_id == item["connection_id"]
        assert saved_payload == body["credential"]

        # Audit row emitted
        mock_audit.assert_called_once()
        audit_kwargs = mock_audit.call_args[1]
        assert audit_kwargs["details"]["provider"] == "database"
        assert audit_kwargs["details"]["engine"] == "postgres"
        assert audit_kwargs["details"]["host_hash"] == item["host_hash"]

    @patch.object(handler_module, "validate_db_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_create_connection_non_admin_403(self, mock_db_fn, mock_validate):
        mock_table = MagicMock()
        # Member but not admin and not org_admin
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},  # not org_admin
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        body = {
            "workspace_id": "ws1",
            "engine": "postgres",
            "host": "h", "port": 5432, "database_name": "d",
            "credential": {"engine": "postgres", "username": "u", "password": "p"},
        }
        result = handler_module.create_connection("u@x.com", body)
        assert result["statusCode"] == 403
        mock_validate.assert_not_called()

    @patch.object(handler_module, "save_credential")
    @patch.object(handler_module, "validate_db_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_create_connection_validation_fails_returns_400(
        self, mock_db_fn, mock_validate, mock_save
    ):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        mock_validate.return_value = helpers_module.ValidationResult(
            valid=False, error="connection refused"
        )

        body = {
            "workspace_id": "ws1",
            "engine": "postgres",
            "host": "h", "port": 5432, "database_name": "d",
            "credential": {"engine": "postgres", "username": "u", "password": "p"},
        }
        result = handler_module.create_connection("u@x.com", body)
        assert result["statusCode"] == 400
        # Credential NOT saved when validation fails - no orphan secrets
        mock_save.assert_not_called()
        # DB record NOT created
        mock_table.put_item.assert_not_called()


class TestListConnections:

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_success(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {
            "Items": [
                {
                    "PK": "WS#ws1",
                    "SK": "DATABASE_CONNECTION#c1",
                    "connection_id": "c1",
                    "engine": "postgres",
                    "host": "h",
                    "host_hash": "abcd1234abcd1234",
                    "known_table_ids": ["foo", "bar"],  # large internal-state field
                }
            ]
        }
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.list_connections("u@x.com", "ws1")
        body = json.loads(result["body"])
        assert len(body["connections"]) == 1
        # known_table_ids stripped (noisy)
        assert "known_table_ids" not in body["connections"][0]
        # engine + host_hash preserved
        assert body["connections"][0]["engine"] == "postgres"
        assert body["connections"][0]["host_hash"] == "abcd1234abcd1234"

    @patch.object(handler_module, "_get_dynamodb")
    def test_list_connections_not_member(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no membership
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.list_connections("u@x.com", "ws1")
        assert result["statusCode"] == 403


class TestUpdateScope:

    def _admin_mocks(self, mock_db_fn, mock_get_conn):
        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": {"role": "workspace_admin"}},
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_get_conn.return_value = {"connection_id": "c1"}

    @patch.object(handler_module, "_queue_sync")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_scope_admin_queues_sync_with_schema_keys(
        self, mock_db_fn, mock_get_conn, mock_queue,
    ):
        """Chunk 06 happy path: 2-segment 'catalog.schema' keys accepted."""
        self._admin_mocks(mock_db_fn, mock_get_conn)
        body = {
            "scope_config": {
                "introspection": {"include": ["analytics.healthcare"], "exclude": []},
                "sampling":      {"include": [], "exclude": []},
                "profiling":     {"enabled": True},
            }
        }
        result = handler_module.update_scope("u@x.com", "ws1", "c1", body)
        assert result["statusCode"] == 200
        mock_queue.assert_called_once_with("ws1", "c1")

    @patch.object(handler_module, "_queue_sync")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_scope_rejects_legacy_three_segment_include(
        self, mock_db_fn, mock_get_conn, mock_queue,
    ):
        """Chunk 06 strict-validation: 'catalog.schema.table' returns 400.
        Connections were disconnected pre-deploy so no legacy survives."""
        self._admin_mocks(mock_db_fn, mock_get_conn)
        body = {
            "scope_config": {
                "introspection": {"include": ["analytics.healthcare.patients"]},
                "sampling":      {"include": []},
            }
        }
        result = handler_module.update_scope("u@x.com", "ws1", "c1", body)
        assert result["statusCode"] == 400
        assert "2 segments" in result["body"]
        mock_queue.assert_not_called()

    @patch.object(handler_module, "_queue_sync")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_scope_rejects_malformed_sampling_include(
        self, mock_db_fn, mock_get_conn, mock_queue,
    ):
        self._admin_mocks(mock_db_fn, mock_get_conn)
        body = {
            "scope_config": {
                "introspection": {"include": ["analytics.healthcare"]},
                "sampling":      {"include": ["analytics"]},  # 1-segment
            }
        }
        result = handler_module.update_scope("u@x.com", "ws1", "c1", body)
        assert result["statusCode"] == 400
        assert "sampling.include" in result["body"]
        mock_queue.assert_not_called()

    @patch.object(handler_module, "_get_dynamodb")
    def test_update_scope_non_admin_403(self, mock_db_fn):
        mock_table = MagicMock()
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db

        result = handler_module.update_scope("u@x.com", "ws1", "c1", {})
        assert result["statusCode"] == 403


class TestDeleteConnection:

    @patch.object(handler_module, "log_admin_action")
    @patch.object(handler_module, "delete_credential")
    @patch.object(handler_module, "_get_sqs")
    @patch.object(handler_module, "get_connection")
    @patch.object(handler_module, "_get_dynamodb")
    def test_delete_connection_queues_delete_prefix_and_audits(
        self, mock_db_fn, mock_get_conn, mock_sqs_fn, _del_cred, mock_audit
    ):
        mock_table = MagicMock()
        # Two membership lookups: _can_access + _is_workspace_admin
        mock_table.get_item.side_effect = [
            {"Item": {"role": "workspace_admin"}},
            {"Item": {"role": "workspace_admin"}},
        ]
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_get_conn.return_value = {
            "connection_id": "c1", "engine": "postgres", "host_hash": "h",
        }
        mock_sqs = MagicMock()
        mock_sqs_fn.return_value = mock_sqs

        result = handler_module.delete_connection("u@x.com", "ws1", "c1")
        assert result["statusCode"] == 200

        # Cleanup queued to delete-worker with source="database"
        sent = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert sent == {
            "action": "delete_prefix",
            "workspace_id": "ws1",
            "source": "database",
            "prefix": "c1/",
        }

        # Audit row emitted
        mock_audit.assert_called_once()
        audit_kwargs = mock_audit.call_args[1]
        assert audit_kwargs["details"]["provider"] == "database"
        assert audit_kwargs["details"]["engine"] == "postgres"


class TestLambdaHandlerRouting:

    def test_404_for_unknown_route(self):
        event = {
            "httpMethod": "GET",
            "path": "/totally/unknown",
            "headers": {"x-user-email": "u@x.com"},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_routes_validate(self):
        with patch.object(handler_module, "validate_credentials") as mock_v:
            mock_v.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "POST",
                "path": "/api/database/connections/validate",
                "headers": {"x-user-email": "u@x.com"},
                "body": json.dumps({"engine": "postgres"}),
            }
            handler_module.lambda_handler(event, None)
            mock_v.assert_called_once()

    def test_routes_create(self):
        with patch.object(handler_module, "create_connection") as mock_c:
            mock_c.return_value = {"statusCode": 201, "body": "{}"}
            event = {
                "httpMethod": "POST",
                "path": "/api/database/connections",
                "headers": {"x-user-email": "u@x.com"},
                "body": json.dumps({"workspace_id": "ws1"}),
            }
            handler_module.lambda_handler(event, None)
            mock_c.assert_called_once()

    def test_routes_list(self):
        with patch.object(handler_module, "list_connections") as mock_l:
            mock_l.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "GET",
                "path": "/workspaces/ws1/database/connections",
                "headers": {"x-user-email": "u@x.com"},
            }
            handler_module.lambda_handler(event, None)
            mock_l.assert_called_once_with("u@x.com", "ws1")

    def test_routes_schemas(self):
        with patch.object(handler_module, "get_schemas") as mock_s:
            mock_s.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "GET",
                "path": "/workspaces/ws1/database/connections/c1/schemas",
                "headers": {"x-user-email": "u@x.com"},
            }
            handler_module.lambda_handler(event, None)
            mock_s.assert_called_once_with("u@x.com", "ws1", "c1")

    def test_routes_scope(self):
        with patch.object(handler_module, "update_scope") as mock_u:
            mock_u.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "PUT",
                "path": "/workspaces/ws1/database/connections/c1/scope",
                "headers": {"x-user-email": "u@x.com"},
                "body": json.dumps({}),
            }
            handler_module.lambda_handler(event, None)
            mock_u.assert_called_once()

    def test_routes_sync(self):
        with patch.object(handler_module, "trigger_sync") as mock_t:
            mock_t.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "POST",
                "path": "/workspaces/ws1/database/connections/c1/sync",
                "headers": {"x-user-email": "u@x.com"},
            }
            handler_module.lambda_handler(event, None)
            mock_t.assert_called_once_with("u@x.com", "ws1", "c1")

    def test_routes_delete(self):
        with patch.object(handler_module, "delete_connection") as mock_d:
            mock_d.return_value = {"statusCode": 200, "body": "{}"}
            event = {
                "httpMethod": "DELETE",
                "path": "/workspaces/ws1/database/connections/c1",
                "headers": {"x-user-email": "u@x.com"},
            }
            handler_module.lambda_handler(event, None)
            mock_d.assert_called_once_with("u@x.com", "ws1", "c1")

    def test_missing_user_email_401(self):
        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/database/connections",
            "headers": {},
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 401
