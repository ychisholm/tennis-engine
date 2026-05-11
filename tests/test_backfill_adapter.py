"""
Tests for src/verification/backfill_adapter.py

Pure-function tests — no DB needed.
"""
from __future__ import annotations

from src.verification.backfill_adapter import (
    _infer_game_winner,
    backfill_points_to_state_rows,
)


def _pt(set_num, game_num, hp, ap):
    return {
        "set_num": set_num,
        "game_num": game_num,
        "home_point": hp,
        "away_point": ap,
    }


# ---------------------------------------------------------------------------
# _infer_game_winner unit tests
# ---------------------------------------------------------------------------

def test_infer_winner_regular_40():
    assert _infer_game_winner("40", "15") == "home"
    assert _infer_game_winner("15", "40") == "away"


def test_infer_winner_advantage():
    assert _infer_game_winner("AD", "40") == "home"
    assert _infer_game_winner("40", "AD") == "away"


def test_infer_winner_deuce_ambiguous():
    assert _infer_game_winner("40", "40") is None


def test_infer_winner_tiebreak():
    assert _infer_game_winner("6", "2") == "home"
    assert _infer_game_winner("4", "7") == "away"
    assert _infer_game_winner("3", "3") is None


# ---------------------------------------------------------------------------
# backfill_points_to_state_rows
# ---------------------------------------------------------------------------

def test_empty_input():
    assert backfill_points_to_state_rows([]) == []


def test_single_game_match_end():
    """3 shown points 15-0, 30-0, 40-0 → 4 StateRows total.
    The synthesized row is the match-end shape: sets=(1,0), games=(1,0),
    score=(0,0) — closing set's final games preserved with zeroed score."""
    rows = [
        _pt(1, 1, "15", "0"),
        _pt(1, 1, "30", "0"),
        _pt(1, 1, "40", "0"),
    ]
    out = backfill_points_to_state_rows(rows)
    assert len(out) == 4
    # Each shown point is at (sets=0,0, games=0,0)
    assert (out[0].score_a, out[0].score_b) == ("15", "0")
    assert (out[0].sets_a, out[0].sets_b) == (0, 0)
    assert (out[0].games_a, out[0].games_b) == (0, 0)
    assert (out[1].score_a, out[1].score_b) == ("30", "0")
    assert (out[2].score_a, out[2].score_b) == ("40", "0")
    # Synthesized match-end row: home wins, games stay at (1,0), sets=(1,0)
    assert (out[3].sets_a, out[3].sets_b) == (1, 0)
    assert (out[3].games_a, out[3].games_b) == (1, 0)
    assert (out[3].score_a, out[3].score_b) == ("0", "0")


def test_mid_set_game_boundary():
    """Two games in set 1: game 1 ends 40-0 (home), game 2 starts at 15-0.
    Mid-set boundary: synthesized row after game 1 has sets=(0,0),
    games=(1,0), score=(0,0)."""
    rows = [
        _pt(1, 1, "15", "0"),
        _pt(1, 1, "30", "0"),
        _pt(1, 1, "40", "0"),
        _pt(1, 2, "15", "0"),
    ]
    out = backfill_points_to_state_rows(rows)
    # 3 shown for game 1 + 1 synthesized boundary + 1 shown for game 2
    # + 1 synthesized match-end
    assert len(out) == 6
    # Mid-set synthesized row (after game 1)
    boundary = out[3]
    assert (boundary.sets_a, boundary.sets_b) == (0, 0)
    assert (boundary.games_a, boundary.games_b) == (1, 0)
    assert (boundary.score_a, boundary.score_b) == ("0", "0")
    # Game 2's first shown point carries the new games count
    assert (out[4].games_a, out[4].games_b) == (1, 0)
    assert (out[4].score_a, out[4].score_b) == ("15", "0")


def test_deuce_game_ending_at_advantage():
    """Game points: 30-30, 40-30, 40-40 (deuce), 40-A (away advantage).
    Away wins next omitted point. Synthesized: games=(0,1)."""
    rows = [
        _pt(1, 1, "30", "30"),
        _pt(1, 1, "40", "30"),
        _pt(1, 1, "40", "40"),
        _pt(1, 1, "40", "AD"),
    ]
    out = backfill_points_to_state_rows(rows)
    assert len(out) == 5  # 4 shown + 1 synthesized
    # Last shown point — score 40-AD passes through
    assert (out[3].score_a, out[3].score_b) == ("40", "AD")
    # Synthesized: away won game → games=(0,1), match-end → sets=(0,1)
    assert (out[4].sets_a, out[4].sets_b) == (0, 1)
    assert (out[4].games_a, out[4].games_b) == (0, 1)
    assert (out[4].score_a, out[4].score_b) == ("0", "0")


def test_tiebreak_game():
    """Single tiebreak game at game_num=13: shown points 1-0, 2-0, ..., 6-2.
    Home wins the omitted 7-2 point.

    Tiebreaks happen at games=6-6. To produce that pre-state we'd need
    earlier games in the input; here we just verify the tiebreak's
    SCORES pass through and the inferred winner is home."""
    rows = [
        _pt(1, 13, "1", "0"),
        _pt(1, 13, "2", "0"),
        _pt(1, 13, "3", "0"),
        _pt(1, 13, "3", "1"),
        _pt(1, 13, "4", "1"),
        _pt(1, 13, "5", "1"),
        _pt(1, 13, "5", "2"),
        _pt(1, 13, "6", "2"),
    ]
    out = backfill_points_to_state_rows(rows)
    assert len(out) == 9  # 8 shown + 1 synthesized
    # Scores pass through as integer strings
    assert (out[0].score_a, out[0].score_b) == ("1", "0")
    assert (out[7].score_a, out[7].score_b) == ("6", "2")
    # Synthesized: home won (6 > 2). games becomes (1, 0) (only one game
    # in this single-game test). Match-end: sets=(1,0).
    assert (out[8].sets_a, out[8].sets_b) == (1, 0)
    assert (out[8].games_a, out[8].games_b) == (1, 0)
    assert (out[8].score_a, out[8].score_b) == ("0", "0")


def test_set_boundary():
    """Two games — last of set 1 (home wins to clinch the set 1-0),
    then first of set 2. Synthesized set-boundary row resets games and
    increments sets to (1, 0)."""
    rows = [
        _pt(1, 1, "15", "0"),
        _pt(1, 1, "30", "0"),
        _pt(1, 1, "40", "0"),
        _pt(2, 1, "15", "0"),
    ]
    out = backfill_points_to_state_rows(rows)
    # 3 shown (set 1) + 1 set-boundary + 1 shown (set 2) + 1 match-end
    assert len(out) == 6
    # Set-boundary synthesized row
    boundary = out[3]
    assert (boundary.sets_a, boundary.sets_b) == (1, 0)
    assert (boundary.games_a, boundary.games_b) == (0, 0)
    assert (boundary.score_a, boundary.score_b) == ("0", "0")
    # Set 2's first shown point uses the new sets count, fresh games
    assert (out[4].sets_a, out[4].sets_b) == (1, 0)
    assert (out[4].games_a, out[4].games_b) == (0, 0)
    assert (out[4].score_a, out[4].score_b) == ("15", "0")


def test_match_end_row_has_no_further_reset():
    """The final synthesized row carries the closing set's final games
    (match_end_shape) — games are NOT reset to 0-0."""
    rows = [
        _pt(1, 1, "15", "0"),
        _pt(1, 1, "30", "0"),
        _pt(1, 1, "40", "0"),
    ]
    out = backfill_points_to_state_rows(rows)
    last = out[-1]
    # Games stay at 1-0 (the closing set's final games), not reset
    assert (last.games_a, last.games_b) == (1, 0)
    assert (last.sets_a, last.sets_b) == (1, 0)
    assert (last.score_a, last.score_b) == ("0", "0")


def test_normalize_lowercase_ad():
    """'a' / 'adv' / 'AD' all normalize to 'AD'."""
    rows = [_pt(1, 1, "a", "40")]
    out = backfill_points_to_state_rows(rows)
    assert out[0].score_a == "AD"
