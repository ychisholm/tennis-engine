#!/usr/bin/env python3
"""
enrich_dashboard_log.py — Replay backfilled raw points through the
LiveMatch engine, attach historical odds via ASOF JOIN, and write fully
populated rows to live_processed.dashboard_log so the React dashboard
History tab renders correctly.

Usage (from project root):
    .venv/bin/python scripts/backtesting/enrich_dashboard_log.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import duckdb
from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from src.engine.live_match import LiveMatch

_DB_PATH = _ROOT / "data" / "processed" / "tennis.duckdb"

_DEFAULT_PLAYER: dict = {
    "p0_hard":   0.63,
    "p0_clay":   0.60,
    "p0_grass":  0.63,
    "archetype": {"sd": 60, "ba": 60, "pe": 60, "tv": 60},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sep(char: str = "─", width: int = 70) -> str:
    return char * width


def _clean(v: object) -> object:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


# ── DB setup ──────────────────────────────────────────────────────────────────

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS live_raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS live_processed")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_processed.dashboard_log (
            ts               TIMESTAMP,
            match_id         INTEGER,
            player_a         VARCHAR,
            player_b         VARCHAR,
            set_num          INTEGER,
            game_num         INTEGER,
            point_num        INTEGER,
            home_point       VARCHAR,
            away_point       VARCHAR,
            server           VARCHAR,
            point_winner     VARCHAR,
            is_ace           BOOLEAN,
            is_double_fault  BOOLEAN,
            model_prob_a     FLOAT,
            bookmaker_prob_a FLOAT,
            edge             FLOAT,
            d_a              FLOAT,
            d_b              FLOAT,
            nmi_a            FLOAT,
            nmi_b            FLOAT,
            sms_a            FLOAT,
            sms_b            FLOAT,
            rms_a            FLOAT,
            rms_b            FLOAT,
            pms_a            FLOAT,
            pms_b            FLOAT,
            gps_a            FLOAT,
            gps_b            FLOAT,
            sets_a           INTEGER,
            sets_b           INTEGER,
            games_a          INTEGER,
            games_b          INTEGER,
            ingestion_source VARCHAR,
            tournament_name  VARCHAR,
            category         VARCHAR
        )
    """)


# ── Data loading ──────────────────────────────────────────────────────────────

def _list_matches(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = conn.execute("""
        SELECT
            p.match_id,
            ANY_VALUE(p.player_a)        AS player_a,
            ANY_VALUE(p.player_b)        AS player_b,
            ANY_VALUE(p.tournament_name) AS tournament_name,
            COUNT(*)                     AS num_points,
            COALESCE(ANY_VALUE(o.num_polls), 0) AS num_polls,
            COALESCE(ANY_VALUE(d.enriched), 0)  AS already_enriched
        FROM live_raw.tennisapi_points p
        LEFT JOIN (
            SELECT match_id, COUNT(*) AS num_polls
            FROM live_raw.oddsapi_polls
            GROUP BY match_id
        ) o ON p.match_id = o.match_id
        LEFT JOIN (
            SELECT match_id, COUNT(*) AS enriched
            FROM live_processed.dashboard_log
            WHERE model_prob_a IS NOT NULL
            GROUP BY match_id
        ) d ON p.match_id = d.match_id
        GROUP BY p.match_id
        ORDER BY p.match_id DESC
    """).fetchall()
    return [
        {
            "match_id":         r[0],
            "player_a":         r[1],
            "player_b":         r[2],
            "tournament_name":  r[3],
            "num_points":       r[4],
            "num_polls":        r[5],
            "already_enriched": r[6],
        }
        for r in rows
    ]


def _load_joined_points(conn: duckdb.DuckDBPyConnection, match_id: int) -> list[dict]:
    """Load points with odds attached via ASOF LEFT JOIN.

    Each point receives the most recent odds poll whose timestamp is at or
    before the point's timestamp.  Synthetic timestamps in tennisapi_points
    (spaced 45 s apart from startTimestamp) make this chronologically correct.
    Points that precede the first odds snapshot get NULL bookmaker_prob_a.
    """
    rows = conn.execute("""
        SELECT
            p.ts, p.match_id, p.player_a, p.player_b,
            p.point_num, p.set_num, p.game_num,
            p.home_point, p.away_point, p.server, p.point_winner,
            p.is_ace, p.is_double_fault,
            p.ingestion_source, p.tournament_name, p.category,
            o.bookmaker_prob_a
        FROM live_raw.tennisapi_points p
        ASOF LEFT JOIN live_raw.oddsapi_polls o
            ON p.match_id = o.match_id AND p.ts >= o.ts
        WHERE p.match_id = ?
        ORDER BY p.point_num ASC
    """, [match_id]).fetchall()

    cols = [
        "ts", "match_id", "player_a", "player_b",
        "point_num", "set_num", "game_num",
        "home_point", "away_point", "server", "point_winner",
        "is_ace", "is_double_fault",
        "ingestion_source", "tournament_name", "category",
        "bookmaker_prob_a",
    ]
    return [dict(zip(cols, row)) for row in rows]


# ── Engine replay ─────────────────────────────────────────────────────────────

_INSERT_DASHBOARD = """
INSERT INTO live_processed.dashboard_log (
    ts, match_id, player_a, player_b,
    set_num, game_num, point_num,
    home_point, away_point, server, point_winner, is_ace, is_double_fault,
    model_prob_a, bookmaker_prob_a, edge,
    d_a, d_b,
    nmi_a, nmi_b, sms_a, sms_b, rms_a, rms_b, pms_a, pms_b, gps_a, gps_b,
    sets_a, sets_b, games_a, games_b,
    ingestion_source, tournament_name, category
) VALUES (
    ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?
)
"""


def _enrich_match(conn: duckdb.DuckDBPyConnection, match_id: int) -> int:
    """Replay the match through LiveMatch, attach odds, write to dashboard_log.
    Returns number of rows inserted.
    """
    points = _load_joined_points(conn, match_id)
    if not points:
        print(f"  [{match_id}] No points found — skipping.")
        return 0

    meta = points[0]
    player_a_name = meta["player_a"] or "Home"
    player_b_name = meta["player_b"] or "Away"

    player_a = {**_DEFAULT_PLAYER, "name": player_a_name}
    player_b = {**_DEFAULT_PLAYER, "name": player_b_name}

    engine = LiveMatch(
        player_a=player_a,
        player_b=player_b,
        surface="hard",
        best_of=3,
    )

    rows_to_insert: list[list] = []
    last_result: dict | None = None

    for pt in points:
        winner_e  = "A" if pt["point_winner"] == "home" else "B"
        serving_e = "A" if pt["server"]       == "home" else "B"

        try:
            result = engine.process_point({"winner": winner_e, "serving": serving_e})
            last_result = result
        except RuntimeError:
            # Engine declared match over; reuse final state so every raw point
            # still gets a dashboard_log row.
            result = last_result

        ms    = result["match_state"]
        probs = result["probabilities"]
        dom   = result["dominance"]
        sa    = dom["breakdown_A"]
        sb    = dom["breakdown_B"]

        model_p  = _clean(probs.get("P_match_A"))
        bookie_p = _clean(pt["bookmaker_prob_a"])
        edge     = (
            _clean(round(model_p - bookie_p, 6))
            if model_p is not None and bookie_p is not None
            else None
        )

        ts = pt["ts"]
        if isinstance(ts, datetime) and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)

        rows_to_insert.append([
            ts,
            int(pt["match_id"]),
            player_a_name,
            player_b_name,
            pt["set_num"],
            pt["game_num"],
            pt["point_num"],
            pt["home_point"],
            pt["away_point"],
            pt["server"],
            pt["point_winner"],
            bool(pt["is_ace"]),
            bool(pt["is_double_fault"]),
            model_p,
            bookie_p,
            edge,
            _clean(dom.get("D_A")),
            _clean(dom.get("D_B")),
            _clean(sa.get("nmi")),
            _clean(sb.get("nmi")),
            _clean(sa.get("sms")),
            _clean(sb.get("sms")),
            _clean(sa.get("rms")),
            _clean(sb.get("rms")),
            _clean(sa.get("pms")),
            _clean(sb.get("pms")),
            _clean(sa.get("gps")),
            _clean(sb.get("gps")),
            ms.get("sets_A"),
            ms.get("sets_B"),
            ms.get("games_A"),
            ms.get("games_B"),
            pt["ingestion_source"] or "backfill",
            pt["tournament_name"],
            pt["category"],
        ])

    conn.execute("BEGIN")
    try:
        conn.execute(
            "DELETE FROM live_processed.dashboard_log WHERE match_id = ?",
            [match_id],
        )
        for row in rows_to_insert:
            conn.execute(_INSERT_DASHBOARD, row)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return len(rows_to_insert)


# ── Programmatic entry point ───────────────────────────────────────────────────

def enrich_match_ids(
    match_ids: list[int],
    conn: duckdb.DuckDBPyConnection,
) -> int:
    """Import-and-call entry point: enrich specific match_ids without prompts.
    Returns total rows written to dashboard_log.
    """
    total = 0
    for match_id in match_ids:
        meta_row = conn.execute("""
            SELECT ANY_VALUE(player_a), ANY_VALUE(player_b)
            FROM live_raw.tennisapi_points WHERE match_id = ?
        """, [match_id]).fetchone()
        label = (
            f"{meta_row[0]} vs {meta_row[1]}" if meta_row else str(match_id)
        )
        print(f"  [{match_id}] {label}")
        try:
            n = _enrich_match(conn, match_id)
            total += n
            print(f"    ✓  {n} rows written to live_processed.dashboard_log")
        except Exception as exc:
            print(f"    ✗  Error: {exc}")
    return total


# ── Prompt ────────────────────────────────────────────────────────────────────

def _prompt_match_ids(conn: duckdb.DuckDBPyConnection) -> list[int]:
    matches = _list_matches(conn)
    if not matches:
        print("  No matches found in live_raw.tennisapi_points.")
        raise SystemExit(1)

    print("\n  Available matches:")
    print(
        f"  {'MATCH ID':<12}  {'Pts':>4}  {'Polls':>5}  {'Enriched':>8}  Matchup"
    )
    print(f"  {'─'*10}  {'─'*4}  {'─'*5}  {'─'*8}  {'─'*40}")
    for m in matches:
        enriched_flag = " ✓" if m["already_enriched"] else ""
        odds_flag     = "  (no odds)" if m["num_polls"] == 0 else ""
        matchup = f"{m['player_a'] or '?'} vs {m['player_b'] or '?'}"
        print(
            f"  {m['match_id']:<12}  {m['num_points']:>4}  {m['num_polls']:>5}"
            f"  {m['already_enriched']:>8}{enriched_flag}  {matchup}{odds_flag}"
        )
    print()

    while True:
        raw = input("Enter match_id (or 'all'): ").strip().lower()
        if raw == "all":
            return [m["match_id"] for m in matches]
        if raw.isdigit():
            mid = int(raw)
            if any(m["match_id"] == mid for m in matches):
                return [mid]
            print(f"  match_id {mid} not found in the list.")
        else:
            print("  Please enter a valid match_id or 'all'.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(_sep("═"))
    print("  Enrich dashboard_log from raw tables")
    print(_sep("═"))

    conn = duckdb.connect(str(_DB_PATH))
    _ensure_tables(conn)

    try:
        match_ids = _prompt_match_ids(conn)
        print()

        total_rows = enrich_match_ids(match_ids, conn)

        print()
        print(_sep("═"))
        print(f"  Done.  Total rows enriched: {total_rows}")
        print(f"  Matches are now visible in the dashboard History tab.")
        print(_sep("═"))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
