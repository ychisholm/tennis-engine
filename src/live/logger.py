from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

import psycopg2

_log = logging.getLogger(__name__)

_SETUP_STMTS = [
    "CREATE SCHEMA IF NOT EXISTS live",
    "CREATE SCHEMA IF NOT EXISTS audit",
    "CREATE SCHEMA IF NOT EXISTS book",
    """
    CREATE TABLE IF NOT EXISTS live.backfill_points (
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
    CREATE TABLE IF NOT EXISTS live.backfill_odds_polls (
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
    CREATE TABLE IF NOT EXISTS live.match_polls (
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
    CREATE TABLE IF NOT EXISTS live.match_states (
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
        country_a             VARCHAR,
        country_b             VARCHAR,
        PRIMARY KEY (match_id, polled_at)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS match_states_score_idx
      ON live.match_states
      (match_id, home_sets_won, away_sets_won,
       home_current_games, away_current_games,
       home_current_point, away_current_point)
    """,
    """
    ALTER TABLE live.match_states
      ADD COLUMN IF NOT EXISTS first_server VARCHAR(10)
    """,
    """
    CREATE TABLE IF NOT EXISTS audit.api_call_log (
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
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_match_id ON audit.api_call_log(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_timestamp ON audit.api_call_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_endpoint ON audit.api_call_log(endpoint)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_log_poll_cycle ON audit.api_call_log(poll_cycle_id)",
    """
    CREATE TABLE IF NOT EXISTS audit.api_response_archive (
        id          BIGSERIAL PRIMARY KEY,
        timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        endpoint    TEXT NOT NULL,
        match_id    VARCHAR(100),
        raw_json    JSONB NOT NULL,
        byte_size   INT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_response_archive_match_id ON audit.api_response_archive(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_api_response_archive_timestamp ON audit.api_response_archive(timestamp)",
]

_INSERT_RAW_ODDS = """
INSERT INTO live.backfill_odds_polls (
    ts, match_id, player_a, player_b,
    bookmaker_prob_a, num_bookmakers, api_credits_remaining
) VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_MATCH_DETAIL = """
INSERT INTO live.match_polls (
    match_id, player_a, player_b, polled_at, status,
    home_sets, away_sets,
    home_period1, away_period1,
    home_period2, away_period2,
    home_period3, away_period3,
    home_current_point, away_current_point,
    winner_code, tournament_name, category
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPSERT_MATCH_STATE = """
INSERT INTO live.match_states (
    match_id, player_a, player_b, polled_at, status,
    home_sets_won, away_sets_won,
    home_set1_games, away_set1_games,
    home_set2_games, away_set2_games,
    home_set3_games, away_set3_games,
    home_current_games, away_current_games,
    home_current_point, away_current_point,
    point_winner, winner_code, tournament_name, category,
    country_a, country_b, first_server
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (match_id, polled_at) DO NOTHING
"""

_BACKFILL_FIRST_SERVER = """
UPDATE live.match_states
   SET first_server = %s
 WHERE match_id = %s AND first_server IS NULL
"""

# "A" is what the API actually returns for advantage; "AD" kept for safety.
_SCORE_RANK_LOGGER: dict[str, int] = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4, "A": 4}


def _rank_point_score(s: Any) -> int:
    """Rank a point score for delta comparison.

    Regular-game tokens ("0", "15", "30", "40", "AD", "A") use the ordinal
    rank above. Tiebreak point scores are integer strings ("0", "1", "2",
    ...) and fall through to int() parsing. Within a single game both sides'
    scores always use the same scheme, so the comparison is consistent.
    """
    s = str(s or "0")
    if s in _SCORE_RANK_LOGGER:
        return _SCORE_RANK_LOGGER[s]
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def _clean(v: Any) -> Any:
    """Convert NaN/inf to None so Postgres stores NULL."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


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

    ph = _rank_point_score(prev.get("home_current_point"))
    pa = _rank_point_score(prev.get("away_current_point"))
    ch = _rank_point_score(curr.get("home_current_point"))
    ca = _rank_point_score(curr.get("away_current_point"))

    if ch > ph or ca < pa:
        return "home"
    if ca > pa or ch < ph:
        return "away"
    return None


def _infer_first_row_winner(curr: dict) -> str | None:
    """For the very first match_states row of a match (no prev row available),
    infer the point_winner when exactly one side has a non-zero point score
    and the other is at '0'. That can only happen if the non-zero side just
    won the point that produced this row.

    Returns None when the inference is ambiguous: both sides non-zero (we
    missed multiple points), or both at 0 (no point played yet)."""
    h = str(curr.get("home_current_point") or "0")
    a = str(curr.get("away_current_point") or "0")
    h_nonzero = h != "0"
    a_nonzero = a != "0"
    if h_nonzero and not a_nonzero:
        return "home"
    if a_nonzero and not h_nonzero:
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


class MatchLogger:
    """
    Writes live data into the live.* and audit.* schemas:
      live.match_polls         — raw match_detail snapshots
      live.match_states        — deduped score-state snapshots (the live truth)
      live.backfill_odds_polls — bookmaker poll ledger (used by backfill scripts)
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

    def log_match_detail(
        self,
        parsed_detail: dict,
        polled_at: datetime | None = None,
    ) -> None:
        """Insert one row into live.match_polls from a parsed detail dict."""
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

    def upsert_match_detail_points(
        self,
        parsed_detail: dict,
        polled_at: datetime,
        first_server: str | None = None,
    ) -> None:
        """Insert one score-state snapshot into live.match_states.

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
                    SELECT 1 FROM live.match_states
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
            # has the identical score state, the 10s poller is just observing
            # an unchanged game. Compare against the *most recent* row only —
            # a match against an earlier row (e.g. deuce returning after AD)
            # is a real new point and must be inserted.
            cur.execute(
                """
                SELECT home_sets_won, away_sets_won,
                       home_current_games, away_current_games,
                       home_current_point, away_current_point
                FROM live.match_states
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
                FROM live.match_states
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
            # unattributed. When this is the first row for the match (no prev),
            # infer from an unambiguous 0-vs-non-zero score so the dashboard
            # can highlight the opening point.
            point_winner = (
                _derive_point_winner(prev, curr_state)
                or _retro_winner_for_prev_game(prev, curr_state)
                or (_infer_first_row_winner(curr_state) if prev is None else None)
            )

            cur.execute(_UPSERT_MATCH_STATE, [
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
                first_server,
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
                    UPDATE live.match_states
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

    def backfill_first_server(self, match_id: int, first_server: str) -> None:
        """One-shot UPDATE that populates first_server for any existing rows
        of this match where it's currently NULL. Idempotent — re-running with
        the same value is a no-op once all rows are filled. Never raises:
        first_server is best-effort metadata, and a DB hiccup here must not
        crash the worker."""
        cur = self._conn.cursor()
        try:
            cur.execute(_BACKFILL_FIRST_SERVER, [first_server, int(match_id)])
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            _log.warning(
                "backfill_first_server failed for match_id=%s: %s",
                match_id, exc,
            )
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MatchLogger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
