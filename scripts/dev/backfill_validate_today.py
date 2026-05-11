#!/usr/bin/env python3
"""
Backfill-side validator dry-run for today's completed matches.

For each match_id that reached status='finished' since today's UTC midnight:
  1. Fetch /point-by-point via TennisFeed (one API call per match).
  2. Flatten the response into ordered points using the same derive_points()
     helper that the production backfill ingestion uses.
  3. Convert the point list into a StateRow sequence the validator can walk
     (one 0-0 starter row at each game boundary, then one row per point
     showing the post-point score, plus a final match-end row).
  4. Run src/verification/validator.py:validate_match() against the
     recorded final score from live.match_polls — same comparison the
     live-side dry-run does.

Prints per-match: live point count vs backfill point count, both verdicts,
both final-score reconciliations. Also writes the fetched backfill points
to live.backfill_points so future runs don't need the API calls.

Strictly read-only with respect to live.match_states / audit.*.

Usage:
    .venv/bin/python scripts/dev/backfill_validate_today.py
"""
from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from src.live.tennis_feed import TennisFeed  # noqa: E402
from src.verification.validator import (  # noqa: E402
    StateRow,
    parse_state_row,
    validate_match,
)
from scripts.backtesting.backfill_today import derive_points  # noqa: E402


def _format_recorded_final_score(poll_row: dict) -> str:
    parts: list[str] = []
    for n in (1, 2, 3, 4, 5):
        h = poll_row.get(f"home_period{n}")
        a = poll_row.get(f"away_period{n}")
        if h is None or a is None:
            continue
        parts.append(f"{h}-{a}")
    return " ".join(parts)


def _fetch_finished_match_ids(cur) -> list[dict]:
    cur.execute(
        """
        SELECT DISTINCT ON (match_id)
               match_id, player_a, player_b
        FROM live.match_polls
        WHERE status = 'finished'
          AND polled_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                          AT TIME ZONE 'UTC'
        ORDER BY match_id, polled_at DESC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_live_states(cur, match_id: int) -> list[StateRow]:
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


def _backfill_points_to_state_rows(points: list[dict]) -> list[StateRow]:
    """
    Convert ordered backfill points into a StateRow sequence the validator
    can walk cleanly.

    For each game we emit:
      - one "game start" row at score=0-0 with the current (sets, games) totals
      - one row per point in the game showing that point's post-state score

    At each game boundary we increment the games count for the winner before
    emitting the next game's start row. At each set boundary we additionally
    commit the set winner and reset games. After the last point we emit a
    "match-end" row carrying the closing set's actual final games with score
    zeroed — the shape the post-fix validator recognizes as a clean
    match-ending set transition.
    """
    rows: list[StateRow] = []
    sets_a = sets_b = 0
    games_a = games_b = 0
    prev_set: int | None = None
    prev_game: int | None = None
    prev_winner: str | None = None
    seq = 0

    def emit(score_a: str, score_b: str) -> None:
        nonlocal seq
        seq += 1
        rows.append(StateRow(
            sets_a=sets_a, sets_b=sets_b,
            games_a=games_a, games_b=games_b,
            score_a=score_a, score_b=score_b,
            polled_at=seq,
        ))

    for pt in points:
        set_num = pt["set_num"]
        game_num = pt["game_num"]
        winner = pt["point_winner"]
        hp = str(pt["home_point"])
        ap = str(pt["away_point"])

        if prev_game is None:
            emit("0", "0")
        elif set_num != prev_set or game_num != prev_game:
            if prev_winner == "home":
                games_a += 1
            elif prev_winner == "away":
                games_b += 1
            if set_num != prev_set:
                if games_a > games_b:
                    sets_a += 1
                elif games_b > games_a:
                    sets_b += 1
                games_a = games_b = 0
            emit("0", "0")

        emit(hp, ap)

        prev_set = set_num
        prev_game = game_num
        prev_winner = winner

    if prev_winner == "home":
        games_a += 1
    elif prev_winner == "away":
        games_b += 1
    if games_a > games_b:
        sets_a += 1
    elif games_b > games_a:
        sets_b += 1
    emit("0", "0")

    return rows


_INSERT_BACKFILL = """
INSERT INTO live.backfill_points (
    ts, match_id, player_a, player_b,
    point_num, set_num, game_num,
    home_point, away_point, server, point_winner,
    is_ace, is_double_fault, ingestion_source
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _persist_backfill(
    conn,
    match_id: int,
    player_a: str | None,
    player_b: str | None,
    points: list[dict],
    base_ts: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM live.backfill_points WHERE match_id = %s",
            (match_id,),
        )
        row = cur.fetchone()
        existing = row["n"] if isinstance(row, dict) else row[0]
        if existing > 0:
            return
        for i, pt in enumerate(points):
            cur.execute(_INSERT_BACKFILL, [
                base_ts, match_id, player_a, player_b,
                i, pt["set_num"], pt["game_num"],
                pt["home_point"], pt["away_point"],
                pt["server"], pt["point_winner"],
                pt["is_ace"], pt["is_double_fault"],
                "backfill_validate_today",
            ])
    conn.commit()


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    api_key = os.getenv("RAPIDAPI_KEY")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1
    if not api_key:
        print("RAPIDAPI_KEY not set.", file=sys.stderr)
        return 1

    feed = TennisFeed(api_key=api_key)
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            matches = _fetch_finished_match_ids(cur)
        print(f"Found {len(matches)} finished matches today (UTC).")
        print("=" * 92)

        live_verdicts: Counter = Counter()
        bf_verdicts: Counter = Counter()
        rows_summary: list[dict] = []

        for m in matches:
            mid = m["match_id"]
            pa = m.get("player_a")
            pb = m.get("player_b")
            with conn.cursor() as cur:
                live_rows = _fetch_live_states(cur, mid)
                poll_row = _fetch_latest_finished_poll(cur, mid)
            recorded = _format_recorded_final_score(poll_row) if poll_row else ""

            live_gaps, live_summary = validate_match(live_rows, recorded, str(mid))
            live_verdicts[live_summary.verdict] += 1

            bf_verdict = "fetch_failed"
            bf_summary = None
            bf_point_count = 0
            bf_final_score = ""
            bf_match: bool | None = None
            error: str | None = None
            try:
                raw = feed.get_point_by_point(mid)
                points = derive_points(raw)
                bf_point_count = len(points)
                bf_rows = _backfill_points_to_state_rows(points)
                bf_gaps, bf_summary = validate_match(bf_rows, recorded, str(mid))
                bf_verdict = bf_summary.verdict
                bf_final_score = bf_summary.live_final_score
                bf_match = bf_summary.final_score_match
            except Exception as exc:
                error = str(exc)
            try:
                if bf_summary is not None:
                    _persist_backfill(conn, mid, pa, pb, points, datetime.now(timezone.utc))
            except Exception as exc:
                error = (error + "; " if error else "") + f"persist failed: {exc}"
            bf_verdicts[bf_verdict] += 1

            rows_summary.append({
                "match_id": mid,
                "live_pts": len(live_rows),
                "live_verdict": live_summary.verdict,
                "live_final": live_summary.live_final_score,
                "live_match": live_summary.final_score_match,
                "live_gaps": live_summary.gap_count,
                "bf_pts": bf_point_count,
                "bf_verdict": bf_verdict,
                "bf_final": bf_final_score,
                "bf_match": bf_match,
                "bf_gaps": bf_summary.gap_count if bf_summary else None,
                "recorded": recorded,
                "error": error,
            })

            time.sleep(0.3)  # gentle rate-limit cushion

        print()
        header = (
            f"{'match_id':>10}  "
            f"{'live_pts':>8} {'live_verdict':<14} {'live_final':<22} {'L=R':>3}  "
            f"{'bf_pts':>6} {'bf_verdict':<14} {'bf_final':<22} {'B=R':>3}  "
            f"recorded"
        )
        print(header)
        print("-" * len(header))
        for r in rows_summary:
            print(
                f"{r['match_id']:>10}  "
                f"{r['live_pts']:>8} {r['live_verdict']:<14} "
                f"{r['live_final']:<22} "
                f"{('T' if r['live_match'] else 'F'):>3}  "
                f"{r['bf_pts']:>6} {r['bf_verdict']:<14} "
                f"{r['bf_final']:<22} "
                f"{('T' if r['bf_match'] else ('F' if r['bf_match'] is False else '-')):>3}  "
                f"{r['recorded']}"
            )
            if r['error']:
                print(f"             error: {r['error']}")

        print()
        print("=" * 92)
        print("Roll-up")
        print("-------")
        print(f"  total matches : {len(matches)}")
        print("  live verdicts:")
        for v, n in sorted(live_verdicts.items()):
            print(f"    {v:15}: {n}")
        print("  backfill verdicts:")
        for v, n in sorted(bf_verdicts.items()):
            print(f"    {v:15}: {n}")

        live_clean = live_verdicts.get("clean", 0)
        bf_clean = bf_verdicts.get("clean", 0)
        bf_reconciled = sum(1 for r in rows_summary if r["bf_match"] is True)
        live_reconciled = sum(1 for r in rows_summary if r["live_match"] is True)
        print()
        print(f"  live  final-score reconciled: {live_reconciled}/{len(matches)}")
        print(f"  backfill final-score reconciled: {bf_reconciled}/{len(matches)}")
        print(f"  live  clean verdict: {live_clean}/{len(matches)}")
        print(f"  backfill clean verdict: {bf_clean}/{len(matches)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
