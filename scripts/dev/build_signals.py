"""Build the 52 signal sub-component columns and merge them into core.ml_game_level.

Step 3 of the tennis prediction engine build sequence. Walks
core.atp_points_enhanced point-by-point per match, computes signal
sub-components (BPI / SDS / RES / CPI / MRS) in within-set and
cumulative-match versions for both players, and adds 52 new columns
to core.ml_game_level.

Outputs:
  - core.ml_game_level rebuilt with 80 columns (28 original + 52 signals)
  - data/processed/ml_game_level.parquet refreshed
  - data/processed/signals_build_report.txt
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

# Make src/ importable regardless of where the script is invoked from.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.signal_engine import SIGNAL_COLUMNS, SignalEngine  # noqa: E402


DB_PATH = "data/processed/tennis.duckdb"
PARQUET_PATH = "data/processed/ml_game_level.parquet"
REPORT_PATH = "data/processed/signals_build_report.txt"


def load_points(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load all in-window points enriched with set/game numbers and integer match id."""
    return con.execute("""
        SELECT
            mim.match_id_int,
            (ape.Set1 + ape.Set2 + 1) AS set_number,
            (ape.Gm1 + ape.Gm2 + 1)   AS game_number_in_set,
            ape.Pt,
            ape.score_before,
            CAST(ape.Svr AS INTEGER)      AS Svr,
            CAST(ape.PtWinner AS INTEGER) AS PtWinner,
            ape.is_tiebreak
        FROM core.atp_points_enhanced ape
        JOIN core.match_id_map mim ON mim.match_id_string = ape.match_id
        WHERE ape.match_date BETWEEN DATE '2015-01-01' AND DATE '2025-12-31'
          AND ape.player1_name IS NOT NULL AND ape.player2_name IS NOT NULL
          AND ape.Set1 IS NOT NULL AND ape.Set2 IS NOT NULL
          AND ape.Gm1  IS NOT NULL AND ape.Gm2  IS NOT NULL
          AND ape.Svr  IS NOT NULL AND ape.PtWinner IS NOT NULL
        ORDER BY mim.match_id_int, ape.Pt
    """).fetchdf()


def compute_signals(points: pd.DataFrame) -> pd.DataFrame:
    """Walk every match through SignalEngine and return one row per game."""
    engine = SignalEngine()
    all_rows: list[dict] = []
    n_matches = points["match_id_int"].nunique()
    print(f"Processing {n_matches:,} matches "
          f"({len(points):,} points total)...", flush=True)
    t0 = time.time()
    last_print = t0
    for i, (mid, group) in enumerate(
        points.groupby("match_id_int", sort=False), start=1
    ):
        rows = engine.process_match(int(mid), group.itertuples(index=False))
        all_rows.extend(rows)
        now = time.time()
        if i % 100 == 0 or now - last_print >= 30:
            print(f"  {i:>5,}/{n_matches:,} matches "
                  f"({now - t0:6.1f}s elapsed, "
                  f"{len(all_rows):,} game rows so far)", flush=True)
            last_print = now
    print(f"  done: {len(all_rows):,} game rows in "
          f"{time.time() - t0:.1f}s", flush=True)
    df = pd.DataFrame(all_rows)
    return df


def merge_into_ml_game_level(
    con: duckdb.DuckDBPyConnection, results: pd.DataFrame
) -> None:
    """Add the 52 signal columns to core.ml_game_level via a join-and-replace.

    Idempotent: any pre-existing signal columns are dropped before the merge
    so re-running the build does not stack duplicates.
    """
    existing = [r[0] for r in con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='core' AND table_name='ml_game_level'
        ORDER BY ordinal_position
    """).fetchall()]
    base_cols = [c for c in existing if c not in set(SIGNAL_COLUMNS)]

    con.register("signal_results", results)

    select_base = ",\n            ".join(f"g.{c}" for c in base_cols)
    select_signal = ",\n            ".join(f"s.{c}" for c in SIGNAL_COLUMNS)
    con.execute(f"""
        CREATE OR REPLACE TABLE core.ml_game_level AS
        SELECT
            {select_base},
            {select_signal}
        FROM core.ml_game_level g
        LEFT JOIN signal_results s
          ON s.match_id_int       = g.match_id_int
         AND s.set_number         = g.set_number
         AND s.game_number_in_set = g.game_number_in_set
        ORDER BY g.match_id_int, g.set_number, g.game_number_in_set
    """)
    con.unregister("signal_results")

    con.execute(
        f"COPY core.ml_game_level TO '{PARQUET_PATH}' (FORMAT 'parquet')"
    )


def write_report(
    con: duckdb.DuckDBPyConnection,
    n_matches: int,
    n_points: int,
    n_signal_rows: int,
    runtime_sec: float,
) -> None:
    lines: list[str] = []

    def log(s: str = "") -> None:
        print(s)
        lines.append(s)

    log("=" * 70)
    log("SIGNAL ENGINEERING BUILD REPORT (Step 3)")
    log("=" * 70)

    n_table_rows = con.execute(
        "SELECT COUNT(*) FROM core.ml_game_level"
    ).fetchone()[0]
    log(f"\nMatches processed:               {n_matches:,}")
    log(f"Points processed:                {n_points:,}")
    log(f"Signal-engine rows emitted:      {n_signal_rows:,}")
    log(f"core.ml_game_level row count:    {n_table_rows:,}")

    n_cols = con.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema='core' AND table_name='ml_game_level'
    """).fetchone()[0]
    log(f"core.ml_game_level column count: {n_cols}")

    # Unmatched check — sentinel: bpi_bp_rate_ws_a is NULL only when the
    # join produced no signal row for that ml_game_level entry.
    unmatched = con.execute("""
        SELECT COUNT(*) FROM core.ml_game_level
        WHERE bpi_bp_rate_ws_a IS NULL
    """).fetchone()[0]
    log(f"\nUnmatched ml_game_level rows:    {unmatched:,}")

    log(f"\nTotal runtime: {runtime_sec:.1f}s "
        f"({runtime_sec / 60.0:.2f} min)")

    log("\n" + "-" * 70)
    log("PER-SIGNAL SUMMARY STATISTICS (populated rows only)")
    log("-" * 70)
    log(f"{'column':<42}{'min':>10}{'mean':>10}{'max':>10}{'std':>10}")
    for c in SIGNAL_COLUMNS:
        mn, mx, avg, sd = con.execute(f"""
            SELECT MIN("{c}"), MAX("{c}"), AVG("{c}"), STDDEV_POP("{c}")
            FROM core.ml_game_level
            WHERE "{c}" IS NOT NULL
        """).fetchone()
        if mn is None:
            log(f"{c:<42}{'(empty)':>10}")
            continue
        log(f"{c:<42}{mn:>10.4f}{avg:>10.4f}{mx:>10.4f}"
            f"{(sd or 0):>10.4f}")

    # Persist the report BEFORE running pytest so the runtime test
    # (test_total_runtime_acceptable) can read the runtime line.
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(lines))

    log("\n" + "=" * 70)
    log("TEST RESULTS (tests/test_signal_engine.py)")
    log("=" * 70)
    proc = subprocess.run(
        ["python3", "-m", "pytest",
         "tests/test_signal_engine.py", "-v", "--tb=short"],
        capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    for line in out.splitlines():
        m = re.match(r".*::(test_\w+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
        if m:
            log(f"  {m.group(1):<48} {m.group(2)}")
    summary = re.search(r"(\d+\s+passed|\d+\s+failed|\d+\s+error)", out)
    if summary:
        log(f"\n  pytest summary: {summary.group(0)}")
    log(f"  pytest exit code: {proc.returncode}")
    if proc.returncode != 0:
        log("\n--- pytest output (full) ---")
        log(out)

    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(lines))
    log(f"\nReport saved to: {REPORT_PATH}")


def main() -> None:
    overall_start = time.time()

    con = duckdb.connect(DB_PATH)
    print(f"Connected to {DB_PATH}", flush=True)

    print("Loading points...", flush=True)
    points = load_points(con)
    n_matches = int(points["match_id_int"].nunique())
    n_points = len(points)
    print(f"  loaded {n_points:,} points across {n_matches:,} matches",
          flush=True)

    results = compute_signals(points)
    n_signal_rows = len(results)

    print("Merging into core.ml_game_level...", flush=True)
    merge_into_ml_game_level(con, results)

    runtime = time.time() - overall_start
    con.close()

    con = duckdb.connect(DB_PATH, read_only=True)
    write_report(con, n_matches, n_points, n_signal_rows, runtime)
    con.close()


if __name__ == "__main__":
    main()
