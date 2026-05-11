"""
Promotes one verified backfill match from audit + live data into the
canonical `book` schema.

Eligibility (per spec):
  - audit.verification_reports row exists for
    (match_id, source='backfill', verification_run_id)
  - that row's verdict ∈ {'clean', 'minor_gaps'}
  - that row's final_score_match = TRUE

If eligible and not already promoted:
  - resolves both players via book.player_resolver.resolve_player
  - inserts one row into book.matches
  - walks live.backfill_points and inserts one book.points row per shown
    point plus one synthesized "game-winning point" per game (since the
    API systematically truncates that final point)
  - upserts book.player_career_stats for both players, accumulating
    serve/return point counts and matches played/won

All writes happen inside a SAVEPOINT. The caller commits.

DO NOT trust live.backfill_points.point_winner — it's known-corrupt.
Point winners are derived from the score-column progression.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

import psycopg2.extensions

from src.book.player_resolver import resolve_player

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score-progression helpers (kept local — small, no need to import from
# backfill_adapter and create a coupling here)
# ---------------------------------------------------------------------------

_REGULAR_RANK = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4, "A": 4}
_REGULAR_VOCAB = frozenset({"0", "15", "30", "40", "AD"})


def _normalize_point(value) -> str:
    if value is None or value == "":
        return "0"
    s = str(value).strip().upper()
    if s in {"A", "AD", "ADV"}:
        return "AD"
    return s


def _is_tiebreak_score(hp: str, ap: str) -> bool:
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


def _derive_within_game_winner(
    prev_hp: str, prev_ap: str, hp: str, ap: str, is_tb: bool
) -> Optional[str]:
    if is_tb:
        try:
            ph = int(prev_hp)
            pa = int(prev_ap)
            ch = int(hp)
            ca = int(ap)
        except (ValueError, TypeError):
            return None
        if ch > ph:
            return "home"
        if ca > pa:
            return "away"
        return None

    ph = _REGULAR_RANK.get(prev_hp, 0)
    pa = _REGULAR_RANK.get(prev_ap, 0)
    ch = _REGULAR_RANK.get(hp, 0)
    ca = _REGULAR_RANK.get(ap, 0)
    # Home advanced OR away regressed from AD (deuce reset by home).
    if ch > ph or ca < pa:
        return "home"
    # Away advanced OR home regressed from AD.
    if ca > pa or ch < ph:
        return "away"
    return None


def _normalize_surface(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    s = str(raw).strip().lower()
    if "clay" in s:
        return "clay"
    if "hard" in s:
        return "hard"
    if "grass" in s:
        return "grass"
    if "carpet" in s:
        return "carpet"
    return s


# ---------------------------------------------------------------------------
# Eligibility + metadata lookups
# ---------------------------------------------------------------------------

_VALID_VERDICTS = frozenset({"clean", "minor_gaps"})


def _check_eligibility(cur, match_id: int, run_id: str) -> Optional[dict]:
    """Returns the verification report row as a dict if eligible, else None."""
    cur.execute(
        """
        SELECT verdict, final_score_match, recorded_final_score
        FROM audit.verification_reports
        WHERE match_id = %s
          AND source = 'backfill'
          AND verification_run_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(match_id), run_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    verdict, score_match, recorded = row
    if verdict not in _VALID_VERDICTS:
        return None
    if not score_match:
        return None
    return {"verdict": verdict, "final_score": recorded or ""}


def _already_promoted(cur, match_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM book.matches WHERE match_id = %s", (match_id,)
    )
    return cur.fetchone() is not None


def _fetch_match_metadata(cur, match_id: int) -> Optional[dict]:
    """Pull player IDs/names from audit JSON; tournament/date/sets from match_polls."""
    cur.execute(
        """
        SELECT (raw_json -> 'event' -> 'homeTeam' ->> 'id')::BIGINT AS home_id,
               (raw_json -> 'event' -> 'awayTeam' ->> 'id')::BIGINT AS away_id,
               raw_json -> 'event' -> 'homeTeam' ->> 'name'         AS home_name,
               raw_json -> 'event' -> 'awayTeam' ->> 'name'         AS away_name,
               raw_json -> 'event' ->> 'groundType'                 AS surface_raw
        FROM audit.api_response_archive
        WHERE match_id = %s
          AND endpoint = 'match_details'
          AND raw_json -> 'event' -> 'homeTeam' ->> 'id' IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (str(match_id),),
    )
    j = cur.fetchone()
    if not j:
        return None
    home_id, away_id, home_name, away_name, surface_raw = j
    if home_id is None or away_id is None:
        return None

    cur.execute(
        """
        SELECT tournament_name,
               MIN(polled_at)::date AS match_date,
               MAX(home_sets)       AS sets_a,
               MAX(away_sets)       AS sets_b
        FROM live.match_polls
        WHERE match_id = %s
        GROUP BY tournament_name
        ORDER BY match_date DESC
        LIMIT 1
        """,
        (match_id,),
    )
    p = cur.fetchone()
    if not p:
        return None
    tournament, match_date, sets_a, sets_b = p

    return {
        "home_id": int(home_id),
        "away_id": int(away_id),
        "home_name": home_name,
        "away_name": away_name,
        "tournament": tournament,
        "surface": _normalize_surface(surface_raw),
        "match_date": match_date,
        "sets_a": int(sets_a or 0),
        "sets_b": int(sets_b or 0),
    }


# ---------------------------------------------------------------------------
# Point walk + stats
# ---------------------------------------------------------------------------

_INSERT_POINT = """
INSERT INTO book.points (
    match_id, set_num, game_num, point_num, global_point_num,
    server_id, point_winner_id, score_after, is_tiebreak
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _bump_stats(stats: dict, server_side: str, winner_side: str) -> None:
    """Update the per-side stats dict in place for one point."""
    receiver = "away" if server_side == "home" else "home"
    stats[server_side]["serve_played"] += 1
    stats[receiver]["return_played"] += 1
    if winner_side == server_side:
        stats[server_side]["serve_won"] += 1
    else:
        stats[receiver]["return_won"] += 1


def _walk_points_and_insert(
    cur, match_id: int, home_id: int, away_id: int, points: list[dict]
) -> dict:
    """
    Walk shown backfill points + synthesize the omitted game-winning
    point. Inserts into book.points and returns per-side stats deltas:
        {"home": {serve_played, serve_won, return_played, return_won},
         "away": same shape}
    """
    stats = {
        "home": {"serve_played": 0, "serve_won": 0, "return_played": 0, "return_won": 0},
        "away": {"serve_played": 0, "serve_won": 0, "return_played": 0, "return_won": 0},
    }
    side_id = {"home": home_id, "away": away_id}

    # Group consecutive rows by (set_num, game_num) preserving order.
    groups: list[tuple[int, int, list[dict]]] = []
    for row in points:
        key = (row["set_num"], row["game_num"])
        if groups and (groups[-1][0], groups[-1][1]) == key:
            groups[-1][2].append(row)
        else:
            groups.append((key[0], key[1], [row]))

    global_point_num = 0

    for set_num, game_num, game_points in groups:
        is_tb = game_num == 13 or any(
            _is_tiebreak_score(
                _normalize_point(p["home_point"]),
                _normalize_point(p["away_point"]),
            )
            for p in game_points
        )

        prev_hp, prev_ap = "0", "0"
        last_server: Optional[str] = None
        last_hp, last_ap = "0", "0"
        within_game_pt = 0

        for pt in game_points:
            hp = _normalize_point(pt["home_point"])
            ap = _normalize_point(pt["away_point"])
            server_side = pt.get("server")
            if server_side not in ("home", "away"):
                # Defensive — should never happen on backfill data with the
                # verified score-progression we already cleared. Skip and log.
                _log.warning(
                    "match=%s set=%s game=%s point=%s: unknown server %r; skipping",
                    match_id, set_num, game_num, pt.get("point_num"), server_side,
                )
                continue
            winner_side = _derive_within_game_winner(
                prev_hp, prev_ap, hp, ap, is_tb
            )
            if winner_side is None:
                _log.warning(
                    "match=%s set=%s game=%s: ambiguous winner "
                    "(%s-%s -> %s-%s); skipping",
                    match_id, set_num, game_num, prev_hp, prev_ap, hp, ap,
                )
                prev_hp, prev_ap = hp, ap
                continue

            within_game_pt += 1
            global_point_num += 1
            cur.execute(
                _INSERT_POINT,
                (
                    match_id, set_num, game_num,
                    within_game_pt, global_point_num,
                    side_id[server_side], side_id[winner_side],
                    f"{hp}-{ap}", is_tb,
                ),
            )
            _bump_stats(stats, server_side, winner_side)

            prev_hp, prev_ap = hp, ap
            last_server = server_side
            last_hp, last_ap = hp, ap

        # Synthesize the omitted game-winning point.
        game_winner = _infer_game_winner(last_hp, last_ap)
        if game_winner is None or last_server is None:
            _log.warning(
                "match=%s set=%s game=%s: cannot infer game winner from "
                "closing score %s-%s; skipping synthesized point",
                match_id, set_num, game_num, last_hp, last_ap,
            )
            continue
        within_game_pt += 1
        global_point_num += 1
        cur.execute(
            _INSERT_POINT,
            (
                match_id, set_num, game_num,
                within_game_pt, global_point_num,
                side_id[last_server], side_id[game_winner],
                "GAME", is_tb,
            ),
        )
        _bump_stats(stats, last_server, game_winner)

    return stats


_UPSERT_STATS = """
INSERT INTO book.player_career_stats (
    player_id,
    serve_points_played, serve_points_won,
    return_points_played, return_points_won,
    matches_played, matches_won, last_updated
) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (player_id) DO UPDATE SET
    serve_points_played  = book.player_career_stats.serve_points_played
                           + EXCLUDED.serve_points_played,
    serve_points_won     = book.player_career_stats.serve_points_won
                           + EXCLUDED.serve_points_won,
    return_points_played = book.player_career_stats.return_points_played
                           + EXCLUDED.return_points_played,
    return_points_won    = book.player_career_stats.return_points_won
                           + EXCLUDED.return_points_won,
    matches_played       = book.player_career_stats.matches_played
                           + EXCLUDED.matches_played,
    matches_won          = book.player_career_stats.matches_won
                           + EXCLUDED.matches_won,
    last_updated         = NOW()
"""


def _upsert_career_stats(
    cur, player_id: int, deltas: dict, won_match: bool
) -> None:
    cur.execute(
        _UPSERT_STATS,
        (
            player_id,
            deltas["serve_played"], deltas["serve_won"],
            deltas["return_played"], deltas["return_won"],
            1, 1 if won_match else 0,
        ),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_INSERT_MATCH = """
INSERT INTO book.matches (
    match_id, match_date, tournament, surface,
    player_a_id, player_b_id, final_score, winner_id,
    sets_a, sets_b, verification_run_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (match_id) DO NOTHING
RETURNING match_id
"""


def promote_match(
    conn,
    *,
    match_id: int,
    verification_run_id: uuid.UUID,
    tennis_feed=None,
) -> bool:
    """
    Promote one verified backfill match into the canonical book schema.

    Returns:
        True  if the match was promoted as a result of this call.
        False if the match doesn't qualify (no verification report,
              ineligible verdict, score didn't match) or was already
              promoted previously.
    """
    run_id_str = str(verification_run_id)

    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        report = _check_eligibility(cur, match_id, run_id_str)
        if report is None:
            return False
        if _already_promoted(cur, match_id):
            return False

        meta = _fetch_match_metadata(cur, match_id)
        if meta is None:
            _log.warning(
                "match=%s: no match metadata available (audit/live join empty)",
                match_id,
            )
            return False

        cur.execute(
            """
            SELECT point_num, set_num, game_num,
                   home_point, away_point, server
            FROM live.backfill_points
            WHERE match_id = %s
            ORDER BY set_num, game_num, point_num
            """,
            (match_id,),
        )
        point_rows = [
            dict(zip(
                ("point_num", "set_num", "game_num", "home_point",
                 "away_point", "server"),
                r,
            ))
            for r in cur.fetchall()
        ]
        if not point_rows:
            _log.warning(
                "match=%s: eligible but no backfill_points present", match_id
            )
            return False

        cur.execute("SAVEPOINT sp_promote_match")
        try:
            # Resolve players (each uses its own savepoint internally).
            resolve_player(
                conn, meta["home_id"],
                name_fallback=meta["home_name"],
                tennis_feed=tennis_feed,
            )
            resolve_player(
                conn, meta["away_id"],
                name_fallback=meta["away_name"],
                tennis_feed=tennis_feed,
            )

            winner_id = (
                meta["home_id"] if meta["sets_a"] > meta["sets_b"]
                else meta["away_id"]
            )

            cur.execute(
                _INSERT_MATCH,
                (
                    match_id, meta["match_date"], meta["tournament"],
                    meta["surface"], meta["home_id"], meta["away_id"],
                    report["final_score"], winner_id,
                    meta["sets_a"], meta["sets_b"], run_id_str,
                ),
            )
            if cur.fetchone() is None:
                # Concurrent insert beat us — back out cleanly.
                cur.execute("RELEASE SAVEPOINT sp_promote_match")
                return False

            stats = _walk_points_and_insert(
                cur, match_id, meta["home_id"], meta["away_id"], point_rows
            )

            _upsert_career_stats(
                cur, meta["home_id"], stats["home"],
                won_match=(winner_id == meta["home_id"]),
            )
            _upsert_career_stats(
                cur, meta["away_id"], stats["away"],
                won_match=(winner_id == meta["away_id"]),
            )

            cur.execute("RELEASE SAVEPOINT sp_promote_match")
            return True
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_promote_match")
            raise


# ---------------------------------------------------------------------------
# Visual check
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import os
    from pathlib import Path

    import psycopg2
    from dotenv import load_dotenv

    _ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(_ROOT / ".env")

    RUN_ID = uuid.UUID("0e8c12ce-2487-4254-b79e-05b3ab7f00f9")
    MID = 16098941

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    conn.autocommit = False
    try:
        promoted = promote_match(
            conn, match_id=MID, verification_run_id=RUN_ID
        )
        print(f"promote_match({MID}, {RUN_ID}) -> {promoted}")
        print()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT match_id, match_date, tournament, surface,
                       player_a_id, player_b_id, final_score, winner_id,
                       sets_a, sets_b, verification_run_id
                FROM book.matches WHERE match_id = %s
                """,
                (MID,),
            )
            row = cur.fetchone()
            print("book.matches:")
            if row:
                cols = [
                    "match_id", "match_date", "tournament", "surface",
                    "player_a_id", "player_b_id", "final_score", "winner_id",
                    "sets_a", "sets_b", "verification_run_id",
                ]
                for c, v in zip(cols, row):
                    print(f"  {c:22} = {v}")
            else:
                print("  (no row)")
            print()

            cur.execute(
                "SELECT COUNT(*) FROM book.points WHERE match_id = %s",
                (MID,),
            )
            print(f"book.points rows for match {MID}: {cur.fetchone()[0]}")
            print()

            cur.execute(
                """
                SELECT p.player_id, p.name, p.country, p.dob, p.hand
                FROM book.players p
                JOIN book.matches m ON p.player_id IN (m.player_a_id, m.player_b_id)
                WHERE m.match_id = %s
                ORDER BY p.player_id
                """,
                (MID,),
            )
            print("book.players for this match:")
            for r in cur.fetchall():
                print(f"  id={r[0]:>7} name={r[1]!r:30} country={r[2]} "
                      f"dob={r[3]} hand={r[4]}")
            print()

            cur.execute(
                """
                SELECT s.player_id,
                       s.serve_points_played, s.serve_points_won,
                       s.return_points_played, s.return_points_won,
                       s.matches_played, s.matches_won
                FROM book.player_career_stats s
                JOIN book.matches m
                  ON s.player_id IN (m.player_a_id, m.player_b_id)
                WHERE m.match_id = %s
                ORDER BY s.player_id
                """,
                (MID,),
            )
            print("book.player_career_stats:")
            print(f"  {'player_id':>10}  {'srv_pl':>7} {'srv_w':>6} "
                  f"{'ret_pl':>7} {'ret_w':>6} {'m_pl':>5} {'m_w':>5}")
            for r in cur.fetchall():
                print(f"  {r[0]:>10}  {r[1]:>7} {r[2]:>6} "
                      f"{r[3]:>7} {r[4]:>6} {r[5]:>5} {r[6]:>5}")

        conn.commit()
        print("\nCommitted.")
    finally:
        conn.close()
