#!/usr/bin/env python3
"""
Dry-run verifier for today's completed matches.

Pulls every match_id from live.match_polls that has reached status='finished'
since today's UTC midnight, walks its live.match_states rows through
src/verification/validator.py, and prints a per-match report plus a roll-up.

Strictly read-only: nothing is written to audit.* tables.

Usage:
    .venv/bin/python scripts/dev/verify_today_dryrun.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from src.verification.validator import (  # noqa: E402
    StateRow,
    parse_state_row,
    validate_match,
)


def _format_recorded_final_score(poll_row: dict) -> str:
    """Build "6-4 7-6" style score from home_periodN/away_periodN columns."""
    parts: list[str] = []
    for n in (1, 2, 3):
        h = poll_row.get(f"home_period{n}")
        a = poll_row.get(f"away_period{n}")
        if h is None or a is None:
            continue
        parts.append(f"{h}-{a}")
    return " ".join(parts)


def _fetch_finished_match_ids(cur) -> list[int]:
    cur.execute(
        """
        SELECT DISTINCT match_id
        FROM live.match_polls
        WHERE status = 'finished'
          AND polled_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                          AT TIME ZONE 'UTC'
        ORDER BY match_id
        """
    )
    return [r["match_id"] for r in cur.fetchall()]


def _fetch_states(cur, match_id: int) -> list[StateRow]:
    cur.execute(
        """
        SELECT polled_at, home_sets_won, away_sets_won,
               home_set1_games, away_set1_games,
               home_set2_games, away_set2_games,
               home_set3_games, away_set3_games,
               home_current_games, away_current_games,
               home_current_point, away_current_point
        FROM live.match_states
        WHERE match_id = %s
        ORDER BY polled_at ASC
        """,
        (match_id,),
    )
    return [parse_state_row(dict(r)) for r in cur.fetchall()]


def _fetch_latest_finished_poll(cur, match_id: int) -> dict | None:
    cur.execute(
        """
        SELECT polled_at, home_sets, away_sets,
               home_period1, away_period1,
               home_period2, away_period2,
               home_period3, away_period3
        FROM live.match_polls
        WHERE match_id = %s AND status = 'finished'
        ORDER BY polled_at DESC
        LIMIT 1
        """,
        (match_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            match_ids = _fetch_finished_match_ids(cur)
            print(f"Found {len(match_ids)} finished matches today (UTC).")
            print("=" * 78)

            verdict_counter: Counter = Counter()
            total_gaps = 0

            for match_id in match_ids:
                state_rows = _fetch_states(cur, match_id)
                poll_row = _fetch_latest_finished_poll(cur, match_id)
                recorded = (
                    _format_recorded_final_score(poll_row) if poll_row else ""
                )

                gaps, summary = validate_match(
                    state_rows, recorded, str(match_id)
                )

                verdict_counter[summary.verdict] += 1
                total_gaps += summary.gap_count

                print()
                print(f"match_id={match_id}")
                print(
                    f"  verdict={summary.verdict}  "
                    f"gap_count={summary.gap_count}  "
                    f"severity_max={summary.severity_max}  "
                    f"inferred_missing_points={summary.inferred_missing_points}"
                )
                print(
                    f"  live_final_score='{summary.live_final_score}'  "
                    f"recorded_final_score='{summary.recorded_final_score}'  "
                    f"match={summary.final_score_match}"
                )
                print(
                    f"  state_rows={len(state_rows)}  "
                    f"total_sets={summary.total_sets}  "
                    f"clean_sets={summary.clean_set_count}  "
                    f"gapped_sets={summary.gapped_set_count}"
                )
                for g in gaps:
                    print(
                        f"    - [{g.severity:6}] {g.gap_type:22} "
                        f"{g.description}"
                    )

            print()
            print("=" * 78)
            print("Roll-up")
            print("-------")
            print(f"  total matches : {len(match_ids)}")
            for verdict, n in sorted(verdict_counter.items()):
                print(f"  {verdict:15}: {n}")
            print(f"  total gaps    : {total_gaps}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
