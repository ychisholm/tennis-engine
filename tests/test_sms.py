#!/usr/bin/env python3
"""
Tests for src/signals/sms.py (Component 4B — Serve Module Score)

Run with:
    python -m pytest tests/test_sms.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.signals.sms import SMSCalculator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_point(serve_number: int, won: bool, speed_kmh=None, point_index: int = 0) -> dict:
    return {"serve_number": serve_number, "won": won, "speed_kmh": speed_kmh, "point_index": point_index}


def _fill_history(calc: SMSCalculator, n: int, serve_number: int, won: bool, speed: float | None, start_idx: int = 0):
    """Add *n* identical points to *calc*."""
    for i in range(n):
        calc.add_serve_point(make_point(serve_number, won, speed, start_idx + i))


@pytest.fixture
def calc():
    return SMSCalculator()


# ---------------------------------------------------------------------------
# 1. SMS_1 uses only the last n points
# ---------------------------------------------------------------------------

def test_sms1_rolling_window():
    """Old 1st-serve losses outside the window should not drag down SMS_1."""
    calc = SMSCalculator()
    # 20 old losses (outside n=15 window)
    for i in range(20):
        calc.add_serve_point(make_point(1, False, 200.0, i))
    # 15 wins that fill the window
    for i in range(15):
        calc.add_serve_point(make_point(1, True, 200.0, 20 + i))

    sms = calc.compute(n=15)
    # SMS_1 should be 100.0 (15/15 won) → full weight in final score
    expected_sms1 = 100.0
    # SMS_2 neutral (50), SMS_3 speed ratio ~1.0→50, SMS_4 no games→50
    expected = 0.35 * expected_sms1 + 0.30 * 50.0 + 0.20 * 50.0 + 0.15 * 50.0
    assert abs(sms - expected) < 0.5, f"Expected SMS ≈ {expected:.2f}, got {sms:.2f}"


# ---------------------------------------------------------------------------
# 2. SMS_2 reflects 2nd-serve win % from last n points only
# ---------------------------------------------------------------------------

def test_sms2_rolling_window():
    """Old 2nd-serve wins outside window should not inflate SMS_2."""
    calc = SMSCalculator()
    # 20 old 2nd-serve wins (outside window)
    for i in range(20):
        calc.add_serve_point(make_point(2, True, 155.0, i))
    # 15 2nd-serve losses that fill the window
    for i in range(15):
        calc.add_serve_point(make_point(2, False, 150.0, 20 + i))

    sms = calc.compute(n=15)
    # SMS_2 should be 0.0 (0/15 won) → depresses score
    expected_sms2 = 0.0
    expected = 0.35 * 50.0 + 0.30 * expected_sms2 + 0.20 * 50.0 + 0.15 * 50.0
    assert abs(sms - expected) < 0.5, f"Expected SMS ≈ {expected:.2f}, got {sms:.2f}"


# ---------------------------------------------------------------------------
# 3. SMS_3 returns 50.0 when fewer than 3 speed readings exist
# ---------------------------------------------------------------------------

def test_sms3_insufficient_speeds(calc):
    calc.add_serve_point(make_point(1, True, 210.0, 0))
    calc.add_serve_point(make_point(1, True, 205.0, 1))
    # Only 2 speed readings — SMS_3 must default to 50.0
    sms3 = calc._sms3(calc._serve_points)
    assert sms3 == 50.0


def test_sms3_no_speeds(calc):
    calc.add_serve_point(make_point(1, True, None, 0))
    calc.add_serve_point(make_point(1, False, None, 1))
    assert calc._sms3(calc._serve_points) == 50.0


# ---------------------------------------------------------------------------
# 4. SMS_3 above 50 when serving faster than match average
# ---------------------------------------------------------------------------

def test_sms3_above_average_speed(calc):
    """Recent serves faster than match average → SMS_3 > 50."""
    # Establish a lower baseline: 10 slower serves
    for i in range(10):
        calc.add_serve_point(make_point(1, True, 180.0, i))
    # Recent 5 serves notably faster
    for i in range(5):
        calc.add_serve_point(make_point(1, True, 220.0, 10 + i))

    sms3 = calc._sms3(calc._serve_points)
    assert sms3 > 50.0, f"Expected SMS_3 > 50.0 when accelerating, got {sms3:.2f}"


# ---------------------------------------------------------------------------
# 5. SMS_3 below 50 when serving slower than match average (fatigue signal)
# ---------------------------------------------------------------------------

def test_sms3_below_average_speed(calc):
    """Recent serves slower than match average → SMS_3 < 50."""
    # Establish a higher baseline: 10 faster serves
    for i in range(10):
        calc.add_serve_point(make_point(1, True, 220.0, i))
    # Recent 5 serves notably slower
    for i in range(5):
        calc.add_serve_point(make_point(1, True, 165.0, 10 + i))

    sms3 = calc._sms3(calc._serve_points)
    assert sms3 < 50.0, f"Expected SMS_3 < 50.0 when decelerating, got {sms3:.2f}"


# ---------------------------------------------------------------------------
# 6. SMS_4 returns 50.0 when no games have been served
# ---------------------------------------------------------------------------

def test_sms4_no_games(calc):
    assert calc._sms4() == 50.0


# ---------------------------------------------------------------------------
# 7. SMS_4 higher for clean 4-point holds than grinding 8-point holds
# ---------------------------------------------------------------------------

def test_sms4_clean_vs_grinding():
    calc_clean = SMSCalculator()
    calc_grind = SMSCalculator()

    # Three clean 4-point holds
    for _ in range(3):
        calc_clean.add_completed_game({"points_played": 4, "held": True})

    # Three grinding 8-point holds
    for _ in range(3):
        calc_grind.add_completed_game({"points_played": 8, "held": True})

    assert calc_clean._sms4() > calc_grind._sms4(), (
        f"Clean holds ({calc_clean._sms4():.2f}) should outscore "
        f"grinding holds ({calc_grind._sms4():.2f})"
    )


# ---------------------------------------------------------------------------
# 8. compute() returns a value in [0, 100]
# ---------------------------------------------------------------------------

def test_compute_in_range():
    calc = SMSCalculator()
    for i in range(20):
        won = i % 3 != 0
        serve = 1 if i % 4 != 0 else 2
        calc.add_serve_point(make_point(serve, won, 190.0 + i * 0.5, i))
    calc.add_completed_game({"points_played": 5, "held": True})
    calc.add_completed_game({"points_played": 6, "held": False})

    result = calc.compute(n=15)
    assert 0.0 <= result <= 100.0, f"SMS out of range: {result}"


# ---------------------------------------------------------------------------
# 9. Archetype weight > 1.0 increases the score (up to 100 ceiling)
# ---------------------------------------------------------------------------

def test_archetype_weight_increases_score():
    def build_calc(weight):
        c = SMSCalculator(archetype_weight=weight)
        for i in range(15):
            c.add_serve_point(make_point(1, True, 200.0, i))
        for i in range(5):
            c.add_serve_point(make_point(2, True, 160.0, 15 + i))
        c.add_completed_game({"points_played": 4, "held": True})
        return c

    s1 = build_calc(1.0).compute()
    s2 = build_calc(1.5).compute()
    # Either s2 is strictly higher, or both hit the 100 ceiling
    assert s2 >= s1, f"weight=1.5 ({s2:.2f}) should be >= weight=1.0 ({s1:.2f})"
    if s1 < 100.0:
        assert s2 > s1


# ---------------------------------------------------------------------------
# 10. reset_set() clears game history but rolling serve points remain
# ---------------------------------------------------------------------------

def test_reset_set_preserves_serve_points():
    calc = SMSCalculator()
    for i in range(10):
        calc.add_serve_point(make_point(1, True, 210.0, i))
    calc.add_completed_game({"points_played": 4, "held": True})
    calc.add_completed_game({"points_played": 6, "held": True})

    calc.reset_set()

    # Games cleared → SMS_4 reverts to 50.0 (no games)
    assert calc._sms4() == 50.0, "reset_set should clear completed games"
    # Serve points still present
    assert len(calc._serve_points) == 10, "reset_set should not clear serve points"


# ---------------------------------------------------------------------------
# 11. Dominant server vs struggling server
# ---------------------------------------------------------------------------

def test_dominant_vs_struggling_server():
    """
    A player winning all 1st serves and holding cleanly should score
    higher than one losing most serves and grinding long games.
    """
    dominant = SMSCalculator()
    struggling = SMSCalculator()

    # Dominant: wins every point on serve, serves fast and consistently
    for i in range(10):
        dominant.add_serve_point(make_point(1, True, 210.0, i))
    for i in range(5):
        dominant.add_serve_point(make_point(2, True, 165.0, 10 + i))
    dominant.add_completed_game({"points_played": 4, "held": True})
    dominant.add_completed_game({"points_played": 4, "held": True})

    # Struggling: loses most points, speed dropping, breaks conceded
    for i in range(10):
        struggling.add_serve_point(make_point(1, False, 180.0, i))
    for i in range(5):
        struggling.add_serve_point(make_point(2, False, 145.0, 10 + i))
    # Speed dropping in recent points
    for i in range(5):
        struggling.add_serve_point(make_point(1, False, 155.0, 15 + i))
    struggling.add_completed_game({"points_played": 10, "held": False})
    struggling.add_completed_game({"points_played": 9, "held": False})

    score_dom = dominant.compute()
    score_str = struggling.compute()
    assert score_dom > score_str, (
        f"Dominant server ({score_dom:.2f}) should outscore "
        f"struggling server ({score_str:.2f})"
    )
