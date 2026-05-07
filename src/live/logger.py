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
        point_num             NUMERIC,
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
        source                VARCHAR,
        tournament_name       VARCHAR,
        category              VARCHAR,
        last_updated          TIMESTAMPTZ,
        PRIMARY KEY (match_id, point_num)
    )
    """,
    # Idempotent migrations for existing deployments.
    "ALTER TABLE live_processed.points ADD COLUMN IF NOT EXISTS source VARCHAR",
    "ALTER TABLE live_processed.points ALTER COLUMN point_num TYPE NUMERIC USING point_num::NUMERIC",
    """
    CREATE TABLE IF NOT EXISTS live_processed.match_detail_points (
        match_id              INTEGER,
        player_a              VARCHAR,
        player_b              VARCHAR,
        polled_at             TIMESTAMPTZ,
        status                VARCHAR,
        home_sets_won         INTEGER,
        away_sets_won         INTEGER,
        home_set1_games       INTEGER,
        away_set1_games       INTEGER,
        home_set2_games       INTEGER,
        away_set2_games       INTEGER,
        home_set3_games       INTEGER,
        away_set3_games       INTEGER,
        home_current_games    INTEGER,
        away_current_games    INTEGER,
        home_current_point    VARCHAR,
        away_current_point    VARCHAR,
        point_winner          VARCHAR,
        winner_code           INTEGER,
        tournament_name       VARCHAR,
        category              VARCHAR,
        PRIMARY KEY (match_id, polled_at)
    )
    """,
    # Migrate existing deployments off the old score-keyed PK. The score tuple
    # alone collapses legitimate deuce oscillation (40-40 ↔ AD-40 ↔ 40-AD) into
    # a single row; (match_id, polled_at) is row-unique without that loss.
    "ALTER TABLE live_processed.match_detail_points DROP CONSTRAINT IF EXISTS match_detail_points_pkey",
    "ALTER TABLE live_processed.match_detail_points ADD CONSTRAINT match_detail_points_pkey PRIMARY KEY (match_id, polled_at)",
    """
    CREATE INDEX IF NOT EXISTS match_detail_points_score_idx
      ON live_processed.match_detail_points
      (match_id, home_sets_won, away_sets_won,
       home_current_games, away_current_games,
       home_current_point, away_current_point)
    """,
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS point_winner VARCHAR",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS home_sets_won INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS away_sets_won INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS home_set1_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS away_set1_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS home_set2_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS away_set2_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS home_set3_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS away_set3_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS home_current_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS away_current_games INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS status VARCHAR",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS winner_code INTEGER",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS country_a VARCHAR",
    "ALTER TABLE live_processed.match_detail_points ADD COLUMN IF NOT EXISTS country_b VARCHAR",
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
    """
    CREATE TABLE IF NOT EXISTS live_raw.api_call_log (
        id              BIGSERIAL PRIMARY KEY,
        timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        endpoint        TEXT NOT NULL,
        request_path    TEXT NOT NULL,
        request_params  JSONB,
        match_id        VARCHAR(100),
        http_status     INT,
        latency_ms      INT,
        response_summary JSONB,
        raw_response_id BIGINT,
        error           TEXT,
        poll_cycle_id   UUID
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_match_id ON live_raw.api_call_log(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_timestamp ON live_raw.api_call_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_endpoint ON live_raw.api_call_log(endpoint)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_poll_cycle ON live_raw.api_call_log(poll_cycle_id)",
    """
    CREATE TABLE IF NOT EXISTS live_raw.api_response_archive (
        id          BIGSERIAL PRIMARY KEY,
        timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        endpoint    TEXT NOT NULL,
        match_id    VARCHAR(100),
        raw_json    JSONB NOT NULL,
        byte_size   INT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_response_archive_match_id ON live_raw.api_response_archive(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_api_response_archive_timestamp ON live_raw.api_response_archive(timestamp)",
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
    source, tournament_name, category, last_updated
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (match_id, point_num) DO UPDATE SET
    set_num        = EXCLUDED.set_num,
    game_num       = EXCLUDED.game_num,
    home_point     = EXCLUDED.home_point,
    away_point     = EXCLUDED.away_point,
    server         = EXCLUDED.server,
    point_winner   = EXCLUDED.point_winner,
    is_ace         = EXCLUDED.is_ace,
    is_double_fault = EXCLUDED.is_double_fault,
    has_gap        = EXCLUDED.has_gap,
    home_sets_won  = EXCLUDED.home_sets_won,
    away_sets_won  = EXCLUDED.away_sets_won,
    home_games_won = EXCLUDED.home_games_won,
    away_games_won = EXCLUDED.away_games_won,
    source         = EXCLUDED.source,
    last_updated   = EXCLUDED.last_updated
"""

_UPSERT_MATCH_DETAIL_POINT = """
INSERT INTO live_processed.match_detail_points (
    match_id, player_a, player_b, polled_at, status,
    home_sets_won, away_sets_won,
    home_set1_games, away_set1_games,
    home_set2_games, away_set2_games,
    home_set3_games, away_set3_games,
    home_current_games, away_current_games,
    home_current_point, away_current_point,
    point_winner, winner_code, tournament_name, category,
    country_a, country_b
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (match_id, polled_at) DO NOTHING
"""

# "A" is what the API actually returns for advantage; "AD" kept for safety.
_SCORE_RANK_LOGGER: dict[str, int] = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4, "A": 4}


def _derive_point_winner(prev: dict, curr: dict) -> str | None:
    """Derive the winner of curr from the score-state delta vs prev.

    Same-game (sets and current_games unchanged): compare point ranks. A point
    won by side X either advances X's score or causes the opponent's AD to
    regress to 40 (deuce reset).

    Game boundary (current_games or sets changed): not used here — the new
    row's point score is 0-0 (or near it), so winner of that point is
    indeterminable from this row alone. Game-boundary winners are assigned
    retroactively via _retro_assign_prev_game_winner on the previous game's
    last row.
    """
    if prev is None:
        return None
    same_sets = (
        (prev.get("home_sets_won") or 0) == (curr.get("home_sets_won") or 0)
        and (prev.get("away_sets_won") or 0) == (curr.get("away_sets_won") or 0)
    )
    same_games = (
        (prev.get("home_current_games") or 0) == (curr.get("home_current_games") or 0)
        and (prev.get("away_current_games") or 0) == (curr.get("away_current_games") or 0)
    )
    if not (same_sets and same_games):
        return None

    ph = _SCORE_RANK_LOGGER.get(str(prev.get("home_current_point") or "0"), 0)
    pa = _SCORE_RANK_LOGGER.get(str(prev.get("away_current_point") or "0"), 0)
    ch = _SCORE_RANK_LOGGER.get(str(curr.get("home_current_point") or "0"), 0)
    ca = _SCORE_RANK_LOGGER.get(str(curr.get("away_current_point") or "0"), 0)

    if ch > ph or ca < pa:
        return "home"
    if ca > pa or ch < ph:
        return "away"
    return None


def _retro_winner_for_prev_game(prev: dict, curr: dict) -> str | None:
    """When curr begins a new game, infer who won prev's game from the games
    counters that incremented between prev and curr."""
    if prev is None:
        return None
    prev_h_sets = prev.get("home_sets_won") or 0
    prev_a_sets = prev.get("away_sets_won") or 0
    curr_h_sets = curr.get("home_sets_won") or 0
    curr_a_sets = curr.get("away_sets_won") or 0
    prev_h_g = prev.get("home_current_games") or 0
    prev_a_g = prev.get("away_current_games") or 0

    if prev_h_sets == curr_h_sets and prev_a_sets == curr_a_sets:
        curr_h_g = curr.get("home_current_games") or 0
        curr_a_g = curr.get("away_current_games") or 0
        if curr_h_g > prev_h_g:
            return "home"
        if curr_a_g > prev_a_g:
            return "away"
        return None

    # Set boundary: previous game finished its set. Read the completed set's
    # totals (carried in curr) and compare to prev's in-set games.
    prev_set = prev_h_sets + prev_a_sets + 1
    if prev_set < 1 or prev_set > 3:
        return None
    completed_h = curr.get(f"home_set{prev_set}_games") or 0
    completed_a = curr.get(f"away_set{prev_set}_games") or 0
    if completed_h > prev_h_g:
        return "home"
    if completed_a > prev_a_g:
        return "away"
    return None

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


_POINT_ORDER = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4}


def _score_sort_key(home_point: Any, away_point: Any) -> int:
    """Map a tennis point score to an ordinal for sorting within a game.

    Standard scores: 0,15,30,40,AD. Ordinal = sum of per-side ranks except
    deuce/AD states (40-40, AD-40, 40-AD) which all collapse to 6.
    Tiebreak scores (integers) are offset by +100 so they sort above any
    standard score and are ordered by total points played.
    """
    h = _POINT_ORDER.get(str(home_point))
    a = _POINT_ORDER.get(str(away_point))
    if h is None or a is None:
        try:
            return 100 + int(home_point) + int(away_point)
        except (ValueError, TypeError):
            return 0
    if h >= 3 and a >= 3:
        return 6
    return h + a


def _point_to_int(s: Any) -> int | None:
    if s is None:
        return None
    s_str = str(s)
    if s_str in _POINT_ORDER:
        return _POINT_ORDER[s_str]
    try:
        return int(s_str)
    except (ValueError, TypeError):
        return None


def _infer_point_winner(prev: dict, curr: dict) -> str | None:
    """Infer who won the point that took prev → curr from the score transition.

    Used for match_detail-sourced rows where point_winner is unknown. Compares
    home_games_won / away_games_won (game-boundary case) and home_point /
    away_point (within-game case, including deuce regression).
    """
    prev_h_g = prev.get("home_games_won") or 0
    prev_a_g = prev.get("away_games_won") or 0
    curr_h_g = curr.get("home_games_won") or 0
    curr_a_g = curr.get("away_games_won") or 0

    if curr_h_g > prev_h_g:
        return "home"
    if curr_a_g > prev_a_g:
        return "away"

    ph = _point_to_int(prev.get("home_point"))
    pa = _point_to_int(prev.get("away_point"))
    ch = _point_to_int(curr.get("home_point"))
    ca = _point_to_int(curr.get("away_point"))
    if any(x is None for x in (ph, pa, ch, ca)):
        return None
    dh = ch - ph
    da = ca - pa
    # Standard advance OR opponent regression from AD → 40 (deuce return).
    if dh > 0 or da < 0:
        return "home"
    if da > 0 or dh < 0:
        return "away"
    return None


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

    def upsert_match_detail_points(
        self,
        parsed_detail: dict,
        polled_at: datetime,
    ) -> None:
        """Insert one score-state snapshot into live_processed.match_detail_points.

        Filters out spurious "0-0" rows that the API briefly emits between
        points (when a non-0-0 score already exists at this game state, a
        later 0-0 is treated as bogus and skipped). Derives point_winner
        from the previous row for this match, and retroactively assigns the
        winner of the previous game's last row when a game boundary is
        detected.
        """
        match_id = parsed_detail.get("match_id")
        try:
            match_id_int = int(match_id) if match_id is not None else None
        except (ValueError, TypeError):
            match_id_int = None
        if match_id_int is None:
            return

        home_pt = parsed_detail.get("home_current_point")
        away_pt = parsed_detail.get("away_current_point")
        if home_pt is None or away_pt is None:
            return

        home_sets = parsed_detail.get("home_sets") or 0
        away_sets = parsed_detail.get("away_sets") or 0
        current_set = home_sets + away_sets + 1
        if current_set < 1 or current_set > 5:
            return

        home_current_games = parsed_detail.get(f"home_period{current_set}") or 0
        away_current_games = parsed_detail.get(f"away_period{current_set}") or 0
        home_pt_s = str(home_pt)
        away_pt_s = str(away_pt)

        cur = self._conn.cursor()
        try:
            # Skip bogus 0-0: a 0-0 row at this (sets, games) state is only
            # legitimate if no other (non 0-0) row already exists there.
            if home_pt_s == "0" and away_pt_s == "0":
                cur.execute(
                    """
                    SELECT 1 FROM live_processed.match_detail_points
                    WHERE match_id = %s
                      AND home_sets_won = %s AND away_sets_won = %s
                      AND home_current_games = %s AND away_current_games = %s
                      AND NOT (home_current_point = '0' AND away_current_point = '0')
                    LIMIT 1
                    """,
                    [match_id_int, home_sets, away_sets,
                     home_current_games, away_current_games],
                )
                if cur.fetchone():
                    return

            # Skip idempotent repolls: if the most recent row for this match
            # has the identical score state, the 15s poller is just observing
            # an unchanged game. Compare against the *most recent* row only —
            # a match against an earlier row (e.g. deuce returning after AD)
            # is a real new point and must be inserted.
            cur.execute(
                """
                SELECT home_sets_won, away_sets_won,
                       home_current_games, away_current_games,
                       home_current_point, away_current_point
                FROM live_processed.match_detail_points
                WHERE match_id = %s
                ORDER BY polled_at DESC
                LIMIT 1
                """,
                [match_id_int],
            )
            latest = cur.fetchone()
            if latest and (
                latest[0] == home_sets
                and latest[1] == away_sets
                and latest[2] == home_current_games
                and latest[3] == away_current_games
                and latest[4] == home_pt_s
                and latest[5] == away_pt_s
            ):
                return

            curr_state = {
                "home_sets_won": home_sets,
                "away_sets_won": away_sets,
                "home_current_games": home_current_games,
                "away_current_games": away_current_games,
                "home_current_point": home_pt_s,
                "away_current_point": away_pt_s,
                "home_set1_games": parsed_detail.get("home_period1") or 0,
                "away_set1_games": parsed_detail.get("away_period1") or 0,
                "home_set2_games": parsed_detail.get("home_period2") or 0,
                "away_set2_games": parsed_detail.get("away_period2") or 0,
                "home_set3_games": parsed_detail.get("home_period3") or 0,
                "away_set3_games": parsed_detail.get("away_period3") or 0,
            }

            # Look up the most recent row for this match (excluding any row
            # that would be the same PK as this one, since that's an idempotent
            # repoll, not a transition).
            cur.execute(
                """
                SELECT home_sets_won, away_sets_won,
                       home_current_games, away_current_games,
                       home_current_point, away_current_point,
                       home_set1_games, away_set1_games,
                       home_set2_games, away_set2_games,
                       home_set3_games, away_set3_games
                FROM live_processed.match_detail_points
                WHERE match_id = %s
                  AND NOT (
                    home_sets_won = %s AND away_sets_won = %s
                    AND home_current_games = %s AND away_current_games = %s
                    AND home_current_point = %s AND away_current_point = %s
                  )
                ORDER BY polled_at DESC
                LIMIT 1
                """,
                [match_id_int,
                 home_sets, away_sets,
                 home_current_games, away_current_games,
                 home_pt_s, away_pt_s],
            )
            prev_row = cur.fetchone()
            prev: dict | None = None
            if prev_row:
                prev = {
                    "home_sets_won": prev_row[0],
                    "away_sets_won": prev_row[1],
                    "home_current_games": prev_row[2],
                    "away_current_games": prev_row[3],
                    "home_current_point": prev_row[4],
                    "away_current_point": prev_row[5],
                    "home_set1_games": prev_row[6],
                    "away_set1_games": prev_row[7],
                    "home_set2_games": prev_row[8],
                    "away_set2_games": prev_row[9],
                    "home_set3_games": prev_row[10],
                    "away_set3_games": prev_row[11],
                }

            # Intra-game delta first; fall back to games-count delta when this
            # row begins a new game (the 0-0 starter then represents the
            # game-winning point that polling missed). Stats counts depend on
            # this — without the fallback, every game's deciding point goes
            # unattributed.
            point_winner = (
                _derive_point_winner(prev, curr_state)
                or _retro_winner_for_prev_game(prev, curr_state)
            )

            cur.execute(_UPSERT_MATCH_DETAIL_POINT, [
                match_id_int,
                parsed_detail.get("player_a"),
                parsed_detail.get("player_b"),
                polled_at,
                parsed_detail.get("status"),
                home_sets,
                away_sets,
                curr_state["home_set1_games"],
                curr_state["away_set1_games"],
                curr_state["home_set2_games"],
                curr_state["away_set2_games"],
                curr_state["home_set3_games"],
                curr_state["away_set3_games"],
                home_current_games,
                away_current_games,
                home_pt_s,
                away_pt_s,
                point_winner,
                parsed_detail.get("winner_code"),
                parsed_detail.get("tournament_name"),
                parsed_detail.get("category"),
                parsed_detail.get("country_a"),
                parsed_detail.get("country_b"),
            ])

            # Retroactive: when this row begins a new game, the previous row
            # was the last point of the prior game. Assign its winner from
                # which side's games count incremented.
            retro = _retro_winner_for_prev_game(prev, curr_state)
            if retro and prev is not None:
                # Only fill in when intra-game derivation couldn't compute one
                # (e.g. the only captured row of a game was its 0-0 starter).
                # Never overwrite an existing winner: the score-delta between
                # two captured rows is point-accurate, while the games-count
                # delta only tells us who won the game, not who won that
                # specific bubble. The dashboard infers game winner from
                # score position via inferGameWinner, so we don't need to
                # mislabel mid-game points to drive game-level display.
                cur.execute(
                    """
                    UPDATE live_processed.match_detail_points
                    SET point_winner = %s
                    WHERE match_id = %s
                      AND home_sets_won = %s AND away_sets_won = %s
                      AND home_current_games = %s AND away_current_games = %s
                      AND home_current_point = %s AND away_current_point = %s
                      AND point_winner IS NULL
                    """,
                    [retro, match_id_int,
                     prev["home_sets_won"], prev["away_sets_won"],
                     prev["home_current_games"], prev["away_current_games"],
                     prev["home_current_point"], prev["away_current_point"]],
                )

            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            _log.warning(
                "upsert_match_detail_points failed for match_id=%s: %s",
                match_id_int, exc,
            )
        finally:
            cur.close()

    def upsert_processed_points(
        self,
        match_id: int | str,
        player_a: str,
        player_b: str,
        match_detail: dict,
    ) -> None:
        """Read raw points for a match, walk them with running score counters,
        detect gaps, fill from live_processed.match_detail_points where
        available, and upsert into live_processed.points.

        Two gap classes are detected:
          - Game-boundary gap: the last point of a completed game is not a
            terminal score (we missed the deciding point).
          - Mid-game gap: two consecutive points within the same game jump by
            more than one position in score progression order.

        Filled rows carry source='match_details' and has_gap=TRUE. When fill
        is unavailable for a detected gap, has_gap=TRUE is set on the
        surrounding point_by_point rows and a warning is logged.

        Deuce cycles: match_detail_points is keyed on (match_id, polled_at),
        so 40-40 ↔ AD-40 ↔ 40-AD oscillations are retained as distinct rows
        and gap-fill works through them. The remaining limitation is the 15s
        poll cadence: deuce points decided faster than the poll interval can
        still go unobserved, but that's a sampling limit, not a dedup loss.
        """
        match_id_int = int(match_id)
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                SELECT point_num, set_num, game_num, home_point, away_point,
                       server, point_winner, is_ace, is_double_fault,
                       tournament_name, category, ts
                FROM live_raw.tennisapi_points
                WHERE match_id = %s
                ORDER BY point_num, ts DESC
                """,
                [match_id_int],
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        # Step 1: dedupe raw rows by point_num, preserving order.
        seen: set = set()
        pbp_points: list[dict] = []
        for row in rows:
            point_num = row[0]
            if point_num is None or point_num in seen:
                continue
            seen.add(point_num)
            pbp_points.append({
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
                "ts": row[11],
                "source": "point_by_point",
                "has_gap_no_fill": False,
            })

        if not pbp_points:
            return

        pbp_points.sort(key=lambda p: float(p["point_num"]))

        # Step 2: load score states already in live_processed.points so that
        # gap-fill candidates already represented are not re-inserted.
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                SELECT set_num, home_games_won, away_games_won,
                       home_point, away_point
                FROM live_processed.points
                WHERE match_id = %s
                """,
                [match_id_int],
            )
            existing_states: set = {tuple(r) for r in cur.fetchall()}
        finally:
            cur.close()

        # Step 3: detect gaps between consecutive raw points.
        gaps: list[tuple[int, int]] = []
        for i in range(1, len(pbp_points)):
            prev = pbp_points[i - 1]
            curr = pbp_points[i]
            if curr["game_num"] != prev["game_num"]:
                terminal = self._is_terminal_score(
                    prev["home_point"], prev["away_point"],
                    prev["server"], prev["point_winner"],
                )
                if not terminal:
                    gaps.append((i - 1, i))
            else:
                prev_key = _score_sort_key(prev["home_point"], prev["away_point"])
                curr_key = _score_sort_key(curr["home_point"], curr["away_point"])
                if curr_key > prev_key + 1:
                    gaps.append((i - 1, i))

        # Step 4: for each gap, query match_detail_points and assemble fills.
        fill_rows: list[dict] = []
        for prev_idx, curr_idx in gaps:
            prev = pbp_points[prev_idx]
            curr = pbp_points[curr_idx]
            window_start = prev.get("ts")
            window_end = curr.get("ts")

            md_rows: list = []
            cur = self._conn.cursor()
            try:
                # Query by score-state range rather than ingestion timestamps.
                # Timestamp-windowing fails for live matches because consecutive
                # pbp points ingested in the same poll cycle are milliseconds
                # apart — no 15s match_detail snapshot can land in that window.
                # Instead, fetch all detail snapshots whose (set, games) state
                # falls between the two gap boundary points, then filter out
                # boundary scores in the loop below.
                prev_set = prev.get("set_num") or 0
                curr_set = curr.get("set_num") or 0
                cur.execute(
                    """
                    SELECT set_num, home_point, away_point,
                           home_sets, away_sets, home_games, away_games,
                           polled_at, tournament_name, category
                    FROM live_processed.match_detail_points
                    WHERE match_id = %s
                      AND set_num BETWEEN %s AND %s
                    ORDER BY polled_at
                    """,
                    [match_id_int, prev_set, curr_set],
                )
                md_rows = cur.fetchall()
            except Exception as exc:
                self._conn.rollback()
                _log.warning(
                    "upsert_processed_points: match_detail_points query "
                    "failed for match_id=%s: %s",
                    match_id_int, exc,
                )
                md_rows = []
            finally:
                cur.close()

            md_rows_sorted = sorted(
                md_rows,
                key=lambda r: (
                    r[0] or 0,
                    (r[5] or 0) + (r[6] or 0),
                    _score_sort_key(r[1], r[2]),
                ),
            )

            boundary_scores = {
                (prev["set_num"], prev["home_point"], prev["away_point"]),
                (curr["set_num"], curr["home_point"], curr["away_point"]),
            }

            this_gap_fills: list[dict] = []
            for md in md_rows_sorted:
                md_set, md_h_pt, md_a_pt = md[0], md[1], md[2]
                md_h_sets, md_a_sets = md[3] or 0, md[4] or 0
                md_h_games, md_a_games = md[5] or 0, md[6] or 0
                md_polled_at = md[7]
                md_tournament, md_category = md[8], md[9]

                if (md_set, md_h_pt, md_a_pt) in boundary_scores:
                    continue
                state_key = (md_set, md_h_games, md_a_games, md_h_pt, md_a_pt)
                if state_key in existing_states:
                    continue
                existing_states.add(state_key)

                this_gap_fills.append({
                    "set_num": md_set,
                    "game_num": md_h_games + md_a_games + 1,
                    "home_point": md_h_pt,
                    "away_point": md_a_pt,
                    "server": None,
                    "point_winner": None,
                    "is_ace": False,
                    "is_double_fault": False,
                    "tournament_name": md_tournament or prev.get("tournament_name"),
                    "category": md_category or prev.get("category"),
                    "ts": md_polled_at,
                    "source": "match_details",
                    "md_home_sets": md_h_sets,
                    "md_away_sets": md_a_sets,
                    "md_home_games": md_h_games,
                    "md_away_games": md_a_games,
                    "has_gap_no_fill": False,
                })

            if not this_gap_fills:
                prev["has_gap_no_fill"] = True
                curr["has_gap_no_fill"] = True
                _log.warning(
                    "upsert_processed_points: no match_detail_points fill "
                    "available for match_id=%s, gap between "
                    "(set=%s, game=%s, score=%s-%s, point_num=%s) and "
                    "(set=%s, game=%s, score=%s-%s, point_num=%s)",
                    match_id_int,
                    prev["set_num"], prev["game_num"],
                    prev["home_point"], prev["away_point"], prev["point_num"],
                    curr["set_num"], curr["game_num"],
                    curr["home_point"], curr["away_point"], curr["point_num"],
                )
                continue

            # Assign fractional point_nums between prev and curr so the fills
            # sort into position without renumbering existing rows.
            base = float(prev["point_num"])
            ceiling = float(curr["point_num"])
            step = min(0.001, max(1e-9, (ceiling - base) / (len(this_gap_fills) + 1)))
            for j, fr in enumerate(this_gap_fills):
                fr["point_num"] = base + (j + 1) * step
            fill_rows.extend(this_gap_fills)

        # Step 5: merge pbp + fills, group by (set_num, game_num) consecutively.
        merged = pbp_points + fill_rows
        merged.sort(key=lambda p: float(p["point_num"]))

        groups: list[tuple[tuple[Any, Any], list[dict]]] = []
        for pt in merged:
            key = (pt["set_num"], pt["game_num"])
            if groups and groups[-1][0] == key:
                groups[-1][1].append(pt)
            else:
                groups.append((key, [pt]))

        period_h = [match_detail.get(f"home_period{i}") for i in (1, 2, 3)]
        period_a = [match_detail.get(f"away_period{i}") for i in (1, 2, 3)]

        # Step 6: walk groups, compute running score, output rows BEFORE
        # incrementing the per-game counters so each row reflects the score
        # in effect at the start of its game.
        home_sets = away_sets = 0
        home_games = away_games = 0
        current_set: Any = None
        last_idx = len(groups) - 1
        processed: list[dict] = []

        for idx, ((set_n, _game_n), game_pts) in enumerate(groups):
            if current_set is None:
                current_set = set_n
            elif set_n != current_set:
                if home_games > away_games:
                    home_sets += 1
                elif away_games > home_games:
                    away_sets += 1
                home_games = away_games = 0
                current_set = set_n

            # Sync running counters from the first md row in this group, if
            # present — its values are authoritative for the pre-game state.
            md_in_group = next(
                (p for p in game_pts if p.get("source") == "match_details"),
                None,
            )
            if md_in_group is not None:
                if md_in_group.get("md_home_sets") is not None:
                    home_sets = md_in_group["md_home_sets"]
                if md_in_group.get("md_away_sets") is not None:
                    away_sets = md_in_group["md_away_sets"]
                if md_in_group.get("md_home_games") is not None:
                    home_games = md_in_group["md_home_games"]
                if md_in_group.get("md_away_games") is not None:
                    away_games = md_in_group["md_away_games"]

            # Output rows with current (pre-increment) counters.
            for pt in game_pts:
                if pt.get("source") == "match_details":
                    row_h_sets = pt.get("md_home_sets") or 0
                    row_a_sets = pt.get("md_away_sets") or 0
                    row_h_games = pt.get("md_home_games") or 0
                    row_a_games = pt.get("md_away_games") or 0
                    row_has_gap = True
                else:
                    row_h_sets = home_sets
                    row_a_sets = away_sets
                    row_h_games = home_games
                    row_a_games = away_games
                    row_has_gap = bool(pt.get("has_gap_no_fill"))

                processed.append({
                    **pt,
                    "has_gap": row_has_gap,
                    "home_sets_won": row_h_sets,
                    "away_sets_won": row_a_sets,
                    "home_games_won": row_h_games,
                    "away_games_won": row_a_games,
                })

            # Increment counters for completed games.
            is_complete = idx < last_idx
            if is_complete:
                last_pt = game_pts[-1]
                game_winner: str | None = None
                if last_pt.get("source") == "point_by_point":
                    terminal = self._is_terminal_score(
                        last_pt["home_point"], last_pt["away_point"],
                        last_pt["server"], last_pt["point_winner"],
                    )
                    if terminal:
                        game_winner = last_pt.get("point_winner")
                if game_winner is None:
                    period_idx = (set_n - 1) if isinstance(set_n, int) else -1
                    if 0 <= period_idx < 3:
                        exp_h = period_h[period_idx] or 0
                        exp_a = period_a[period_idx] or 0
                        if exp_h > home_games:
                            game_winner = "home"
                        elif exp_a > away_games:
                            game_winner = "away"
                if game_winner is None and idx + 1 <= last_idx:
                    next_md = next(
                        (
                            p for p in groups[idx + 1][1]
                            if p.get("source") == "match_details"
                        ),
                        None,
                    )
                    if next_md is not None and next_md.get("set_num") == set_n:
                        nh = next_md.get("md_home_games") or 0
                        na = next_md.get("md_away_games") or 0
                        if nh > home_games:
                            game_winner = "home"
                        elif na > away_games:
                            game_winner = "away"
                if game_winner is None and last_pt.get("source") == "point_by_point":
                    game_winner = last_pt.get("point_winner")

                if game_winner == "home":
                    home_games += 1
                elif game_winner == "away":
                    away_games += 1

        # Step 7: infer point_winner for match_details rows from score deltas.
        for i, pt in enumerate(processed):
            if pt.get("source") == "match_details" and pt.get("point_winner") is None:
                if i > 0:
                    pt["point_winner"] = _infer_point_winner(processed[i - 1], pt)

        # Step 8: upsert all rows.
        now = datetime.now(timezone.utc)
        cur = self._conn.cursor()
        try:
            for pt in processed:
                cur.execute(_INSERT_PROCESSED_POINT, [
                    match_id_int, player_a, player_b,
                    pt["set_num"], pt["game_num"], pt["point_num"],
                    pt["home_point"], pt["away_point"],
                    pt["server"], pt["point_winner"],
                    bool(pt["is_ace"]) if pt.get("is_ace") is not None else False,
                    bool(pt["is_double_fault"]) if pt.get("is_double_fault") is not None else False,
                    bool(pt["has_gap"]),
                    pt["home_sets_won"], pt["away_sets_won"],
                    pt["home_games_won"], pt["away_games_won"],
                    pt.get("source") or "point_by_point",
                    pt.get("tournament_name"), pt.get("category"), now,
                ])
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            _log.warning(
                "upsert_processed_points: insert failed for match_id=%s: %s",
                match_id_int, exc,
            )
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MatchLogger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
