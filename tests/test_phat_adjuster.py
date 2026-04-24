#!/usr/bin/env python3
"""
Tests for src/phat_adjuster.py (Component 5B — p-hat Adjuster)

Run with:
    python -m pytest tests/test_phat_adjuster.py -v
"""

import math
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.phat_adjuster import PhatAdjuster


@pytest.fixture
def adj():
    return PhatAdjuster()


# ---------------------------------------------------------------------------
# 1. delta=0: p_hat equals p0 (sigmoid(0)=0.5 → adjustment = k*(2*0.5-1) = 0)
# ---------------------------------------------------------------------------

def test_delta_zero_neutral(adj):
    result = adj.adjust(p0_A=0.55, p0_B=0.45, delta=0.0)
    assert result["p_hat_A"] == pytest.approx(0.55)
    assert result["p_hat_B"] == pytest.approx(0.45)


def test_delta_zero_equal_baselines(adj):
    result = adj.adjust(p0_A=0.50, p0_B=0.50, delta=0.0)
    assert result["p_hat_A"] == pytest.approx(0.50)
    assert result["p_hat_B"] == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# 2. Positive delta raises p_hat_A and lowers p_hat_B
# ---------------------------------------------------------------------------

def test_positive_delta_raises_A_lowers_B(adj):
    r0 = adj.adjust(0.52, 0.48, delta=0.0)
    r1 = adj.adjust(0.52, 0.48, delta=30.0)
    assert r1["p_hat_A"] > r0["p_hat_A"], "Positive delta should raise p_hat_A"
    assert r1["p_hat_B"] < r0["p_hat_B"], "Positive delta should lower p_hat_B"


# ---------------------------------------------------------------------------
# 3. Negative delta raises p_hat_B and lowers p_hat_A
# ---------------------------------------------------------------------------

def test_negative_delta_raises_B_lowers_A(adj):
    r0 = adj.adjust(0.52, 0.48, delta=0.0)
    r1 = adj.adjust(0.52, 0.48, delta=-30.0)
    assert r1["p_hat_A"] < r0["p_hat_A"], "Negative delta should lower p_hat_A"
    assert r1["p_hat_B"] > r0["p_hat_B"], "Negative delta should raise p_hat_B"


# ---------------------------------------------------------------------------
# 4. Large positive delta clips p_hat_A to 0.85 ceiling
# ---------------------------------------------------------------------------

def test_large_positive_delta_clips_high():
    # p0_A=0.80 + k=0.08 would be 0.88 without clipping
    adj = PhatAdjuster(k=0.08, sigma=25.0)
    result = adj.adjust(p0_A=0.80, p0_B=0.20, delta=100.0)
    assert result["p_hat_A"] == pytest.approx(0.85), (
        f"Expected p_hat_A clipped to 0.85, got {result['p_hat_A']}"
    )


# ---------------------------------------------------------------------------
# 5. Large negative delta clips p_hat_A to 0.35 floor
# ---------------------------------------------------------------------------

def test_large_negative_delta_clips_low():
    # p0_A=0.40 - k=0.08 would be 0.32 without clipping
    adj = PhatAdjuster(k=0.08, sigma=25.0)
    result = adj.adjust(p0_A=0.40, p0_B=0.60, delta=-100.0)
    assert result["p_hat_A"] == pytest.approx(0.35), (
        f"Expected p_hat_A clipped to 0.35, got {result['p_hat_A']}"
    )


# ---------------------------------------------------------------------------
# 6. Symmetry: A's adjustment at delta=X equals B's adjustment at delta=-X
# ---------------------------------------------------------------------------

def test_adjustment_symmetry(adj):
    delta = 40.0
    p0 = 0.50   # equal baselines so adjustments are directly comparable

    r_pos = adj.adjust(p0, p0, delta= delta)
    r_neg = adj.adjust(p0, p0, delta=-delta)

    adj_A_pos = r_pos["p_hat_A"] - p0
    adj_B_neg = r_neg["p_hat_B"] - p0

    assert adj_A_pos == pytest.approx(adj_B_neg, abs=1e-9), (
        f"A's adjustment at +delta ({adj_A_pos:.6f}) should equal "
        f"B's adjustment at -delta ({adj_B_neg:.6f})"
    )


# ---------------------------------------------------------------------------
# 7. Adjustment magnitude never exceeds k in absolute terms
# ---------------------------------------------------------------------------

def test_adjustment_bounded_by_k():
    adj = PhatAdjuster(k=0.08, sigma=25.0)
    # Use a p0 safely away from the clips so the clip doesn't interfere
    p0 = 0.60
    for delta in (-100.0, -50.0, -10.0, 0.0, 10.0, 50.0, 100.0):
        result = adj.adjust(p0, 1.0 - p0, delta)
        raw_adj_A = result["p_hat_A"] - p0
        assert abs(raw_adj_A) <= adj.k + 1e-9, (
            f"delta={delta}: |adjustment| {abs(raw_adj_A):.6f} exceeds k={adj.k}"
        )
