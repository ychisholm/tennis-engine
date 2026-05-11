"""
Adapter: raw live.backfill_points rows  →  list[StateRow] for the validator.

Pure functions only — no DB access, no environment reads, no I/O.
Inputs arrive as Python data; outputs leave as Python data.

Backfill structure (discovered through experiment, see
scripts/dev/backfill_validate_proper.py for the diagnostic that surfaced
this):

  * Each row is one shown point with correct score columns
    (home_point/away_point) and reliable set_num/game_num boundaries.
  * The API systematically omits the **game-winning point** of every
    game — the last shown point in each game is one point before game
    end (e.g. 40-15 with home about to win, or 6-2 in a tiebreak with
    home about to clinch at 7-2).
  * The point_winner column is unreliable (a known bug in the
    production derive_points logic). This adapter does NOT consult it
    — game winners are inferred purely from the closing score.

For each game group we emit:
  * one StateRow per shown point at that point's score, carrying the
    running (sets, games) totals;
  * one synthesized "game-end" StateRow with score 0-0 and counts
    updated per the next group's context:
        - mid-set game-end   → games incremented for winner
        - set boundary       → sets incremented, games reset to 0-0
                               (the validator's fresh_set_start shape)
        - match end          → sets incremented, games stay at the
                               closing set's final count
                               (the validator's match_end_shape)

The match_end_shape relies on the post-fix validator
(src/verification/validator.py) recognising a closing-set games
state with zeroed score as a clean match-ending set transition.
"""
from __future__ import annotations

from typing import Iterable, Optional

from src.verification.validator import StateRow


_REGULAR_VOCAB = {"0", "15", "30", "40", "AD"}


def _normalize_point(value) -> str:
    """Map None/'' to '0' and "A"/"ADV" to "AD". Integer-string tiebreak
    scores pass through unchanged (after upper/strip)."""
    if value is None or value == "":
        return "0"
    s = str(value).strip().upper()
    if s in {"A", "AD", "ADV"}:
        return "AD"
    return s


def _is_tiebreak_score(hp: str, ap: str) -> bool:
    """True iff at least one side carries an integer-string score outside
    the regular game vocabulary."""
    for v in (hp, ap):
        if v in _REGULAR_VOCAB:
            continue
        try:
            int(v)
            return True
        except (ValueError, TypeError):
            return False
    return False


def _infer_game_winner(last_hp: str, last_ap: str) -> Optional[str]:
    """From the last shown point's score, infer who won the (omitted)
    game-winning point. Returns 'home', 'away', or None if ambiguous
    (e.g. a deuce 40-40 that got further truncated)."""
    if _is_tiebreak_score(last_hp, last_ap):
        try:
            h = int(last_hp)
            a = int(last_ap)
        except (ValueError, TypeError):
            return None
        if h > a:
            return "home"
        if a > h:
            return "away"
        return None

    if last_hp == "AD":
        return "home"
    if last_ap == "AD":
        return "away"
    if last_hp == "40" and last_ap != "40":
        return "home"
    if last_ap == "40" and last_hp != "40":
        return "away"
    return None


def _group_by_game(rows: Iterable[dict]) -> list[tuple[int, int, list[dict]]]:
    """Group consecutive rows by (set_num, game_num), preserving input order."""
    groups: list[tuple[int, int, list[dict]]] = []
    for row in rows:
        key = (row["set_num"], row["game_num"])
        if groups and (groups[-1][0], groups[-1][1]) == key:
            groups[-1][2].append(row)
        else:
            groups.append((key[0], key[1], [row]))
    return groups


def backfill_points_to_state_rows(rows: Iterable[dict]) -> list[StateRow]:
    """
    Convert raw live.backfill_points rows (already ordered by point_num
    ASC, all from one match) into the StateRow sequence the validator
    expects.

    Each input dict carries at minimum:
        set_num, game_num, home_point, away_point
    (point_winner is intentionally not consulted.)
    """
    rows = list(rows)
    if not rows:
        return []

    groups = _group_by_game(rows)

    state_rows: list[StateRow] = []
    sets_a = sets_b = 0
    games_a = games_b = 0
    seq = 0

    for i, (set_num, game_num, points) in enumerate(groups):
        # Emit one StateRow per shown point at that point's score state.
        for pt in points:
            hp = _normalize_point(pt.get("home_point"))
            ap = _normalize_point(pt.get("away_point"))
            seq += 1
            state_rows.append(StateRow(
                sets_a=sets_a, sets_b=sets_b,
                games_a=games_a, games_b=games_b,
                score_a=hp, score_b=ap,
                polled_at=seq,
            ))

        # Infer winner of the omitted game-winning point from the last
        # shown score, then commit it to the running games count.
        last = points[-1]
        last_hp = _normalize_point(last.get("home_point"))
        last_ap = _normalize_point(last.get("away_point"))
        winner = _infer_game_winner(last_hp, last_ap)
        if winner == "home":
            games_a += 1
        elif winner == "away":
            games_b += 1

        next_group = groups[i + 1] if i + 1 < len(groups) else None

        if next_group is None:
            # Match end: increment sets for set winner; games stay at the
            # closing set's final count. This is the match_end_shape the
            # validator recognises.
            if games_a > games_b:
                sets_a += 1
            elif games_b > games_a:
                sets_b += 1
            seq += 1
            state_rows.append(StateRow(
                sets_a=sets_a, sets_b=sets_b,
                games_a=games_a, games_b=games_b,
                score_a="0", score_b="0",
                polled_at=seq,
            ))
        elif next_group[0] != set_num:
            # Set boundary: increment sets and reset games. This is the
            # validator's fresh_set_start shape.
            if games_a > games_b:
                sets_a += 1
            elif games_b > games_a:
                sets_b += 1
            games_a = games_b = 0
            seq += 1
            state_rows.append(StateRow(
                sets_a=sets_a, sets_b=sets_b,
                games_a=games_a, games_b=games_b,
                score_a="0", score_b="0",
                polled_at=seq,
            ))
        else:
            # Mid-set game boundary: games already incremented above;
            # synthesized row carries that with a zeroed point score.
            seq += 1
            state_rows.append(StateRow(
                sets_a=sets_a, sets_b=sets_b,
                games_a=games_a, games_b=games_b,
                score_a="0", score_b="0",
                polled_at=seq,
            ))

    return state_rows


# ---------------------------------------------------------------------------
# Visual check — not part of the module's public surface. Run with
#     .venv/bin/python -m src.verification.backfill_adapter
# to pull today's first finished match's backfill points and print the
# first 10 StateRows.
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
                SELECT DISTINCT match_id
                FROM live.backfill_points
                WHERE ingestion_source = 'backfill_validate_today'
                ORDER BY match_id
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                print(
                    "No backfill points found "
                    "(expected ingestion_source='backfill_validate_today')."
                )
            else:
                mid = row["match_id"]
                cur.execute(
                    """
                    SELECT set_num, game_num, home_point, away_point
                    FROM live.backfill_points
                    WHERE match_id = %s
                      AND ingestion_source = 'backfill_validate_today'
                    ORDER BY point_num ASC
                    """,
                    (mid,),
                )
                raw = [dict(r) for r in cur.fetchall()]
                state_rows = backfill_points_to_state_rows(raw)
                print(
                    f"match {mid}: {len(raw)} backfill points, "
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
