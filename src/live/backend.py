from __future__ import annotations

import logging
import math
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from src.live.collector import ACTIVE_MATCH_IDS, COUNTRY_MAP
from src.live.tennis_feed import TennisFeed

_log = logging.getLogger(__name__)

_DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "index.html"

app = FastAPI(title="Tennis Engine")

# Lazy singleton — created on first /upcoming_matches request so the backend
# can start even if RAPIDAPI_KEY isn't set yet.
_feed: TennisFeed | None = None


def _get_feed() -> TennisFeed:
    global _feed
    if _feed is None:
        _feed = TennisFeed()
    return _feed


def _conn():
    """Open a new psycopg2 connection from DATABASE_URL."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url)


def _clean_val(v: Any) -> Any:
    """Convert NaN/inf → None and datetime → ISO-8601 string."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _rows_to_json(cur) -> list[dict]:
    """Turn a psycopg2 cursor's result set into a list of JSON-safe dicts."""
    if cur.description is None:
        return []
    cols = [desc[0] for desc in cur.description]
    return [
        {col: _clean_val(val) for col, val in zip(cols, row)}
        for row in cur.fetchall()
    ]


def _safe_query(conn, sql: str, params=None) -> list[dict]:
    """Execute *sql*, return list of dicts.  Returns [] if the table doesn't exist."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
            return _rows_to_json(cur)
    except psycopg2.ProgrammingError:
        # Table not yet created (e.g. fresh DB) — roll back so the connection
        # is still usable, then return an empty list.
        conn.rollback()
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _derive_server(
    first_server: str | None,
    home_set1_games: int,
    away_set1_games: int,
    home_set2_games: int,
    away_set2_games: int,
    home_set3_games: int,
    away_set3_games: int,
    home_current_games: int,
    away_current_games: int,
    home_current_point: Any,
    away_current_point: Any,
    set_num: int,
) -> str | None:
    """Derive who is serving the current point from completed-set games,
    in-set games, and (inside a tiebreak) points played so far.

    Returns 'home', 'away', or None when first_server is unknown.

    Outside a tiebreak: server alternates every game starting with first_server.
    Inside a tiebreak: the player who would have served game 13 starts the
    tiebreak and serves point 1; thereafter pairs of points alternate
    (1, 2-3, 4-5, 6-7, …). points-counts that can't be cast to int (e.g. an
    unexpected 'A' bubbling up from some edge case) fall back to the
    pre-tiebreak alternation rather than raising.
    """
    if first_server is None:
        return None

    completed_games = 0
    # Sum games from sets that have been completed (set_num is the
    # currently-being-played set; sets 1..set_num-1 are completed).
    for s in range(1, set_num):
        if s == 1:
            completed_games += (home_set1_games or 0) + (away_set1_games or 0)
        elif s == 2:
            completed_games += (home_set2_games or 0) + (away_set2_games or 0)
        elif s == 3:
            completed_games += (home_set3_games or 0) + (away_set3_games or 0)
    completed_games += (home_current_games or 0) + (away_current_games or 0)
    current_game_number = completed_games + 1

    other_side = "away" if first_server == "home" else "home"
    regular_server = first_server if (current_game_number % 2 == 1) else other_side

    is_in_tiebreak = (home_current_games == 6 and away_current_games == 6)
    if not is_in_tiebreak:
        return regular_server

    starter = regular_server
    other = "away" if starter == "home" else "home"
    try:
        points_played = int(home_current_point) + int(away_current_point)
    except (TypeError, ValueError):
        # 'A'/'AD' shouldn't happen in a tiebreak, but if it does don't crash;
        # fall back to the starter rather than guessing point parity.
        return regular_server

    n = max(points_played, 1)
    if n == 1:
        return starter
    # Points after the first alternate in pairs:
    #   2-3 → other, 4-5 → starter, 6-7 → other, …
    group = (n - 2) // 2
    return other if (group % 2 == 0) else starter


def _enrich_detail_points(rows: list[dict]) -> list[dict]:
    """Convert match_detail_points rows into point-level rows for the dashboard.

    Server identity is computed per-row from each row's first_server column
    (populated by the worker once known) plus completed-set games, in-set
    games, and — inside a tiebreak — points played. When first_server is
    unknown, server is None for that match; the dashboard displays no server
    indicator rather than a wrong one.

    point_winner is read directly from the column — derivation lives in the
    logger so all consumers see the same value.

    Rows with status='finished' represent the post-match snapshot. They
    carry the authoritative final tally (sets/per-set games) but are flagged
    with is_complete_marker=True so the point-by-point view skips them.
    The score header reads the latest row's per-set game columns and
    home_sets_won/away_sets_won directly, so it sees the final state.

    Player A = home, Player B = away throughout.
    """
    if not rows:
        return []

    sorted_rows = sorted(rows, key=lambda r: str(r.get("polled_at") or ""))

    result: list[dict] = []
    for row in sorted_rows:
        status = (row.get("status") or "").lower()
        is_complete_marker = status == "finished"

        home_sets = row.get("home_sets_won") or 0
        away_sets = row.get("away_sets_won") or 0
        # For the complete marker we don't want to invent a phantom set N+1.
        # Cap set_num to the last played set so PBP grouping stays consistent
        # even when this row sneaks past a frontend filter.
        set_num = home_sets + away_sets + (0 if is_complete_marker else 1)
        if set_num < 1:
            set_num = 1

        home_g = row.get("home_current_games") or 0
        away_g = row.get("away_current_games") or 0

        if home_g == 6 and away_g == 6:
            game_num = 13
        else:
            game_num = home_g + away_g + 1

        server = _derive_server(
            first_server=row.get("first_server"),
            home_set1_games=row.get("home_set1_games") or 0,
            away_set1_games=row.get("away_set1_games") or 0,
            home_set2_games=row.get("home_set2_games") or 0,
            away_set2_games=row.get("away_set2_games") or 0,
            home_set3_games=row.get("home_set3_games") or 0,
            away_set3_games=row.get("away_set3_games") or 0,
            home_current_games=home_g,
            away_current_games=away_g,
            home_current_point=row.get("home_current_point") or "0",
            away_current_point=row.get("away_current_point") or "0",
            set_num=set_num,
        )

        result.append({
            "point_num":          len(result),
            "match_id":           row.get("match_id"),
            "player_a":           row.get("player_a"),
            "player_b":           row.get("player_b"),
            "set_num":            set_num,
            "game_num":           game_num,
            "home_point":         str(row.get("home_current_point") or "0"),
            "away_point":         str(row.get("away_current_point") or "0"),
            "server":             server,
            "point_winner":       row.get("point_winner"),
            "home_sets_won":      home_sets,
            "away_sets_won":      away_sets,
            "home_games_won":     home_g,
            "away_games_won":     away_g,
            "home_set1_games":    row.get("home_set1_games"),
            "away_set1_games":    row.get("away_set1_games"),
            "home_set2_games":    row.get("home_set2_games"),
            "away_set2_games":    row.get("away_set2_games"),
            "home_set3_games":    row.get("home_set3_games"),
            "away_set3_games":    row.get("away_set3_games"),
            "status":             row.get("status"),
            "is_complete_marker": is_complete_marker,
            "is_ace":             False,
            "is_double_fault":    False,
            "has_gap":            False,
            "tournament_name":    row.get("tournament_name"),
            "category":           row.get("category"),
            "last_updated":       row.get("polled_at"),
        })

    return result


def _trim_unplayed_sets(set_a: list, set_b: list, sets_a: int, sets_b: int, is_final: bool) -> tuple[list, list]:
    """Strip trailing set slots that were never played.

    Keeps a slot if either side has any games in it OR (for in-progress matches)
    it is the currently-being-played set. For finished matches, only sets where
    at least one game was won are kept.
    """
    sa = list(set_a or [])
    sb = list(set_b or [])
    current_idx = (sets_a or 0) + (sets_b or 0)
    keep_through = -1
    for i in range(max(len(sa), len(sb))):
        a_g = sa[i] if i < len(sa) else 0
        b_g = sb[i] if i < len(sb) else 0
        has_games = (a_g or 0) > 0 or (b_g or 0) > 0
        is_current_in_progress = (not is_final) and i == current_idx
        if has_games or is_current_in_progress:
            keep_through = i
    return sa[:keep_through + 1], sb[:keep_through + 1]


@app.get("/matches")
def list_matches():
    with closing(_conn()) as conn:
        rows = _safe_query(conn, """
            WITH latest AS (
                SELECT DISTINCT ON (match_id) *
                FROM live.match_states
                ORDER BY match_id, polled_at DESC
            )
            SELECT
                match_id,
                player_a,
                player_b,
                tournament_name,
                UPPER(category)                    AS category,
                to_char(polled_at, 'YYYY-MM-DD')   AS match_date,
                status,
                winner_code,
                country_a,
                country_b,
                home_sets_won                      AS sets_a,
                away_sets_won                      AS sets_b,
                ARRAY[home_set1_games, home_set2_games, home_set3_games] AS set_scores_a,
                ARRAY[away_set1_games, away_set2_games, away_set3_games] AS set_scores_b
            FROM latest
            ORDER BY polled_at DESC
        """)
        conn.commit()
        for row in rows:
            is_final = (
                (row.get("status") or "").lower() == "finished"
                or row.get("winner_code") is not None
            )
            sa, sb = _trim_unplayed_sets(
                row.get("set_scores_a"),
                row.get("set_scores_b"),
                row.get("sets_a") or 0,
                row.get("sets_b") or 0,
                is_final,
            )
            row["set_scores_a"] = sa
            row["set_scores_b"] = sb
            row["is_final"] = is_final
    return rows


@app.get("/live_matches")
def list_live_matches():
    if not ACTIVE_MATCH_IDS:
        return []
    with closing(_conn()) as conn:
        result = _safe_query(conn, """
            SELECT * FROM (
                SELECT DISTINCT ON (match_id)
                    match_id,
                    player_a,
                    player_b,
                    home_sets_won,
                    away_sets_won,
                    home_current_games  AS home_games_won,
                    away_current_games  AS away_games_won,
                    home_current_point  AS home_point,
                    away_current_point  AS away_point,
                    NULL::VARCHAR       AS server,
                    tournament_name,
                    category,
                    country_a,
                    country_b,
                    polled_at           AS last_seen,
                    status              AS _status
                FROM live.match_states
                WHERE match_id = ANY(%s)
                ORDER BY match_id, polled_at DESC
            ) latest
            WHERE _status = 'inprogress'
        """, [list(ACTIVE_MATCH_IDS)])
        conn.commit()
    for row in result:
        row.pop("_status", None)
    # Fall back to in-memory COUNTRY_MAP for matches whose first poll hasn't
    # yet persisted country to the DB.
    for row in result:
        if row.get("country_a") is None or row.get("country_b") is None:
            ca, cb = COUNTRY_MAP.get(row["match_id"], (None, None))
            if row.get("country_a") is None:
                row["country_a"] = ca
            if row.get("country_b") is None:
                row["country_b"] = cb
    return result


@app.get("/match/{match_id}")
def get_match(match_id: int):
    with closing(_conn()) as conn:
        rows = _safe_query(conn, """
            SELECT
                match_id, player_a, player_b, polled_at, status,
                home_sets_won, away_sets_won,
                home_set1_games, away_set1_games,
                home_set2_games, away_set2_games,
                home_set3_games, away_set3_games,
                home_current_games, away_current_games,
                home_current_point, away_current_point,
                point_winner, winner_code, tournament_name, category,
                first_server
            FROM live.match_states
            WHERE match_id = %s
            ORDER BY
                home_sets_won, away_sets_won,
                home_current_games, away_current_games,
                polled_at
        """, [match_id])
        conn.commit()
    return _enrich_detail_points(rows)


@app.get("/dashboard")
def dashboard():
    if not _DASHBOARD.exists():
        raise HTTPException(status_code=404, detail="Dashboard HTML not found")
    return FileResponse(str(_DASHBOARD), media_type="text/html")


@app.get("/upcoming_matches")
def upcoming_matches() -> list[dict[str, Any]]:
    try:
        return _get_feed().get_upcoming_matches(days_ahead=1)
    except Exception as exc:
        _log.warning("upcoming_matches error: %s", exc)
        return []


@app.get("/")
def root():
    return Response(
        content='<meta http-equiv="refresh" content="0; url=/dashboard">',
        media_type="text/html",
    )
