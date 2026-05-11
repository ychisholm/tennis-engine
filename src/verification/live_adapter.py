"""
Adapter: raw live.match_states rows  →  list[StateRow] for the validator.

Pure functions only — no DB access, no environment reads, no I/O.
The orchestrator is responsible for fetching rows in the right order
and for handing the resulting StateRow list to validate_match.

The conversion is a thin wrapper around parse_state_row() from the
validator module: it maps home → A, away → B, normalizes the "A"
advantage indicator to "AD", and skips status='notstarted' rows
(pre-match polling that carries no game state).
"""
from __future__ import annotations

from typing import Iterable

from src.verification.validator import StateRow, parse_state_row


def live_match_states_to_state_rows(rows: Iterable[dict]) -> list[StateRow]:
    """
    Convert raw live.match_states rows (already ordered by polled_at ASC,
    all from one match) into the StateRow sequence the validator expects.

    Each input dict should carry the column names from live.match_states:
        status, home_sets_won, away_sets_won,
        home_set1_games, away_set1_games, …,
        home_current_games, away_current_games,
        home_current_point, away_current_point,
        polled_at

    Rows with status='notstarted' are filtered out — they precede any
    real game state. Every other row is mapped via parse_state_row.
    The output order matches the input order (minus the filtered rows).
    """
    return [
        parse_state_row(row)
        for row in rows
        if row.get("status") != "notstarted"
    ]


# ---------------------------------------------------------------------------
# Visual check — not part of the module's public surface. Run with
#     .venv/bin/python -m src.verification.live_adapter
# to pull today's first finished match and print the first 10 StateRows.
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import os
    from pathlib import Path

    import psycopg2
    from dotenv import load_dotenv
    from psycopg2.extras import RealDictCursor

    _ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(_ROOT / ".env")

    conn = psycopg2.connect(
        os.getenv("DATABASE_URL"), cursor_factory=RealDictCursor
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT match_id FROM live.match_polls
                WHERE status = 'finished'
                  AND polled_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                                  AT TIME ZONE 'UTC'
                ORDER BY match_id
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                print("No finished matches today.")
            else:
                mid = row["match_id"]
                cur.execute(
                    """
                    SELECT polled_at, status,
                           home_sets_won, away_sets_won,
                           home_set1_games, away_set1_games,
                           home_set2_games, away_set2_games,
                           home_set3_games, away_set3_games,
                           home_current_games, away_current_games,
                           home_current_point, away_current_point
                    FROM live.match_states
                    WHERE match_id = %s
                    ORDER BY polled_at ASC
                    """,
                    (mid,),
                )
                raw = [dict(r) for r in cur.fetchall()]
                state_rows = live_match_states_to_state_rows(raw)
                print(
                    f"match {mid}: {len(raw)} raw rows, "
                    f"{len(state_rows)} state rows. First 10:"
                )
                for sr in state_rows[:10]:
                    print(
                        f"  sets={sr.sets_a}-{sr.sets_b}  "
                        f"games={sr.games_a}-{sr.games_b}  "
                        f"score={sr.score_a}-{sr.score_b}  "
                        f"polled_at={sr.polled_at}"
                    )
    finally:
        conn.close()
