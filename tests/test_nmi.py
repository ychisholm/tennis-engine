#!/usr/bin/env python3
"""
Tests for src/signals/nmi.py (Component 4A — Near-Miss Index)

Run with:
    python -m pytest tests/test_nmi.py -v
"""

import math
import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.signals.nmi import NMICalculator

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A service game where receiver reached 0-40 (maximum pressure)
GAME_0_40 = {
    "points": ["0-0", "0-15", "0-30", "0-40", "15-40", "30-40", "deuce", "ad-server", "game"],
    "game_index": 0,
    "converted": False,
}

# A service game where receiver reached only 30-30 / deuce (moderate pressure)
GAME_DEUCE_ONLY = {
    "points": ["0-0", "15-0", "15-15", "15-30", "15-40", "30-40", "deuce", "ad-server", "game"],
    "game_index": 1,
    "converted": False,
}

# A service game where receiver barely applied pressure
GAME_EASY_HOLD = {
    "points": ["0-0", "15-0", "30-0", "40-0", "game"],
    "game_index": 2,
    "converted": False,
}


@pytest.fixture
def calc():
    return NMICalculator()


# ---------------------------------------------------------------------------
# 1. pressure_weight: correct values for all named states + unknown → 0.0
# ---------------------------------------------------------------------------

def test_pressure_weight_known_states(calc):
    assert calc.pressure_weight("0-40") == 1.0
    assert calc.pressure_weight("15-40") == 0.8
    assert calc.pressure_weight("30-40") == 0.5
    assert calc.pressure_weight("deuce-ad-receiver") == 0.5
    assert calc.pressure_weight("40-ad") == 0.5
    assert calc.pressure_weight("30-30") == 0.2
    assert calc.pressure_weight("deuce") == 0.2


def test_pressure_weight_unknown_states(calc):
    for state in ("0-0", "15-0", "30-0", "40-0", "game", "ad-server", "0-15", ""):
        assert calc.pressure_weight(state) == 0.0, (
            f"Expected 0.0 for '{state}', got {calc.pressure_weight(state)}"
        )


# ---------------------------------------------------------------------------
# 2. recency_weight: tau=0 → 1.0, tau=4 → ~0.5, tau=8 → ~0.25
# ---------------------------------------------------------------------------

def test_recency_weight_tau_zero(calc):
    w = calc.recency_weight(game_index=5, current_game_index=5)
    assert abs(w - 1.0) < 1e-9

def test_recency_weight_half_life(calc):
    # tau = lambda_val (4) → weight should be 0.5
    w = calc.recency_weight(game_index=0, current_game_index=4, lambda_val=4.0)
    assert abs(w - 0.5) < 1e-9

def test_recency_weight_double_half_life(calc):
    # tau = 2 * lambda_val (8) → weight should be 0.25
    w = calc.recency_weight(game_index=0, current_game_index=8, lambda_val=4.0)
    assert abs(w - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# 3. Game with 0-40 reached scores higher than one that only reached 30-30
# ---------------------------------------------------------------------------

def test_pressure_score_ordering(calc):
    score_0_40 = calc.compute_pressure_score(GAME_0_40)
    score_deuce = calc.compute_pressure_score(GAME_DEUCE_ONLY)
    assert score_0_40 > score_deuce, (
        f"0-40 game ({score_0_40:.2f}) should outscore deuce-only game ({score_deuce:.2f})"
    )


# ---------------------------------------------------------------------------
# 4. compute() returns 0.0 when no games have been added
# ---------------------------------------------------------------------------

def test_compute_empty(calc):
    assert calc.compute(current_game_index=0) == 0.0


# ---------------------------------------------------------------------------
# 5. compute() returns 100.0 for the first non-zero game (it is also the max)
# ---------------------------------------------------------------------------

def test_compute_first_game_is_100(calc):
    calc.add_game(GAME_0_40)
    score = calc.compute(current_game_index=0)
    assert score == 100.0, f"First non-zero game should return 100.0, got {score}"


# ---------------------------------------------------------------------------
# 6. A more recent game with the same pressure outweighs an older one
# ---------------------------------------------------------------------------

def test_recency_decay_ordering():
    # Evaluate the same game at two different current_game_index values.
    # When the game is "recent" (tau=0) it should score 100; when it is
    # 8 games in the past (tau=8, two half-lives) it should score much lower.
    calc = NMICalculator()
    calc.add_game({
        "points": ["0-0", "0-15", "0-30", "0-40", "game"],
        "game_index": 0,
        "converted": False,
    })

    score_near = calc.compute(current_game_index=0)  # tau=0 → weight=1.0
    score_far = calc.compute(current_game_index=8)   # tau=8 → weight=0.25

    assert score_near > score_far, (
        f"Score when game is recent ({score_near:.2f}) should exceed "
        f"score when game is 8 games old ({score_far:.2f})"
    )
    # With lambda=4 and tau=8 the weight is 0.25, so the far score should be
    # exactly 25.0 (nmi_raw decays but running_max stays at the tau=0 value).
    assert abs(score_far - 25.0) < 1e-6, (
        f"Expected far score ≈ 25.0 (two half-lives), got {score_far:.6f}"
    )


# ---------------------------------------------------------------------------
# 7. Archetype weight 2.0 produces a higher score than 1.0 (clipped at 100)
# ---------------------------------------------------------------------------

def test_archetype_weight_scaling():
    # Use a game that yields something well below 50 when normalised,
    # so that the 2x multiplier doesn't simply clip both to 100.
    moderate_game = {
        "points": ["0-0", "15-0", "15-15", "30-15", "30-30", "40-30", "game"],
        "game_index": 0,
        "converted": False,
    }
    high_pressure_game = {
        "points": ["0-0", "0-15", "0-30", "0-40", "15-40", "30-40", "deuce",
                   "deuce-ad-receiver", "deuce", "ad-server", "game"],
        "game_index": 1,
        "converted": False,
    }

    calc_1x = NMICalculator(archetype_weight=1.0)
    calc_2x = NMICalculator(archetype_weight=2.0)

    for c in (calc_1x, calc_2x):
        c.add_game(high_pressure_game)  # sets the running max
        c.add_game(moderate_game)

    score_1x = calc_1x.compute(current_game_index=2)
    score_2x = calc_2x.compute(current_game_index=2)

    assert score_2x > score_1x or score_2x == 100.0, (
        f"archetype_weight=2.0 ({score_2x:.2f}) should be >= archetype_weight=1.0 ({score_1x:.2f})"
    )

    # Explicitly verify the 2x case is higher or clipped
    if score_1x < 50.0:
        assert score_2x == pytest.approx(score_1x * 2.0, abs=1e-6)
    else:
        assert score_2x == 100.0


# ---------------------------------------------------------------------------
# 8. reset() clears state so compute() returns 0.0 afterwards
# ---------------------------------------------------------------------------

def test_reset_clears_state(calc):
    calc.add_game(GAME_0_40)
    assert calc.compute(current_game_index=0) == 100.0  # sanity check

    calc.reset()
    assert calc.compute(current_game_index=5) == 0.0
