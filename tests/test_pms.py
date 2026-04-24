#!/usr/bin/env python3
"""
Tests for src/signals/pms.py (Component 4D — Physical Module Score)

Run with:
    python -m pytest tests/test_pms.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.signals.pms import PMSCalculator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rp(won: bool, rally_length: int, point_index: int = 0) -> dict:
    return {"won": won, "rally_length": rally_length, "point_index": point_index}


COUNTER_PUNCHER = {"SD": 30, "PE": 90}
BIG_SERVER      = {"SD": 90, "PE": 40}
BASE_PROFILE    = None   # use base weights


@pytest.fixture
def calc():
    return PMSCalculator()


# ---------------------------------------------------------------------------
# 1. Bucket defaults to 50.0 when no points present
# ---------------------------------------------------------------------------

def test_bucket_defaults_to_50(calc):
    assert calc._bucket_win_pct(1, 3)    == 50.0, "PMS_1 should default to 50.0"
    assert calc._bucket_win_pct(4, 8)    == 50.0, "PMS_2 should default to 50.0"
    assert calc._bucket_win_pct(9, None) == 50.0, "PMS_3 should default to 50.0"


# ---------------------------------------------------------------------------
# 2. Winning all long rallies scores higher on PMS_3 than losing them
# ---------------------------------------------------------------------------

def test_pms3_win_vs_lose_long_rallies():
    calc_win  = PMSCalculator()
    calc_lose = PMSCalculator()

    for i in range(10):
        calc_win.add_rally_point(rp(True,  rally_length=12, point_index=i))
        calc_lose.add_rally_point(rp(False, rally_length=12, point_index=i))

    assert calc_win._bucket_win_pct(9, None)  == 100.0
    assert calc_lose._bucket_win_pct(9, None) == 0.0
    assert calc_win.compute() > calc_lose.compute()


# ---------------------------------------------------------------------------
# 3. All weight dicts sum to 1.0
# ---------------------------------------------------------------------------

def test_weights_sum_to_one():
    for profile in (None, COUNTER_PUNCHER, BIG_SERVER, {"SD": 50, "PE": 50}):
        calc = PMSCalculator(archetype_profile=profile)
        w = calc._get_bucket_weights()
        assert abs(sum(w.values()) - 1.0) < 1e-9, (
            f"Weights don't sum to 1.0 for profile={profile}: {w}"
        )


# ---------------------------------------------------------------------------
# 4. Counter-puncher profile gives higher weight to long rallies than base
# ---------------------------------------------------------------------------

def test_counter_puncher_long_weight():
    base   = PMSCalculator(archetype_profile=BASE_PROFILE)
    cp     = PMSCalculator(archetype_profile=COUNTER_PUNCHER)
    assert cp._get_bucket_weights()["long"] > base._get_bucket_weights()["long"], (
        "Counter-puncher should weight long rallies more than base"
    )


# ---------------------------------------------------------------------------
# 5. Big server profile gives higher weight to short rallies than base
# ---------------------------------------------------------------------------

def test_big_server_short_weight():
    base = PMSCalculator(archetype_profile=BASE_PROFILE)
    bs   = PMSCalculator(archetype_profile=BIG_SERVER)
    assert bs._get_bucket_weights()["short"] > base._get_bucket_weights()["short"], (
        "Big server should weight short rallies more than base"
    )


# ---------------------------------------------------------------------------
# 6. PMS_4 returns 100 when current speed equals set-1 speed (no fatigue)
# ---------------------------------------------------------------------------

def test_pms4_no_fatigue():
    calc = PMSCalculator()
    for _ in range(5):
        calc.add_serve_speed(200.0, is_set1=True)
    for _ in range(5):
        calc.add_serve_speed(200.0)  # same speed → no fatigue

    assert calc._pms4() == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 7. PMS_4 below 50 when current speed is meaningfully lower than set-1
# ---------------------------------------------------------------------------

def test_pms4_fatigue_present():
    # 200 → 180 km/h is a 10% drop.
    # ratio = -(20/200) = -0.10  →  pms4 = (-0.10 + 1.0) * 100 = 90.0
    # The [-1, 0] → [0, 100] scale is linear: a 50% drop reaches 50, a 100%
    # drop reaches 0.  A 10% drop should land at exactly 90.0.
    calc = PMSCalculator()
    for _ in range(5):
        calc.add_serve_speed(200.0, is_set1=True)
    for _ in range(5):
        calc.add_serve_speed(180.0)  # 10% slower than set-1 baseline

    val = calc._pms4()
    assert val < 100.0, f"Expected fatigue to reduce PMS_4 below 100, got {val:.2f}"
    assert val == pytest.approx(90.0), (
        f"10% speed drop should give PMS_4 = 90.0, got {val:.2f}"
    )


# ---------------------------------------------------------------------------
# 8. PMS_4 defaults gracefully when set1_speeds is empty
# ---------------------------------------------------------------------------

def test_pms4_no_set1_speeds(calc):
    # No set-1 baseline at all
    assert calc._pms4() == 0.0

def test_pms4_too_few_current_speeds(calc):
    # Set-1 baseline present but fewer than 3 current readings
    for _ in range(5):
        calc.add_serve_speed(200.0, is_set1=True)
    calc.add_serve_speed(198.0)   # only 1 current speed
    calc.add_serve_speed(197.0)   # still only 2
    assert calc._pms4() == 0.0


# ---------------------------------------------------------------------------
# 9. reset_set() clears rally history and current speeds but preserves set-1
# ---------------------------------------------------------------------------

def test_reset_set_preserves_set1():
    calc = PMSCalculator()
    for _ in range(5):
        calc.add_serve_speed(200.0, is_set1=True)
    for _ in range(5):
        calc.add_serve_speed(195.0)
    for i in range(6):
        calc.add_rally_point(rp(i % 2 == 0, rally_length=5, point_index=i))

    calc.reset_set()

    assert calc._rally_points == [], "reset_set should clear rally points"
    assert calc._current_set_speeds == [], "reset_set should clear current set speeds"
    assert len(calc._set1_speeds) == 5, "reset_set must not clear set-1 speeds"
    assert calc._set1_frozen, "set-1 baseline should be frozen after first reset_set"


def test_reset_set_does_not_overwrite_set1():
    """A second reset_set call must not wipe the frozen set-1 baseline."""
    calc = PMSCalculator()
    for _ in range(4):
        calc.add_serve_speed(210.0, is_set1=True)
    calc.reset_set()   # first boundary — freezes set-1

    # Simulate set 2: add new current speeds and call reset again
    for _ in range(3):
        calc.add_serve_speed(205.0)
    calc.reset_set()   # second boundary — must not overwrite set-1

    assert len(calc._set1_speeds) == 4, (
        "Second reset_set should not modify frozen set-1 speeds"
    )


# ---------------------------------------------------------------------------
# 10. Counter-puncher winning long rallies vs losing them
# ---------------------------------------------------------------------------

def test_counter_puncher_long_rally_integration():
    win  = PMSCalculator(archetype_profile=COUNTER_PUNCHER)
    lose = PMSCalculator(archetype_profile=COUNTER_PUNCHER)

    for i in range(10):
        win.add_rally_point(rp(True,  rally_length=14, point_index=i))
        lose.add_rally_point(rp(False, rally_length=14, point_index=i))

    assert win.compute() > lose.compute(), (
        "Counter-puncher winning long rallies should outscore one losing them"
    )


# ---------------------------------------------------------------------------
# 11. compute() in [0, 100] for typical mixed inputs
# ---------------------------------------------------------------------------

def test_compute_in_range():
    calc = PMSCalculator(archetype_profile={"SD": 55, "PE": 60})

    lengths = [1, 2, 3, 5, 6, 7, 10, 12, 4, 8, 9, 2, 15, 6, 3]
    for i, length in enumerate(lengths):
        calc.add_rally_point(rp(i % 3 != 0, rally_length=length, point_index=i))

    for _ in range(5):
        calc.add_serve_speed(200.0, is_set1=True)
    for _ in range(5):
        calc.add_serve_speed(192.0)

    result = calc.compute()
    assert 0.0 <= result <= 100.0, f"PMS out of range: {result:.4f}"
