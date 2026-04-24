#!/usr/bin/env python3
"""
Tests for src/temporal_engine.py (Component 5A — Temporal Engine)

Run with:
    python -m pytest tests/test_temporal_engine.py -v
"""

import math
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.temporal_engine import TemporalEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NEUTRAL = {"nmi": 50.0, "sms": 50.0, "rms": 50.0, "pms": 50.0, "gps": 50.0}
ALL_100 = {"nmi": 100.0, "sms": 100.0, "rms": 100.0, "pms": 100.0, "gps": 100.0}
ALL_0   = {"nmi": 0.0,   "sms": 0.0,   "rms": 0.0,   "pms": 0.0,   "gps": 0.0}


@pytest.fixture
def engine():
    return TemporalEngine()


# ---------------------------------------------------------------------------
# 1. Cold start: D_A=50, D_B=50, delta=0
# ---------------------------------------------------------------------------

def test_cold_start(engine):
    result = engine.update(NEUTRAL, NEUTRAL, point_index=0)
    assert result["D_A"] == pytest.approx(50.0), f"Expected D_A=50, got {result['D_A']}"
    assert result["D_B"] == pytest.approx(50.0), f"Expected D_B=50, got {result['D_B']}"
    assert result["delta"] == pytest.approx(0.0), f"Expected delta=0, got {result['delta']}"


# ---------------------------------------------------------------------------
# 2. Player with all signals at 100 scores higher than one at 50
# ---------------------------------------------------------------------------

def test_dominant_player_scores_higher(engine):
    # Feed several points with A dominating, GPS flip: A's gps used for B, B's gps for A
    # Set both gps=50 so the flip is neutral; A's other signals are all 100.
    sig_a = {"nmi": 100.0, "sms": 100.0, "rms": 100.0, "pms": 100.0, "gps": 50.0}
    sig_b = {"nmi": 50.0,  "sms": 50.0,  "rms": 50.0,  "pms": 50.0,  "gps": 50.0}

    result = None
    for i in range(10):
        result = engine.update(sig_a, sig_b, point_index=i)

    assert result["D_A"] > result["D_B"], (
        f"D_A ({result['D_A']:.2f}) should exceed D_B ({result['D_B']:.2f})"
    )
    assert result["delta"] > 0.0


# ---------------------------------------------------------------------------
# 3. Recency decay: old observations contribute less than recent ones
# ---------------------------------------------------------------------------

def test_recency_decay():
    """
    Decay is observable only when there are at least two observations at
    different indices.  With a single observation the weighted average always
    equals that observation's value (the weight cancels in the ratio).

    Scenario: old low-score point at index 0, recent high-score point at index 8.
    At current_point_index=8:
      tau=0  (recent)  → weight=1.00   composite ≈ 100
      tau=8  (old)     → weight=0.25   composite ≈ 0
      weighted avg = (1.0*~100 + 0.25*~0) / 1.25 = 80
    """
    sig_high = {"nmi": 100.0, "sms": 100.0, "rms": 100.0, "pms": 100.0, "gps": 0.0}
    engine = TemporalEngine(lambda_decay=4.0)

    engine.update(ALL_0,    NEUTRAL, point_index=0)  # old, low
    engine.update(sig_high, NEUTRAL, point_index=8)  # recent, high

    score = engine._recency_layer(engine._history_a, current_point_index=8)
    assert score > 60.0, (
        f"Recent high-score point should dominate; expected > 60, got {score:.2f}"
    )

    # Reverse: old high, recent low — score should be much lower
    engine2 = TemporalEngine(lambda_decay=4.0)
    engine2.update(sig_high, NEUTRAL, point_index=0)  # old, high
    engine2.update(ALL_0,    NEUTRAL, point_index=8)  # recent, low

    score2 = engine2._recency_layer(engine2._history_a, current_point_index=8)
    assert score2 < 40.0, (
        f"Old high-score point should be discounted; expected < 40, got {score2:.2f}"
    )
    assert score > score2, "Recent high should outscore old high"


# ---------------------------------------------------------------------------
# 4. Set boundary carryover: set layer seeded with gamma * final_recency
# ---------------------------------------------------------------------------

def test_set_boundary_carryover():
    engine = TemporalEngine(gamma=0.20)

    sig_a = {"nmi": 80.0, "sms": 80.0, "rms": 80.0, "pms": 80.0, "gps": 50.0}
    for i in range(5):
        engine.update(sig_a, NEUTRAL, point_index=i)

    # Capture the recency score just before the boundary
    last_idx = engine._history_a[-1][0]
    final_rec_a = engine._recency_layer(engine._history_a, last_idx)

    engine.handle_set_boundary()

    # After the boundary the set layer should contain exactly one carryover point
    assert len(engine._set_a) == 1, (
        f"Expected 1 carryover point in set_a, got {len(engine._set_a)}"
    )
    expected_carryover = 0.20 * final_rec_a
    assert engine._set_a[0] == pytest.approx(expected_carryover, rel=1e-6), (
        f"Expected carryover {expected_carryover:.4f}, got {engine._set_a[0]:.4f}"
    )
    # Point history for recency layer should be cleared
    assert engine._history_a == []


# ---------------------------------------------------------------------------
# 5. GPS perspective flip: high opponent GPS raises Player A's dominance
# ---------------------------------------------------------------------------

def test_gps_perspective_flip():
    """
    Player B has GPS=100 (server under max pressure).
    Player A has GPS=0 (serving cleanly).
    After the flip, A's composite uses B's GPS=100 (positive for A).
    """
    engine_flip    = TemporalEngine()
    engine_no_flip = TemporalEngine()

    sig_a_base = {"nmi": 50.0, "sms": 50.0, "rms": 50.0, "pms": 50.0, "gps": 0.0}
    sig_b_high_gps = {"nmi": 50.0, "sms": 50.0, "rms": 50.0, "pms": 50.0, "gps": 100.0}
    sig_b_low_gps  = {"nmi": 50.0, "sms": 50.0, "rms": 50.0, "pms": 50.0, "gps": 0.0}

    result_with_pressure   = engine_flip.update(sig_a_base, sig_b_high_gps, point_index=0)
    result_without_pressure = engine_no_flip.update(sig_a_base, sig_b_low_gps, point_index=0)

    assert result_with_pressure["D_A"] > result_without_pressure["D_A"], (
        f"A's dominance should be higher when B's GPS is high (flip): "
        f"{result_with_pressure['D_A']:.2f} vs {result_without_pressure['D_A']:.2f}"
    )


# ---------------------------------------------------------------------------
# 6. Archetype weights: doubling NMI weight increases NMI's influence
# ---------------------------------------------------------------------------

def test_archetype_weight_nmi_influence():
    """
    Player A has NMI=100 and all other signals at 0.
    With default weights, composite = (1*100 + 4*0) / 5 = 20.
    With NMI weight=2, composite = (2*100 + 4*0) / 6 ≈ 33.3.
    Higher NMI weight should produce a higher dominance score.
    """
    sig_a = {"nmi": 100.0, "sms": 0.0, "rms": 0.0, "pms": 0.0, "gps": 50.0}
    # gps=50 on both sides so the flip is neutral

    engine_default = TemporalEngine()
    engine_boosted = TemporalEngine()

    w_default = {"nmi": 1.0, "sms": 1.0, "rms": 1.0, "pms": 1.0, "gps": 1.0}
    w_boosted = {"nmi": 2.0, "sms": 1.0, "rms": 1.0, "pms": 1.0, "gps": 1.0}

    r_default = engine_default.update(sig_a, NEUTRAL, point_index=0,
                                      archetype_weights_a=w_default)
    r_boosted = engine_boosted.update(sig_a, NEUTRAL, point_index=0,
                                      archetype_weights_a=w_boosted)

    assert r_boosted["D_A"] > r_default["D_A"], (
        f"Boosted NMI weight should raise D_A: "
        f"default={r_default['D_A']:.4f}, boosted={r_boosted['D_A']:.4f}"
    )


# ---------------------------------------------------------------------------
# 7. Delta direction: A dominant → delta > 0; B dominant → delta < 0
# ---------------------------------------------------------------------------

def test_delta_direction():
    engine_a = TemporalEngine()
    engine_b = TemporalEngine()

    sig_strong = {"nmi": 90.0, "sms": 90.0, "rms": 90.0, "pms": 90.0, "gps": 50.0}
    sig_weak   = {"nmi": 10.0, "sms": 10.0, "rms": 10.0, "pms": 10.0, "gps": 50.0}

    result_a_dom = None
    result_b_dom = None
    for i in range(5):
        result_a_dom = engine_a.update(sig_strong, sig_weak, point_index=i)
        result_b_dom = engine_b.update(sig_weak, sig_strong, point_index=i)

    assert result_a_dom["delta"] > 0, (
        f"A dominant: expected delta > 0, got {result_a_dom['delta']:.4f}"
    )
    assert result_b_dom["delta"] < 0, (
        f"B dominant: expected delta < 0, got {result_b_dom['delta']:.4f}"
    )


# ---------------------------------------------------------------------------
# 8. After reset(), cold start defaults are restored
# ---------------------------------------------------------------------------

def test_reset_restores_cold_start(engine):
    sig_a = {"nmi": 80.0, "sms": 80.0, "rms": 80.0, "pms": 80.0, "gps": 50.0}
    for i in range(5):
        engine.update(sig_a, NEUTRAL, point_index=i)

    engine.reset()

    result = engine.update(NEUTRAL, NEUTRAL, point_index=0)
    assert result["D_A"] == pytest.approx(50.0)
    assert result["D_B"] == pytest.approx(50.0)
    assert result["delta"] == pytest.approx(0.0)
