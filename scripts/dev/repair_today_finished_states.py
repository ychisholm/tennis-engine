#!/usr/bin/env python3
"""
One-shot repair for today's status='finished' rows in live.match_states.

Before the writer fix, the terminal 'finished' poll wrote rows with
home_current_games=0, away_current_games=0 and stale point strings
(e.g. '40-15'), because period{sets_a + sets_b + 1} aimed at a
non-existent next set. This script repairs those rows in place by
sourcing current_games from the just-completed set (N = sets_a + sets_b,
i.e. the home_setN_games / away_setN_games columns) and zeroing the
point strings.

Strictly scoped to polled_at >= CURRENT_DATE AND status = 'finished'.

Usage:
    .venv/bin/python scripts/dev/repair_today_finished_states.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


_BEFORE_QUERY = """
SELECT match_id, polled_at,
       home_sets_won, away_sets_won,
       home_current_games, away_current_games,
       home_current_point, away_current_point,
       home_set1_games, away_set1_games,
       home_set2_games, away_set2_games,
       home_set3_games, away_set3_games
FROM live.match_states
WHERE polled_at >= CURRENT_DATE
  AND status = 'finished'
ORDER BY match_id, polled_at
"""

_REPAIR_QUERY = """
UPDATE live.match_states
SET home_current_games = CASE (home_sets_won + away_sets_won)
        WHEN 1 THEN home_set1_games
        WHEN 2 THEN home_set2_games
        WHEN 3 THEN home_set3_games
        ELSE home_current_games
    END,
    away_current_games = CASE (home_sets_won + away_sets_won)
        WHEN 1 THEN away_set1_games
        WHEN 2 THEN away_set2_games
        WHEN 3 THEN away_set3_games
        ELSE away_current_games
    END,
    home_current_point = '0',
    away_current_point = '0'
WHERE polled_at >= CURRENT_DATE
  AND status = 'finished'
"""


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(_BEFORE_QUERY)
            before = cur.fetchall()
            print(f"Rows matching repair scope (today, status='finished'): {len(before)}")
            print()
            print("BEFORE (sample):")
            for r in before[:5]:
                print(
                    f"  match={r[0]} polled={r[1]} sets={r[2]}-{r[3]} "
                    f"curG={r[4]}-{r[5]} pt={r[6]}-{r[7]} "
                    f"s1g={r[8]}-{r[9]} s2g={r[10]}-{r[11]} s3g={r[12]}-{r[13]}"
                )
            if len(before) > 5:
                print(f"  ... ({len(before) - 5} more)")
            print()

            cur.execute(_REPAIR_QUERY)
            updated = cur.rowcount

            cur.execute(_BEFORE_QUERY)
            after = cur.fetchall()
            print(f"UPDATE rowcount: {updated}")
            print()
            print("AFTER (same sample):")
            for r in after[:5]:
                print(
                    f"  match={r[0]} polled={r[1]} sets={r[2]}-{r[3]} "
                    f"curG={r[4]}-{r[5]} pt={r[6]}-{r[7]} "
                    f"s1g={r[8]}-{r[9]} s2g={r[10]}-{r[11]} s3g={r[12]}-{r[13]}"
                )
            if len(after) > 5:
                print(f"  ... ({len(after) - 5} more)")

        conn.commit()
        print()
        print("COMMITTED.")
    except Exception as exc:
        conn.rollback()
        print(f"REPAIR FAILED — rolled back: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
