from __future__ import annotations

import logging
import math
import os
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

# All columns in dashboard_log (excludes any window-function helpers like rn).
_DL_COLS = (
    "ts, match_id, player_a, player_b, set_num, game_num, point_num, "
    "home_point, away_point, server, point_winner, is_ace, is_double_fault, "
    "model_prob_a, bookmaker_prob_a, edge, d_a, d_b, "
    "nmi_a, nmi_b, sms_a, sms_b, rms_a, rms_b, pms_a, pms_b, gps_a, gps_b, "
    "sets_a, sets_b, games_a, games_b, ingestion_source, tournament_name, category"
)

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

def _enrich_detail_points(rows: list[dict], first_server: str = "home") -> list[dict]:
    """Convert match_detail_points rows into point-level rows for the dashboard.

    Assigns set_num/game_num/point_num and derives server from game parity
    (who served game 1 = first_server). point_winner is read directly from
    the column — derivation lives in the logger so all consumers see the
    same value.

    Rows with status='finished' represent the post-match snapshot whose
    home_sets_won/away_sets_won has already advanced to the final tally
    (creating a phantom set N+1 row); these are excluded from the dashboard
    point-by-point view. Final score is shown via /matches.

    Player A = home, Player B = away throughout.
    """
    if not rows:
        return []

    sorted_rows = sorted(rows, key=lambda r: str(r.get("polled_at") or ""))

    result: list[dict] = []
    for row in sorted_rows:
        if (row.get("status") or "").lower() == "finished":
            continue

        home_sets = row.get("home_sets_won") or 0
        away_sets = row.get("away_sets_won") or 0
        set_num = home_sets + away_sets + 1

        home_g = row.get("home_current_games") or 0
        away_g = row.get("away_current_games") or 0

        if home_g == 6 and away_g == 6:
            game_num = 13
        else:
            game_num = home_g + away_g + 1

        prev_set_games = 0
        for s in range(1, set_num):
            prev_set_games += (row.get(f"home_set{s}_games") or 0) + (row.get(f"away_set{s}_games") or 0)
        total_before = prev_set_games + home_g + away_g

        if total_before % 2 == 0:
            server = first_server
        else:
            server = "away" if first_server == "home" else "home"

        result.append({
            "point_num":       len(result),
            "match_id":        row.get("match_id"),
            "player_a":        row.get("player_a"),
            "player_b":        row.get("player_b"),
            "set_num":         set_num,
            "game_num":        game_num,
            "home_point":      str(row.get("home_current_point") or "0"),
            "away_point":      str(row.get("away_current_point") or "0"),
            "server":          server,
            "point_winner":    row.get("point_winner"),
            "home_sets_won":   home_sets,
            "away_sets_won":   away_sets,
            "home_games_won":  home_g,
            "away_games_won":  away_g,
            "is_ace":          False,
            "is_double_fault": False,
            "has_gap":         False,
            "tournament_name": row.get("tournament_name"),
            "category":        row.get("category"),
            "last_updated":    row.get("polled_at"),
        })

    return result


@app.get("/matches")
def list_matches():
    conn = _conn()
    try:
        rows = _safe_query(conn, """
            WITH latest AS (
                SELECT DISTINCT ON (match_id) *
                FROM live_processed.match_detail_points
                ORDER BY match_id, polled_at DESC
            )
            SELECT
                match_id,
                player_a,
                player_b,
                tournament_name,
                UPPER(category)                    AS category,
                to_char(polled_at, 'YYYY-MM-DD')   AS match_date,
                home_sets_won                      AS sets_a,
                away_sets_won                      AS sets_b,
                ARRAY_REMOVE(
                    ARRAY[home_set1_games, home_set2_games, home_set3_games],
                    NULL
                )                                  AS set_scores_a,
                ARRAY_REMOVE(
                    ARRAY[away_set1_games, away_set2_games, away_set3_games],
                    NULL
                )                                  AS set_scores_b
            FROM latest
            ORDER BY polled_at DESC
        """)
        for row in rows:
            row["is_final"] = True
    finally:
        conn.close()
    return rows


@app.get("/live_matches")
def list_live_matches():
    if not ACTIVE_MATCH_IDS:
        return []
    conn = _conn()
    try:
        result = _safe_query(conn, """
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
                polled_at           AS last_seen
            FROM live_processed.match_detail_points
            WHERE match_id = ANY(%s)
            ORDER BY match_id, polled_at DESC
        """, [list(ACTIVE_MATCH_IDS)])
    finally:
        conn.close()
    for row in result:
        ca, cb = COUNTRY_MAP.get(row["match_id"], (None, None))
        row["country_a"] = ca
        row["country_b"] = cb
    return result


@app.get("/match/{match_id}")
def get_match(match_id: int):
    conn = _conn()
    try:
        rows = _safe_query(conn, """
            SELECT
                match_id, player_a, player_b, polled_at, status,
                home_sets_won, away_sets_won,
                home_set1_games, away_set1_games,
                home_set2_games, away_set2_games,
                home_set3_games, away_set3_games,
                home_current_games, away_current_games,
                home_current_point, away_current_point,
                point_winner, winner_code, tournament_name, category
            FROM live_processed.match_detail_points
            WHERE match_id = %s
            ORDER BY
                home_sets_won, away_sets_won,
                home_current_games, away_current_games,
                polled_at
        """, [match_id])
    finally:
        conn.close()
    return _enrich_detail_points(rows)


@app.get("/match/{match_id}/latest")
def get_latest(match_id: int):
    conn = _conn()
    try:
        result = _safe_query(conn, f"""
            SELECT {_DL_COLS} FROM (
                SELECT * FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (PARTITION BY point_num ORDER BY ts DESC) AS rn
                    FROM live_processed.dashboard_log
                    WHERE match_id = %s
                ) deduped
                WHERE rn = 1
                ORDER BY point_num DESC
                LIMIT 20
            ) last20
            ORDER BY point_num ASC
        """, [match_id])
    finally:
        conn.close()
    return result


@app.get("/live_summary")
def live_summary():
    conn = _conn()
    try:
        result = _safe_query(conn, """
            SELECT match_id, player_a, player_b, sets_a, sets_b, games_a, games_b
            FROM (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY match_id ORDER BY point_num DESC
                       ) AS rn
                FROM live_processed.dashboard_log
                WHERE ingestion_source = 'live'
            ) sub
            WHERE rn = 1
            ORDER BY match_id DESC
        """)
    finally:
        conn.close()
    return result


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
