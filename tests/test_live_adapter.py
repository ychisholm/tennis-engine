"""
Tests for src/verification/live_adapter.py

Pure-function tests — no DB needed.
"""
from __future__ import annotations

from src.verification.live_adapter import live_match_states_to_state_rows


def _row(**overrides):
    base = {
        "polled_at": 0,
        "status": "inprogress",
        "home_sets_won": 0,
        "away_sets_won": 0,
        "home_set1_games": 0, "away_set1_games": 0,
        "home_set2_games": 0, "away_set2_games": 0,
        "home_set3_games": 0, "away_set3_games": 0,
        "home_current_games": 0, "away_current_games": 0,
        "home_current_point": "0", "away_current_point": "0",
    }
    base.update(overrides)
    return base


def test_simple_4_point_game():
    """15-0, 30-0, 40-0, then game won (games=1-0, score 0-0)."""
    rows = [
        _row(polled_at=1, home_current_point="15", away_current_point="0"),
        _row(polled_at=2, home_current_point="30", away_current_point="0"),
        _row(polled_at=3, home_current_point="40", away_current_point="0"),
        _row(polled_at=4,
             home_current_games=1, away_current_games=0,
             home_current_point="0", away_current_point="0"),
    ]
    out = live_match_states_to_state_rows(rows)
    assert len(out) == 4

    assert (out[0].sets_a, out[0].sets_b) == (0, 0)
    assert (out[0].games_a, out[0].games_b) == (0, 0)
    assert (out[0].score_a, out[0].score_b) == ("15", "0")
    assert out[0].polled_at == 1

    assert (out[2].score_a, out[2].score_b) == ("40", "0")
    assert (out[3].games_a, out[3].games_b) == (1, 0)
    assert (out[3].score_a, out[3].score_b) == ("0", "0")


def test_advantage_normalization():
    """The live tracker emits 'A' for advantage; the adapter must normalize to 'AD'."""
    rows = [_row(home_current_point="A", away_current_point="40")]
    out = live_match_states_to_state_rows(rows)
    assert len(out) == 1
    assert out[0].score_a == "AD"
    assert out[0].score_b == "40"


def test_tiebreak_score_passthrough():
    """Numeric tiebreak scores pass through as strings unchanged."""
    rows = [
        _row(home_current_games=6, away_current_games=6,
             home_current_point="5", away_current_point="3"),
    ]
    out = live_match_states_to_state_rows(rows)
    assert len(out) == 1
    assert (out[0].games_a, out[0].games_b) == (6, 6)
    assert (out[0].score_a, out[0].score_b) == ("5", "3")


def test_set_boundary_passthrough():
    """A set-boundary pair (set 1 closing → set 2 starting) flows through unmodified."""
    rows = [
        _row(polled_at=1,
             home_set1_games=6, away_set1_games=2,
             home_current_games=5, away_current_games=2,
             home_current_point="40", away_current_point="30"),
        _row(polled_at=2,
             home_sets_won=1, away_sets_won=0,
             home_set1_games=6, away_set1_games=2,
             home_current_games=0, away_current_games=0,
             home_current_point="0", away_current_point="0"),
    ]
    out = live_match_states_to_state_rows(rows)
    assert len(out) == 2
    assert (out[0].sets_a, out[0].sets_b) == (0, 0)
    assert (out[0].games_a, out[0].games_b) == (5, 2)
    assert (out[1].sets_a, out[1].sets_b) == (1, 0)
    assert (out[1].games_a, out[1].games_b) == (0, 0)
    assert (out[1].score_a, out[1].score_b) == ("0", "0")


def test_finished_match_end_row():
    """A status='finished' row with closing-set games + zeroed points is preserved."""
    rows = [
        _row(polled_at=99,
             status="finished",
             home_sets_won=2, away_sets_won=0,
             home_set1_games=6, away_set1_games=2,
             home_set2_games=6, away_set2_games=0,
             home_current_games=6, away_current_games=0,
             home_current_point="0", away_current_point="0"),
    ]
    out = live_match_states_to_state_rows(rows)
    assert len(out) == 1
    assert (out[0].sets_a, out[0].sets_b) == (2, 0)
    assert (out[0].games_a, out[0].games_b) == (6, 0)
    assert (out[0].score_a, out[0].score_b) == ("0", "0")


def test_notstarted_rows_filtered():
    """status='notstarted' rows should be dropped from the output."""
    rows = [
        _row(polled_at=0, status="notstarted"),
        _row(polled_at=1, status="notstarted"),
        _row(polled_at=2, status="inprogress",
             home_current_point="15", away_current_point="0"),
    ]
    out = live_match_states_to_state_rows(rows)
    assert len(out) == 1
    assert out[0].polled_at == 2
    assert (out[0].score_a, out[0].score_b) == ("15", "0")


def test_empty_input():
    assert live_match_states_to_state_rows([]) == []
