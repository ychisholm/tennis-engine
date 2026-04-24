#!/usr/bin/env python3
"""
Tests for src/signals/gps.py (Component 4E — Game Length Pressure Score)

Run with:
    python -m pytest tests/test_gps.py -v
"""

import math
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.signals.gps import GPSCalculator


@pytest.fixture
def calc():
    return GPSCalculator()


# ---------------------------------------------------------------------------
# 1. game_pressure: 4 pts = 0, 6 pts = 2, 8 pts = 4
# ---------------------------------------------------------------------------

def test_game_pressure_clean_hold(calc):
    assert calc.game_pressure(4) == 0.0

def test_game_pressure_deuce(calc):
    assert calc.game_pressure(6) == 2.0

def test_game_pressure_double_deuce(calc):
    assert calc.game_pressure(8) == 4.0

def test_game_pressure_below_4_is_zero(calc):
    # Shouldn't happen in practice, but the formula must not go negative
    assert calc.game_pressure(3) == 0.0
    assert calc.game_pressure(0) == 0.0


# ---------------------------------------------------------------------------
# 2. recency_weight: tau=0 → 1.0, tau=4 → ~0.5, tau=8 → ~0.25
# ---------------------------------------------------------------------------

def test_recency_weight_tau_zero(calc):
    assert abs(calc.recency_weight(3, 3) - 1.0) < 1e-9

def test_recency_weight_half_life(calc):
    w = calc.recency_weight(0, 4, lambda_val=4.0)
    assert abs(w - 0.5) < 1e-9

def test_recency_weight_double_half_life(calc):
    w = calc.recency_weight(0, 8, lambda_val=4.0)
    assert abs(w - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# 3. Multiple long games scores higher than all clean holds
# ---------------------------------------------------------------------------

def test_long_games_outscore_clean_holds():
    long_server = GPSCalculator()
    long_server.add_completed_game({"points_played": 7, "game_index": 0})
    long_server.add_completed_game({"points_played": 6, "game_index": 2})
    long_server.add_completed_game({"points_played": 8, "game_index": 4})

    clean_server = GPSCalculator()
    clean_server.add_completed_game({"points_played": 4, "game_index": 0})
    clean_server.add_completed_game({"points_played": 4, "game_index": 2})
    clean_server.add_completed_game({"points_played": 4, "game_index": 4})

    assert long_server.compute(current_game_index=5) > clean_server.compute(current_game_index=5)


# ---------------------------------------------------------------------------
# 4. compute() returns 0.0 when no games have been added
# ---------------------------------------------------------------------------

def test_compute_empty(calc):
    assert calc.compute(current_game_index=0) == 0.0


# ---------------------------------------------------------------------------
# 5. First non-zero game returns 100.0 (it is its own running max)
# ---------------------------------------------------------------------------

def test_first_nonzero_game_is_100(calc):
    calc.add_completed_game({"points_played": 6, "game_index": 0})
    assert calc.compute(current_game_index=0) == 100.0


# ---------------------------------------------------------------------------
# 6. More recent long game outweighs an older long game of equal length
# ---------------------------------------------------------------------------

def test_recency_decay_on_equal_pressure():
    # Same game (6 pts), evaluated at current_game_index=0 vs 8.
    # At index 0 (tau=0): weight=1.0  → score=100
    # At index 8 (tau=8): weight=0.25 → running_max stays, score=25
    calc = GPSCalculator()
    calc.add_completed_game({"points_played": 6, "game_index": 0})

    score_near = calc.compute(current_game_index=0)   # tau=0
    score_far  = calc.compute(current_game_index=8)   # tau=8, two half-lives

    assert score_near > score_far, (
        f"Recent game ({score_near:.2f}) should outscore distant game ({score_far:.2f})"
    )
    assert abs(score_far - 25.0) < 1e-6, (
        f"Two half-lives → weight=0.25 → GPS=25.0, got {score_far:.6f}"
    )


# ---------------------------------------------------------------------------
# 7. A clean 4-point hold contributes nothing regardless of recency
# ---------------------------------------------------------------------------

def test_clean_hold_contributes_nothing(calc):
    calc.add_completed_game({"points_played": 4, "game_index": 0})
    assert calc.compute(current_game_index=0) == 0.0
    assert calc.compute(current_game_index=1) == 0.0
    assert calc.compute(current_game_index=5) == 0.0


# ---------------------------------------------------------------------------
# 8. Archetype weight of 2.0 raises score (up to 100 ceiling)
# ---------------------------------------------------------------------------

def test_archetype_weight_raises_score():
    def build(weight):
        c = GPSCalculator(archetype_weight=weight)
        # Single deuce game — will normalise to 100 at tau=0,
        # so evaluate slightly in the past to get a sub-100 base score.
        c.add_completed_game({"points_played": 6, "game_index": 0})
        c.compute(current_game_index=0)  # prime running max
        return c

    calc_1x = build(1.0)
    calc_2x = build(2.0)

    # Evaluate with decay so base score < 100 before multiplier
    s1 = calc_1x.compute(current_game_index=4)  # tau=4 → weight=0.5 → norm=50
    s2 = calc_2x.compute(current_game_index=4)

    assert s2 >= s1
    if s1 < 100.0:
        assert s2 > s1


# ---------------------------------------------------------------------------
# 9. reset() clears state so compute() returns 0.0 after reset
# ---------------------------------------------------------------------------

def test_reset_clears_state(calc):
    calc.add_completed_game({"points_played": 7, "game_index": 0})
    assert calc.compute(current_game_index=0) == 100.0  # sanity check

    calc.reset()
    assert calc.compute(current_game_index=1) == 0.0
    assert calc._running_max == 0.0
    assert calc._games == []


# ---------------------------------------------------------------------------
# 10. Mix of long and short games produces a value between 0 and 100
# ---------------------------------------------------------------------------

def test_mixed_games_in_range():
    calc = GPSCalculator()
    game_lengths = [4, 6, 4, 8, 5, 4, 7, 4, 6, 5]
    for i, pts in enumerate(game_lengths):
        calc.add_completed_game({"points_played": pts, "game_index": i})

    result = calc.compute(current_game_index=len(game_lengths))
    assert 0.0 <= result <= 100.0, f"GPS out of range: {result:.4f}"
    # Must be strictly positive since there are games with pts > 4
    assert result > 0.0
