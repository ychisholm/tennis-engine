#!/usr/bin/env python3
"""
Data pipeline: loads Jeff Sackmann ATP/WTA CSVs into a DuckDB database.

Sources
-------
  ATP matches   : data/raw/tennis_atp/atp_matches_????.csv
  WTA matches   : data/raw/tennis_wta/wta_matches_????.csv
  ATP points    : data/raw/tennis_MatchChartingProject/charting-m-points*.csv
  WTA points    : data/raw/tennis_MatchChartingProject/charting-w-points*.csv

Usage
-----
    python src/data_pipeline.py
"""

import glob
import sys
from pathlib import Path

import duckdb

DEFAULT_DB_PATH = "data/processed/tennis.duckdb"

# (glob_pattern, table_name)
DEFAULT_TABLES = [
    ("data/raw/tennis_atp/atp_matches_????.csv", "atp_matches"),
    ("data/raw/tennis_wta/wta_matches_????.csv", "wta_matches"),
    ("data/raw/tennis_MatchChartingProject/charting-m-points*.csv", "atp_points"),
    ("data/raw/tennis_MatchChartingProject/charting-w-points*.csv", "wta_points"),
]


def _files_sql_list(files: list[str]) -> str:
    """Format a Python list of paths as a DuckDB array literal."""
    quoted = ", ".join(f"'{f}'" for f in files)
    return f"[{quoted}]"


def load_table(
    con: duckdb.DuckDBPyConnection,
    pattern: str,
    table_name: str,
) -> tuple[int, int] | None:
    """
    Read all CSVs matching *pattern* into *table_name*.

    Returns (row_count, col_count) on success, None if no files matched.
    """
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"  [skip] {table_name}: no files matched {pattern!r}", file=sys.stderr)
        return None

    files_list = _files_sql_list(files)
    con.execute(f"""
        CREATE OR REPLACE TABLE {table_name} AS
        SELECT * FROM read_csv_auto({files_list}, union_by_name = true)
    """)

    row_count: int = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    col_count: int = len(con.execute(f"DESCRIBE {table_name}").fetchall())
    return row_count, col_count


def run_pipeline(
    db_path: str = DEFAULT_DB_PATH,
    tables: list[tuple[str, str]] = DEFAULT_TABLES,
) -> dict[str, tuple[int, int]]:
    """
    Load all CSV sources into DuckDB.

    Parameters
    ----------
    db_path:
        Path to the DuckDB file to create or overwrite.
    tables:
        List of (glob_pattern, table_name) pairs to load.

    Returns
    -------
    dict mapping table_name -> (row_count, col_count) for every table
    that was successfully loaded.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(db_path)
    results: dict[str, tuple[int, int]] = {}

    for pattern, table_name in tables:
        result = load_table(con, pattern, table_name)
        if result is not None:
            results[table_name] = result

    con.close()
    return results


def main() -> None:
    print(f"Loading data into {DEFAULT_DB_PATH} ...\n")
    results = run_pipeline()

    if not results:
        print(
            "No tables were loaded.\n"
            "Make sure data/raw/tennis_atp/ and data/raw/tennis_wta/ are populated.",
            file=sys.stderr,
        )
        sys.exit(1)

    col_w = 20
    print(f"\n{'Table':<{col_w}} {'Rows':>12} {'Columns':>10}")
    print("-" * (col_w + 24))
    for table_name, (rows, cols) in results.items():
        print(f"{table_name:<{col_w}} {rows:>12,} {cols:>10}")


if __name__ == "__main__":
    main()
