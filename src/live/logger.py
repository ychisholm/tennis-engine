from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "processed" / "tennis.duckdb"

_SETUP_STMTS = [
    "CREATE SCHEMA IF NOT EXISTS live_raw",
    "CREATE SCHEMA IF NOT EXISTS live_processed",
    """
    CREATE TABLE IF NOT EXISTS live_raw.tennisapi_points (
        ts               TIMESTAMP,
        match_id         INTEGER,
        player_a         VARCHAR,
        player_b         VARCHAR,
        point_num        INTEGER,
        set_num          INTEGER,
        game_num         INTEGER,
        home_point       VARCHAR,
        away_point       VARCHAR,
        server           VARCHAR,
        point_winner     VARCHAR,
        is_ace           BOOLEAN,
        is_double_fault  BOOLEAN,
        ingestion_source VARCHAR,
        tournament_name  VARCHAR,
        category         VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_raw.oddsapi_polls (
        ts                    TIMESTAMP,
        match_id              INTEGER,
        player_a              VARCHAR,
        player_b              VARCHAR,
        bookmaker_prob_a      FLOAT,
        num_bookmakers        INTEGER,
        api_credits_remaining INTEGER
    )
    """,
    """
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
    """,
]

_INSERT_RAW_POINT = """
INSERT INTO live_raw.tennisapi_points (
    ts, match_id, player_a, player_b,
    point_num, set_num, game_num,
    home_point, away_point, server, point_winner, is_ace, is_double_fault,
    ingestion_source, tournament_name, category
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_RAW_ODDS = """
INSERT INTO live_raw.oddsapi_polls (
    ts, match_id, player_a, player_b,
    bookmaker_prob_a, num_bookmakers, api_credits_remaining
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

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


def _clean(v: Any) -> Any:
    """Convert NaN/inf to None so DuckDB stores NULL."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


class MatchLogger:
    """
    Writes live data into the Medallion-style DuckDB schemas:
      live_raw.tennisapi_points  — immutable API payload ledger
      live_raw.oddsapi_polls     — immutable bookmaker poll ledger
      live_processed.dashboard_log — merged view for the dashboard
    """

    _LOCK_RETRY_DELAY = 3.0
    _LOCK_MAX_RETRIES = 20  # 60s total wait

    def __init__(self, db_path: str | Path | None = None) -> None:
        import time
        self._path = str(db_path or _DB_PATH)
        self._conn = self._open_conn()
        for stmt in _SETUP_STMTS:
            self._conn.execute(stmt)

    def _open_conn(self) -> duckdb.DuckDBPyConnection:
        import time
        for attempt in range(self._LOCK_MAX_RETRIES):
            try:
                return duckdb.connect(self._path)
            except duckdb.IOException as exc:
                if "Conflicting lock" not in str(exc):
                    raise
                if attempt == 0:
                    print(
                        "\n[logger] DuckDB is locked by another process "
                        "(TablePlus or another terminal).\n"
                        "         Close that connection — this will auto-resume.\n"
                    )
                print(f"         Waiting... ({attempt + 1}/{self._LOCK_MAX_RETRIES})", end="\r", flush=True)
                time.sleep(self._LOCK_RETRY_DELAY)
        raise RuntimeError(
            f"Could not acquire DuckDB lock after "
            f"{self._LOCK_MAX_RETRIES * self._LOCK_RETRY_DELAY:.0f}s. "
            "Close TablePlus or any other process with the DB open."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_raw_point(
        self,
        match_id: int | str,
        player_a: str,
        player_b: str,
        point_dict: dict,
        point_num: int = 0,
        tournament_name: str | None = None,
        category: str | None = None,
        ingestion_source: str = "live",
    ) -> None:
        # Accept both live-feed keys (set_number/game_number/home_point_score)
        # and backfill keys (set_num/game_num/home_point).
        self._conn.execute(_INSERT_RAW_POINT, [
            datetime.now(timezone.utc).replace(tzinfo=None),
            int(match_id),
            player_a,
            player_b,
            point_num,
            point_dict.get("set_number") or point_dict.get("set_num"),
            point_dict.get("game_number") or point_dict.get("game_num"),
            point_dict.get("home_point_score") or point_dict.get("home_point"),
            point_dict.get("away_point_score") or point_dict.get("away_point"),
            point_dict.get("server"),
            point_dict.get("point_winner"),
            bool(point_dict.get("is_ace", False)),
            bool(point_dict.get("is_double_fault", False)),
            ingestion_source,
            tournament_name,
            category,
        ])

    def log_raw_odds(
        self,
        match_id: int | str,
        player_a: str,
        player_b: str,
        odds_result: dict,
    ) -> None:
        self._conn.execute(_INSERT_RAW_ODDS, [
            datetime.now(timezone.utc).replace(tzinfo=None),
            int(match_id),
            player_a,
            player_b,
            _clean(odds_result.get("bookmaker_implied_prob")),
            odds_result.get("num_bookmakers"),
            odds_result.get("api_credits_remaining"),
        ])

    def log_processed_state(
        self,
        match_id: int | str,
        player_a: str,
        player_b: str,
        point_dict: dict,
        prob_output: dict,
        last_odds: dict | None,
        point_num: int = 0,
        tournament_name: str | None = None,
        category: str | None = None,
        ingestion_source: str = "live",
    ) -> None:
        ms    = prob_output["match_state"]
        probs = prob_output["probabilities"]
        dom   = prob_output["dominance"]
        sa    = dom["breakdown_A"]
        sb    = dom["breakdown_B"]

        model_p  = _clean(probs.get("P_match_A"))
        bookie_p = _clean(last_odds["home_implied_prob"]) if last_odds else None
        edge     = (
            _clean(round(model_p - bookie_p, 6))
            if (model_p is not None and bookie_p is not None)
            else None
        )

        self._conn.execute(_INSERT_DASHBOARD, [
            datetime.now(timezone.utc).replace(tzinfo=None),
            int(match_id),
            player_a,
            player_b,
            point_dict.get("set_number", ms.get("set_number")),
            point_dict.get("game_number", ms.get("game_number")),
            point_num,
            point_dict.get("home_point_score"),
            point_dict.get("away_point_score"),
            point_dict.get("server"),
            point_dict.get("point_winner"),
            bool(point_dict.get("is_ace", False)),
            bool(point_dict.get("is_double_fault", False)),
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
            ingestion_source,
            tournament_name,
            category,
        ])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MatchLogger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
