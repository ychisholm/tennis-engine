#!/usr/bin/env python3
"""
Tests for src/signals/rms.py (Component 4C — Return Module Score)

Run with:
    python -m pytest tests/test_rms.py -v
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.signals.nmi import NMICalculator
from src.engine.signals.rms import RMSCalculator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_rp(won: bool, point_index: int = 0) -> dict:
    return {"won": won, "point_index": point_index}


def blank_nmi() -> NMICalculator:
    """NMICalculator with no games added — NMI will return 0."""
    return NMICalculator()


def high_pressure_nmi(game_index: int = 0) -> NMICalculator:
    """NMICalculator primed with a maximum-pressure game."""
    nmi = NMICalculator()
    nmi.add_game({
        "points": ["0-0", "0-15", "0-30", "0-40", "15-40", "30-40",
                   "deuce", "deuce-ad-receiver", "deuce", "ad-server", "game"],
        "game_index": game_index,
        "converted": False,
    })
    return nmi


@pytest.fixture
def rms_blank():
    """RMSCalculator with an empty NMI."""
    return RMSCalculator(nmi=blank_nmi())


# ---------------------------------------------------------------------------
# 1. RMS_1 uses only the last n return points
# ---------------------------------------------------------------------------

def test_rms1_rolling_window():
    """Old losses outside the window must not depress RMS_1."""
    calc = RMSCalculator(nmi=blank_nmi())
    # 20 old losses (outside n=15)
    for i in range(20):
        calc.add_return_point(make_rp(False, i))
    # 15 wins fill the window
    for i in range(15):
        calc.add_return_point(make_rp(True, 20 + i))

    assert calc._rms1(n=15) == 100.0


# ---------------------------------------------------------------------------
# 2. RMS_1 defaults to 50.0 when no return points exist
# ---------------------------------------------------------------------------

def test_rms1_default_no_points(rms_blank):
    assert rms_blank._rms1() == 50.0


# ---------------------------------------------------------------------------
# 3. RMS_2 returns 50.0 when no break points have been faced
# ---------------------------------------------------------------------------

def test_rms2_default_no_bp(rms_blank):
    assert rms_blank._rms2() == 50.0


# ---------------------------------------------------------------------------
# 4. RMS_2 reflects break conversion rate correctly
# ---------------------------------------------------------------------------

def test_rms2_conversion_rate():
    calc = RMSCalculator(nmi=blank_nmi())
    calc.record_break_point_opportunity(converted=True)
    calc.record_break_point_opportunity(converted=True)
    calc.record_break_point_opportunity(converted=False)
    calc.record_break_point_opportunity(converted=False)
    # 2 of 4 converted = 50 %
    assert calc._rms2() == pytest.approx(50.0)

    calc2 = RMSCalculator(nmi=blank_nmi())
    calc2.record_break_point_opportunity(converted=True)
    calc2.record_break_point_opportunity(converted=True)
    calc2.record_break_point_opportunity(converted=True)
    # 3 of 3 converted = 100 %
    assert calc2._rms2() == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 5. RMS_3 pulls directly from the NMI instance
# ---------------------------------------------------------------------------

def test_rms3_uses_nmi():
    """Calculator with a high-pressure NMI game should score higher on RMS_3."""
    nmi_empty = blank_nmi()
    nmi_full = high_pressure_nmi(game_index=0)

    calc_empty = RMSCalculator(nmi=nmi_empty)
    calc_full = RMSCalculator(nmi=nmi_full)

    rms3_empty = nmi_empty.compute(current_game_index=0)
    rms3_full = nmi_full.compute(current_game_index=0)

    assert rms3_empty == 0.0, "Empty NMI should return 0.0"
    assert rms3_full == 100.0, "Single high-pressure game should be the running max → 100"

    # Verify the difference flows through to compute()
    score_empty = calc_empty.compute(current_game_index=0)
    score_full = calc_full.compute(current_game_index=0)
    assert score_full > score_empty


# ---------------------------------------------------------------------------
# 6. NMI weight (0.40) dominates — max NMI with neutral others scores > 50
# ---------------------------------------------------------------------------

def test_nmi_dominates_when_maxed():
    """
    RMS_1=50, RMS_2=50, RMS_3=100 →
    RMS = 0.35×50 + 0.25×50 + 0.40×100 = 17.5 + 12.5 + 40 = 70 > 50.
    """
    nmi = high_pressure_nmi(game_index=0)
    calc = RMSCalculator(nmi=nmi)
    # No return points (RMS_1=50) and no break point opportunities (RMS_2=50)
    score = calc.compute(current_game_index=0)
    assert score == pytest.approx(70.0), f"Expected 70.0, got {score:.4f}"


# ---------------------------------------------------------------------------
# 7. compute() returns a value in [0, 100]
# ---------------------------------------------------------------------------

def test_compute_in_range():
    nmi = NMICalculator()
    nmi.add_game({
        "points": ["0-0", "15-0", "15-15", "15-30", "15-40", "30-40", "deuce", "game"],
        "game_index": 0,
        "converted": False,
    })
    calc = RMSCalculator(nmi=nmi)
    for i in range(20):
        calc.add_return_point(make_rp(i % 3 != 0, i))
    for _ in range(3):
        calc.record_break_point_opportunity(converted=True)
    for _ in range(2):
        calc.record_break_point_opportunity(converted=False)

    result = calc.compute(current_game_index=2)
    assert 0.0 <= result <= 100.0


# ---------------------------------------------------------------------------
# 8. Archetype weight 2.0 increases score (up to 100 ceiling)
# ---------------------------------------------------------------------------

def test_archetype_weight_scales_score():
    def build(weight):
        nmi = NMICalculator()
        nmi.add_game({
            "points": ["0-0", "0-15", "0-30", "0-40", "game"],
            "game_index": 0,
            "converted": False,
        })
        c = RMSCalculator(nmi=nmi, archetype_weight=weight)
        for i in range(10):
            c.add_return_point(make_rp(i % 2 == 0, i))
        c.record_break_point_opportunity(converted=True)
        c.record_break_point_opportunity(converted=False)
        return c

    s1 = build(1.0).compute(current_game_index=1)
    s2 = build(2.0).compute(current_game_index=1)
    assert s2 >= s1
    if s1 < 100.0:
        assert s2 > s1


# ---------------------------------------------------------------------------
# 9. reset_set() clears return points and break counters
# ---------------------------------------------------------------------------

def test_reset_set():
    calc = RMSCalculator(nmi=blank_nmi())
    for i in range(10):
        calc.add_return_point(make_rp(True, i))
    calc.record_break_point_opportunity(converted=True)
    calc.record_break_point_opportunity(converted=True)

    calc.reset_set()

    assert calc._return_points == []
    assert calc._break_points_faced == 0
    assert calc._breaks_converted == 0
    assert calc._rms1() == 50.0
    assert calc._rms2() == 50.0


# ---------------------------------------------------------------------------
# 10. Strong returner vs weak returner end-to-end
# ---------------------------------------------------------------------------

def test_strong_vs_weak_returner():
    """
    Strong: wins 80% of return points, converts breaks, high NMI pressure.
    Weak:   wins 20% of return points, no conversions, no NMI pressure.
    """
    # Strong returner
    nmi_strong = NMICalculator()
    nmi_strong.add_game({
        "points": ["0-0", "0-15", "0-30", "0-40", "15-40", "30-40",
                   "deuce", "deuce-ad-receiver", "deuce", "ad-server", "game"],
        "game_index": 0,
        "converted": False,
    })
    strong = RMSCalculator(nmi=nmi_strong)
    for i in range(15):
        strong.add_return_point(make_rp(i < 12, i))  # 12/15 = 80%
    for _ in range(4):
        strong.record_break_point_opportunity(converted=True)
    for _ in range(1):
        strong.record_break_point_opportunity(converted=False)

    # Weak returner
    nmi_weak = blank_nmi()
    weak = RMSCalculator(nmi=nmi_weak)
    for i in range(15):
        weak.add_return_point(make_rp(i < 3, i))  # 3/15 = 20%
    for _ in range(5):
        weak.record_break_point_opportunity(converted=False)

    score_strong = strong.compute(current_game_index=1)
    score_weak = weak.compute(current_game_index=1)
    assert score_strong > score_weak, (
        f"Strong returner ({score_strong:.2f}) should outscore "
        f"weak returner ({score_weak:.2f})"
    )
