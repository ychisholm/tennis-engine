"""Tests for Component 2A: Markov probability engine."""

import pytest
from src.engine.markov_engine import (
    game_win_prob,
    tiebreak_win_prob,
    match_win_prob,
    compute_live_probabilities,
    GAME_TABLE,
    TIEBREAK_TABLE,
)


class TestGameWinProb:
    def test_symmetry_at_half(self):
        """At p=0.5 from 0-0 the game is symmetric: P = 0.5."""
        assert abs(game_win_prob(0.5) - 0.5) < 1e-9

    def test_strong_server_from_zero(self):
        """At p=0.65 from 0-0 the server should win ~78-82% of games."""
        prob = game_win_prob(0.65)
        assert 0.75 < prob < 0.85

    def test_certain_win(self):
        """Server at 40-0 (3-0) is one point from winning."""
        assert game_win_prob(0.9, 3, 0) > 0.99

    def test_certain_loss(self):
        """Server at 0-40 (0-3) is on the brink of losing."""
        assert game_win_prob(0.1, 0, 3) < 0.01

    def test_deuce_formula(self):
        """At deuce, P = p² / (p² + (1-p)²)."""
        p = 0.6
        expected = (p * p) / (p * p + (1 - p) * (1 - p))
        assert abs(game_win_prob(p, 3, 3) - expected) < 1e-9

    def test_higher_p_higher_prob(self):
        """Higher point-win probability must yield higher game-win probability."""
        assert game_win_prob(0.6) > game_win_prob(0.5)

    def test_lookup_table_populated(self):
        """GAME_TABLE should cover 0.35..0.85."""
        assert 0.35 in GAME_TABLE
        assert 0.85 in GAME_TABLE
        assert len(GAME_TABLE) == 51


class TestTiebreakWinProb:
    def test_symmetry_at_half(self):
        """Equal players in a tiebreak → 50/50."""
        assert abs(tiebreak_win_prob(0.5, 0.5) - 0.5) < 1e-9

    def test_server_advantage(self):
        """Better server should win tiebreak more than half the time."""
        assert tiebreak_win_prob(0.65, 0.55) > 0.5

    def test_lookup_table_populated(self):
        """TIEBREAK_TABLE should have 51×51 entries."""
        assert len(TIEBREAK_TABLE) == 51 * 51
        assert (0.65, 0.55) in TIEBREAK_TABLE


class TestMatchWinProb:
    def test_equal_players_equal_score(self):
        """Equal p_hat, score 0-0 → P(A wins) = 0.5."""
        prob = match_win_prob(0.65, 0.65)
        assert abs(prob - 0.5) < 1e-6

    def test_better_server_favoured(self):
        """p_hat_A=0.70 vs p_hat_B=0.60 from 0-0 → A is heavy favourite."""
        prob = match_win_prob(0.70, 0.60)
        assert prob > 0.65

    def test_match_already_won(self):
        """If A leads 2-0 in a best-of-3, A has already won."""
        prob = match_win_prob(0.65, 0.65, sets_A=2, sets_B=0)
        assert prob == 1.0

    def test_match_already_lost(self):
        prob = match_win_prob(0.65, 0.65, sets_A=0, sets_B=2)
        assert prob == 0.0

    def test_clipped_to_unit_interval(self):
        prob = match_win_prob(0.99, 0.01)
        assert 0.0 <= prob <= 1.0


class TestComputeLiveProbabilities:
    def _default_state(self, **overrides):
        state = dict(
            sets_A=0, sets_B=0,
            games_A=0, games_B=0,
            points_A=0, points_B=0,
            serving_player="A",
            best_of=3,
        )
        state.update(overrides)
        return state

    def test_returns_required_keys(self):
        result = compute_live_probabilities(0.65, 0.65, self._default_state())
        assert "P_game_A" in result
        assert "P_set_A" in result
        assert "P_match_A" in result

    def test_values_in_unit_interval(self):
        result = compute_live_probabilities(0.65, 0.55, self._default_state())
        for key, val in result.items():
            assert 0.0 <= val <= 1.0, f"{key}={val} out of range"

    def test_symmetry_equal_players(self):
        result = compute_live_probabilities(0.65, 0.65, self._default_state())
        assert abs(result["P_match_A"] - 0.5) < 1e-6

    def test_serving_player_b(self):
        """When B serves, A's game-win prob should be < 0.5 if players equal."""
        result = compute_live_probabilities(
            0.65, 0.65, self._default_state(serving_player="B")
        )
        assert result["P_game_A"] < 0.5
