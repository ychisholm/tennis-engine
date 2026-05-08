#!/usr/bin/env python3
"""
Phase 1 schema migration.

Moves and renames the existing live_raw and live_processed tables into the
new top-level schemas (live, audit, book), drops the orphaned
live_processed.points and live_processed.dashboard_log tables, and finally
drops the now-empty live_raw and live_processed schemas.

The migration runs inside a single transaction. Any failure rolls every
operation back. Re-running after a successful run is a no-op (idempotent).

Usage:
    .venv/bin/python scripts/setup/migrate_to_v2_schemas.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

NEW_SCHEMAS = ("live", "audit", "book")

# (old_schema, old_table, new_schema, new_table)
RENAMES: list[tuple[str, str, str, str]] = [
    ("live_raw",       "match_details",        "live",  "match_polls"),
    ("live_processed", "match_detail_points",  "live",  "match_states"),
    ("live_raw",       "tennisapi_points",     "live",  "backfill_points"),
    ("live_raw",       "oddsapi_polls",        "live",  "backfill_odds_polls"),
    ("live_raw",       "api_call_log",         "audit", "api_call_log"),
    ("live_raw",       "api_response_archive", "audit", "api_response_archive"),
    ("public",         "poll_audit_log",       "audit", "poll_audit_log"),
]

DROPS: list[tuple[str, str]] = [
    ("live_processed", "points"),
    ("live_processed", "dashboard_log"),
]

EMPTY_SCHEMAS = ("live_raw", "live_processed")


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
          AND table_type = 'BASE TABLE'
        """,
        (schema, table),
    )
    return cur.fetchone() is not None


def _schema_exists(cur, schema: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    return cur.fetchone() is not None


def _create_schemas(cur, log: list[str]) -> None:
    for s in NEW_SCHEMAS:
        cur.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(s))
        )
        log.append(f"CREATE SCHEMA IF NOT EXISTS {s}")


def _move_and_rename(
    cur, src_s: str, src_t: str, dst_s: str, dst_t: str, log: list[str]
) -> None:
    src_present = _table_exists(cur, src_s, src_t)
    dst_present = _table_exists(cur, dst_s, dst_t)
    if dst_present and not src_present:
        log.append(f"SKIP {src_s}.{src_t} -> {dst_s}.{dst_t} (already migrated)")
        return
    if not src_present:
        log.append(f"SKIP {src_s}.{src_t} -> {dst_s}.{dst_t} (source missing)")
        return
    cur.execute(
        sql.SQL("ALTER TABLE {}.{} SET SCHEMA {}").format(
            sql.Identifier(src_s), sql.Identifier(src_t), sql.Identifier(dst_s)
        )
    )
    log.append(f"ALTER TABLE {src_s}.{src_t} SET SCHEMA {dst_s}")
    if src_t != dst_t:
        cur.execute(
            sql.SQL("ALTER TABLE {}.{} RENAME TO {}").format(
                sql.Identifier(dst_s),
                sql.Identifier(src_t),
                sql.Identifier(dst_t),
            )
        )
        log.append(f"ALTER TABLE {dst_s}.{src_t} RENAME TO {dst_t}")


def _rename_indexes(
    cur, src_s: str, src_t: str, dst_s: str, dst_t: str, log: list[str]
) -> None:
    """Rename indexes whose name embeds the old schema or old table name."""
    cur.execute(
        """
        SELECT indexname FROM pg_indexes
        WHERE schemaname = %s AND tablename = %s
        """,
        (dst_s, dst_t),
    )
    rows = cur.fetchall()
    for (idx_name,) in rows:
        new_name = idx_name
        if src_t != dst_t and src_t in new_name:
            new_name = new_name.replace(src_t, dst_t)
        if src_s != dst_s and src_s in new_name:
            new_name = new_name.replace(src_s, dst_s)
        if new_name == idx_name:
            continue
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
            (dst_s, new_name),
        )
        if cur.fetchone() is not None:
            log.append(
                f"SKIP rename index {dst_s}.{idx_name} -> {new_name} "
                "(target name already exists)"
            )
            continue
        cur.execute(
            sql.SQL("ALTER INDEX {}.{} RENAME TO {}").format(
                sql.Identifier(dst_s),
                sql.Identifier(idx_name),
                sql.Identifier(new_name),
            )
        )
        log.append(f"ALTER INDEX {dst_s}.{idx_name} RENAME TO {new_name}")


def _drop_table(cur, schema: str, table: str, log: list[str]) -> None:
    if _table_exists(cur, schema, table):
        cur.execute(
            sql.SQL("DROP TABLE {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        )
        log.append(f"DROP TABLE {schema}.{table}")
    else:
        log.append(f"SKIP DROP {schema}.{table} (already gone)")


def _drop_empty_schemas(cur, log: list[str]) -> None:
    for s in EMPTY_SCHEMAS:
        if not _schema_exists(cur, s):
            log.append(f"SKIP DROP SCHEMA {s} (already gone)")
            continue
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            """,
            (s,),
        )
        if cur.fetchone()[0] > 0:
            log.append(f"SKIP DROP SCHEMA {s} (not empty)")
            continue
        cur.execute(
            sql.SQL("DROP SCHEMA {}").format(sql.Identifier(s))
        )
        log.append(f"DROP SCHEMA {s}")


def _print_post_migration_summary(cur) -> None:
    print()
    print("Tables now in live, audit, book:")
    print("-" * 70)
    for schema in NEW_SCHEMAS:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema,),
        )
        tables = [r[0] for r in cur.fetchall()]
        if not tables:
            print(f"  {schema:<8}  (no tables)")
            continue
        for t in tables:
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(t)
                )
            )
            n = cur.fetchone()[0]
            print(f"  {schema:<8}  {t:<28}  {n:,} rows")


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    log: list[str] = []
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            _create_schemas(cur, log)
            for src_s, src_t, dst_s, dst_t in RENAMES:
                _move_and_rename(cur, src_s, src_t, dst_s, dst_t, log)
                if _table_exists(cur, dst_s, dst_t):
                    _rename_indexes(cur, src_s, src_t, dst_s, dst_t, log)
            for s, t in DROPS:
                _drop_table(cur, s, t, log)
            _drop_empty_schemas(cur, log)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print("Migration FAILED — transaction rolled back.", file=sys.stderr)
        print(f"  Error: {exc}", file=sys.stderr)
        if log:
            print("  Operations attempted before failure:", file=sys.stderr)
            for line in log:
                print(f"    {line}", file=sys.stderr)
        conn.close()
        return 2

    print("Migration committed. Operations performed:")
    for line in log:
        print(f"  {line}")
    try:
        with conn.cursor() as cur:
            _print_post_migration_summary(cur)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
