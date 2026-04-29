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

@app.get("/matches")
def list_matches():
    conn = _conn()
    try:
        return _safe_query(conn, """
            SELECT DISTINCT match_id, player_a, player_b
            FROM live_processed.dashboard_log
            ORDER BY match_id DESC
        """)
    finally:
        conn.close()


@app.get("/live_matches")
def list_live_matches():
    if not ACTIVE_MATCH_IDS:
        return []
    conn = _conn()
    try:
        # DISTINCT ON (match_id) ordered by ts DESC gives the latest row per
        # match — equivalent to the DuckDB LAST() aggregate.
        result = _safe_query(conn, f"""
            WITH latest AS (
              SELECT DISTINCT ON (match_id)
                match_id, home_point, away_point, server,
                sets_a, sets_b, games_a, games_b,
                ts AS last_seen, player_a, player_b,
                tournament_name, category
              FROM live_processed.dashboard_log
              WHERE match_id = ANY(%s)
              ORDER BY match_id, ts DESC
            )
            SELECT * FROM latest ORDER BY last_seen DESC
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
        # QUALIFY is DuckDB-only; use a subquery with ROW_NUMBER() instead.
        result = _safe_query(conn, f"""
            SELECT {_DL_COLS} FROM (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY point_num ORDER BY ts DESC) AS rn
                FROM live_processed.dashboard_log
                WHERE match_id = %s
            ) sub
            WHERE rn = 1
            ORDER BY point_num
        """, [match_id])
    finally:
        conn.close()
    return result


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
