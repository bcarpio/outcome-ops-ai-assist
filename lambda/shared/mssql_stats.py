"""
MSSQL aggregate-stats override for the upstream Databao introspector.

The upstream `MSSQLIntrospector` does not implement `collect_stats` — it
inherits the no-op base class implementation that returns `(None, None)`.
Postgres and MySQL both override it, which is why those engines come
back from `introspect_database()` with populated `table.stats` and
`column.stats` fields while MSSQL comes back with `stats: null` on
every table and column.

This module ships a thin subclass `OutcomeOpsMSSQLIntrospector` that
provides the missing `collect_stats` implementation. Both Lambdas
import it and wire it into their `_get_introspector*` helpers in
place of the upstream class.

Two SQL passes per `collect_stats` call:

  1. **Table row counts** — single `sys.partitions` query covering all
     in-scope schemas. `db_datareader` membership is sufficient
     (catalog-view visibility follows SELECT grants on the
     underlying tables, which db_datareader provides).

  2. **Per-column stats** — one composite SELECT per table with
     `COUNT(*)`, `COUNT([col])`, `COUNT(DISTINCT [col])`, `MIN([col])`,
     `MAX([col])` expressions in the projection list. One round trip
     per table, not per column. Wide-text / blob / MAX-length / spatial
     column types skip the `COUNT(DISTINCT)` / `MIN` / `MAX` slots
     because their distinct-sort cost is unbounded; they still
     contribute `null_count` / `non_null_count`.

When JetBrains ships their own `collect_stats` upstream, retire this
module by removing the wiring in `_get_introspector_for_sync` and
`_get_introspector` and deleting this file. The stats-shape contract
this module produces matches the upstream `TableStatsEntry` /
`ColumnStatsEntry` dataclasses exactly so downstream consumers
(the `_build_table_chunk_yaml` serializer in database-sync, the KB
embedding layer, MCP retrieval) need no changes.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Tuple

logger = logging.getLogger(__name__)


# MSSQL types where COUNT(DISTINCT) sorts the whole table by the column;
# for warehouse-scale tables that's an unbounded cost. Also captures
# types that don't have a usable order at all (xml, sql_variant). For
# these we emit ONLY COUNT(non-null) — no distinct/min/max.
_SKIP_DISTINCT_PREFIXES: tuple = (
    "text", "ntext",                # legacy unbounded text
    "image",                        # legacy unbounded binary
    "xml",                          # variable XML
    "geography", "geometry",        # spatial - no ordering
    "hierarchyid",                  # opaque path
    "sql_variant",                  # heterogeneous - distinct ill-defined
)

# MSSQL types where COUNT(DISTINCT) works fine but MIN/MAX raises
# "Operand data type X is invalid for min operator" at the server.
# Distinct still gives us useful cardinality (e.g., a bit column will
# return 1 or 2); min/max have no meaning anyway.
_SKIP_MIN_MAX_PREFIXES: tuple = (
    "bit",                          # boolean - no order operator
    "binary", "varbinary",          # raw bytes - order is meaningless
)


def _skip_distinct(type_str: str | None) -> bool:
    """True if COUNT(DISTINCT col) should be omitted for this MSSQL type.

    Catches `(max)` length variants (varchar(MAX), nvarchar(MAX),
    varbinary(MAX)) and the unbounded / unordered prefix list.
    """
    if not type_str:
        return False
    t = type_str.lower()
    if "(max)" in t:
        return True
    return any(t.startswith(p) for p in _SKIP_DISTINCT_PREFIXES)


def _skip_min_max(type_str: str | None) -> bool:
    """True if MIN(col) / MAX(col) should be omitted for this MSSQL type.

    Any column that skips distinct also skips min/max (the wide-text
    case). Additionally, bit / binary types have distinct values worth
    reporting but no usable min/max.
    """
    if _skip_distinct(type_str):
        return True
    if not type_str:
        return False
    t = type_str.lower()
    return any(t.startswith(p) for p in _SKIP_MIN_MAX_PREFIXES)


def _quote_ident(ident: str) -> str:
    """Quote an MSSQL identifier in [brackets], escaping internal ]
    per T-SQL rules. Returns ``[escaped]``.
    """
    return "[" + (ident or "").replace("]", "]]") + "]"


def _build_table_rowcount_sql(schema_names: list[str]) -> str:
    """SQL to read approximate row counts for every table in the given
    schemas from sys.partitions. index_id IN (0, 1) covers the
    heap-or-clustered base partitions exactly once per table.
    """
    # Schema names are sourced from upstream introspector output, not
    # user input — safe to inline. Single-quote any embedded apostrophes
    # defensively anyway.
    schema_list = ", ".join("'" + s.replace("'", "''") + "'" for s in schema_names)
    return f"""
        SELECT
            s.name AS schema_name,
            t.name AS table_name,
            SUM(p.rows) AS row_count
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.partitions p ON p.object_id = t.object_id
        WHERE p.index_id IN (0, 1)
          AND s.name IN ({schema_list})
        GROUP BY s.name, t.name
    """.strip()


def _build_column_stats_sql(
    catalog: str,
    schema: str,
    table: str,
    columns: list[tuple[str, str | None]],
) -> tuple[str, list[str], list[tuple[bool, bool]]]:
    """Build the composite per-table column-stats SELECT.

    Args:
        catalog: database name (e.g. "analytics"). MSSQL three-part name
            is required when the connection might be against a different
            default database.
        schema, table: target table identifiers.
        columns: list of (column_name, type_str) tuples in the same
            order the caller wants to map them back.

    Returns:
        (sql, column_names_in_alias_order, skip_flags)

        - sql: composite SELECT projecting `_total_rows`, plus
          `_nonnull_<idx>` for every column and `_distinct_<idx>` /
          `_min_<idx>` / `_max_<idx>` for columns whose types allow.
        - column_names_in_alias_order: column names in the order the
          aliases were generated, for result mapping.
        - skip_flags: parallel list of (skip_distinct, skip_min_max)
          per column. skip_min_max is implied by skip_distinct (the
          wide-text case) but tracked separately so the result mapper
          knows which keys to expect.

    All aliases are positional (`_nonnull_0`, `_distinct_0`, ...) to
    avoid clashing with column names that happen to contain SQL
    keywords or special chars.
    """
    select_parts: list[str] = ["COUNT(*) AS _total_rows"]
    skip_flags: list[tuple[bool, bool]] = []
    column_names: list[str] = []

    for idx, (col_name, type_str) in enumerate(columns):
        quoted = _quote_ident(col_name)
        skip_dist = _skip_distinct(type_str)
        skip_mm = _skip_min_max(type_str)
        skip_flags.append((skip_dist, skip_mm))
        column_names.append(col_name)

        select_parts.append(f"COUNT({quoted}) AS _nonnull_{idx}")
        if not skip_dist:
            select_parts.append(f"COUNT(DISTINCT {quoted}) AS _distinct_{idx}")
        if not skip_mm:
            select_parts.append(f"MIN({quoted}) AS _min_{idx}")
            select_parts.append(f"MAX({quoted}) AS _max_{idx}")

    fq_table = f"{_quote_ident(catalog)}.{_quote_ident(schema)}.{_quote_ident(table)}"
    sql = "SELECT " + ", ".join(select_parts) + f" FROM {fq_table}"
    return sql, column_names, skip_flags


def _row_to_column_stats(
    row: dict,
    column_names: list[str],
    skip_flags: list[tuple[bool, bool]],
    schema_name: str,
    table_name: str,
    total_rows: int | None,
) -> list:
    """Map one composite-query result row into a list of
    `ColumnStatsEntry` instances, one per column.

    Imports the upstream entry types lazily so the module is importable
    in test envs without `databao_context_engine` installed.
    """
    from databao_context_engine.plugins.databases.databases_types import (
        ColumnStats, ColumnStatsEntry,
    )
    from databao_context_engine.plugins.databases.base_introspector import (
        BaseIntrospector,
    )

    out = []
    for idx, (col_name, (skip_dist, skip_mm)) in enumerate(zip(column_names, skip_flags)):
        non_null = row.get(f"_nonnull_{idx}")
        non_null = int(non_null) if non_null is not None else None

        null_count = None
        if total_rows is not None and non_null is not None:
            null_count = max(total_rows - non_null, 0)

        distinct_count = None
        if not skip_dist:
            distinct_raw = row.get(f"_distinct_{idx}")
            distinct_count = int(distinct_raw) if distinct_raw is not None else None

        min_value: Any = None
        max_value: Any = None
        if not skip_mm:
            min_value = row.get(f"_min_{idx}")
            max_value = row.get(f"_max_{idx}")

        cardinality_kind, _ = BaseIntrospector._compute_cardinality_stats(distinct_count)

        out.append(ColumnStatsEntry(
            schema_name=schema_name,
            table_name=table_name,
            column_name=col_name,
            stats=ColumnStats(
                null_count=null_count,
                non_null_count=non_null,
                distinct_count=distinct_count,
                cardinality_kind=cardinality_kind,
                min_value=min_value,
                max_value=max_value,
                total_row_count=total_rows,
            ),
        ))
    return out


def collect_mssql_stats(connector, connection, catalog: str, scope) -> tuple[list, list]:
    """Mirror of `BaseIntrospector.collect_stats` for MSSQL.

    Walks the in-scope catalog/schema/table tree (passed in via the
    upstream `CatalogScope`), runs:

      1. One `sys.partitions` query for table row counts.
      2. One composite `SELECT` per table for per-column stats.

    Returns `(table_stats, column_stats)` — lists of `TableStatsEntry`
    and `ColumnStatsEntry` respectively. On any per-table failure the
    table's stats are skipped (best-effort: a query timeout on one
    wide table shouldn't poison the whole pass).
    """
    from databao_context_engine.plugins.databases.databases_types import (
        TableStats, TableStatsEntry,
    )

    schema_names = [s.schema_name for s in scope.schemas]
    if not schema_names:
        return [], []

    # --- Pass 1: table row counts ---
    table_stats: list = []
    row_count_by_key: dict = {}
    try:
        rowcount_rows = connector.execute(
            connection, _build_table_rowcount_sql(schema_names), (),
        )
        for r in rowcount_rows:
            sch = r.get("schema_name")
            tbl = r.get("table_name")
            rc_raw = r.get("row_count")
            row_count = int(rc_raw) if rc_raw is not None else None
            if sch is None or tbl is None:
                continue
            row_count_by_key[(sch, tbl)] = row_count
            table_stats.append(TableStatsEntry(
                schema_name=sch,
                table_name=tbl,
                stats=TableStats(row_count=row_count, approximate=True),
            ))
    except Exception as e:
        logger.warning("MSSQL table row-count pass failed; "
                       "table.stats will be null: %s", e)

    # --- Pass 2: per-table column stats ---
    column_stats: list = []
    for schema_scope in scope.schemas:
        schema_name = schema_scope.schema_name
        for table_ref in schema_scope.tables:
            # Only handle tables (skip views — projecting MIN/MAX/COUNT
            # DISTINCT against a view is correct but uncached and the
            # cost is unpredictable; views currently aren't introspected
            # for stats by Postgres or MySQL either).
            if table_ref.kind.value != "table":
                continue
            table_name = table_ref.table_name
            cols = [(c.name, c.type) for c in (table_ref.columns or [])]
            if not cols:
                continue
            try:
                sql, col_names, skip_flags = _build_column_stats_sql(
                    catalog, schema_name, table_name, cols,
                )
                rows = connector.execute(connection, sql, ())
                if not rows:
                    continue
                composite = rows[0]
                total_rows_raw = composite.get("_total_rows")
                # Prefer the COUNT(*) we just measured over the
                # sys.partitions estimate from pass 1 (it's exact
                # rather than approximate). Fall back to the
                # partitions estimate when COUNT(*) is missing.
                total_rows = (
                    int(total_rows_raw)
                    if total_rows_raw is not None
                    else row_count_by_key.get((schema_name, table_name))
                )
                column_stats.extend(_row_to_column_stats(
                    composite, col_names, skip_flags,
                    schema_name, table_name, total_rows,
                ))
            except Exception as e:
                # Best-effort: log + continue, don't fail the whole pass.
                logger.warning("MSSQL column-stats pass failed for "
                               "%s.%s.%s: %s",
                               catalog, schema_name, table_name, e)
                continue

    return table_stats, column_stats


def get_outcomeops_mssql_introspector():
    """Return the subclass type wired up. Lazy import so the module is
    importable in test environments without `databao_context_engine`.
    """
    from databao_context_engine.plugins.databases.mssql.mssql_connector import (
        MSSQLConnector,
    )
    from databao_context_engine.plugins.databases.mssql.mssql_introspector import (
        MSSQLIntrospector,
    )

    class OutcomeOpsMSSQLIntrospector(MSSQLIntrospector):
        """Adds the missing collect_stats override. Everything else
        inherits from upstream so when JetBrains ships their own
        collect_stats we can retire this class with a single import
        flip."""

        def collect_stats(self, connection, catalog: str, scope):
            return collect_mssql_stats(self._connector, connection, catalog, scope)

    return OutcomeOpsMSSQLIntrospector(MSSQLConnector())
