"""
Tests for src/live/logger.py (PostgreSQL backend)

Requires DATABASE_URL to be set.  All tests are skipped automatically when
the env-var is absent so the suite stays green in environments without a
Postgres instance.

Uses match_id=99999 as a sentinel and deletes those rows in teardown so the
tests are side-effect-free on a shared database.
"""

from __future__ import annotations

import math
import os

import pytest

# ---------------------------------------------------------------------------
# Skip-gate: every test in this module is skipped without DATABASE_URL
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping PostgreSQL logger tests",
)

from src.live.logger import MatchLogger  # noqa: E402  (import after skip-gate)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------
_MID = 99999  # sentinel match_id — cleaned up in teardown

_POINT_DICT = {
    "server":           "home",
    "home_point_score": "15",
    "away_point_score": "0",
    "point_winner":     "home",
    "is_ace":           True,
    "is_double_fault":  False,
    "game_number":      1,
    "set_number":       1,
}

_PROB_OUTPUT = {
    "timestamp": "2026-04-21T12:00:00Z",
    "match_state": {
        "sets_A": 0, "sets_B": 0,
        "games_A": 0, "games_B": 0,
        "points_A": 1, "points_B": 0,
        "serving_player": "A",
        "set_number": 1,
        "game_number": 1,
    },
    "dominance": {
        "D_A": 0.55, "D_B": 0.45, "delta": 0.10,
        "breakdown_A": {"nmi": 0.0, "sms": 0.62, "rms": 0.48, "pms": 0.51, "gps": 0.0},
        "breakdown_B": {"nmi": 0.0, "sms": 0.58, "rms": 0.45, "pms": 0.49, "gps": 0.0},
    },
    "adjusted_p": {"p_hat_A": 0.65, "p_hat_B": 0.62},
    "probabilities": {"P_game_A": 0.72, "P_set_A": 0.68, "P_match_A": 0.71},
    "match_over": False,
    "winner": None,
}

_ODDS = {"home_implied_prob": 0.65, "away_implied_prob": 0.35}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def logger():
    """Create a MatchLogger and delete sentinel rows on teardown."""
    ml = MatchLogger()
    yield ml
    # Teardown — remove sentinel data written during the test
    with ml._conn.cursor() as cur:
        for tbl in (
            "live_raw.tennisapi_points",
            "live_raw.oddsapi_polls",
            "live_processed.dashboard_log",
        ):
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE match_id = %s", [_MID])
            except Exception:
                ml._conn.rollback()
    ml._conn.commit()
    ml.close()


def _raw_point(ml: MatchLogger, *, match_id=_MID, point_num=0, point_dict=None):
    ml.log_raw_point(
        match_id=match_id,
        player_a="Federer",
        player_b="Djokovic",
        point_dict=point_dict or _POINT_DICT,
        point_num=point_num,
        tournament_name="Wimbledon",
        category="atp",
    )


def _processed(ml: MatchLogger, *, match_id=_MID, point_num=0, odds=_ODDS):
    last_odds = {"home_implied_prob": odds["home_implied_prob"]} if odds else None
    ml.log_processed_state(
        match_id=match_id,
        player_a="Federer",
        player_b="Djokovic",
        point_dict=_POINT_DICT,
        prob_output=_PROB_OUTPUT,
        last_odds=last_odds,
        point_num=point_num,
        tournament_name="Wimbledon",
        category="atp",
    )


def _fetchone(ml: MatchLogger, sql: str, params=None):
    with ml._conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()


def _fetchval(ml: MatchLogger, sql: str, params=None):
    row = _fetchone(ml, sql, params)
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_tables_created(logger):
    """All three Medallion tables must exist after MatchLogger.__init__."""
    for schema, table in [
        ("live_raw",       "tennisapi_points"),
        ("live_raw",       "oddsapi_polls"),
        ("live_processed", "dashboard_log"),
    ]:
        count = _fetchval(logger, """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        """, [schema, table])
        assert count == 1, f"{schema}.{table} was not created"


def test_tennisapi_points_columns(logger):
    cols = set(row[0] for row in _fetchone(logger, """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'live_raw' AND table_name = 'tennisapi_points'
    """, ).__class__.__mro__)  # placeholder — replaced below
    # re-query properly
    with logger._conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'live_raw' AND table_name = 'tennisapi_points'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {
        "ts", "match_id", "player_a", "player_b",
        "point_num", "set_num", "game_num",
        "home_point", "away_point", "server", "point_winner",
        "is_ace", "is_double_fault", "ingestion_source",
        "tournament_name", "category",
    }
    assert expected <= cols


# ---------------------------------------------------------------------------
# Insertion tests — log_raw_point
# ---------------------------------------------------------------------------

def test_raw_point_inserts_one_row(logger):
    _raw_point(logger)
    count = _fetchval(
        logger,
        "SELECT COUNT(*) FROM live_raw.tennisapi_points WHERE match_id = %s",
        [_MID],
    )
    assert count == 1


def test_multiple_raw_points_accumulate(logger):
    for i in range(5):
        _raw_point(logger, point_num=i)
    count = _fetchval(
        logger,
        "SELECT COUNT(*) FROM live_raw.tennisapi_points WHERE match_id = %s",
        [_MID],
    )
    assert count == 5


def test_raw_point_fields(logger):
    _raw_point(logger, point_num=7)
    row = _fetchone(logger, """
        SELECT match_id, player_a, player_b, set_num, game_num, point_num,
               server, point_winner, is_ace, is_double_fault
        FROM live_raw.tennisapi_points
        WHERE match_id = %s
    """, [_MID])
    assert row[0] == _MID
    assert row[1] == "Federer"
    assert row[2] == "Djokovic"
    assert row[3] == 1      # set_num
    assert row[4] == 1      # game_num
    assert row[5] == 7      # point_num
    assert row[6] == "home"
    assert row[7] == "home"
    assert row[8] is True   # is_ace
    assert row[9] is False  # is_double_fault


# ---------------------------------------------------------------------------
# Insertion tests — log_raw_odds
# ---------------------------------------------------------------------------

def test_raw_odds_inserts_one_row(logger):
    logger.log_raw_odds(
        match_id=_MID,
        player_a="Federer",
        player_b="Djokovic",
        odds_result={"bookmaker_implied_prob": 0.65, "num_bookmakers": 3,
                     "api_credits_remaining": 500},
    )
    count = _fetchval(
        logger,
        "SELECT COUNT(*) FROM live_raw.oddsapi_polls WHERE match_id = %s",
        [_MID],
    )
    assert count == 1


# ---------------------------------------------------------------------------
# Insertion tests — log_processed_state
# ---------------------------------------------------------------------------

def test_processed_inserts_one_row(logger):
    _processed(logger)
    count = _fetchval(
        logger,
        "SELECT COUNT(*) FROM live_processed.dashboard_log WHERE match_id = %s",
        [_MID],
    )
    assert count == 1


def test_probability_and_edge(logger):
    _processed(logger)
    row = _fetchone(logger, """
        SELECT model_prob_a, bookmaker_prob_a, edge
        FROM live_processed.dashboard_log
        WHERE match_id = %s
    """, [_MID])
    assert abs(row[0] - 0.71) < 1e-4   # model_prob_a
    assert abs(row[1] - 0.65) < 1e-4   # bookmaker_prob_a
    assert abs(row[2] - 0.06) < 1e-4   # edge = 0.71 - 0.65


def test_dominance_fields(logger):
    _processed(logger)
    row = _fetchone(logger, """
        SELECT d_a, d_b FROM live_processed.dashboard_log WHERE match_id = %s
    """, [_MID])
    assert abs(row[0] - 0.55) < 1e-4
    assert abs(row[1] - 0.45) < 1e-4


def test_signal_fields(logger):
    _processed(logger)
    row = _fetchone(logger, """
        SELECT sms_a, sms_b, rms_a, rms_b
        FROM live_processed.dashboard_log
        WHERE match_id = %s
    """, [_MID])
    assert abs(row[0] - 0.62) < 1e-4
    assert abs(row[1] - 0.58) < 1e-4
    assert abs(row[2] - 0.48) < 1e-4
    assert abs(row[3] - 0.45) < 1e-4


def test_no_odds_stores_null(logger):
    _processed(logger, odds=None)
    row = _fetchone(logger, """
        SELECT bookmaker_prob_a, edge
        FROM live_processed.dashboard_log
        WHERE match_id = %s
    """, [_MID])
    assert row[0] is None
    assert row[1] is None


def test_nan_signal_stored_as_null(logger):
    nan_prob = {
        **_PROB_OUTPUT,
        "dominance": {
            **_PROB_OUTPUT["dominance"],
            "D_A": float("nan"),
            "breakdown_A": {k: float("nan") for k in ("nmi", "sms", "rms", "pms", "gps")},
        },
    }
    logger.log_processed_state(
        match_id=_MID, player_a="A", player_b="B",
        point_dict=_POINT_DICT, prob_output=nan_prob,
        last_odds=None, point_num=0,
    )
    row = _fetchone(logger, """
        SELECT d_a, nmi_a FROM live_processed.dashboard_log WHERE match_id = %s
    """, [_MID])
    assert row[0] is None
    assert row[1] is None


# ---------------------------------------------------------------------------
# Context-manager test
# ---------------------------------------------------------------------------

def test_context_manager():
    with MatchLogger() as ml:
        _raw_point(ml)
        count = _fetchval(ml, """
            SELECT COUNT(*) FROM live_raw.tennisapi_points WHERE match_id = %s
        """, [_MID])
        assert count == 1
    # Clean up sentinel rows (connection already closed — reopen briefly)
    cleanup = MatchLogger()
    with cleanup._conn.cursor() as cur:
        for tbl in (
            "live_raw.tennisapi_points",
            "live_raw.oddsapi_polls",
            "live_processed.dashboard_log",
        ):
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE match_id = %s", [_MID])
            except Exception:
                cleanup._conn.rollback()
    cleanup._conn.commit()
    cleanup.close()
