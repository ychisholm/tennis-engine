from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

_log = logging.getLogger(__name__)

_SETUP_STMTS = [
    "CREATE SCHEMA IF NOT EXISTS live_raw",
    "CREATE SCHEMA IF NOT EXISTS live_processed",
    """
    CREATE TABLE IF NOT EXISTS live_raw.tennisapi_points (
        ts               TIMESTAMPTZ,
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
        ts                    TIMESTAMPTZ,
        match_id              INTEGER,
        player_a              VARCHAR,
        player_b              VARCHAR,
        bookmaker_prob_a      FLOAT,
        num_bookmakers        INTEGER,
        api_credits_remaining INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_raw.match_details (
        match_id              INTEGER,
        player_a              VARCHAR,
        player_b              VARCHAR,
        polled_at             TIMESTAMPTZ,
        status                VARCHAR,
        home_sets             INTEGER,
        away_sets             INTEGER,
        home_period1          INTEGER,
        away_period1          INTEGER,
        home_period2          INTEGER,
        away_period2          INTEGER,
        home_period3          INTEGER,
        away_period3          INTEGER,
        home_current_point    VARCHAR,
        away_current_point    VARCHAR,
        winner_code           INTEGER,
        tournament_name       VARCHAR,
        category              VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_processed.points (
        match_id              INTEGER,
        player_a              VARCHAR,
        player_b              VARCHAR,
        set_num               INTEGER,
        game_num              INTEGER,
        point_num             INTEGER,
        home_point            VARCHAR,
        away_point            VARCHAR,
        server                VARCHAR,
        point_winner          VARCHAR,
        is_ace                BOOLEAN,
        is_double_fault       BOOLEAN,
        has_gap               BOOLEAN,
        home_sets_won         INTEGER,
        away_sets_won         INTEGER,
        home_games_won        INTEGER,
        away_games_won        INTEGER,
        tournament_name       VARCHAR,
        category              VARCHAR,
        last_updated          TIMESTAMPTZ,
        PRIMARY KEY (match_id, point_num)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_processed.dashboard_log (
        ts               TIMESTAMPTZ,
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
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_RAW_ODDS = """
INSERT INTO live_raw.oddsapi_polls (
    ts, match_id, player_a, player_b,
    bookmaker_prob_a, num_bookmakers, api_credits_remaining
) VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_MATCH_DETAIL = """
INSERT INTO live_raw.match_details (
    match_id, player_a, player_b, polled_at, status,
    home_sets, away_sets,
    home_period1, away_period1,
    home_period2, away_period2,
    home_period3, away_period3,
    home_current_point, away_current_point,
    winner_code, tournament_name, category
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_PROCESSED_POINT = """
INSERT INTO live_processed.points (
    match_id, player_a, player_b,
    set_num, game_num, point_num,
    home_point, away_point, server, point_winner,
    is_ace, is_double_fault, has_gap,
    home_sets_won, away_sets_won,
    home_games_won, away_games_won,
    tournament_name, category, last_updated
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (match_id, point_num) DO UPDATE SET
    last_updated   = EXCLUDED.last_updated,
    has_gap        = EXCLUDED.has_gap,
    home_sets_won  = EXCLUDED.home_sets_won,
    away_sets_won  = EXCLUDED.away_sets_won,
    home_games_won = EXCLUDED.home_games_won,
    away_games_won = EXCLUDED.away_games_won
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
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s
)
"""


def _clean(v: Any) -> Any:
    """Convert NaN/inf to None so Postgres stores NULL."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


class MatchLogger:
    """
    Writes live data into the Medallion-style PostgreSQL schemas:
      live_raw.tennisapi_points  — immutable API payload ledger
      live_raw.oddsapi_polls     — immutable bookmaker poll ledger
      live_processed.dashboard_log — merged view for the dashboard
    """

    def __init__(self, db_url: str | None = None) -> None:
        url = db_url or os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set."
            )
        self._conn = psycopg2.connect(url)
        self._conn.autocommit = False
        cur = self._conn.cursor()
        for stmt in _SETUP_STMTS:
            cur.execute(stmt)
        self._conn.commit()
        cur.close()

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
        cur = self._conn.cursor()
        cur.execute(_INSERT_RAW_POINT, [
            datetime.now(timezone.utc),
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
        self._conn.commit()
        cur.close()

    def log_raw_odds(
        self,
        match_id: int | str,
        player_a: str,
        player_b: str,
        odds_result: dict,
    ) -> None:
        cur = self._conn.cursor()
        cur.execute(_INSERT_RAW_ODDS, [
            datetime.now(timezone.utc),
            int(match_id),
            player_a,
            player_b,
            _clean(odds_result.get("bookmaker_implied_prob")),
            odds_result.get("num_bookmakers"),
            odds_result.get("api_credits_remaining"),
        ])
        self._conn.commit()
        cur.close()

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

        cur = self._conn.cursor()
        cur.execute(_INSERT_DASHBOARD, [
            datetime.now(timezone.utc),
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
        self._conn.commit()
        cur.close()

    def log_match_detail(
        self,
        parsed_detail: dict,
        polled_at: datetime | None = None,
    ) -> None:
        """Insert one row into live_raw.match_details from a parsed detail dict."""
        ts = polled_at or datetime.now(timezone.utc)
        cur = self._conn.cursor()
        try:
            cur.execute(_INSERT_MATCH_DETAIL, [
                int(parsed_detail["match_id"]),
                parsed_detail.get("player_a"),
                parsed_detail.get("player_b"),
                ts,
                parsed_detail.get("status"),
                parsed_detail.get("home_sets"),
                parsed_detail.get("away_sets"),
                parsed_detail.get("home_period1"),
                parsed_detail.get("away_period1"),
                parsed_detail.get("home_period2"),
                parsed_detail.get("away_period2"),
                parsed_detail.get("home_period3"),
                parsed_detail.get("away_period3"),
                parsed_detail.get("home_current_point"),
                parsed_detail.get("away_current_point"),
                parsed_detail.get("winner_code"),
                parsed_detail.get("tournament_name"),
                parsed_detail.get("category"),
            ])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    @staticmethod
    def _is_terminal_score(
        home_pt: Any,
        away_pt: Any,
        server: str | None,
        point_winner: str | None,
    ) -> bool:
        """Return True if the (home, away) score with given server/winner ends a game.

        Standard game terminal scores (server-receiver perspective):
            server wins:   40-0, 40-15, 40-30, AD-40
            receiver wins: 0-40, 15-40, 30-40, 40-AD
        Tiebreak terminal: either side >= 7 with 2+ point lead.
        """
        if home_pt is None or away_pt is None:
            return False
        h, a = str(home_pt), str(away_pt)

        std_scores = {"0", "15", "30", "40", "AD"}
        is_tiebreak = h not in std_scores or a not in std_scores
        if is_tiebreak:
            try:
                h_int, a_int = int(h), int(a)
            except (ValueError, TypeError):
                return False
            return max(h_int, a_int) >= 7 and abs(h_int - a_int) >= 2

        if server == "home":
            s, r = h, a
        elif server == "away":
            s, r = a, h
        else:
            return False

        server_terminal = {("40", "0"), ("40", "15"), ("40", "30"), ("AD", "40")}
        receiver_terminal = {("0", "40"), ("15", "40"), ("30", "40"), ("40", "AD")}

        if (s, r) in server_terminal:
            return point_winner == server
        if (s, r) in receiver_terminal:
            return point_winner is not None and point_winner != server
        return False

    def upsert_processed_points(
        self,
        match_id: int | str,
        player_a: str,
        player_b: str,
        match_detail: dict,
    ) -> None:
        """Read raw points for a match, walk them with running score counters,
        detect gaps (game endings without a terminal score), and upsert into
        live_processed.points.
        """
        match_id_int = int(match_id)
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                SELECT point_num, set_num, game_num, home_point, away_point,
                       server, point_winner, is_ace, is_double_fault,
                       tournament_name, category
                FROM live_raw.tennisapi_points
                WHERE match_id = %s
                ORDER BY point_num, ts
                """,
                [match_id_int],
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        # Step 1: dedupe by point_num, keep first occurrence
        seen: set[int] = set()
        points: list[dict] = []
        for row in rows:
            point_num = row[0]
            if point_num is None or point_num in seen:
                continue
            seen.add(point_num)
            points.append({
                "point_num": point_num,
                "set_num": row[1],
                "game_num": row[2],
                "home_point": row[3],
                "away_point": row[4],
                "server": row[5],
                "point_winner": row[6],
                "is_ace": row[7],
                "is_double_fault": row[8],
                "tournament_name": row[9],
                "category": row[10],
            })

        if not points:
            return

        # Step 2: group consecutive points by (set_num, game_num)
        groups: list[tuple[tuple[Any, Any], list[dict]]] = []
        for pt in points:
            key = (pt["set_num"], pt["game_num"])
            if groups and groups[-1][0] == key:
                groups[-1][1].append(pt)
            else:
                groups.append((key, [pt]))

        period_h = [match_detail.get(f"home_period{i}") for i in (1, 2, 3)]
        period_a = [match_detail.get(f"away_period{i}") for i in (1, 2, 3)]

        home_sets = away_sets = 0
        home_games = away_games = 0
        current_set: Any = None

        processed: list[dict] = []
        last_idx = len(groups) - 1

        for idx, ((set_n, _game_n), game_pts) in enumerate(groups):
            # Set boundary: previous set is over when we enter a new one
            if current_set is None:
                current_set = set_n
            elif set_n != current_set:
                if home_games > away_games:
                    home_sets += 1
                elif away_games > home_games:
                    away_sets += 1
                home_games = away_games = 0
                current_set = set_n

            is_complete = idx < last_idx
            last_pt = game_pts[-1]
            has_gap = False

            if is_complete:
                terminal = self._is_terminal_score(
                    last_pt["home_point"],
                    last_pt["away_point"],
                    last_pt["server"],
                    last_pt["point_winner"],
                )
                has_gap = not terminal

                if terminal:
                    game_winner = last_pt["point_winner"]
                else:
                    game_winner = None
                    period_idx = (set_n - 1) if isinstance(set_n, int) else -1
                    if 0 <= period_idx < 3:
                        exp_h = period_h[period_idx] or 0
                        exp_a = period_a[period_idx] or 0
                        if exp_h > home_games:
                            game_winner = "home"
                        elif exp_a > away_games:
                            game_winner = "away"
                    if game_winner is None:
                        game_winner = last_pt["point_winner"]

                if game_winner == "home":
                    home_games += 1
                elif game_winner == "away":
                    away_games += 1

            for pt in game_pts:
                processed.append({
                    **pt,
                    "has_gap": has_gap,
                    "home_sets_won": home_sets,
                    "away_sets_won": away_sets,
                    "home_games_won": home_games,
                    "away_games_won": away_games,
                })

        now = datetime.now(timezone.utc)
        cur = self._conn.cursor()
        try:
            for pt in processed:
                cur.execute(_INSERT_PROCESSED_POINT, [
                    match_id_int, player_a, player_b,
                    pt["set_num"], pt["game_num"], pt["point_num"],
                    pt["home_point"], pt["away_point"],
                    pt["server"], pt["point_winner"],
                    bool(pt["is_ace"]) if pt["is_ace"] is not None else False,
                    bool(pt["is_double_fault"]) if pt["is_double_fault"] is not None else False,
                    bool(pt["has_gap"]),
                    pt["home_sets_won"], pt["away_sets_won"],
                    pt["home_games_won"], pt["away_games_won"],
                    pt["tournament_name"], pt["category"], now,
                ])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MatchLogger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
