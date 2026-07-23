"""PostgreSQL helpers for collector.

Design rules: bound connection hangs for CronJobs and route every data-table write through
one safe, quoting-aware upsert so revised health data overwrites earlier rows consistently.
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg
from psycopg import sql

from collector.config import Config


def connect(cfg: Config) -> psycopg.Connection:
    """autocommit=False; caller owns transaction boundaries. connect_timeout bounded
    (10 s) — an unbounded connect hang inside a CronJob is a silent stall."""
    return psycopg.connect(
        host=cfg.pghost,
        port=cfg.pgport,
        dbname=cfg.pgdatabase,
        user=cfg.pguser,
        password=cfg.pgpassword,
        connect_timeout=10,
        autocommit=False,
    )


def upsert(
    cur: psycopg.Cursor,
    table: str,
    key_cols: Sequence[str],
    cols: Sequence[str],
    rows: Sequence[tuple],
    set_ingested_at: bool = False,
) -> int:
    """INSERT INTO table (cols) VALUES ... ON CONFLICT (key_cols) DO UPDATE SET
    <every non-key col> = EXCLUDED.<col> [, ingested_at = now() when set_ingested_at].
    Returns len(rows). Empty rows -> 0 without touching the DB."""
    if not rows:
        return 0

    table_parts = table.split(".")
    if len(table_parts) != 2 or not table_parts[0] or not table_parts[1]:
        raise ValueError("table must be schema-qualified as 'schema.table'")

    schema_name, table_name = table_parts
    key_cols_list = list(key_cols)
    cols_list = list(cols)

    if not key_cols_list:
        raise ValueError("key_cols must not be empty")
    if not cols_list:
        raise ValueError("cols must not be empty")

    missing_key_cols = [col for col in key_cols_list if col not in cols_list]
    if missing_key_cols:
        raise ValueError(
            "key_cols must all be present in cols: " + ", ".join(missing_key_cols)
        )

    key_col_set = set(key_cols_list)
    update_cols = [col for col in cols_list if col not in key_col_set]
    if not update_cols:
        raise ValueError("upsert requires at least one non-key column in cols")

    assignments = [
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(col), sql.Identifier(col))
        for col in update_cols
    ]
    if set_ingested_at:
        # Updated rows must refresh freshness metadata when the destination table owns it.
        assignments.append(sql.SQL("ingested_at = now()"))

    statement = sql.SQL(
        "INSERT INTO {}.{} ({}) VALUES ({}) "
        "ON CONFLICT ({}) DO UPDATE SET {}"
    ).format(
        sql.Identifier(schema_name),
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(col) for col in cols_list),
        sql.SQL(", ").join(sql.Placeholder() for _ in cols_list),
        sql.SQL(", ").join(sql.Identifier(col) for col in key_cols_list),
        sql.SQL(", ").join(assignments),
    )

    cur.executemany(statement, rows)
    return len(rows)
