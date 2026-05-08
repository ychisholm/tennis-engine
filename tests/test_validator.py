"""
Tests for src/verification/validator.py.

Pure-function tests — no database connection. Each test constructs a list
of StateRow objects manually and asserts on the gaps and summary returned
by validate_match. Safe to run with DATABASE_URL unset.
"""
from __future__ import annotations

from src.verification.validator import (
    StateRow,
    parse_state_row,
    validate_match,
)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _row(sets_a: int, sets_b: int, games_a: int, games_b: int,
         score_a, score_b) -> StateRow:
    return StateRow(
        sets_a=sets_a,
        sets_b=sets_b,
        games_a=games_a,
        games_b=games_b,
        score_a=str(score_a),
        score_b=str(score_b),
    )


def _love_game_a(sets_a: int, sets_b: int,
                 games_a: int, games_b: int) -> list[StateRow]:
    """Rows AFTER a 0-0 starting state for a love game won by A.

    Returns the four point-by-point rows: 15-0, 30-0, 40-0, then the row
    that shows games_a incremented and the score reset to 0-0.
    """
    return [
        _row(sets_a, sets_b, games_a, games_b, "15", "0"),
        _row(sets_a, sets_b, games_a, games_b, "30", "0"),
        _row(sets_a, sets_b, games_a, games_b, "40", "0"),
        _row(sets_a, sets_b, games_a + 1, games_b, "0", "0"),
    ]


def _love_set_end_a(sets_a: int, sets_b: int,
                    games_a: int, games_b: int) -> list[StateRow]:
    """Rows for the last (love) game of a set won by A.

    Final row shows sets_a incremented and games / score reset to 0-0.
    """
    return [
        _row(sets_a, sets_b, games_a, games_b, "15", "0"),
        _row(sets_a, sets_b, games_a, games_b, "30", "0"),
        _row(sets_a, sets_b, games_a, games_b, "40", "0"),
        _row(sets_a + 1, sets_b, 0, 0, "0", "0"),
    ]


def _clean_6_0_set_a(sets_a: int, sets_b: int) -> list[StateRow]:
    """All 24 transitions of a 6-0 set won by A, including the 0-0 start.

    First row is the (sets_a, sets_b, 0, 0, 0, 0) initial state and the
    last row is the post-set-transition row at sets_a+1.
    """
    rows = [_row(sets_a, sets_b, 0, 0, "0", "0")]
    for g in range(5):
        rows.extend(_love_game_a(sets_a, sets_b, g, 0))
    rows.extend(_love_set_end_a(sets_a, sets_b, 5, 0))
    return rows


# ---------------------------------------------------------------------------
# 1. Clean short match
# ---------------------------------------------------------------------------

def test_clean_short_match():
    """Full 6-0 single-set match, every point captured."""
    rows = _clean_6_0_set_a(0, 0)
    gaps, summary = validate_match(rows, "6-0", "match-1")

    assert gaps == []
    assert summary.verdict == "clean"
    assert summary.final_score_match is True
    assert summary.clean_set_count == 1
    assert summary.gap_count == 0
    assert summary.severity_max is None
    assert summary.live_final_score == "6-0"


# ---------------------------------------------------------------------------
# 2. Long deuce game — multiple AD-back-to-deuce oscillations
# ---------------------------------------------------------------------------

def test_long_deuce_game():
    """Deuce → AD_A → DEUCE → AD_B → DEUCE → AD_A → ... → GAME_A is legal."""
    rows = [
        _row(0, 0, 0, 0, "0",  "0"),
        _row(0, 0, 0, 0, "15", "0"),
        _row(0, 0, 0, 0, "30", "0"),
        _row(0, 0, 0, 0, "40", "0"),
        _row(0, 0, 0, 0, "40", "15"),
        _row(0, 0, 0, 0, "40", "30"),
        _row(0, 0, 0, 0, "40", "40"),  # DEUCE
        _row(0, 0, 0, 0, "AD", "40"),  # AD_A
        _row(0, 0, 0, 0, "40", "40"),  # back to DEUCE  (NOT a regression)
        _row(0, 0, 0, 0, "40", "AD"),  # AD_B
        _row(0, 0, 0, 0, "40", "40"),  # DEUCE
        _row(0, 0, 0, 0, "AD", "40"),  # AD_A
        _row(0, 0, 0, 0, "40", "40"),  # DEUCE
        _row(0, 0, 0, 0, "40", "AD"),  # AD_B
        _row(0, 0, 0, 0, "40", "40"),  # DEUCE
        _row(0, 0, 0, 0, "AD", "40"),  # AD_A
        _row(0, 0, 1, 0, "0",  "0"),   # GAME_A
    ]
    gaps, summary = validate_match(rows, "", "match-deuce")

    assert gaps == []
    assert summary.verdict == "clean"
    assert summary.gap_count == 0


# ---------------------------------------------------------------------------
# 3. Single missed point — 15-15 → 40-15
# ---------------------------------------------------------------------------

def test_single_missed_point():
    """A 2-edge jump (one unobserved intermediate state) is a single missed point."""
    rows = [
        _row(0, 0, 0, 0, "0",  "0"),
        _row(0, 0, 0, 0, "15", "0"),
        _row(0, 0, 0, 0, "15", "15"),
        _row(0, 0, 0, 0, "40", "15"),  # GAP: jump from 15-15
        _row(0, 0, 1, 0, "0",  "0"),
    ]
    gaps, summary = validate_match(rows, "", "match-single-miss")

    assert len(gaps) == 1
    assert gaps[0].gap_type == "score_jump"
    assert gaps[0].inferred_skipped_points == 1
    assert gaps[0].severity == "low"
    assert summary.verdict == "minor_gaps"


# ---------------------------------------------------------------------------
# 4. Multiple missed points
# ---------------------------------------------------------------------------

def test_multiple_missed_points():
    """A 5-edge jump (four unobserved intermediate states) yields a medium-severity gap.

    The prompt described this case as "0-0 jumps to 40-15" with assertion
    `inferred_skipped_points == 4`. The shortest path 0-0 → 40-15 in the
    legal-transition graph is only 4 edges (3 unobserved intermediates), so
    we use 0-0 → 40-30 here, whose shortest path is 5 edges (4 unobserved
    intermediates). The spirit of the test — a multi-point in-game jump
    flagged as medium severity — is preserved.
    """
    rows = [
        _row(0, 0, 0, 0, "0",  "0"),
        _row(0, 0, 0, 0, "40", "30"),  # GAP: jump from 0-0
        _row(0, 0, 1, 0, "0",  "0"),
    ]
    gaps, summary = validate_match(rows, "", "match-multi-miss")

    assert len(gaps) == 1
    assert gaps[0].gap_type == "score_jump"
    assert gaps[0].inferred_skipped_points == 4
    assert gaps[0].severity == "medium"


# ---------------------------------------------------------------------------
# 5. Score regression
# ---------------------------------------------------------------------------

def test_score_regression():
    """30-15 → 15-15 has no forward path in the legal-transition graph."""
    rows = [
        _row(0, 0, 0, 0, "30", "15"),
        _row(0, 0, 0, 0, "15", "15"),  # REGRESSION
    ]
    gaps, summary = validate_match(rows, "", "match-regression")

    assert len(gaps) == 1
    assert gaps[0].gap_type == "score_regression"
    assert gaps[0].severity == "medium"


# ---------------------------------------------------------------------------
# 6. Duplicate state
# ---------------------------------------------------------------------------

def test_duplicate_state():
    rows = [
        _row(0, 0, 0, 0, "15", "0"),
        _row(0, 0, 0, 0, "15", "0"),  # duplicate
    ]
    gaps, summary = validate_match(rows, "", "match-dup")

    assert len(gaps) == 1
    assert gaps[0].gap_type == "duplicate_state"
    assert gaps[0].severity == "low"


# ---------------------------------------------------------------------------
# 7. Clean tiebreak (7-3)
# ---------------------------------------------------------------------------

def test_clean_tiebreak():
    """Walk every tiebreak point through to a clean 7-3 finish, then the set ends."""
    rows = [
        _row(0, 0, 6, 6, "0", "0"),
        _row(0, 0, 6, 6, "1", "0"),
        _row(0, 0, 6, 6, "2", "0"),
        _row(0, 0, 6, 6, "3", "0"),
        _row(0, 0, 6, 6, "3", "1"),
        _row(0, 0, 6, 6, "4", "1"),
        _row(0, 0, 6, 6, "4", "2"),
        _row(0, 0, 6, 6, "4", "3"),
        _row(0, 0, 6, 6, "5", "3"),
        _row(0, 0, 6, 6, "6", "3"),
        _row(0, 0, 6, 6, "7", "3"),  # A wins tiebreak
        _row(1, 0, 0, 0, "0", "0"),  # set 1 ends 7-6
    ]
    gaps, summary = validate_match(rows, "7-6", "match-tb-clean")

    assert gaps == []
    assert summary.verdict == "clean"
    assert summary.live_final_score == "7-6"
    assert summary.final_score_match is True


# ---------------------------------------------------------------------------
# 8. Tiebreak with one gap
# ---------------------------------------------------------------------------

def test_tiebreak_with_gap():
    """6-6 tiebreak goes 0-0 → 1-0 → 2-0 → 4-0 (one tiebreak point unobserved)."""
    rows = [
        _row(0, 0, 6, 6, "0", "0"),
        _row(0, 0, 6, 6, "1", "0"),
        _row(0, 0, 6, 6, "2", "0"),
        _row(0, 0, 6, 6, "4", "0"),  # GAP: jump from 2-0 (3-0 unobserved)
        _row(0, 0, 6, 6, "5", "0"),
        _row(0, 0, 6, 6, "6", "0"),
        _row(0, 0, 6, 6, "7", "0"),  # A wins tiebreak
        _row(1, 0, 0, 0, "0", "0"),
    ]
    gaps, summary = validate_match(rows, "7-6", "match-tb-gap")

    assert len(gaps) == 1
    assert gaps[0].gap_type == "score_jump"
    assert gaps[0].inferred_skipped_points == 1


# ---------------------------------------------------------------------------
# 9. Game jump
# ---------------------------------------------------------------------------

def test_game_jump():
    """Games go from 2-1 to 4-1 — a whole game unobserved."""
    rows = [
        _row(0, 0, 2, 1, "0", "0"),
        _row(0, 0, 4, 1, "0", "0"),  # game_jump: A's games went up by 2
    ]
    gaps, summary = validate_match(rows, "", "match-game-jump")

    assert len(gaps) == 1
    assert gaps[0].gap_type == "game_jump"
    assert gaps[0].severity == "medium"


# ---------------------------------------------------------------------------
# 10. Set jump
# ---------------------------------------------------------------------------

def test_set_jump():
    """Sets count increments without the previous set having completed legitimately."""
    rows = [
        _row(0, 0, 3, 2, "30", "30"),
        _row(1, 0, 0, 0, "0",  "0"),  # implied games would be 4-2 — invalid set end
    ]
    gaps, summary = validate_match(rows, "3-2", "match-set-jump")

    assert len(gaps) == 1
    assert gaps[0].gap_type == "set_jump"
    assert gaps[0].severity == "high"


# ---------------------------------------------------------------------------
# 11. Final-state mismatch
# ---------------------------------------------------------------------------

def test_final_state_mismatch():
    """A clean walk that ends 6-0 vs a recorded final score of 7-5 → mismatch."""
    rows = _clean_6_0_set_a(0, 0)
    gaps, summary = validate_match(rows, "7-5", "match-final-mismatch")

    # Among the gaps, we expect a final_state_mismatch (and no other gaps,
    # since the walk through 6-0 is itself clean).
    mismatch = [g for g in gaps if g.gap_type == "final_state_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0].severity == "high"
    assert summary.verdict == "major_gaps"
    assert summary.final_score_match is False


# ---------------------------------------------------------------------------
# 12. Advantage set (no tiebreak), set ends 12-10
# ---------------------------------------------------------------------------

def test_advantage_set():
    """An advantage-set game at 11-10 closes out the set 12-10 cleanly.

    Starting deep in the advantage set keeps the test compact while still
    exercising regular-game scoring at games > 6 and a set-end at 12-10.
    """
    rows = [
        _row(0, 0, 11, 10, "0",  "0"),
        _row(0, 0, 11, 10, "15", "0"),
        _row(0, 0, 11, 10, "30", "0"),
        _row(0, 0, 11, 10, "40", "0"),
        _row(1, 0, 0,  0,  "0",  "0"),  # 12-10, set ends
    ]
    gaps, summary = validate_match(rows, "12-10", "match-adv-set")

    assert gaps == []
    assert summary.live_final_score == "12-10"
    assert summary.verdict == "clean"


# ---------------------------------------------------------------------------
# 13. Set breakdown — clean set 1, set 2 with one gap
# ---------------------------------------------------------------------------

def test_set_breakdown():
    rows = _clean_6_0_set_a(0, 0)  # 25 rows; ends at (1, 0, 0, 0, 0, 0)
    # Set 2: game 1 has a score_jump (0-0 → 30-0 skips 15-0, inferred 1)
    rows.extend([
        _row(1, 0, 0, 0, "30", "0"),  # GAP
        _row(1, 0, 0, 0, "40", "0"),
        _row(1, 0, 1, 0, "0",  "0"),
    ])
    # Games 2-5 of set 2 (clean love games)
    for g in range(1, 5):
        rows.extend(_love_game_a(1, 0, g, 0))
    # Game 6 of set 2 — set ends 6-0
    rows.extend(_love_set_end_a(1, 0, 5, 0))

    gaps, summary = validate_match(rows, "6-0 6-0", "match-breakdown")

    assert summary.clean_set_count == 1
    assert summary.gapped_set_count == 1
    assert len(summary.set_breakdown) == 2

    set1 = summary.set_breakdown[0]
    set2 = summary.set_breakdown[1]
    assert set1["set_num"] == 1
    assert set1["clean"] is True
    assert set1["gap_count"] == 0
    assert set2["set_num"] == 2
    assert set2["clean"] is False
    assert set2["gap_count"] == 1


# ---------------------------------------------------------------------------
# 14. Total inferred missing points across multiple jumps
# ---------------------------------------------------------------------------

def test_inferred_missing_points_total():
    """Three score_jumps with inferred 1, 2, 3 — summary should report 6 in total."""
    rows = [
        _row(0, 0, 0, 0, "0",  "0"),
        # Game 1 — jump 15-15 → 40-15 (path 2, inferred 1)
        _row(0, 0, 0, 0, "15", "0"),
        _row(0, 0, 0, 0, "15", "15"),
        _row(0, 0, 0, 0, "40", "15"),
        _row(0, 0, 1, 0, "0",  "0"),
        # Game 2 — jump 0-0 → 40-0 (path 3, inferred 2)
        _row(0, 0, 1, 0, "40", "0"),
        _row(0, 0, 2, 0, "0",  "0"),
        # Game 3 — jump 0-0 → 40-15 (path 4, inferred 3)
        _row(0, 0, 2, 0, "40", "15"),
        _row(0, 0, 3, 0, "0",  "0"),
    ]
    gaps, summary = validate_match(rows, "", "match-inferred-total")

    score_jumps = [g for g in gaps if g.gap_type == "score_jump"]
    assert len(score_jumps) == 3
    inferred_values = sorted(g.inferred_skipped_points for g in score_jumps)
    assert inferred_values == [1, 2, 3]
    assert summary.inferred_missing_points == 6


# ---------------------------------------------------------------------------
# parse_state_row — quick coverage of the schema-adapter contract
# ---------------------------------------------------------------------------

def test_parse_state_row_normalizes_advantage():
    """The live tracker stores advantage as 'A'; parse_state_row normalizes to 'AD'."""
    raw = {
        "home_sets_won": 1,
        "away_sets_won": 0,
        "home_current_games": 5,
        "away_current_games": 4,
        "home_current_point": "A",
        "away_current_point": "40",
        "polled_at": "2026-05-07T00:00:00Z",
    }
    parsed = parse_state_row(raw)
    assert parsed.sets_a == 1
    assert parsed.sets_b == 0
    assert parsed.games_a == 5
    assert parsed.games_b == 4
    assert parsed.score_a == "AD"
    assert parsed.score_b == "40"
    assert parsed.polled_at == "2026-05-07T00:00:00Z"
