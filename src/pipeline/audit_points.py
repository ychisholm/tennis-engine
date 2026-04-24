#!/usr/bin/env python3
"""
audit_points.py — Audits the point-level tables in tennis.duckdb.

Sections for each table:
  1. SCHEMA
  2. COVERAGE OVERVIEW (row count, unique matches, year range)
  3. FIELD-BY-FIELD NULL AUDIT (sorted by coverage % ascending)
  4. SAMPLE ROWS (3 rows)
  5. KEY FIELD DEEP DIVE (serve speed, rally, score, outcome, server columns)

Run:
    python src/audit_points.py
    python src/audit_points.py --db-path data/processed/tennis.duckdb
"""

from __future__ import annotations

import argparse
import sys

import duckdb

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = "data/processed/tennis.duckdb"

# Coverage thresholds
CRITICAL_THRESHOLD = 0.20   # < 20 %  → CRITICAL
WARNING_THRESHOLD  = 0.50   # < 50 %  → WARNING

# Keywords that flag a column for the Key Field Deep Dive.
# Matching is case-insensitive and checks if any keyword appears in the col name.
_KEY_KEYWORDS = [
    "svr", "serve", "speed", "1st", "2nd",
    "rally", "pts", "score",
    "winner", "pt",
    "set", "gm",
]

# Columns to skip for the deep dive (they duplicate info or are identifiers)
_DEEP_DIVE_SKIP = {"match_id"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sep(char: str = "─", width: int = 72) -> None:
    print(char * width)


def _header(text: str) -> None:
    _sep()
    print(f"  {text}")
    _sep()


def _coverage_flag(pct: float) -> str:
    if pct < CRITICAL_THRESHOLD:
        return "  ◀ CRITICAL"
    if pct < WARNING_THRESHOLD:
        return "  ◀ WARNING"
    return ""


def _is_key_column(col_name: str) -> bool:
    lower = col_name.lower()
    for kw in _KEY_KEYWORDS:
        if kw in lower:
            return True
    return False


def _is_numeric(dtype: str) -> bool:
    d = dtype.upper()
    return any(t in d for t in ("INT", "FLOAT", "DOUBLE", "DECIMAL", "BIGINT",
                                 "HUGEINT", "REAL", "NUMERIC"))


# ---------------------------------------------------------------------------
# Audit sections
# ---------------------------------------------------------------------------

def audit_schema(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple]:
    """Print schema and return list of (col_name, dtype) tuples."""
    print("\n[1] SCHEMA")
    rows = con.execute(f"DESCRIBE {table}").fetchall()
    cols = [(r[0], r[1]) for r in rows]
    name_w = max(len(c[0]) for c in cols) + 2
    print(f"  {'Column':<{name_w}}  Type")
    print(f"  {'-'*name_w}  {'-'*20}")
    for name, dtype in cols:
        print(f"  {name:<{name_w}}  {dtype}")
    return cols


def audit_coverage_overview(con: duckdb.DuckDBPyConnection, table: str) -> int:
    """Print overview stats. Returns total row count."""
    print("\n[2] COVERAGE OVERVIEW")

    total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    unique_matches = con.execute(
        f"SELECT COUNT(DISTINCT match_id) FROM {table}"
    ).fetchone()[0]

    # Year from first 4 chars of match_id
    year_row = con.execute(f"""
        SELECT
            MIN(LEFT(match_id, 4))::INTEGER AS min_year,
            MAX(LEFT(match_id, 4))::INTEGER AS max_year
        FROM {table}
        WHERE match_id IS NOT NULL
          AND LENGTH(match_id) >= 4
          AND LEFT(match_id, 4) ~ '^[0-9]{{4}}$'
    """).fetchone()
    min_year, max_year = year_row if year_row else (None, None)

    print(f"  Total rows        : {total:,}")
    print(f"  Unique matches    : {unique_matches:,}")
    if min_year and max_year:
        print(f"  Year range        : {min_year} – {max_year}")
    else:
        print(f"  Year range        : (could not derive)")

    return total


def audit_null_coverage(
    con: duckdb.DuckDBPyConnection,
    table: str,
    cols: list[tuple],
    total: int,
) -> None:
    """Print field-by-field null audit, sorted by coverage % ascending."""
    print("\n[3] FIELD-BY-FIELD NULL AUDIT  (sorted by coverage % ascending)")
    print(f"  {'Column':<22}  {'Non-null':>10}  {'Null':>10}  {'Coverage':>9}  Flag")
    print(f"  {'-'*22}  {'-'*10}  {'-'*10}  {'-'*9}  ----")

    # Build SELECT with one COUNT per column
    exprs = ", ".join(
        f"COUNT(\"{c}\") AS \"{c}\"" for c, _ in cols
    )
    row = con.execute(f"SELECT {exprs} FROM {table}").fetchone()
    non_null_counts = {cols[i][0]: row[i] for i in range(len(cols))}

    stats = []
    for name, _ in cols:
        nn = non_null_counts[name]
        null_c = total - nn
        pct = nn / total if total > 0 else 0.0
        stats.append((pct, name, nn, null_c))

    stats.sort(key=lambda x: x[0])

    for pct, name, nn, null_c in stats:
        flag = _coverage_flag(pct)
        print(
            f"  {name:<22}  {nn:>10,}  {null_c:>10,}  {pct:>8.1%}  {flag}"
        )


def audit_sample_rows(con: duckdb.DuckDBPyConnection, table: str, cols: list[tuple]) -> None:
    """Print 3 sample rows."""
    print("\n[4] SAMPLE ROWS (3 rows)")
    rows = con.execute(f"SELECT * FROM {table} LIMIT 3").fetchall()
    col_names = [c[0] for c in cols]

    for i, row in enumerate(rows, 1):
        print(f"\n  --- Row {i} ---")
        for name, val in zip(col_names, row):
            print(f"    {name:<22} = {val!r}")


def audit_key_fields(
    con: duckdb.DuckDBPyConnection,
    table: str,
    cols: list[tuple],
    total: int,
) -> None:
    """Deep dive on columns matching key keywords."""
    key_cols = [
        (name, dtype) for name, dtype in cols
        if _is_key_column(name) and name not in _DEEP_DIVE_SKIP
    ]
    print(f"\n[5] KEY FIELD DEEP DIVE ({len(key_cols)} columns)")

    for name, dtype in key_cols:
        print(f"\n  ── {name}  [{dtype}]")

        # Value distribution — top 10
        try:
            dist = con.execute(f"""
                SELECT "{name}", COUNT(*) AS cnt
                FROM {table}
                WHERE "{name}" IS NOT NULL
                GROUP BY "{name}"
                ORDER BY cnt DESC
                LIMIT 10
            """).fetchall()

            if not dist:
                print("    (all null)")
            else:
                print(f"    Top values:")
                for val, cnt in dist:
                    pct = cnt / total * 100
                    print(f"      {str(val):<30}  {cnt:>8,}  ({pct:.1f}%)")
        except Exception as e:
            print(f"    (distribution query failed: {e})")

        # Min / max / mean for numeric columns
        if _is_numeric(dtype):
            try:
                stats = con.execute(f"""
                    SELECT
                        MIN("{name}"),
                        MAX("{name}"),
                        AVG("{name}")
                    FROM {table}
                    WHERE "{name}" IS NOT NULL
                """).fetchone()
                if stats and stats[0] is not None:
                    print(f"    Min = {stats[0]},  Max = {stats[1]},  Mean = {stats[2]:.4f}")
            except Exception as e:
                print(f"    (stats query failed: {e})")


# ---------------------------------------------------------------------------
# Main audit driver
# ---------------------------------------------------------------------------

def audit_table(con: duckdb.DuckDBPyConnection, table: str) -> None:
    _sep("═")
    print(f"  TABLE: {table}")
    _sep("═")

    # Check existence
    exists = con.execute(f"""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_name = '{table}'
    """).fetchone()[0]
    if not exists:
        print(f"  ✗ Table '{table}' does not exist. Skipping.\n")
        return

    # Check emptiness
    total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if total == 0:
        print(f"  ✗ Table '{table}' exists but is empty. Skipping.\n")
        return

    cols = audit_schema(con, table)
    total = audit_coverage_overview(con, table)
    audit_null_coverage(con, table, cols, total)
    audit_sample_rows(con, table, cols)
    audit_key_fields(con, table, cols, total)

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit point-level tables in tennis.duckdb"
    )
    parser.add_argument(
        "--db-path",
        default=DB_PATH,
        help=f"Path to DuckDB file (default: {DB_PATH})",
    )
    args = parser.parse_args()

    try:
        con = duckdb.connect(args.db_path, read_only=True)
    except Exception as e:
        print(f"ERROR: Could not open database at '{args.db_path}': {e}", file=sys.stderr)
        sys.exit(1)

    for table in ("atp_points", "wta_points"):
        audit_table(con, table)

    con.close()


if __name__ == "__main__":
    main()
