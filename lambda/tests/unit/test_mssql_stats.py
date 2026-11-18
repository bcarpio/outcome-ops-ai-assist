# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lambda/shared/mssql_stats.py.

Covers the type-categorization helpers, SQL-generation helpers, result
mapping, and the end-to-end collect_mssql_stats function (with a mocked
connector + scope).
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

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

_mod_path = os.path.join(
    os.path.dirname(__file__), "..", "..", "shared", "mssql_stats.py"
)
_spec = importlib.util.spec_from_file_location("mssql_stats", _mod_path)
mssql_stats = importlib.util.module_from_spec(_spec)
sys.modules["mssql_stats"] = mssql_stats
_spec.loader.exec_module(mssql_stats)


# ============================================================================
# Type categorization
# ============================================================================


class TestTypeCategorization:

    def test_plain_types_keep_all_stats(self):
        for t in ["int", "varchar(50)", "date", "datetime2", "decimal(10,2)", "char(4)"]:
            assert not mssql_stats._skip_distinct(t)
            assert not mssql_stats._skip_min_max(t)

    def test_wide_text_skips_everything(self):
        for t in ["text", "ntext", "varchar(max)", "nvarchar(MAX)", "image", "xml"]:
            assert mssql_stats._skip_distinct(t)
            assert mssql_stats._skip_min_max(t)

    def test_bit_skips_min_max_but_keeps_distinct(self):
        # The whole reason this nuance exists - bit columns can't MIN/MAX
        # but we still want to know there are 2 distinct values.
        assert not mssql_stats._skip_distinct("bit")
        assert mssql_stats._skip_min_max("bit")

    def test_binary_skips_min_max_keeps_distinct(self):
        assert not mssql_stats._skip_distinct("binary(16)")
        assert mssql_stats._skip_min_max("binary(16)")
        assert not mssql_stats._skip_distinct("varbinary(50)")
        assert mssql_stats._skip_min_max("varbinary(50)")

    def test_varbinary_max_skips_everything(self):
        # The (MAX) marker beats the binary-prefix rule.
        assert mssql_stats._skip_distinct("varbinary(MAX)")
        assert mssql_stats._skip_min_max("varbinary(MAX)")

    def test_none_type_safe(self):
        assert not mssql_stats._skip_distinct(None)
        assert not mssql_stats._skip_min_max(None)


# ============================================================================
# Identifier quoting
# ============================================================================


class TestQuoteIdent:

    def test_simple_name(self):
        assert mssql_stats._quote_ident("patients") == "[patients]"

    def test_escapes_closing_bracket(self):
        # T-SQL escape is ]] inside [ ... ]
        assert mssql_stats._quote_ident("weird]name") == "[weird]]name]"

    def test_empty_safe(self):
        assert mssql_stats._quote_ident("") == "[]"


# ============================================================================
# SQL construction
# ============================================================================


class TestRowcountSQL:

    def test_single_schema(self):
        sql = mssql_stats._build_table_rowcount_sql(["healthcare"])
        assert "sys.partitions" in sql
        assert "p.index_id IN (0, 1)" in sql
        assert "s.name IN ('healthcare')" in sql

    def test_multiple_schemas(self):
        sql = mssql_stats._build_table_rowcount_sql(["healthcare", "billing"])
        assert "s.name IN ('healthcare', 'billing')" in sql

    def test_schema_apostrophe_escaped(self):
        # Defensive - schema names with apostrophes shouldn't break the query.
        sql = mssql_stats._build_table_rowcount_sql(["weird's"])
        assert "'weird''s'" in sql


class TestColumnStatsSQL:

    def test_all_plain_columns(self):
        sql, names, flags = mssql_stats._build_column_stats_sql(
            "analytics", "healthcare", "patients",
            [("patient_id", "int"), ("email", "varchar(120)")],
        )
        assert "[analytics].[healthcare].[patients]" in sql
        assert "COUNT(*) AS _total_rows" in sql
        assert "COUNT([patient_id]) AS _nonnull_0" in sql
        assert "COUNT(DISTINCT [patient_id]) AS _distinct_0" in sql
        assert "MIN([patient_id]) AS _min_0" in sql
        assert "MAX([patient_id]) AS _max_0" in sql
        assert "COUNT([email]) AS _nonnull_1" in sql
        assert "COUNT(DISTINCT [email]) AS _distinct_1" in sql
        assert names == ["patient_id", "email"]
        assert flags == [(False, False), (False, False)]

    def test_wide_text_column_omits_distinct_and_min_max(self):
        sql, _, flags = mssql_stats._build_column_stats_sql(
            "db", "sch", "tbl",
            [("notes", "nvarchar(MAX)")],
        )
        assert "COUNT([notes]) AS _nonnull_0" in sql
        assert "_distinct_0" not in sql
        assert "_min_0" not in sql
        assert "_max_0" not in sql
        assert flags == [(True, True)]

    def test_bit_column_keeps_distinct_omits_min_max(self):
        # The diagnoses.primary_flag case from the live smoke test.
        sql, _, flags = mssql_stats._build_column_stats_sql(
            "db", "sch", "tbl",
            [("primary_flag", "bit")],
        )
        assert "COUNT([primary_flag]) AS _nonnull_0" in sql
        assert "COUNT(DISTINCT [primary_flag]) AS _distinct_0" in sql
        assert "_min_0" not in sql
        assert "_max_0" not in sql
        assert flags == [(False, True)]


# ============================================================================
# Result mapping
# ============================================================================


@databao_required
class TestRowToColumnStats:

    def test_full_plain_column(self):
        row = {
            "_total_rows": 300,
            "_nonnull_0": 300,
            "_distinct_0": 300,
            "_min_0": "MRN0000001",
            "_max_0": "MRN0000300",
        }
        out = mssql_stats._row_to_column_stats(
            row,
            column_names=["mrn"],
            skip_flags=[(False, False)],
            schema_name="healthcare",
            table_name="patients",
            total_rows=300,
        )
        assert len(out) == 1
        e = out[0]
        assert e.schema_name == "healthcare"
        assert e.table_name == "patients"
        assert e.column_name == "mrn"
        assert e.stats.null_count == 0
        assert e.stats.non_null_count == 300
        assert e.stats.distinct_count == 300
        assert e.stats.min_value == "MRN0000001"
        assert e.stats.max_value == "MRN0000300"
        assert e.stats.total_row_count == 300

    def test_bit_column_omits_min_max(self):
        row = {
            "_total_rows": 1200,
            "_nonnull_0": 1200,
            "_distinct_0": 2,
            # no _min_0 / _max_0 keys - skip_mm True
        }
        out = mssql_stats._row_to_column_stats(
            row,
            column_names=["primary_flag"],
            skip_flags=[(False, True)],
            schema_name="healthcare",
            table_name="diagnoses",
            total_rows=1200,
        )
        e = out[0]
        assert e.stats.distinct_count == 2
        assert e.stats.min_value is None
        assert e.stats.max_value is None
        # Cardinality bucket still computes off distinct_count.
        from databao_context_engine.plugins.databases.databases_types import (
            CardinalityBucket,
        )
        assert e.stats.cardinality_kind == CardinalityBucket.VERY_LOW

    def test_wide_text_column_only_has_null_counts(self):
        row = {
            "_total_rows": 800,
            "_nonnull_0": 600,
            # no distinct/min/max keys
        }
        out = mssql_stats._row_to_column_stats(
            row,
            column_names=["chief_complaint"],
            skip_flags=[(True, True)],
            schema_name="healthcare",
            table_name="encounters",
            total_rows=800,
        )
        e = out[0]
        assert e.stats.null_count == 200
        assert e.stats.non_null_count == 600
        assert e.stats.distinct_count is None
        assert e.stats.min_value is None
        assert e.stats.max_value is None

    def test_null_total_rows_does_not_crash(self):
        row = {"_total_rows": None, "_nonnull_0": None}
        out = mssql_stats._row_to_column_stats(
            row,
            column_names=["x"],
            skip_flags=[(False, False)],
            schema_name="s", table_name="t",
            total_rows=None,
        )
        assert out[0].stats.null_count is None
        assert out[0].stats.non_null_count is None


# ============================================================================
# End-to-end collect_mssql_stats
# ============================================================================


class _StubColumn:
    def __init__(self, name, type_str):
        self.name = name
        self.type = type_str


class _StubKind:
    def __init__(self, value):
        self.value = value


class _StubTableRef:
    def __init__(self, table_name, kind_value, columns):
        self.table_name = table_name
        self.kind = _StubKind(kind_value)
        self.columns = columns


class _StubSchemaScope:
    def __init__(self, schema_name, tables):
        self.schema_name = schema_name
        self.tables = tables


class _StubScope:
    def __init__(self, schemas):
        self.schemas = schemas


@databao_required
class TestCollectMssqlStats:

    @pytest.fixture
    def scope(self):
        return _StubScope([
            _StubSchemaScope("healthcare", [
                _StubTableRef("patients", "table", [
                    _StubColumn("patient_id", "int"),
                    _StubColumn("email", "varchar(120)"),
                ]),
                _StubTableRef("notes_view", "view", [
                    _StubColumn("body", "nvarchar(MAX)"),
                ]),
            ]),
        ])

    def test_skips_views(self, scope):
        connector = MagicMock()
        # Pass 1: rowcount returns one entry for the patients table.
        # Pass 2: should only fire for patients, NOT notes_view.
        connector.execute.side_effect = [
            [{"schema_name": "healthcare", "table_name": "patients", "row_count": 300}],
            [{
                "_total_rows": 300,
                "_nonnull_0": 300, "_distinct_0": 300, "_min_0": 1, "_max_0": 300,
                "_nonnull_1": 280, "_distinct_1": 280,
                "_min_1": "a@x.test", "_max_1": "z@x.test",
            }],
        ]

        table_stats, column_stats = mssql_stats.collect_mssql_stats(
            connector, connection=MagicMock(),
            catalog="analytics", scope=scope,
        )

        # Pass 1 (rowcount) + Pass 2 for patients only = 2 execute calls
        assert connector.execute.call_count == 2
        assert len(table_stats) == 1
        assert table_stats[0].table_name == "patients"
        assert table_stats[0].stats.row_count == 300
        # patients has 2 columns -> 2 column stats; notes_view skipped.
        assert len(column_stats) == 2
        assert {(c.column_name, c.stats.null_count) for c in column_stats} == {
            ("patient_id", 0), ("email", 20),
        }

    def test_per_table_failure_is_isolated(self, scope):
        # Drop the view so we have 2 tables instead of 1.
        scope.schemas[0].tables.append(_StubTableRef("encounters", "table", [
            _StubColumn("encounter_id", "int"),
        ]))
        connector = MagicMock()

        def execute_side_effect(_conn, sql, _params):
            if "sys.partitions" in sql:
                return [
                    {"schema_name": "healthcare", "table_name": "patients", "row_count": 300},
                    {"schema_name": "healthcare", "table_name": "encounters", "row_count": 800},
                ]
            # First per-table query for patients - blow up.
            if "[patients]" in sql:
                raise RuntimeError("simulated upstream failure")
            # Second per-table query for encounters - succeed.
            return [{"_total_rows": 800, "_nonnull_0": 800, "_distinct_0": 800,
                     "_min_0": 1, "_max_0": 800}]

        connector.execute.side_effect = execute_side_effect
        table_stats, column_stats = mssql_stats.collect_mssql_stats(
            connector, connection=MagicMock(),
            catalog="analytics", scope=scope,
        )
        # Both tables get row_count from pass 1.
        assert len(table_stats) == 2
        # Only encounters' column stats survived the per-table failure
        # in patients.
        assert {c.column_name for c in column_stats} == {"encounter_id"}

    def test_empty_scope_returns_empty(self):
        connector = MagicMock()
        scope = _StubScope([])
        table_stats, column_stats = mssql_stats.collect_mssql_stats(
            connector, connection=MagicMock(),
            catalog="analytics", scope=scope,
        )
        assert table_stats == []
        assert column_stats == []
        connector.execute.assert_not_called()

    def test_pass1_rowcount_failure_does_not_block_pass2(self, scope):
        connector = MagicMock()

        def execute_side_effect(_conn, sql, _params):
            if "sys.partitions" in sql:
                raise RuntimeError("VIEW DATABASE STATE denied or similar")
            return [{
                "_total_rows": 300,
                "_nonnull_0": 300, "_distinct_0": 300, "_min_0": 1, "_max_0": 300,
                "_nonnull_1": 280, "_distinct_1": 280,
                "_min_1": "a@x.test", "_max_1": "z@x.test",
            }]

        connector.execute.side_effect = execute_side_effect
        table_stats, column_stats = mssql_stats.collect_mssql_stats(
            connector, connection=MagicMock(),
            catalog="analytics", scope=scope,
        )
        # Pass 1 failed silently; table_stats empty but column_stats
        # still got computed off the COUNT(*) in pass 2.
        assert table_stats == []
        assert len(column_stats) == 2
        # total_row_count fell back to the COUNT(*) result, not pass-1.
        assert column_stats[0].stats.total_row_count == 300
