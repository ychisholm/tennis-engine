from __future__ import annotations

import json
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from src.live.collector import ACTIVE_MATCH_IDS

_DB_PATH   = Path(__file__).resolve().parents[2] / "data" / "processed" / "tennis.duckdb"
_DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "index.html"

app = FastAPI(title="Tennis Engine")


def _conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(_DB_PATH))


def _safe_df_to_json(conn: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    """Run *sql*, return JSON-safe list of dicts (NaN → null)."""
    try:
        df = conn.execute(sql, params or []).fetchdf()
    except duckdb.CatalogException:
        return []
    # pandas to_json handles NaN → null; parse back for FastAPI to re-serialise
    return json.loads(df.to_json(orient="records"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/matches")
def list_matches():
    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT DISTINCT match_id, player_a, player_b
            FROM live_processed.dashboard_log
            ORDER BY match_id DESC
        """).fetchall()
    except duckdb.CatalogException:
        rows = []
    finally:
        conn.close()
    return [{"match_id": r[0], "player_a": r[1], "player_b": r[2]} for r in rows]


@app.get("/live_matches")
def list_live_matches():
    if not ACTIVE_MATCH_IDS:
        return []
    conn = _conn()
    try:
        placeholders = ", ".join("?" * len(ACTIVE_MATCH_IDS))
        sql = f"""
            WITH latest AS (
              SELECT match_id,
                     LAST(home_point ORDER BY ts) as home_point,
                     LAST(away_point ORDER BY ts) as away_point,
                     LAST(server     ORDER BY ts) as server,
                     LAST(sets_a     ORDER BY ts) as sets_a,
                     LAST(sets_b     ORDER BY ts) as sets_b,
                     LAST(games_a    ORDER BY ts) as games_a,
                     LAST(games_b    ORDER BY ts) as games_b,
                     MAX(ts) as last_seen,
                     ANY_VALUE(player_a) as player_a,
                     ANY_VALUE(player_b) as player_b,
                     ANY_VALUE(tournament_name) as tournament_name,
                     ANY_VALUE(category) as category
              FROM live_processed.dashboard_log
              WHERE match_id IN ({placeholders})
              GROUP BY match_id
            )
            SELECT * FROM latest ORDER BY last_seen DESC
        """
        result = _safe_df_to_json(conn, sql, list(ACTIVE_MATCH_IDS))
    finally:
        conn.close()
    return result


@app.get("/match/{match_id}")
def get_match(match_id: int):
    conn = _conn()
    try:
        result = _safe_df_to_json(
            conn,
            "SELECT * FROM live_processed.dashboard_log WHERE match_id = ? ORDER BY point_num",
            [match_id],
        )
    finally:
        conn.close()
    return result


@app.get("/match/{match_id}/latest")
def get_latest(match_id: int):
    conn = _conn()
    try:
        result = _safe_df_to_json(
            conn,
            """
            SELECT * FROM (
                SELECT * FROM live_processed.dashboard_log WHERE match_id = ?
                ORDER BY point_num DESC LIMIT 20
            ) ORDER BY point_num ASC
            """,
            [match_id],
        )
    finally:
        conn.close()
    return result


@app.get("/live_summary")
def live_summary():
    conn = _conn()
    try:
        result = _safe_df_to_json(conn, """
            SELECT match_id, player_a, player_b, sets_a, sets_b, games_a, games_b
            FROM (
                SELECT *, ROW_NUMBER() OVER (
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


@app.get("/")
def root():
    return Response(
        content='<meta http-equiv="refresh" content="0; url=/dashboard">',
        media_type="text/html",
    )
