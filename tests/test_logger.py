"""
Tests for src/live/logger.py

Uses a temp DuckDB file — no live API calls, no shared state with tennis.duckdb.
"""

from __future__ import annotations

import math
import pytest

from src.live.logger import MatchLogger

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_POINT_DICT = {
    "server": "home",
    "home_point_score": "15",
    "away_point_score": "0",
    "point_winner": "home",
    "is_ace": True,
    "is_double_fault": False,
    "game_number": 1,
    "set_number": 1,
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


@pytest.fixture
def logger(tmp_path):
    ml = MatchLogger(db_path=tmp_path / "test.duckdb")
    yield ml
    ml.close()


def _log_one(logger, *, match_id=99999, point_num=0, odds=_ODDS, point_dict=None):
    logger.log_point(
        match_id=match_id,
        player_a="Federer",
        player_b="Djokovic",
        point_dict=point_dict or _POINT_DICT,
        prob_output=_PROB_OUTPUT,
        odds=odds,
        point_num=point_num,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_table_created(logger):
    result = logger._conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = 'live_match_log'"
    ).fetchone()
    assert result is not None, "live_match_log table should exist after init"


def test_table_has_correct_columns(logger):
    cols = {
        row[0]
        for row in logger._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'live_match_log'"
        ).fetchall()
    }
    expected = {
        "ts", "match_id", "player_a", "player_b",
        "set_num", "game_num", "point_num",
        "home_point", "away_point", "server", "point_winner",
        "is_ace", "is_double_fault",
        "model_prob_a", "bookmaker_prob_a", "edge",
        "d_a", "d_b",
        "nmi_a", "nmi_b", "sms_a", "sms_b",
        "rms_a", "rms_b", "pms_a", "pms_b", "gps_a", "gps_b",
    }
    assert expected <= cols


# ---------------------------------------------------------------------------
# Insertion
# ---------------------------------------------------------------------------

def test_log_point_inserts_one_row(logger):
    _log_one(logger)
    count = logger._conn.execute("SELECT COUNT(*) FROM live_match_log").fetchone()[0]
    assert count == 1


def test_multiple_points_accumulate(logger):
    for i in range(5):
        _log_one(logger, point_num=i)
    count = logger._conn.execute("SELECT COUNT(*) FROM live_match_log").fetchone()[0]
    assert count == 5


# ---------------------------------------------------------------------------
# Field values
# ---------------------------------------------------------------------------

def test_match_and_player_fields(logger):
    _log_one(logger, match_id=12345)
    row = logger._conn.execute(
        "SELECT match_id, player_a, player_b FROM live_match_log"
    ).fetchone()
    assert row == (12345, "Federer", "Djokovic")


def test_set_game_point_num(logger):
    _log_one(logger, point_num=7)
    row = logger._conn.execute(
        "SELECT set_num, game_num, point_num FROM live_match_log"
    ).fetchone()
    assert row == (1, 1, 7)


def test_point_metadata(logger):
    _log_one(logger)
    row = logger._conn.execute(
        "SELECT server, point_winner, is_ace, is_double_fault FROM live_match_log"
    ).fetchone()
    assert row == ("home", "home", True, False)


def test_probability_and_edge(logger):
    _log_one(logger)
    row = logger._conn.execute(
        "SELECT model_prob_a, bookmaker_prob_a, edge FROM live_match_log"
    ).fetchone()
    assert abs(row[0] - 0.71) < 1e-4
    assert abs(row[1] - 0.65) < 1e-4
    assert abs(row[2] - 0.06) < 1e-4


def test_dominance_fields(logger):
    _log_one(logger)
    row = logger._conn.execute("SELECT d_a, d_b FROM live_match_log").fetchone()
    assert abs(row[0] - 0.55) < 1e-4
    assert abs(row[1] - 0.45) < 1e-4


def test_signal_fields(logger):
    _log_one(logger)
    row = logger._conn.execute(
        "SELECT sms_a, sms_b, rms_a, rms_b FROM live_match_log"
    ).fetchone()
    assert abs(row[0] - 0.62) < 1e-4
    assert abs(row[1] - 0.58) < 1e-4
    assert abs(row[2] - 0.48) < 1e-4
    assert abs(row[3] - 0.45) < 1e-4


# ---------------------------------------------------------------------------
# Null / edge-case handling
# ---------------------------------------------------------------------------

def test_no_odds_stores_null(logger):
    _log_one(logger, odds=None)
    row = logger._conn.execute(
        "SELECT bookmaker_prob_a, edge FROM live_match_log"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_nan_signal_stored_as_null(logger):
    prob = {
        **_PROB_OUTPUT,
        "dominance": {
            **_PROB_OUTPUT["dominance"],
            "D_A": float("nan"),
            "breakdown_A": {k: float("nan") for k in ("nmi", "sms", "rms", "pms", "gps")},
            "breakdown_B": _PROB_OUTPUT["dominance"]["breakdown_B"],
        },
    }
    logger.log_point(
        match_id=99999, player_a="A", player_b="B",
        point_dict=_POINT_DICT, prob_output=prob, odds=None, point_num=0,
    )
    row = logger._conn.execute("SELECT d_a, nmi_a FROM live_match_log").fetchone()
    assert row[0] is None
    assert row[1] is None


def test_context_manager(tmp_path):
    with MatchLogger(db_path=tmp_path / "ctx.duckdb") as ml:
        _log_one(ml)
        count = ml._conn.execute("SELECT COUNT(*) FROM live_match_log").fetchone()[0]
    assert count == 1
