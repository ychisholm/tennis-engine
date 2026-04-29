#!/usr/bin/env python3
"""
run_full_backfill.py — Orchestrate the complete backfill pipeline for a
single tournament day with a single set of prompts:

    Phase 1: Points  → live_raw.tennisapi_points
    Phase 2: Odds    → live_raw.oddsapi_polls
    Phase 3: Enrich  → live_processed.dashboard_log

All three phases share one PostgreSQL connection opened from DATABASE_URL.

Usage (from project root):
    .venv/bin/python scripts/backtesting/run_full_backfill.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import psycopg2
from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from backfill_today import (
    _prompt_tour,
    _prompt_date,
    _fetch_events,
    _build_tournament_list,
    _prompt_tournament,
    _ensure_tables as _ensure_points_table,
    fetch_matches,
    get_existing_match_ids,
    backfill_matches,
    print_summary,
)
from backfill_tournament_odds import (
    _ensure_tables as _ensure_odds_table,
    run_odds_backfill,
)
from enrich_dashboard_log import (
    _ensure_tables as _ensure_dashboard_table,
    enrich_match_ids,
)


def _sep(char: str = "─", width: int = 70) -> str:
    return char * width


def _phase(label: str) -> None:
    print()
    print(_sep("═"))
    print(f"  {label}")
    print(_sep("═"))
    print()


def _get_conn():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("  ERROR: DATABASE_URL environment variable is not set.")
        raise SystemExit(1)
    try:
        return psycopg2.connect(url)
    except psycopg2.OperationalError as exc:
        print(f"  ERROR: Could not connect to PostgreSQL — {exc}")
        raise SystemExit(1)


def main() -> None:
    print(_sep("═"))
    print("  Full Backfill Orchestrator")
    print("  Points → Odds → Enrichment in a single run")
    print(_sep("═"))
    print()

    # ── Discovery: prompt once for tour / date / tournament ───────────────────
    tour     = _prompt_tour()
    date_str = _prompt_date()
    yf, mf, df = (int(x) for x in date_str.split("-"))

    print(f"\n  Fetching events for {date_str}...")
    all_events = _fetch_events(df, mf, yf)

    entries = _build_tournament_list(all_events, tour)
    if not entries:
        print(f"  No {tour} tournaments found for {date_str}. Aborting.")
        return

    tournament_name, tournament_uid = _prompt_tournament(entries)

    print(f"\n  Tour       : {tour}")
    print(f"  Date       : {date_str}")
    print(f"  Tournament : {tournament_name}  (uid={tournament_uid})")

    conn = _get_conn()

    try:
        # Ensure all three Medallion tables exist before any phase runs.
        _ensure_points_table(conn)
        _ensure_odds_table(conn)
        _ensure_dashboard_table(conn)

        # ── Phase 1: Point backfill ───────────────────────────────────────────
        _phase("PHASE 1: Point Backfill")

        matches = fetch_matches(tour, date_str, tournament_uid, tournament_name)
        if not matches:
            print("  No finished singles matches found. Aborting.")
            return

        existing = get_existing_match_ids(conn)
        for m in matches:
            m["_db_count"] = existing.get(m["id"], 0)

        summaries = backfill_matches(matches, conn)
        print_summary(summaries)

        # Include both freshly backfilled and already-complete matches so
        # Phase 3 enriches everything touched in this run.
        ids_to_enrich = [
            s["id"]
            for s in summaries
            if s["error"] is None
        ]

        if not ids_to_enrich:
            print("  No matches available to enrich — aborting.")
            return

        newly_backfilled = [
            s["id"]
            for s in summaries
            if s["error"] is None and not s.get("skipped")
        ]

        print(f"  Matches to enrich : {ids_to_enrich}")
        if newly_backfilled:
            print(f"  Newly backfilled  : {newly_backfilled}")

        # ── Phase 2: Odds backfill ────────────────────────────────────────────
        _phase("PHASE 2: Odds Backfill")

        try:
            run_odds_backfill(tour, date_str, tournament_uid, tournament_name, conn)
        except RuntimeError as exc:
            print(f"  ✗  Odds backfill skipped: {exc}")
            print("     Continuing to enrichment with whatever odds data exists...")

        # ── Phase 3: Dashboard enrichment ─────────────────────────────────────
        _phase("PHASE 3: Dashboard Enrichment")

        total_rows = enrich_match_ids(ids_to_enrich, conn)

        # ── Summary ───────────────────────────────────────────────────────────
        print()
        print(_sep("═"))
        print("  Full backfill complete.")
        print(f"  Matches processed   : {len(ids_to_enrich)}")
        print(f"  Newly backfilled    : {len(newly_backfilled)}")
        print(f"  Dashboard rows      : {total_rows}")
        print(_sep("═"))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
