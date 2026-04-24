#!/usr/bin/env python3
"""
run_backtest.py — CLI entry point for the 6A backtester.

Usage
-----
    python scripts/run_backtest.py --tour atp --max-matches 50
    python scripts/run_backtest.py --tour wta --max-matches 100
"""

import argparse
import sys
import os

# Ensure the project root is on the path when run directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from src.backtester import Backtester


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest the LiveMatch prediction engine against charting data."
    )
    parser.add_argument(
        "--tour",
        default="atp",
        choices=["atp", "wta"],
        help="Tour to backtest (default: atp)",
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of matches to process (default: all)",
    )
    parser.add_argument(
        "--db-path",
        default="data/processed/tennis.duckdb",
        help="Path to the DuckDB database (default: data/processed/tennis.duckdb)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/backtesting",
        help="Directory for output CSV files (default: data/backtesting)",
    )
    args = parser.parse_args()

    total_matches = args.max_matches  # may be None

    def progress(completed: int, total: int) -> None:
        label = str(total) if total_matches is not None else "?"
        if completed % 10 == 0:
            print(f"Completed {completed}/{label} matches...", flush=True)

    print(f"Starting backtest: tour={args.tour}, max_matches={args.max_matches}")
    bt = Backtester(db_path=args.db_path, output_dir=args.output_dir)

    out_path = bt.run(
        tour=args.tour,
        max_matches=args.max_matches,
        progress_callback=progress,
    )

    print(f"\nDone. Output written to: {out_path}")


if __name__ == "__main__":
    main()
