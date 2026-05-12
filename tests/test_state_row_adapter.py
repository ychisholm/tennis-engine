"""Tests for src/live/state_row_adapter.py.

Pure unit tests — synthetic dict inputs, no DB, no fixtures beyond
pytest defaults.

Groups (per the Phase 6D Prompt 2 spec):
  A — module-level pure helpers
  B — find_legal_path (within-game / compression)
  C — find_game_end_path (game-close synthesis)
  D — adapter end-to-end
  E — server identity through a match
"""
from __future__ import annotations

import pytest

from src.live.state_row_adapter import (
    NoLegalPathError,
    Point,
    StateRowAdapter,
    classify_transition,
    compute_server_for_game,
    compute_set_and_game,
    count_completed_games_in_match,
    find_game_end_path,
    find_legal_path,
    format_score_before,
    is_tiebreak_score,
    normalize_point_token,
)


# ---------------------------------------------------------------------------
# Synthetic-row helpers
# ---------------------------------------------------------------------------

def _row(
    *,
    sets_a: int = 0,
    sets_b: int = 0,
    set1_a: int = 0,
    set1_b: int = 0,
    set2_a: int = 0,
    set2_b: int = 0,
    set3_a: int = 0,
    set3_b: int = 0,
    games_a: int = 0,
    games_b: int = 0,
    pt_a: str = "0",
    pt_b: str = "0",
    point_winner: str | None = None,
    status: str = "inprogress",
    first_server: str | None = None,
) -> dict:
    """Build a synthetic live.match_states row dict."""
    return {
        "home_sets_won": sets_a,
        "away_sets_won": sets_b,
        "home_set1_games": set1_a,
        "away_set1_games": set1_b,
        "home_set2_games": set2_a,
        "away_set2_games": set2_b,
        "home_set3_games": set3_a,
        "away_set3_games": set3_b,
        "home_current_games": games_a,
        "away_current_games": games_b,
        "home_current_point": pt_a,
        "away_current_point": pt_b,
        "point_winner": point_winner,
        "status": status,
        "first_server": first_server,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Group A — module-level pure helpers
# ═══════════════════════════════════════════════════════════════════════════

# --- classify_transition -----------------------------------------------------

def test_classify_transition_no_prev_trivial():
    curr = _row(sets_a=0, sets_b=0, games_a=0, games_b=0, pt_a="0", pt_b="0")
    assert classify_transition(None, curr) == "NO_PREV_TRIVIAL"


def test_classify_transition_no_prev_trivial_with_point_progress():
    # First observed row of the match is 15-0 — still NO_PREV_TRIVIAL because
    # sets and games are both zero.
    curr = _row(sets_a=0, sets_b=0, games_a=0, games_b=0, pt_a="15", pt_b="0")
    assert classify_transition(None, curr) == "NO_PREV_TRIVIAL"


def test_classify_transition_no_prev_nontrivial():
    # Worker spawned mid-match: prev=None, curr already has progress.
    curr = _row(sets_a=0, sets_b=0, games_a=1, games_b=2, pt_a="30", pt_b="0")
    assert classify_transition(None, curr) == "NO_PREV_NONTRIVIAL"


def test_classify_transition_no_change():
    prev = _row(games_a=2, games_b=1, pt_a="30", pt_b="15")
    curr = _row(games_a=2, games_b=1, pt_a="30", pt_b="15")
    assert classify_transition(prev, curr) == "NO_CHANGE"


def test_classify_transition_within_game():
    prev = _row(games_a=2, games_b=1, pt_a="15", pt_b="0")
    curr = _row(games_a=2, games_b=1, pt_a="30", pt_b="0")
    assert classify_transition(prev, curr) == "WITHIN_GAME"


def test_classify_transition_game_end():
    prev = _row(games_a=2, games_b=1, pt_a="40", pt_b="30")
    curr = _row(games_a=3, games_b=1, pt_a="0", pt_b="0")
    assert classify_transition(prev, curr) == "GAME_END"


def test_classify_transition_set_end():
    prev = _row(games_a=0, games_b=5, pt_a="15", pt_b="40")
    curr = _row(sets_b=1, games_a=0, games_b=0, pt_a="0", pt_b="0")
    assert classify_transition(prev, curr) == "SET_END"


def test_classify_transition_match_end_shape():
    # Match-end: writer carries closing set's games in current_games on
    # the terminal 'finished' poll.
    prev = _row(sets_b=1, games_a=2, games_b=5, pt_a="15", pt_b="40")
    curr = _row(
        sets_b=2, set1_a=0, set1_b=6, set2_a=2, set2_b=6,
        games_a=2, games_b=6, pt_a="0", pt_b="0", status="finished",
    )
    assert classify_transition(prev, curr) == "MATCH_END"


def test_classify_transition_malformed_games_regression():
    prev = _row(games_a=3, games_b=2, pt_a="0", pt_b="0")
    curr = _row(games_a=2, games_b=2, pt_a="0", pt_b="0")
    assert classify_transition(prev, curr) == "MALFORMED"


def test_classify_transition_malformed_multi_game_jump():
    prev = _row(games_a=2, games_b=1, pt_a="40", pt_b="30")
    curr = _row(games_a=4, games_b=1, pt_a="0", pt_b="0")
    assert classify_transition(prev, curr) == "MALFORMED"


def test_classify_transition_malformed_set_regression():
    prev = _row(sets_a=1, sets_b=0, games_a=0, games_b=0, pt_a="0", pt_b="0")
    curr = _row(sets_a=0, sets_b=0, games_a=6, games_b=4, pt_a="0", pt_b="0")
    assert classify_transition(prev, curr) == "MALFORMED"


# --- count_completed_games_in_match -----------------------------------------

def test_count_completed_games_at_match_start():
    row = _row(sets_a=0, sets_b=0, games_a=0, games_b=0)
    assert count_completed_games_in_match(row) == 0


def test_count_completed_games_mid_first_game():
    row = _row(sets_a=0, sets_b=0, games_a=0, games_b=0, pt_a="30", pt_b="15")
    # No games completed yet; current game (#1) is in progress.
    assert count_completed_games_in_match(row) == 0


def test_count_completed_games_after_one_game_in_set_1():
    row = _row(sets_a=0, sets_b=0, games_a=1, games_b=0, pt_a="0", pt_b="0")
    assert count_completed_games_in_match(row) == 1


def test_count_completed_games_mid_match():
    # Two completed sets, currently 22 games into a 23-game in-progress game.
    row = _row(
        sets_a=1, sets_b=1,
        set1_a=6, set1_b=3,
        set2_a=2, set2_b=6,
        games_a=3, games_b=2,
    )
    # set1=6+3=9, set2=2+6=8, current=3+2=5  → total=22.
    assert count_completed_games_in_match(row) == 22


def test_count_completed_games_at_match_end_shape():
    # On a status='finished' row the closing set's games appear BOTH in
    # the per-set columns and in current_games — don't double-count.
    row = _row(
        sets_a=0, sets_b=2,
        set1_a=0, set1_b=6,
        set2_a=2, set2_b=6,
        games_a=2, games_b=6,  # writer duplicated set2_games here
        status="finished",
    )
    # Loop sums set1 (6) + set2 (8) = 14. Skip current_games on finished.
    assert count_completed_games_in_match(row) == 14


# --- compute_server_for_game ------------------------------------------------

def test_compute_server_first_server_home_game_1_is_home():
    assert compute_server_for_game("home", 0) == 1


def test_compute_server_first_server_home_game_2_is_away():
    assert compute_server_for_game("home", 1) == 2


def test_compute_server_first_server_away_game_1_is_away():
    assert compute_server_for_game("away", 0) == 2


def test_compute_server_first_server_away_game_2_is_home():
    assert compute_server_for_game("away", 1) == 1


def test_compute_server_after_set_boundary():
    # First set ends 6-3 (9 games). First_server='home' served games
    # 1,3,5,7,9; away served 2,4,6,8. Game 10 (=2nd set game 1) is away.
    assert compute_server_for_game("home", 9) == 2


# --- compute_set_and_game ---------------------------------------------------

def test_compute_set_and_game_first_game_set_1():
    row = _row(sets_a=0, sets_b=0, games_a=0, games_b=0)
    assert compute_set_and_game(row) == (1, 1)


def test_compute_set_and_game_mid_set():
    row = _row(sets_a=0, sets_b=0, games_a=2, games_b=1)
    # 4 games done, game 5 in progress, set 1.
    assert compute_set_and_game(row) == (1, 4)


def test_compute_set_and_game_set_2_first_game():
    row = _row(sets_a=1, sets_b=0, games_a=0, games_b=0)
    assert compute_set_and_game(row) == (2, 1)


def test_compute_set_and_game_tiebreak_is_13():
    row = _row(games_a=6, games_b=6, pt_a="3", pt_b="2")
    assert compute_set_and_game(row) == (1, 13)


def test_compute_set_and_game_at_6_6_pre_tiebreak_is_13():
    # games=6-6 but points still regular vocab (just after last regular
    # game ended). The next point IS the first tiebreak point, so the
    # current in-progress game number is 13.
    row = _row(games_a=6, games_b=6, pt_a="0", pt_b="0")
    assert compute_set_and_game(row) == (1, 13)


# --- is_tiebreak_score ------------------------------------------------------

def test_is_tiebreak_score_regular_vocab_returns_false():
    assert not is_tiebreak_score(_row(pt_a="30", pt_b="15"))
    assert not is_tiebreak_score(_row(pt_a="AD", pt_b="40"))
    assert not is_tiebreak_score(_row(pt_a="A", pt_b="40"))  # normalized to AD
    assert not is_tiebreak_score(_row(pt_a="0", pt_b="0"))


def test_is_tiebreak_score_at_6_6_with_integer_returns_true():
    assert is_tiebreak_score(_row(games_a=6, games_b=6, pt_a="3", pt_b="2"))


def test_is_tiebreak_score_at_5_5_with_integer_still_returns_true():
    # The function looks only at the point tokens — even outside a
    # normal tiebreak context, integer-string points read as tiebreak
    # vocabulary. (Higher-level classify_transition + game/set context
    # ensures we don't misinterpret.)
    assert is_tiebreak_score(_row(games_a=5, games_b=5, pt_a="3", pt_b="0"))


def test_is_tiebreak_score_at_5_5_regular_returns_false():
    assert not is_tiebreak_score(_row(games_a=5, games_b=5, pt_a="40", pt_b="30"))


# --- normalize_point_token --------------------------------------------------

def test_normalize_point_token_A_becomes_AD():
    assert normalize_point_token("A") == "AD"


def test_normalize_point_token_AD_passes_through():
    assert normalize_point_token("AD") == "AD"


def test_normalize_point_token_passes_through_regular_vocab():
    for tok in ("0", "15", "30", "40"):
        assert normalize_point_token(tok) == tok


def test_normalize_point_token_passes_through_integer_string():
    for tok in ("0", "1", "2", "7", "12"):
        assert normalize_point_token(tok) == tok


def test_normalize_point_token_None_becomes_zero():
    assert normalize_point_token(None) == "0"
    assert normalize_point_token("") == "0"


# --- format_score_before ----------------------------------------------------

def test_format_score_before_regular():
    assert format_score_before("15", "0") == "15-0"
    assert format_score_before("40", "30") == "40-30"


def test_format_score_before_with_AD():
    assert format_score_before("AD", "40") == "AD-40"
    assert format_score_before("40", "AD") == "40-AD"


def test_format_score_before_tiebreak():
    assert format_score_before("7", "5") == "7-5"
    assert format_score_before("10", "8") == "10-8"


# ═══════════════════════════════════════════════════════════════════════════
# Group B — find_legal_path (within-game / compression)
# ═══════════════════════════════════════════════════════════════════════════

def test_path_single_point_in_game():
    path = find_legal_path(("15", "0"), ("30", "0"), False, "home")
    assert len(path) == 1
    assert path[0] == (("15", "0"), ("30", "0"), "home")


def test_path_two_point_compression_both_sides_one_pick_home_last():
    # (0,30) → (15,40) — one home win, one away win, last preferred 'home'.
    path = find_legal_path(("0", "30"), ("15", "40"), False, "home")
    assert len(path) == 2
    assert path[-1][2] == "home"
    # First edge advances away (rank-up away), second edge advances home.
    assert path[0] == (("0", "30"), ("0", "40"), "away")
    assert path[1] == (("0", "40"), ("15", "40"), "home")


def test_path_two_point_compression_pick_away_last():
    # Same prev/curr but prefer 'away' as last edge.
    path = find_legal_path(("0", "30"), ("15", "40"), False, "away")
    assert len(path) == 2
    assert path[-1][2] == "away"
    assert path[0] == (("0", "30"), ("15", "30"), "home")
    assert path[1] == (("15", "30"), ("15", "40"), "away")


def test_path_three_point_compression():
    # (0,0) → (30,15): 2 home wins + 1 away win, preferred last 'home'.
    path = find_legal_path(("0", "0"), ("30", "15"), False, "home")
    assert len(path) == 3
    assert path[-1][2] == "home"
    # Check sequence terminates at curr.
    assert path[-1][1] == ("30", "15")


def test_path_preferred_last_side_picks_correct_ordering():
    # Two shortest paths exist from (15,0) → (30,15):
    #   A: (15,0)→(15,15)→(30,15)  edges [away, home]   last=home
    #   B: (15,0)→(30,0)→(30,15)   edges [home, away]   last=away
    path_home = find_legal_path(("15", "0"), ("30", "15"), False, "home")
    assert path_home[-1][2] == "home"
    path_away = find_legal_path(("15", "0"), ("30", "15"), False, "away")
    assert path_away[-1][2] == "away"


def test_path_through_deuce_reset():
    # (AD, 40) → (40, 40): one edge, deuce reset by away.
    path = find_legal_path(("AD", "40"), ("40", "40"), False, "away")
    assert path == [(("AD", "40"), ("40", "40"), "away")]


def test_path_no_legal_path_raises_NoLegalPathError():
    # Impossible: (15, 30) → (30, 15). Home advanced AND away regressed
    # without an AD reset — no legal route in the game graph.
    with pytest.raises(NoLegalPathError) as exc_info:
        find_legal_path(("15", "30"), ("30", "15"), False, "home")
    assert "no legal" in exc_info.value.reason


def test_path_tiebreak_simple_increment():
    path = find_legal_path(("3", "2"), ("4", "2"), True, "home")
    assert path == [(("3", "2"), ("4", "2"), "home")]


def test_path_tiebreak_compression():
    # (3, 2) → (4, 3): one home, one away, prefer home last.
    path = find_legal_path(("3", "2"), ("4", "3"), True, "home")
    assert len(path) == 2
    assert path[-1][2] == "home"
    assert path[0] == (("3", "2"), ("3", "3"), "away")
    assert path[1] == (("3", "3"), ("4", "3"), "home")


def test_path_tiebreak_compression_prefer_away():
    path = find_legal_path(("3", "2"), ("4", "3"), True, "away")
    assert len(path) == 2
    assert path[-1][2] == "away"
    assert path[0] == (("3", "2"), ("4", "2"), "home")
    assert path[1] == (("4", "2"), ("4", "3"), "away")


def test_path_tiebreak_regression_raises():
    with pytest.raises(NoLegalPathError):
        find_legal_path(("5", "4"), ("3", "4"), True, "home")


def test_path_zero_edges_when_pts_match():
    assert find_legal_path(("15", "30"), ("15", "30"), False, "home") == []


# ═══════════════════════════════════════════════════════════════════════════
# Group C — find_game_end_path (game-close synthesis)
# ═══════════════════════════════════════════════════════════════════════════

def test_game_end_path_clean_hold_at_40_30():
    # (40, 30) → home wins: one edge to GAME_A.
    path = find_game_end_path(("40", "30"), "home", False)
    assert path == [(("40", "30"), "GAME_A", "home")]


def test_game_end_path_break_at_30_40():
    path = find_game_end_path(("30", "40"), "away", False)
    assert path == [(("30", "40"), "GAME_B", "away")]


def test_game_end_path_deuce_to_game_via_AD():
    # (40, 40) → home wins: (40,40)→(AD,40)→GAME_A. 2 home edges.
    path = find_game_end_path(("40", "40"), "home", False)
    assert len(path) == 2
    assert all(edge[2] == "home" for edge in path)
    assert path[-1][1] == "GAME_A"


def test_game_end_path_long_deuce_sequence():
    # (40, 30) → away wins via long deuce: (40,30)→(40,40)→(40,AD)→GAME_B.
    path = find_game_end_path(("40", "30"), "away", False)
    assert len(path) == 3
    assert path[-1][2] == "away"
    assert path[-1][1] == "GAME_B"


def test_game_end_path_at_AD_to_game():
    path = find_game_end_path(("AD", "40"), "home", False)
    assert path == [(("AD", "40"), "GAME_A", "home")]


def test_game_end_path_tiebreak_clean_7_to_5():
    # (5, 5) → home wins: target = max(7, 5+2) = 7. delta = 2.
    path = find_game_end_path(("5", "5"), "home", True)
    assert len(path) == 2
    assert all(edge[2] == "home" for edge in path)
    assert path[0] == (("5", "5"), ("6", "5"), "home")
    assert path[1] == (("6", "5"), ("7", "5"), "home")


def test_game_end_path_tiebreak_simple_7_to_4():
    # (4, 4) → home wins: target = max(7, 4+2) = 7. delta = 3.
    path = find_game_end_path(("4", "4"), "home", True)
    assert len(path) == 3
    assert all(edge[2] == "home" for edge in path)
    assert path[-1] == (("6", "4"), ("7", "4"), "home")


def test_game_end_path_tiebreak_long_extended():
    # (7, 7) → home wins: target = max(7, 7+2) = 9. delta = 2.
    path = find_game_end_path(("7", "7"), "home", True)
    assert len(path) == 2
    assert path[-1] == (("8", "7"), ("9", "7"), "home")


def test_game_end_path_tiebreak_already_won_returns_empty():
    # Engine close was missed on a prior transition — return [].
    path = find_game_end_path(("7", "4"), "home", True)
    assert path == []


def test_game_end_path_unreachable_raises_for_invalid_input():
    # game_winner must be 'home' or 'away'.
    with pytest.raises(NoLegalPathError):
        find_game_end_path(("0", "0"), "neither", False)


def test_game_end_path_regular_unparseable_start_raises():
    # If somebody passes an unknown start (not a graph node), no path.
    with pytest.raises(NoLegalPathError):
        find_game_end_path(("99", "99"), "home", False)


# ═══════════════════════════════════════════════════════════════════════════
# Group D — adapter end-to-end
# ═══════════════════════════════════════════════════════════════════════════

def _adapter(first_server: str = "home") -> StateRowAdapter:
    return StateRowAdapter(first_server=first_server)


def test_adapter_constructor_rejects_bad_first_server():
    with pytest.raises(ValueError):
        StateRowAdapter(first_server="left")


def test_transition_first_row_trivial_returns_first_point():
    # prev=None, curr at the very start with 15-0 and point_winner='home'.
    curr = _row(pt_a="15", pt_b="0", point_winner="home")
    pts = _adapter("home").transition(None, curr)
    assert len(pts) == 1
    p = pts[0]
    assert p.set_number == 1
    assert p.game_number_in_set == 1
    assert p.score_before == "0-0"
    assert p.Svr == 1  # home serves game 1 when first_server='home'
    assert p.PtWinner == 1
    assert p.is_tiebreak is False


def test_transition_first_row_at_zero_zero_returns_empty():
    # First poll observed at 0-0, no actual point played yet.
    curr = _row(pt_a="0", pt_b="0", point_winner=None)
    assert _adapter("home").transition(None, curr) == []


def test_transition_first_row_nontrivial_returns_empty(caplog):
    # Worker spawned mid-match: prev=None, curr already at games=2-1.
    curr = _row(games_a=2, games_b=1, pt_a="30", pt_b="0", point_winner="home")
    assert _adapter("home").transition(None, curr) == []


def test_transition_no_change_returns_empty():
    prev = _row(games_a=2, games_b=1, pt_a="30", pt_b="15")
    curr = _row(games_a=2, games_b=1, pt_a="30", pt_b="15")
    assert _adapter("home").transition(prev, curr) == []


def test_transition_single_point_in_game():
    prev = _row(games_a=2, games_b=1, pt_a="15", pt_b="0")
    curr = _row(
        games_a=2, games_b=1, pt_a="30", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 1
    # 4 games done (2+1+0+1=... wait: set1 cols all 0, current 2+1=3,
    # so 3 completed before this game). Game 4 → server flips from
    # first_server='home' three times = away. So Svr=2.
    assert pts[0].set_number == 1
    assert pts[0].game_number_in_set == 4
    assert pts[0].score_before == "15-0"
    assert pts[0].Svr == 2  # game 4 served by away when first='home'
    assert pts[0].PtWinner == 1


def test_transition_two_point_compression_ordering_rule():
    # The canonical recon §13B example from match 16098967:
    # prev pt=0-30, curr pt=15-40, point_winner='home'.
    prev = _row(games_a=2, games_b=1, pt_a="0", pt_b="30")
    curr = _row(
        games_a=2, games_b=1, pt_a="15", pt_b="40", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 2
    # First point: away advances from 30 to 40.
    assert pts[0].score_before == "0-30"
    assert pts[0].PtWinner == 2
    # Second point (last): home advances from 0 to 15.
    assert pts[1].score_before == "0-40"
    assert pts[1].PtWinner == 1


def test_transition_three_point_compression():
    prev = _row(games_a=0, games_b=0, pt_a="0", pt_b="0")
    curr = _row(
        games_a=0, games_b=0, pt_a="30", pt_b="15", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 3
    # Last point's PtWinner respects preferred='home'.
    assert pts[-1].PtWinner == 1
    # Score progression ends right before (30, 15).
    last_score = pts[-1].score_before
    assert last_score in ("15-15", "30-0")


def test_transition_game_end_clean_hold():
    # prev=(40,30) → curr in next game (games_a went 2→3, prev games=2-1).
    prev = _row(games_a=2, games_b=1, pt_a="40", pt_b="30")
    curr = _row(
        games_a=3, games_b=1, pt_a="0", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 1
    p = pts[0]
    assert p.set_number == 1
    assert p.game_number_in_set == 4
    assert p.score_before == "40-30"
    assert p.PtWinner == 1


def test_transition_game_end_break():
    prev = _row(games_a=2, games_b=1, pt_a="30", pt_b="40")
    curr = _row(
        games_a=2, games_b=2, pt_a="0", pt_b="0", point_winner="away",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 1
    p = pts[0]
    assert p.score_before == "30-40"
    assert p.PtWinner == 2


def test_transition_game_end_deuce_ad_game():
    # prev=(40,40), curr at next game with home winning.
    prev = _row(games_a=2, games_b=1, pt_a="40", pt_b="40")
    curr = _row(
        games_a=3, games_b=1, pt_a="0", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    # Two synthesized home points: (40,40)→(AD,40)→GAME_A.
    assert len(pts) == 2
    assert all(p.PtWinner == 1 for p in pts)
    assert pts[0].score_before == "40-40"
    assert pts[1].score_before == "AD-40"


def test_transition_set_end():
    # Empirical from match 16160007 set 1.
    prev = _row(games_a=0, games_b=5, pt_a="15", pt_b="40")
    curr = _row(
        sets_b=1, set1_a=0, set1_b=6,
        games_a=0, games_b=0, pt_a="0", pt_b="0",
        point_winner="away",
    )
    pts = _adapter("away").transition(prev, curr)
    assert len(pts) == 1
    p = pts[0]
    assert p.set_number == 1
    assert p.game_number_in_set == 6  # 5 games done, this is game 6
    assert p.score_before == "15-40"
    assert p.PtWinner == 2


def test_transition_match_end_shape():
    prev = _row(
        sets_a=0, sets_b=1, set1_a=0, set1_b=6,
        games_a=2, games_b=5, pt_a="15", pt_b="40",
    )
    curr = _row(
        sets_a=0, sets_b=2, set1_a=0, set1_b=6, set2_a=2, set2_b=6,
        games_a=2, games_b=6, pt_a="0", pt_b="0", status="finished",
        point_winner="away",
    )
    pts = _adapter("away").transition(prev, curr)
    assert len(pts) == 1
    p = pts[0]
    # Set 2, game 8 (2+5+1).
    assert p.set_number == 2
    assert p.game_number_in_set == 8
    assert p.score_before == "15-40"
    assert p.PtWinner == 2


def test_transition_tiebreak_entry_first_point():
    # prev games=6-6 pt=0-0 (last regular game just ended).
    # curr games=6-6 pt=1-0 with home winning.
    prev = _row(games_a=6, games_b=6, pt_a="0", pt_b="0")
    curr = _row(
        games_a=6, games_b=6, pt_a="1", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 1
    p = pts[0]
    assert p.set_number == 1
    assert p.game_number_in_set == 13
    assert p.is_tiebreak is True
    assert p.score_before == "0-0"
    assert p.PtWinner == 1


def test_transition_tiebreak_in_progress_single_point():
    prev = _row(games_a=6, games_b=6, pt_a="3", pt_b="2")
    curr = _row(
        games_a=6, games_b=6, pt_a="4", pt_b="2", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 1
    p = pts[0]
    assert p.is_tiebreak is True
    assert p.game_number_in_set == 13
    assert p.score_before == "3-2"
    assert p.PtWinner == 1


def test_transition_tiebreak_in_progress_compression():
    prev = _row(games_a=6, games_b=6, pt_a="3", pt_b="2")
    curr = _row(
        games_a=6, games_b=6, pt_a="4", pt_b="3", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 2
    # All tiebreak.
    assert all(p.is_tiebreak for p in pts)
    # Preferred='home' last.
    assert pts[-1].PtWinner == 1
    # First synthesized point is the away win.
    assert pts[0].PtWinner == 2
    assert pts[0].score_before == "3-2"
    assert pts[1].score_before == "3-3"


def test_transition_tiebreak_both_sides_advance_null_point_winner_raises():
    # Empirical NULL case from match 16159253: tiebreak compression
    # where both digits incremented and writer left point_winner NULL.
    prev = _row(games_a=6, games_b=6, pt_a="3", pt_b="2")
    curr = _row(
        games_a=6, games_b=6, pt_a="4", pt_b="3", point_winner=None,
    )
    with pytest.raises(NoLegalPathError) as exc_info:
        _adapter("home").transition(prev, curr)
    assert exc_info.value.prev is prev
    assert exc_info.value.curr is curr
    assert "ambiguous" in exc_info.value.reason.lower()


def test_transition_tiebreak_set_end():
    # Tiebreak ends with home winning 7-5 → sets increment.
    prev = _row(games_a=6, games_b=6, pt_a="6", pt_b="5")
    curr = _row(
        sets_a=1, set1_a=7, set1_b=6,
        games_a=0, games_b=0, pt_a="0", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    # target = max(7, 5+2) = 7. delta = 7-6 = 1. Synth 1 point.
    assert len(pts) == 1
    p = pts[0]
    assert p.is_tiebreak is True
    assert p.game_number_in_set == 13
    assert p.PtWinner == 1
    assert p.score_before == "6-5"


def test_transition_glitch_no_legal_path_raises():
    # Empirical glitch from match 16160007: prev pt=15-30 → curr pt=30-15.
    # Home advanced AND away regressed without an AD reset — impossible.
    prev = _row(games_a=0, games_b=0, pt_a="15", pt_b="30")
    curr = _row(
        games_a=0, games_b=0, pt_a="30", pt_b="15", point_winner="home",
    )
    with pytest.raises(NoLegalPathError) as exc_info:
        _adapter("home").transition(prev, curr)
    assert exc_info.value.prev is prev
    assert exc_info.value.curr is curr


def test_transition_malformed_multi_game_jump_raises():
    prev = _row(games_a=2, games_b=1, pt_a="40", pt_b="30")
    curr = _row(games_a=4, games_b=1, pt_a="0", pt_b="0")
    with pytest.raises(NoLegalPathError) as exc_info:
        _adapter("home").transition(prev, curr)
    assert "MALFORMED" in exc_info.value.reason


def test_transition_malformed_games_regression_raises():
    prev = _row(games_a=3, games_b=2, pt_a="0", pt_b="0")
    curr = _row(games_a=2, games_b=2, pt_a="0", pt_b="0")
    with pytest.raises(NoLegalPathError):
        _adapter("home").transition(prev, curr)


def test_transition_within_game_null_winner_unambiguous_infers():
    # Writer's _derive_point_winner returns home unambiguously for
    # 15-0 → 30-0, but suppose somehow the curr row has NULL: adapter
    # should still derive 'home' from the rank delta.
    prev = _row(games_a=0, games_b=0, pt_a="15", pt_b="0")
    curr = _row(
        games_a=0, games_b=0, pt_a="30", pt_b="0", point_winner=None,
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 1
    assert pts[0].PtWinner == 1


def test_transition_within_game_deuce_reset():
    # prev=(AD,40), curr=(40,40): away wins by deuce reset.
    prev = _row(games_a=1, games_b=1, pt_a="AD", pt_b="40")
    curr = _row(
        games_a=1, games_b=1, pt_a="40", pt_b="40", point_winner="away",
    )
    pts = _adapter("home").transition(prev, curr)
    assert len(pts) == 1
    p = pts[0]
    assert p.PtWinner == 2
    assert p.score_before == "AD-40"


def test_transition_normalises_A_to_AD():
    # API can return raw 'A' for advantage; the adapter should treat
    # it as 'AD' in graph operations.
    prev = _row(games_a=1, games_b=1, pt_a="A", pt_b="40")
    curr = _row(
        games_a=2, games_b=1, pt_a="0", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    # GAME_END: synthesize (AD,40) → GAME_A. One home point.
    assert len(pts) == 1
    assert pts[0].score_before == "AD-40"
    assert pts[0].PtWinner == 1


def test_no_legal_path_error_carries_prev_curr():
    prev = _row(pt_a="15", pt_b="30")
    curr = _row(pt_a="30", pt_b="15", point_winner="home")
    with pytest.raises(NoLegalPathError) as exc_info:
        _adapter("home").transition(prev, curr)
    assert exc_info.value.prev is prev
    assert exc_info.value.curr is curr
    assert exc_info.value.reason


# ═══════════════════════════════════════════════════════════════════════════
# Group E — server identity through a match
# ═══════════════════════════════════════════════════════════════════════════

def test_server_alternates_each_game():
    # Build a sequence: 4 successive single-point WITHIN_GAME transitions
    # in 4 different games. Confirm Svr alternates.
    adapter = _adapter("home")
    seen_servers = []
    for game_idx in range(4):
        # Prev at start of this game (games_a + games_b = game_idx).
        # For simplicity put all on home_current_games up to 4, but
        # alternation only depends on TOTAL completed games, so let's
        # use (game_idx, 0).
        prev_games_a, prev_games_b = game_idx, 0
        prev = _row(
            games_a=prev_games_a, games_b=prev_games_b,
            pt_a="0", pt_b="0",
        )
        curr = _row(
            games_a=prev_games_a, games_b=prev_games_b,
            pt_a="15", pt_b="0", point_winner="home",
        )
        pts = adapter.transition(prev, curr)
        seen_servers.append(pts[0].Svr)
    # first_server='home' → game 1 server=1, game 2 server=2, ...
    assert seen_servers == [1, 2, 1, 2]


def test_server_after_set_with_odd_games():
    # Set 1 ended 6-3 (9 games). Set 2 game 1 → server is opposite of
    # game 1 (i.e., away since 9 is odd).
    prev = _row(sets_a=1, set1_a=6, set1_b=3, games_a=0, games_b=0)
    curr = _row(
        sets_a=1, set1_a=6, set1_b=3, games_a=0, games_b=0,
        pt_a="15", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    # 9 completed games before game 10. Server = away.
    assert pts[0].Svr == 2


def test_server_after_set_with_even_games():
    # Set 1 ended 7-5 (12 games). Set 2 game 1 → server is SAME as
    # game 1 (since 12 is even).
    prev = _row(sets_a=1, set1_a=7, set1_b=5, games_a=0, games_b=0)
    curr = _row(
        sets_a=1, set1_a=7, set1_b=5, games_a=0, games_b=0,
        pt_a="15", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    assert pts[0].Svr == 1


def test_server_into_tiebreak_uses_regular_alternation():
    # At games=6-6 just before the tiebreak's first point:
    # 12 regular games done, server alternation → game 13 server
    # is 'first_server' (even completed count).
    prev = _row(games_a=6, games_b=6, pt_a="0", pt_b="0")
    curr = _row(
        games_a=6, games_b=6, pt_a="1", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    # 12 completed games, even → server = first_server = home = 1.
    assert pts[0].Svr == 1


def test_server_into_tiebreak_first_server_away():
    prev = _row(games_a=6, games_b=6, pt_a="0", pt_b="0")
    curr = _row(
        games_a=6, games_b=6, pt_a="1", pt_b="0", point_winner="home",
    )
    pts = _adapter("away").transition(prev, curr)
    # 12 completed games, even → server = first_server = away = 2.
    assert pts[0].Svr == 2


def test_server_in_multi_set_match():
    # After set 1 ended 7-6(7) — 13 games (tiebreak counts as 1).
    # Wait actually a tiebreak is one game in tennis bookkeeping (game 13).
    # So set 1 ending 7-6 = 13 games. Set 2 first game server alternates.
    # But we count by completed_games_in_match: set1=7+6=13, set2 cur=0+0.
    # Total=13. Server of set 2 game 1 = opposite of first_server.
    prev = _row(sets_a=1, set1_a=7, set1_b=6, games_a=0, games_b=0)
    curr = _row(
        sets_a=1, set1_a=7, set1_b=6, games_a=0, games_b=0,
        pt_a="15", pt_b="0", point_winner="home",
    )
    pts = _adapter("home").transition(prev, curr)
    # 13 completed, odd → server=2.
    assert pts[0].Svr == 2


# ═══════════════════════════════════════════════════════════════════════════
# Sanity: Point dataclass shape
# ═══════════════════════════════════════════════════════════════════════════

def test_point_is_frozen_dataclass():
    p = Point(
        set_number=1, game_number_in_set=1, score_before="0-0",
        Svr=1, PtWinner=1, is_tiebreak=False,
    )
    with pytest.raises(Exception):
        p.set_number = 2  # type: ignore[misc]
