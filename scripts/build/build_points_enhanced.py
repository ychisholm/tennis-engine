#!/usr/bin/env python3
"""
Build train.points_enhanced from train.points_cleaned (base) joined with
train.match_rankings (rankings) and live.backfill_matches (tournament_level).

points_cleaned is the authoritative, already-cleaned point stream: it carries
the before-point score state (games / set scores / within-game ordinals),
is_tiebreak, the six situation flags, server/returner ids, point_winner and
server_won_point, and it INCLUDES synthesized game-winning points that the
source API omitted. We therefore pull identity / score-state / flag / target
columns straight from each cleaned row, and walk a match's points only to
accumulate the causal in-match signals (the *_cm columns) that points_cleaned
does not store.

For every (decided) point we emit one row containing:
  - identity + match context (incl. tournament_level from backfill_matches)
  - score state BEFORE the point
  - six situation flags (BP / GP / server&returner SP / server&returner MP)
  - service-game running counters + total_games_played
  - server/returner rankings (from match_rankings)
  - 11 cumulative-match (cm) signals per player + within-set hold rate

cm signals reflect state strictly BEFORE the current point; accumulators are
updated only AFTER the row is emitted. Synthesized points count as real played
points. Rows whose outcome is unknown (server_won_point IS NULL) are skipped.

Usage:
    .venv/bin/python scripts/build/build_points_enhanced.py
    .venv/bin/python scripts/build/build_points_enhanced.py --limit 50
    .venv/bin/python scripts/build/build_points_enhanced.py --inspect-only
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from dotenv import load_dotenv


_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pressure-point ordinal pairs (server_ord, returner_ord) for non-tiebreak
# games. The set is symmetric, so checking (player_a, player_b) is equivalent.
PRESSURE_PAIRS = frozenset({
    (1, 1), (2, 2), (3, 3),
    (2, 3), (3, 2),
    (1, 3), (3, 1),
    (0, 3), (3, 0),
    (3, 4), (4, 3),
})

BATCH_SIZE = 50


def _r2(x: Optional[float]) -> Optional[float]:
    """Round a rate/percentage to 2 decimals; preserve None."""
    return None if x is None else round(x, 2)


# ---------------------------------------------------------------------------
# Per-player signal accumulator (cumulative-match scope; ws reuses it for hold)
# ---------------------------------------------------------------------------

class PlayerSignals:
    """Accumulators for one player. cm instances use every field; ws instances
    only ever read hold_rate(), so only their service-game fields are kept
    current."""

    __slots__ = (
        'service_games_completed', 'service_games_held',
        'serve_points_won', 'serve_points_played',
        'return_points_won', 'return_points_played',
        'bp_faced', 'bp_saved', 'bp_created', 'bp_converted',
        'pressure_points_won', 'pressure_points_played',
        'last_10', 'last_20',
        'game_streak',
    )

    def __init__(self):
        self.service_games_completed = 0
        self.service_games_held = 0
        self.serve_points_won = 0
        self.serve_points_played = 0
        self.return_points_won = 0
        self.return_points_played = 0
        self.bp_faced = 0
        self.bp_saved = 0
        self.bp_created = 0
        self.bp_converted = 0
        self.pressure_points_won = 0
        self.pressure_points_played = 0
        self.last_10 = deque(maxlen=10)
        self.last_20 = deque(maxlen=20)
        self.game_streak = 0

    def hold_rate(self) -> Optional[float]:
        if self.service_games_completed == 0:
            return None
        return _r2(self.service_games_held / self.service_games_completed)

    def serve_win_pct(self) -> Optional[float]:
        if self.serve_points_played == 0:
            return None
        return _r2(self.serve_points_won / self.serve_points_played)

    def bp_saved_rate(self) -> Optional[float]:
        if self.bp_faced == 0:
            return None
        return _r2(self.bp_saved / self.bp_faced)

    def return_win_pct(self) -> Optional[float]:
        if self.return_points_played == 0:
            return None
        return _r2(self.return_points_won / self.return_points_played)

    def bp_conv_rate(self) -> Optional[float]:
        if self.bp_created == 0:
            return None
        return _r2(self.bp_converted / self.bp_created)

    def pressure_win_pct(self) -> Optional[float]:
        if self.pressure_points_played == 0:
            return None
        return _r2(self.pressure_points_won / self.pressure_points_played)

    def pts_won_last_10(self) -> Optional[float]:
        if len(self.last_10) < 10:
            return None
        return _r2(sum(self.last_10) / 10.0)

    def pts_won_last_20(self) -> Optional[float]:
        if len(self.last_20) < 20:
            return None
        return _r2(sum(self.last_20) / 20.0)


# ---------------------------------------------------------------------------
# Per-match state machine
# ---------------------------------------------------------------------------

class MatchState:
    """Walks one match's points_cleaned rows in order, pulling before-point
    state directly from each row and accumulating the causal cm signals."""

    def __init__(self, ctx: dict):
        # ctx carries match-constant fields + tournament_level + the four raw
        # home/away ranking values (None when match_rankings has no row).
        self.ctx = ctx
        self.best_of = int(ctx.get('best_of') or 3)

        self.cm_a = PlayerSignals()
        self.cm_b = PlayerSignals()
        self.ws_a = PlayerSignals()
        self.ws_b = PlayerSignals()

        self.global_point_num = 0
        self.prev_sg: Optional[tuple] = None  # (set_num, game_num) of last row seen

        # Running game counters (reset per game).
        self.bp_count_this_game = 0
        self.pts_played_this_game = 0
        self.server_pts_won_this_game = 0
        self.returner_pts_won_this_game = 0

        # total_games_played support.
        self.completed_set_games = 0   # games in fully-completed sets
        self.games_in_set = 0          # games completed in the current set

        # Just-ended-game trackers (describe the game in progress until a
        # boundary resolves it).
        self.cur_game_server_is_a: Optional[bool] = None
        self.cur_game_is_tiebreak: bool = False
        self.cur_game_last_a_won: Optional[bool] = None
        self.cur_game_has_winner: bool = False

        self.skipped = 0
        self.warnings: list[str] = []

    # ----- transitions -----

    def _reset_in_game_counters(self) -> None:
        self.bp_count_this_game = 0
        self.pts_played_this_game = 0
        self.server_pts_won_this_game = 0
        self.returner_pts_won_this_game = 0
        self.cur_game_server_is_a = None
        self.cur_game_is_tiebreak = False
        self.cur_game_last_a_won = None
        self.cur_game_has_winner = False

    def _reset_ws(self) -> None:
        self.ws_a = PlayerSignals()
        self.ws_b = PlayerSignals()

    def _resolve_prior_game(self) -> None:
        """Resolve the game that just ended (signalled by a (set,game) change),
        using the trackers captured while walking that game. Updates hold stats
        (regular games only) and game_streak. Skips silently when the game's
        winner was never determinable (only undecided points)."""
        if not self.cur_game_has_winner:
            self.warnings.append("game winner undetermined at boundary")
            return

        winner_a = self.cur_game_last_a_won

        # game_streak (cm + ws).
        if winner_a:
            self.cm_a.game_streak += 1
            self.ws_a.game_streak += 1
            self.cm_b.game_streak = 0
            self.ws_b.game_streak = 0
        else:
            self.cm_b.game_streak += 1
            self.ws_b.game_streak += 1
            self.cm_a.game_streak = 0
            self.ws_a.game_streak = 0

        # hold accounting — regular (non-tiebreak) games only.
        if not self.cur_game_is_tiebreak and self.cur_game_server_is_a is not None:
            server_is_a = self.cur_game_server_is_a
            server_won_game = (winner_a == server_is_a)
            pair = (self.cm_a, self.ws_a) if server_is_a else (self.cm_b, self.ws_b)
            for sig in pair:
                sig.service_games_completed += 1
                if server_won_game:
                    sig.service_games_held += 1

    # ----- main entry: process one cleaned row, produce at most one output row -----

    def process_point(self, pt: dict) -> Optional[tuple]:
        ctx = self.ctx

        if self.prev_sg is None:
            new_set = True
            new_game = True
        else:
            new_set = pt['set_num'] != self.prev_sg[0]
            new_game = new_set or pt['game_num'] != self.prev_sg[1]

        # Resolve the prior game (and possibly set) BEFORE handling curr.
        if new_game and self.prev_sg is not None:
            self._resolve_prior_game()
            self.games_in_set += 1
            if new_set:
                self.completed_set_games += self.games_in_set
                self.games_in_set = 0
                self._reset_ws()
            self._reset_in_game_counters()

        server_id = pt['server_id']
        server_is_a = (server_id == ctx['player_a_id']) if server_id is not None else None
        is_tiebreak = bool(pt['is_tiebreak'])

        swp = pt['server_won_point']
        pw = pt['point_winner']
        decided = (swp is not None) and (pw in ('home', 'away'))

        if not decided:
            # Cannot emit (server_won is NOT NULL) and outcome is unknown, so we
            # neither emit nor accumulate. Keep game trackers coherent in case
            # later points of this game are decided.
            if server_is_a is not None:
                self.cur_game_server_is_a = server_is_a
            self.cur_game_is_tiebreak = is_tiebreak
            self.prev_sg = (pt['set_num'], pt['game_num'])
            self.skipped += 1
            return None

        server_won = (swp == 1)
        a_won = (pw == 'home')
        s = pt['home_points_ord'] if server_is_a else pt['away_points_ord']
        r = pt['away_points_ord'] if server_is_a else pt['home_points_ord']
        is_bp = bool(pt['is_break_point'])

        # total games completed in the match before this point = games in
        # completed sets + games completed in the current set (the cleaned
        # before-point home/away game counts).
        total_games_played = (
            self.completed_set_games
            + (pt['home_games'] or 0)
            + (pt['away_games'] or 0)
        )

        # Rankings mapped to server / returner (home == player_a).
        if server_is_a:
            server_rank = ctx['home_rank']
            server_rank_points = ctx['home_rank_points']
            returner_rank = ctx['away_rank']
            returner_rank_points = ctx['away_rank_points']
        else:
            server_rank = ctx['away_rank']
            server_rank_points = ctx['away_rank_points']
            returner_rank = ctx['home_rank']
            returner_rank_points = ctx['home_rank_points']

        self.global_point_num += 1
        row = (
            ctx['match_id'],
            pt['set_num'],
            pt['game_num'],
            pt['point_num'],
            self.global_point_num,
            ctx['match_date'],
            ctx['tournament'],
            ctx['tournament_level'],
            ctx['surface'],
            ctx['round'],
            ctx['tour'],
            self.best_of,
            ctx['player_a_id'],
            ctx['player_b_id'],
            bool(server_is_a),
            server_won,
            s,
            r,
            pt['home_games'],
            pt['away_games'],
            pt['home_sets'],
            pt['away_sets'],
            is_tiebreak,
            (pt['set_num'] == self.best_of),
            is_bp,
            bool(pt['is_game_point']),
            bool(pt['server_set_point']),
            bool(pt['returner_set_point']),
            bool(pt['server_match_point']),
            bool(pt['returner_match_point']),
            self.bp_count_this_game,
            self.pts_played_this_game,
            self.server_pts_won_this_game,
            self.returner_pts_won_this_game,
            total_games_played,
            server_rank,
            server_rank_points,
            returner_rank,
            returner_rank_points,
            # cm signals — player_a
            self.cm_a.hold_rate(),
            self.cm_a.serve_win_pct(),
            self.cm_a.bp_faced,
            self.cm_a.bp_saved_rate(),
            self.cm_a.return_win_pct(),
            self.cm_a.bp_created,
            self.cm_a.bp_conv_rate(),
            self.cm_a.pressure_win_pct(),
            self.cm_a.pts_won_last_10(),
            self.cm_a.pts_won_last_20(),
            self.cm_a.game_streak,
            # cm signals — player_b
            self.cm_b.hold_rate(),
            self.cm_b.serve_win_pct(),
            self.cm_b.bp_faced,
            self.cm_b.bp_saved_rate(),
            self.cm_b.return_win_pct(),
            self.cm_b.bp_created,
            self.cm_b.bp_conv_rate(),
            self.cm_b.pressure_win_pct(),
            self.cm_b.pts_won_last_10(),
            self.cm_b.pts_won_last_20(),
            self.cm_b.game_streak,
            # within-set hold rate only
            self.ws_a.hold_rate(),
            self.ws_b.hold_rate(),
        )

        # Update accumulators with this point's outcome (AFTER emit).
        self._apply_outcome(server_is_a, a_won, server_won, is_bp, is_tiebreak, s, r)

        # Update just-ended-game trackers for the in-progress game.
        self.cur_game_server_is_a = server_is_a
        self.cur_game_is_tiebreak = is_tiebreak
        self.cur_game_last_a_won = a_won
        self.cur_game_has_winner = True

        self.prev_sg = (pt['set_num'], pt['game_num'])
        return row

    def _apply_outcome(
        self,
        server_is_a: bool,
        a_won: bool,
        server_won: bool,
        is_bp: bool,
        is_tiebreak: bool,
        s,
        r,
    ) -> None:
        # Serve / return points (cm only — ws emits hold_rate only).
        if server_is_a:
            self.cm_a.serve_points_played += 1
            self.cm_b.return_points_played += 1
            if server_won:
                self.cm_a.serve_points_won += 1
            else:
                self.cm_b.return_points_won += 1
        else:
            self.cm_b.serve_points_played += 1
            self.cm_a.return_points_played += 1
            if server_won:
                self.cm_b.serve_points_won += 1
            else:
                self.cm_a.return_points_won += 1

        # Break-point accounting.
        if is_bp:
            if server_is_a:
                self.cm_a.bp_faced += 1
                self.cm_b.bp_created += 1
                if server_won:
                    self.cm_a.bp_saved += 1
                else:
                    self.cm_b.bp_converted += 1
            else:
                self.cm_b.bp_faced += 1
                self.cm_a.bp_created += 1
                if server_won:
                    self.cm_b.bp_saved += 1
                else:
                    self.cm_a.bp_converted += 1
            self.bp_count_this_game += 1

        # Pressure points (regular games only).
        if not is_tiebreak and s is not None and r is not None and (s, r) in PRESSURE_PAIRS:
            self.cm_a.pressure_points_played += 1
            self.cm_b.pressure_points_played += 1
            if a_won:
                self.cm_a.pressure_points_won += 1
            else:
                self.cm_b.pressure_points_won += 1

        # Last-10 / last-20 deques (all points).
        a_pt = 1 if a_won else 0
        b_pt = 1 - a_pt
        self.cm_a.last_10.append(a_pt)
        self.cm_a.last_20.append(a_pt)
        self.cm_b.last_10.append(b_pt)
        self.cm_b.last_20.append(b_pt)

        # In-game counters.
        self.pts_played_this_game += 1
        if server_won:
            self.server_pts_won_this_game += 1
        else:
            self.returner_pts_won_this_game += 1


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

CREATE_SCHEMA_DDL = "CREATE SCHEMA IF NOT EXISTS train"

DROP_TABLE_DDL = "DROP TABLE IF EXISTS train.points_enhanced"

CREATE_TABLE_DDL = """
CREATE TABLE train.points_enhanced (
    match_id            BIGINT       NOT NULL,
    set_num             SMALLINT     NOT NULL,
    game_num            SMALLINT     NOT NULL,
    point_num           INT          NOT NULL,
    global_point_num    INT          NOT NULL,

    match_date          DATE,
    tournament          TEXT,
    tournament_level    INTEGER,
    surface             TEXT,
    round               TEXT,
    tour                TEXT,
    best_of             SMALLINT,
    player_a_id         BIGINT       NOT NULL,
    player_b_id         BIGINT       NOT NULL,
    server_is_player_a  BOOLEAN      NOT NULL,

    server_won          BOOLEAN      NOT NULL,

    server_pts_in_game      SMALLINT,
    returner_pts_in_game    SMALLINT,
    player_a_games_in_set   SMALLINT,
    player_b_games_in_set   SMALLINT,
    player_a_sets_won       SMALLINT,
    player_b_sets_won       SMALLINT,
    is_tiebreak             BOOLEAN  NOT NULL,
    is_deciding_set         BOOLEAN  NOT NULL,

    is_break_point        BOOLEAN    NOT NULL,
    is_game_point         BOOLEAN    NOT NULL,
    server_set_point      BOOLEAN    NOT NULL,
    returner_set_point    BOOLEAN    NOT NULL,
    server_match_point    BOOLEAN    NOT NULL,
    returner_match_point  BOOLEAN    NOT NULL,
    bp_count_this_game    SMALLINT   NOT NULL,

    pts_played_this_game        SMALLINT NOT NULL,
    server_pts_won_this_game    SMALLINT NOT NULL,
    returner_pts_won_this_game  SMALLINT NOT NULL,
    total_games_played          SMALLINT NOT NULL,

    server_rank             INTEGER,
    server_rank_points      INTEGER,
    returner_rank           INTEGER,
    returner_rank_points    INTEGER,

    player_a_hold_rate_cm           DOUBLE PRECISION,
    player_a_serve_win_pct_cm       DOUBLE PRECISION,
    player_a_bp_faced_cm            SMALLINT,
    player_a_bp_saved_rate_cm       DOUBLE PRECISION,
    player_a_return_win_pct_cm      DOUBLE PRECISION,
    player_a_bp_created_cm          SMALLINT,
    player_a_bp_conv_rate_cm        DOUBLE PRECISION,
    player_a_pressure_win_pct_cm    DOUBLE PRECISION,
    player_a_pts_won_last_10_cm     DOUBLE PRECISION,
    player_a_pts_won_last_20_cm     DOUBLE PRECISION,
    player_a_game_streak_cm         SMALLINT,

    player_b_hold_rate_cm           DOUBLE PRECISION,
    player_b_serve_win_pct_cm       DOUBLE PRECISION,
    player_b_bp_faced_cm            SMALLINT,
    player_b_bp_saved_rate_cm       DOUBLE PRECISION,
    player_b_return_win_pct_cm      DOUBLE PRECISION,
    player_b_bp_created_cm          SMALLINT,
    player_b_bp_conv_rate_cm        DOUBLE PRECISION,
    player_b_pressure_win_pct_cm    DOUBLE PRECISION,
    player_b_pts_won_last_10_cm     DOUBLE PRECISION,
    player_b_pts_won_last_20_cm     DOUBLE PRECISION,
    player_b_game_streak_cm         SMALLINT,

    player_a_hold_rate_ws           DOUBLE PRECISION,
    player_b_hold_rate_ws           DOUBLE PRECISION,

    PRIMARY KEY (match_id, set_num, game_num, point_num)
)
"""

OUTPUT_COLUMNS = (
    "match_id, set_num, game_num, point_num, global_point_num, "
    "match_date, tournament, tournament_level, surface, round, tour, best_of, "
    "player_a_id, player_b_id, server_is_player_a, "
    "server_won, "
    "server_pts_in_game, returner_pts_in_game, "
    "player_a_games_in_set, player_b_games_in_set, "
    "player_a_sets_won, player_b_sets_won, "
    "is_tiebreak, is_deciding_set, "
    "is_break_point, is_game_point, "
    "server_set_point, returner_set_point, server_match_point, returner_match_point, "
    "bp_count_this_game, "
    "pts_played_this_game, server_pts_won_this_game, returner_pts_won_this_game, "
    "total_games_played, "
    "server_rank, server_rank_points, returner_rank, returner_rank_points, "
    "player_a_hold_rate_cm, player_a_serve_win_pct_cm, player_a_bp_faced_cm, "
    "player_a_bp_saved_rate_cm, player_a_return_win_pct_cm, player_a_bp_created_cm, "
    "player_a_bp_conv_rate_cm, player_a_pressure_win_pct_cm, "
    "player_a_pts_won_last_10_cm, player_a_pts_won_last_20_cm, player_a_game_streak_cm, "
    "player_b_hold_rate_cm, player_b_serve_win_pct_cm, player_b_bp_faced_cm, "
    "player_b_bp_saved_rate_cm, player_b_return_win_pct_cm, player_b_bp_created_cm, "
    "player_b_bp_conv_rate_cm, player_b_pressure_win_pct_cm, "
    "player_b_pts_won_last_10_cm, player_b_pts_won_last_20_cm, player_b_game_streak_cm, "
    "player_a_hold_rate_ws, player_b_hold_rate_ws"
)


# ---------------------------------------------------------------------------
# Source queries
# ---------------------------------------------------------------------------

# One row per match, ordered like the old build (date then id). Match metadata
# is constant within a match in points_cleaned; DISTINCT ON guards against any
# stray per-row variation.
_FETCH_MATCHES_SQL = """
SELECT match_id, match_date, tour, tournament, surface, round, best_of,
       player_a_id, player_b_id
FROM (
    SELECT DISTINCT ON (match_id)
        match_id,
        match_date,
        tour,
        tournament_name  AS tournament,
        surface,
        round,
        best_of,
        home_player_id   AS player_a_id,
        away_player_id   AS player_b_id
    FROM train.points_cleaned
    ORDER BY match_id
) m
ORDER BY match_date NULLS LAST, match_id
"""

# points_cleaned is keyed by (match_id, set_num, game_num, point_num), so it is
# already deduplicated; ordering by (set, game, point) is all that is required.
_FETCH_POINTS_SQL = """
SELECT set_num, game_num, point_num, is_synthesized,
       home_sets, away_sets, home_games, away_games,
       home_points_ord, away_points_ord, is_tiebreak,
       is_break_point, is_game_point,
       server_set_point, returner_set_point,
       server_match_point, returner_match_point,
       server_id, point_winner, server_won_point
FROM train.points_cleaned
WHERE match_id = %s
ORDER BY set_num, game_num, point_num
"""

_FETCH_TOURNAMENT_LEVEL_SQL = """
SELECT match_id, tournament_level FROM live.backfill_matches
"""

_FETCH_RANKINGS_SQL = """
SELECT match_id, home_rank, home_rank_points, away_rank, away_rank_points
FROM train.match_rankings
"""


# ---------------------------------------------------------------------------
# Schema inspection (printed before any build logic runs)
# ---------------------------------------------------------------------------

def print_source_schemas(conn) -> None:
    print()
    print("=" * 72)
    print("SOURCE SCHEMAS (confirmed before build)")
    print("=" * 72)
    targets = [
        ("train", "points_cleaned"),
        ("train", "match_rankings"),
        ("live", "backfill_matches"),
    ]
    with conn.cursor() as cur:
        for schema, tbl in targets:
            print()
            print(f"--- {schema}.{tbl} ---")
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema, tbl),
            )
            rows = cur.fetchall()
            if not rows:
                print("  (table not found)")
            for col, typ, nullable in rows:
                print(f"  {col:24s} {typ:26s} nullable={nullable}")
    print()


# ---------------------------------------------------------------------------
# Match-level lookups
# ---------------------------------------------------------------------------

def fetch_match_ctxs(cur, limit: Optional[int]) -> list[dict]:
    sql = _FETCH_MATCHES_SQL
    if limit:
        sql = sql + f" LIMIT {int(limit)}"
    cur.execute(sql)
    return [dict(r) for r in cur.fetchall()]


def fetch_tournament_levels(cur) -> dict:
    cur.execute(_FETCH_TOURNAMENT_LEVEL_SQL)
    return {r['match_id']: r['tournament_level'] for r in cur.fetchall()}


def fetch_rankings(cur) -> dict:
    cur.execute(_FETCH_RANKINGS_SQL)
    return {
        r['match_id']: (
            r['home_rank'], r['home_rank_points'],
            r['away_rank'], r['away_rank_points'],
        )
        for r in cur.fetchall()
    }


def fetch_points_for_match(cur, match_id: int) -> list[dict]:
    cur.execute(_FETCH_POINTS_SQL, (match_id,))
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Build driver
# ---------------------------------------------------------------------------

def build(conn, limit: Optional[int]) -> tuple[int, int, list[str]]:
    """Returns (matches_processed, rows_written, skip_messages)."""
    skips: list[str] = []
    matches_processed = 0
    rows_written = 0

    with conn.cursor(cursor_factory=RealDictCursor) as lk_cur:
        ctxs = fetch_match_ctxs(lk_cur, limit)
        tournament_levels = fetch_tournament_levels(lk_cur)
        rankings = fetch_rankings(lk_cur)

    print(
        f"Found {len(ctxs)} matches in train.points_cleaned; "
        f"{len(tournament_levels)} backfill_matches rows, "
        f"{len(rankings)} match_rankings rows loaded."
    )

    insert_sql = f"INSERT INTO train.points_enhanced ({OUTPUT_COLUMNS}) VALUES %s"

    batch_rows: list[tuple] = []
    batch_match_count = 0
    batch_idx = 0
    started = time.monotonic()

    with conn.cursor(cursor_factory=RealDictCursor) as fetch_cur, conn.cursor() as ins_cur:
        for ctx in ctxs:
            mid = ctx['match_id']

            # Merge match-level joins into ctx (NULLs when absent).
            ctx['tournament_level'] = tournament_levels.get(mid)
            hr, hrp, ar, arp = rankings.get(mid, (None, None, None, None))
            ctx['home_rank'] = hr
            ctx['home_rank_points'] = hrp
            ctx['away_rank'] = ar
            ctx['away_rank_points'] = arp

            try:
                points = fetch_points_for_match(fetch_cur, mid)
            except Exception as exc:
                skips.append(f"match {mid}: fetch failed: {exc}")
                continue

            if not points:
                skips.append(f"match {mid}: no rows in points_cleaned")
                continue

            state = MatchState(ctx)
            match_rows: list[tuple] = []
            try:
                for pt in points:
                    out = state.process_point(pt)
                    if out is not None:
                        match_rows.append(out)
            except Exception as exc:
                skips.append(f"match {mid}: processing failed — {exc}")
                continue

            if state.warnings:
                first = state.warnings[0]
                more = f" (+{len(state.warnings) - 1} more)" if len(state.warnings) > 1 else ""
                print(f"  WARN match {mid}: {first}{more}")

            batch_rows.extend(match_rows)
            matches_processed += 1
            rows_written += len(match_rows)
            batch_match_count += 1

            if batch_match_count >= BATCH_SIZE:
                execute_values(ins_cur, insert_sql, batch_rows, page_size=1000)
                conn.commit()
                batch_idx += 1
                elapsed = time.monotonic() - started
                print(
                    f"Batch {batch_idx} complete — "
                    f"{matches_processed} matches processed, "
                    f"{rows_written} total rows written "
                    f"({elapsed:.1f}s elapsed)"
                )
                batch_rows = []
                batch_match_count = 0

        if batch_rows:
            execute_values(ins_cur, insert_sql, batch_rows, page_size=1000)
            conn.commit()
            batch_idx += 1
            elapsed = time.monotonic() - started
            print(
                f"Batch {batch_idx} (final) complete — "
                f"{matches_processed} matches processed, "
                f"{rows_written} total rows written "
                f"({elapsed:.1f}s elapsed)"
            )

    return matches_processed, rows_written, skips


# ---------------------------------------------------------------------------
# Verification (runs after build completes)
# ---------------------------------------------------------------------------

def verify(conn) -> None:
    print()
    print("=" * 72)
    print("VERIFICATION")
    print("=" * 72)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM train.points_enhanced")
        total = cur.fetchone()[0]
        print(f"  1. total rows: {total}")

        cur.execute(
            "SELECT tour, COUNT(*) FROM train.points_enhanced GROUP BY tour ORDER BY tour"
        )
        print("  2. rows per tour:")
        for tour, n in cur.fetchall():
            print(f"       {tour}: {n}")

        cur.execute(
            "SELECT COUNT(*) FROM train.points_enhanced WHERE server_won IS NULL"
        )
        print(f"  3. rows where server_won IS NULL: {cur.fetchone()[0]} (expect 0)")

        cur.execute(
            "SELECT COUNT(*) FROM train.points_enhanced WHERE tournament_level IS NOT NULL"
        )
        nn_lvl = cur.fetchone()[0]
        print(f"  4. rows with non-null tournament_level: {nn_lvl} / {total}")

        cur.execute(
            "SELECT COUNT(*) FROM train.points_enhanced WHERE server_rank IS NOT NULL"
        )
        nn_rank = cur.fetchone()[0]
        print(f"  5. rows with non-null server_rank: {nn_rank} / {total}")

        cur.execute("SELECT MAX(total_games_played) FROM train.points_enhanced")
        print(f"  6. max total_games_played: {cur.fetchone()[0]}")

        cur.execute("SELECT match_id FROM train.points_enhanced LIMIT 1")
        row = cur.fetchone()
        if not row:
            print("  7. no rows available to sample.")
            return
        sample_mid = row[0]

        cur.execute(
            """
            SELECT set_num, game_num, point_num, server_is_player_a,
                   server_pts_in_game, returner_pts_in_game, total_games_played,
                   is_break_point, server_won, player_a_serve_win_pct_cm
            FROM train.points_enhanced
            WHERE match_id = %s
            ORDER BY set_num, game_num, point_num
            LIMIT 10
            """,
            (sample_mid,),
        )
        print(f"  7. sample first 10 rows of match {sample_mid}:")
        print(
            "       set game  pt  srv_is_a  s  r  tgp  bp     server_won  a_serve_win_pct_cm"
        )
        first_swp_cm = None
        rows = cur.fetchall()
        for i, r in enumerate(rows):
            (set_n, gm, pt, sia, sp, rp, tgp, bp, sw, swp) = r
            if i == 0:
                first_swp_cm = swp
            swp_str = "NULL" if swp is None else f"{swp:.3f}"
            print(
                f"       {set_n:>3} {gm:>3} {pt:>3}  {sia!s:>8}  "
                f"{sp!s:>2} {rp!s:>2}  {tgp!s:>3}  {bp!s:>5}  {sw!s:>10}  {swp_str:>8}"
            )

        if first_swp_cm is None:
            print("  8. PASS — player_a_serve_win_pct_cm is NULL on first point of match")
        else:
            print(
                f"  8. FAIL — player_a_serve_win_pct_cm on first point = "
                f"{first_swp_cm} (expected NULL)"
            )

    print()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most this many matches (for testing).",
    )
    parser.add_argument(
        "--inspect-only", action="store_true",
        help="Print source schemas and exit without building.",
    )
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(db_url)
    try:
        print_source_schemas(conn)
        if args.inspect_only:
            return 0

        print("=" * 72)
        print("BUILD train.points_enhanced")
        print("=" * 72)

        with conn.cursor() as cur:
            cur.execute(CREATE_SCHEMA_DDL)
            cur.execute(DROP_TABLE_DDL)
            cur.execute(CREATE_TABLE_DDL)
        conn.commit()
        print("  schema train ensured; table train.points_enhanced (re)created.")
        print()

        matches_processed, rows_written, skips = build(conn, args.limit)

        print()
        print("=" * 72)
        print("BUILD SUMMARY")
        print("=" * 72)
        print(f"  matches processed: {matches_processed}")
        print(f"  rows written:      {rows_written}")
        print(f"  matches skipped:   {len(skips)}")
        for s in skips[:20]:
            print(f"    - {s}")
        if len(skips) > 20:
            print(f"    ... and {len(skips) - 20} more")

        verify(conn)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
