#!/usr/bin/env python3
"""
Tests for src/live_match.py (Component 5C — LiveMatch Orchestrator)

Run with:
    python -m pytest tests/test_live_match.py -v
"""

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.live_match import LiveMatch

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

PLAYER_A = {
    "name": "Isner",
    "p0_hard": 0.65, "p0_clay": 0.60, "p0_grass": 0.67,
    "archetype": {"sd": 80, "ba": 45, "pe": 55, "tv": 60},
}
PLAYER_B = {
    "name": "Nadal",
    "p0_hard": 0.62, "p0_clay": 0.64, "p0_grass": 0.60,
    "archetype": {"sd": 55, "ba": 80, "pe": 85, "tv": 75},
}


def make_match(**kwargs) -> LiveMatch:
    return LiveMatch(PLAYER_A, PLAYER_B, **kwargs)


def _pt(winner="A", rally=3, speed=None, first=True, serving=None) -> dict:
    d = {"winner": winner, "rally_length": rally, "is_first_serve": first}
    if speed is not None:
        d["serve_speed"] = speed
    if serving is not None:
        d["serving"] = serving
    return d


def _win_game(match: LiveMatch, winner: str, server: str | None = None) -> dict:
    """Win a clean 4-point game for *winner*."""
    result = None
    for _ in range(4):
        result = match.process_point(_pt(winner=winner, serving=server))
    return result


def _win_set(match: LiveMatch, winner: str, loser: str) -> None:
    """Win a 6-0 set for *winner*."""
    for _ in range(6):
        _win_game(match, winner)


# ---------------------------------------------------------------------------
# 1. Initialisation
# ---------------------------------------------------------------------------

def test_init_no_errors():
    m = make_match()
    assert m._sets_a == 0
    assert m._sets_b == 0
    assert m._games_a == 0
    assert m._games_b == 0
    assert m._sp == 0
    assert m._rp == 0
    assert m._set_number == 1
    assert m._point_index == 0
    assert m._serving == "A"
    assert m._match_over is False
    assert m._winner is None


# ---------------------------------------------------------------------------
# 2. process_point returns all required output schema keys
# ---------------------------------------------------------------------------

REQUIRED_TOP = {"timestamp", "match_state", "dominance", "adjusted_p",
                "probabilities", "match_over", "winner"}
REQUIRED_STATE = {"sets_A", "sets_B", "games_A", "games_B",
                  "points_A", "points_B", "serving_player",
                  "set_number", "game_number"}
REQUIRED_DOM   = {"D_A", "D_B", "delta", "breakdown_A", "breakdown_B"}
REQUIRED_PROBS = {"P_game_A", "P_set_A", "P_match_A"}

def test_output_schema():
    m = make_match()
    r = m.process_point(_pt())
    assert REQUIRED_TOP    <= r.keys()
    assert REQUIRED_STATE  <= r["match_state"].keys()
    assert REQUIRED_DOM    <= r["dominance"].keys()
    assert REQUIRED_PROBS  <= r["probabilities"].keys()
    assert set(r["dominance"]["breakdown_A"]) == {"nmi", "sms", "rms", "pms", "gps"}


# ---------------------------------------------------------------------------
# 3. Score progression: 0→15→30→40 (no game won yet)
# ---------------------------------------------------------------------------

def test_score_progression():
    m = make_match()
    scores = []
    for _ in range(3):
        r = m.process_point(_pt(winner="A"))
        scores.append(r["match_state"]["points_A"])
    # After 1 point: A has 1 (= "15"), after 2: 2 (= "30"), after 3: 3 (= "40")
    assert scores == [1, 2, 3], f"Expected [1,2,3], got {scores}"
    # No game completed yet
    assert m._games_a == 0


# ---------------------------------------------------------------------------
# 4. Game win: 4 points resets score and advances game count
# ---------------------------------------------------------------------------

def test_game_win_resets_score():
    m = make_match()
    r = _win_game(m, "A")
    assert m._games_a == 1, "A should have 1 game after winning 4 points"
    assert m._sp == 0 and m._rp == 0, "Point score should reset after game"
    assert r["match_state"]["games_A"] == 1


def test_game_number_increments():
    m = make_match()
    before = m._games_a + m._games_b + 1  # game 1
    _win_game(m, "A")
    after = m._games_a + m._games_b + 1   # game 2
    assert after == before + 1


# ---------------------------------------------------------------------------
# 5. Set boundary: set_number increments, carryover fires (D ≠ cold start)
# ---------------------------------------------------------------------------

def test_set_boundary_fires():
    m = make_match()
    _win_set(m, "A", "B")  # A wins set 6-0
    assert m._sets_a == 1
    assert m._set_number == 2
    assert m._games_a == 0 and m._games_b == 0, "Games reset at set boundary"

def test_set_boundary_carryover_nonzero():
    """After carryover, the temporal engine set layer should be seeded > 0."""
    m = make_match()
    # Feed some dominant points before the set ends
    for _ in range(20):
        m.process_point(_pt(winner="A", speed=210.0))
    # Win the set for A (may need more points if score isn't 6-0 yet)
    while m._sets_a == 0:
        m.process_point(_pt(winner="A"))
    # After set boundary the set layer is seeded with carryover, not 0
    assert len(m._temporal._set_a) > 0
    assert m._temporal._set_a[0] > 0.0, (
        f"Carryover should be > 0 after a dominant set, got {m._temporal._set_a[0]}"
    )


# ---------------------------------------------------------------------------
# 6. Serve rotation: serving_player alternates each game
# ---------------------------------------------------------------------------

def test_serve_rotation():
    m = make_match()
    assert m._serving == "A"
    _win_game(m, "A")          # game 1 — A serves
    assert m._serving == "B", "B should serve game 2"
    _win_game(m, "B")          # game 2 — B serves
    assert m._serving == "A", "A should serve game 3"


# ---------------------------------------------------------------------------
# 7. GPS perspective: server under pressure raises receiver's D score
# ---------------------------------------------------------------------------

def test_gps_perspective():
    """
    Force a long service game for B (8 points: reaches deuce then B holds).
    GPS pressure is only non-zero for games > 4 points, so a clean 4-0 hold
    contributes nothing — we need a game that stretches to deuce.

    Sequence: B,A,B,A,B,A → 3-3 deuce (6 pts), then B,B → ad-server, game (8 pts).
    game_pressure(8) = 4  →  GPS_b > 0  →  temporal engine flip boosts A's composite.
    """
    m = make_match()
    long_game = ["B", "A", "B", "A", "B", "A", "B", "B"]
    for winner in long_game:
        m.process_point(_pt(winner=winner, serving="B"))

    r = m.process_point(_pt(winner="A", serving="A"))
    d = r["dominance"]
    assert d["breakdown_B"]["gps"] > 0.0, (
        f"B's GPS should be > 0 after an 8-point service game, got {d['breakdown_B']['gps']}"
    )


# ---------------------------------------------------------------------------
# 8. p_hat responds to dominance after dominant run for A
# ---------------------------------------------------------------------------

def test_phat_responds_to_dominance():
    m = make_match()
    p0_A = PLAYER_A["p0_hard"]

    # Feed 15 dominant points for A (A wins everything, high rally wins)
    for i in range(15):
        m.process_point(_pt(winner="A", rally=10, speed=215.0))

    r = m.process_point(_pt(winner="A", rally=10, speed=215.0))
    p_hat_A = r["adjusted_p"]["p_hat_A"]
    assert p_hat_A >= p0_A, (
        f"p_hat_A ({p_hat_A:.4f}) should be >= p0_A ({p0_A:.4f}) after dominant run"
    )


# ---------------------------------------------------------------------------
# 9. Match over after 2 sets won (best_of=3)
# ---------------------------------------------------------------------------

def test_match_over_after_two_sets():
    m = make_match(best_of=3)
    _win_set(m, "A", "B")  # Set 1: A wins 6-0
    assert m._match_over is False, "Match not over after 1 set"
    _win_set(m, "A", "B")  # Set 2: A wins 6-0
    assert m._match_over is True
    assert m._winner == "A"

def test_match_over_flag_in_output():
    m = make_match(best_of=3)
    _win_set(m, "A", "B")
    _win_set(m, "A", "B")
    # Last point of the match should show match_over=True
    # (match_over is set when the winning point is processed)
    # Process one more point — should raise since match is over
    with pytest.raises(RuntimeError):
        m.process_point(_pt())


# ---------------------------------------------------------------------------
# 10. Tiebreak triggers at 6-6
# ---------------------------------------------------------------------------

def test_tiebreak_triggers_at_6_6():
    m = make_match()
    # Create a 6-6 set: alternate game wins A-B-A-B-A-B-A-B-A-B-A-B
    for i in range(12):
        winner = "A" if i % 2 == 0 else "B"
        _win_game(m, winner)
    assert m._games_a == 6 and m._games_b == 6, (
        f"Expected 6-6 in games, got {m._games_a}-{m._games_b}"
    )
    assert m._in_tiebreak is True, "Tiebreak should be active at 6-6"

def test_tiebreak_score_advances():
    m = make_match()
    # Get to 6-6
    for i in range(12):
        _win_game(m, "A" if i % 2 == 0 else "B")
    # Play one tiebreak point
    r = m.process_point(_pt(winner="A"))
    assert r["match_state"]["points_A"] == 1, (
        f"Expected tiebreak points_A=1 after one TB point, "
        f"got {r['match_state']['points_A']}"
    )
