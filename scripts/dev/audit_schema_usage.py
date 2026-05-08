#!/usr/bin/env python3
"""
One-shot diagnostic: inventory tables in production schemas
(live_raw, live_processed, public), cross-reference against the planned
migration map, and grep the local codebase for references to each table.

Strictly READ-ONLY: the database connection is opened with
set_session(readonly=True) and only SELECT statements are issued.

Usage:
    python scripts/dev/audit_schema_usage.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

_THIS_FILE = Path(__file__).resolve()
_ROOT = _THIS_FILE.parents[2]
load_dotenv(_ROOT / ".env")

SCHEMAS = ["live_raw", "live_processed", "public"]

MIGRATION_MAP: dict[tuple[str, str], tuple[str, str | None]] = {
    ("live_raw",       "match_details"):        ("rename_move", "live.match_polls"),
    ("live_processed", "match_detail_points"):  ("rename_move", "live.match_states"),
    ("live_raw",       "tennisapi_points"):     ("rename_move", "live.backfill_points"),
    ("live_raw",       "oddsapi_polls"):        ("rename_move", "live.backfill_odds_polls"),
    ("live_raw",       "api_call_log"):         ("move",        "audit.api_call_log"),
    ("live_raw",       "api_response_archive"): ("move",        "audit.api_response_archive"),
    ("public",         "poll_audit_log"):       ("move",        "audit.poll_audit_log"),
    ("live_processed", "points"):               ("drop",        None),
    ("live_processed", "dashboard_log"):        ("drop",        None),
}

EXCLUDE_DIRS = [".venv", "node_modules", ".git", "data"]


def list_tables(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    return [r[0] for r in cur.fetchall()]


def first_timestamp_column(cur, schema: str, table: str) -> str | None:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND data_type IN ('timestamp with time zone',
                            'timestamp without time zone')
        ORDER BY ordinal_position
        LIMIT 1
        """,
        (schema, table),
    )
    row = cur.fetchone()
    return row[0] if row else None


def column_count(cur, schema: str, table: str) -> int:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return cur.fetchone()[0]


def row_count(cur, schema: str, table: str) -> int:
    cur.execute(
        sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
    )
    return cur.fetchone()[0]


def max_timestamp(cur, schema: str, table: str, col: str):
    cur.execute(
        sql.SQL("SELECT MAX({}) FROM {}.{}").format(
            sql.Identifier(col), sql.Identifier(schema), sql.Identifier(table)
        )
    )
    return cur.fetchone()[0]


def grep_pattern(pattern: str) -> list[str]:
    args = ["grep", "-rlnF"]
    for d in EXCLUDE_DIRS:
        args.append(f"--exclude-dir={d}")
    args.extend([pattern, str(_ROOT)])
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode not in (0, 1):
        return []
    files: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if Path(line).resolve() == _THIS_FILE:
                continue
        except OSError:
            pass
        files.append(line)
    return files


def classify(schema: str, table: str) -> tuple[str, str]:
    plan = MIGRATION_MAP.get((schema, table))
    if plan is None:
        return ("UNCATALOGED -- review", "")
    action, target = plan
    if action == "drop":
        return ("ORPHAN (will drop)", "DROP")
    label = "RENAME+MOVE" if action == "rename_move" else "MOVE"
    return ("OK (in plan)", f"{label} -> {target}")


def relpath(p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(_ROOT))
    except ValueError:
        return p


def build_report() -> tuple[str, list[tuple[str, str]]]:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.set_session(readonly=True)

    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    header: list[str] = [
        f"# Schema Audit — {today}",
        "",
        f"Generated at: {now}",
        f"Schemas audited: {', '.join(SCHEMAS)}",
        "",
    ]
    schema_blocks: list[str] = []
    uncataloged: list[tuple[str, str]] = []

    try:
        with conn.cursor() as cur:
            for schema in SCHEMAS:
                tables = list_tables(cur, schema)
                schema_blocks.append(f"## Schema `{schema}`")
                schema_blocks.append("")
                if not tables:
                    schema_blocks.append("_No tables found._")
                    schema_blocks.append("")
                    continue

                for table in tables:
                    status, plan_str = classify(schema, table)
                    if status.startswith("UNCATALOGED"):
                        uncataloged.append((schema, table))

                    rcount = row_count(cur, schema, table)
                    ts_col = first_timestamp_column(cur, schema, table)
                    if ts_col is None:
                        ts_str = "no timestamp column"
                    else:
                        max_ts = max_timestamp(cur, schema, table, ts_col)
                        if max_ts is None:
                            ts_str = f"`{ts_col}`: NULL"
                        else:
                            ts_str = f"`{ts_col}`: {max_ts}"
                    ccount = column_count(cur, schema, table)

                    fq_name = f"{schema}.{table}"
                    fq_files = grep_pattern(fq_name)
                    bare_files = grep_pattern(table)

                    schema_blocks.append(f"### `{fq_name}`")
                    schema_blocks.append("")
                    schema_blocks.append(f"- **Status**: {status}")
                    if plan_str:
                        schema_blocks.append(f"- **Plan**: {plan_str}")
                    schema_blocks.append(f"- **Rows**: {rcount:,}")
                    schema_blocks.append(f"- **Most recent timestamp**: {ts_str}")
                    schema_blocks.append(f"- **Columns**: {ccount}")
                    schema_blocks.append(
                        f"- **Code refs to `{fq_name}`**: "
                        f"{len(fq_files)} file(s)"
                    )
                    for f in fq_files[:5]:
                        schema_blocks.append(f"    - `{relpath(f)}`")
                    schema_blocks.append(
                        f"- **Code refs to bare `{table}`**: "
                        f"{len(bare_files)} file(s)"
                    )
                    for f in bare_files[:5]:
                        schema_blocks.append(f"    - `{relpath(f)}`")
                    schema_blocks.append("")
    finally:
        conn.close()

    summary: list[str] = []
    if uncataloged:
        summary.append("## UNCATALOGED tables — REVIEW BEFORE MIGRATING")
        summary.append("")
        summary.append(
            f"**{len(uncataloged)} table(s) exist in the database but are NOT in "
            "the migration plan:**"
        )
        summary.append("")
        for schema, table in uncataloged:
            summary.append(f"- `{schema}.{table}`")
        summary.append("")
    else:
        summary.append("## UNCATALOGED tables")
        summary.append("")
        summary.append("_None — every found table is in the migration plan._")
        summary.append("")

    return "\n".join(header + summary + schema_blocks) + "\n", uncataloged


def main() -> int:
    report, uncataloged = build_report()

    out_dir = _ROOT / "data" / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"schema_audit_{date.today().isoformat()}.md"
    out_path.write_text(report)

    sys.stdout.write(report)
    sys.stdout.flush()

    print(f"\nReport saved to: {out_path}", file=sys.stderr)
    if uncataloged:
        print(
            f"WARNING: {len(uncataloged)} UNCATALOGED table(s) — "
            "see top of report.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
