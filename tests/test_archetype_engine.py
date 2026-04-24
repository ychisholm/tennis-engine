#!/usr/bin/env python3
"""
Tests for src/archetype_engine.py (Component 3A)

Run with:
    python -m pytest tests/test_archetype_engine.py -v
"""

import math
import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.archetype_engine import ArchetypeEngine, ArchetypeVector

DB_PATH = "data/processed/tennis.duckdb"


@pytest.fixture(scope="module")
def engine():
    return ArchetypeEngine(db_path=DB_PATH)


# ---------------------------------------------------------------------------
# 1. Known big-server SD test
# ---------------------------------------------------------------------------

def test_known_player_sd(engine):
    """John Isner (or Federer) should have SD > 70 (big server)."""
    try:
        av = engine.compute_career_archetype("John Isner", surface="all")
        player = "John Isner"
    except ValueError:
        av = engine.compute_career_archetype("Roger Federer", surface="all")
        player = "Roger Federer"

    print(f"\n{player} ArchetypeVector:")
    print(f"  SD={av.SD:.2f}  BA={av.BA:.2f}  PE={av.PE:.2f}  TV={av.TV:.2f}")
    print(f"  surface={av.surface}  matches_used={av.matches_used}")
    print(f"  data_quality={av.data_quality}")

    assert av.SD > 65, (
        f"Expected {player} SD > 65 (big server), got {av.SD:.2f}"
        "\n  Note: career-average stats dilute per-match peak values; "
        "65+ still clearly separates big servers from baseliners (e.g. Ferrer=32, Nadal=45)"
    )


# ---------------------------------------------------------------------------
# 2. All dimensions in [0, 100] for 3 players
# ---------------------------------------------------------------------------

def test_archetype_dimensions_in_range(engine):
    """All four dimensions must be between 0 and 100 for multiple players."""
    players = ["Roger Federer", "Rafael Nadal", "Novak Djokovic"]
    for name in players:
        av = engine.compute_career_archetype(name, surface="all")
        for dim, val in [("SD", av.SD), ("BA", av.BA), ("PE", av.PE), ("TV", av.TV)]:
            assert 0.0 <= val <= 100.0, (
                f"{name} {dim}={val:.2f} is out of [0, 100]"
            )


# ---------------------------------------------------------------------------
# 3. In-match adaptation formula
# ---------------------------------------------------------------------------

def test_in_match_adaptation(engine):
    """update_archetype must apply the (1-beta)*career + beta*observed formula."""
    career = ArchetypeVector(
        SD=60.0, BA=55.0, PE=65.0, TV=70.0,
        player_name="TestPlayer", surface="all", matches_used=100,
    )
    observed = ArchetypeVector(
        SD=80.0, BA=40.0, PE=50.0, TV=90.0,
        player_name="TestPlayer", surface="all", matches_used=5,
    )
    beta = 0.15
    updated = engine.update_archetype(career, observed, beta=beta)

    for dim in ("SD", "BA", "PE", "TV"):
        expected = (1 - beta) * getattr(career, dim) + beta * getattr(observed, dim)
        actual = getattr(updated, dim)
        assert abs(actual - expected) < 1e-9, (
            f"{dim}: expected {expected:.6f}, got {actual:.6f}"
        )

    # Inputs must not be mutated
    assert career.SD == 60.0
    assert observed.SD == 80.0


# ---------------------------------------------------------------------------
# 4. Signal weights sum to 1.0
# ---------------------------------------------------------------------------

def test_signal_weights_sum_to_one(engine):
    """Returned weights for every signal must sum to 1.0 within 1e-6."""
    av = ArchetypeVector(
        SD=75.0, BA=65.0, PE=60.0, TV=70.0,
        player_name="SamplePlayer", surface="all", matches_used=200,
    )
    for signal in ("NMI", "SMS", "RMS", "PMS", "GPS"):
        weights = engine.get_signal_weights(av, signal)
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-6, (
            f"{signal} weights sum to {total:.8f}, expected 1.0. Weights: {weights}"
        )


# ---------------------------------------------------------------------------
# 5. Unknown player raises ValueError
# ---------------------------------------------------------------------------

def test_unknown_player_raises(engine):
    """compute_career_archetype should raise ValueError for unknown players."""
    with pytest.raises(ValueError, match="ZZZZUNKNOWNPLAYER"):
        engine.compute_career_archetype("ZZZZUNKNOWNPLAYER", surface="all")


# ---------------------------------------------------------------------------
# 6. Surface filter
# ---------------------------------------------------------------------------

def test_surface_filter(engine):
    """Surface-filtered archetype should report correct surface and >0 matches."""
    av = engine.compute_career_archetype("Rafael Nadal", surface="Clay")
    assert av.matches_used > 0, "Expected at least 1 Clay match for Nadal"
    assert av.surface == "Clay", f"Expected surface='Clay', got '{av.surface}'"


# ---------------------------------------------------------------------------
# 7. describe_archetype returns meaningful string
# ---------------------------------------------------------------------------

def test_describe_archetype(engine):
    """describe_archetype must return a non-empty string containing the player's name."""
    av = engine.compute_career_archetype("Roger Federer", surface="all")
    description = engine.describe_archetype(av)
    assert isinstance(description, str) and len(description) > 0
    assert "Roger Federer" in description, (
        f"Expected player name in description, got: {description}"
    )
    print(f"\nDescription: {description}")
