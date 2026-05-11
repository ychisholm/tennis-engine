"""Tests for the Markov baseline feature (Step 4)."""

import math
from pathlib import Path

import pytest

from src.markov_engine import (
    clear_set_cache,
    game_win_prob,
    set_win_prob,
    tiebreak_win_prob,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "processed" / "tennis.duckdb"


# ---------------------------------------------------------------------------
# Pure-math tests
# ---------------------------------------------------------------------------

def test_g_sanity():
    assert math.isclose(game_win_prob(0.5), 0.5, abs_tol=0.01)
    assert game_win_prob(0.66) > 0.80
    assert game_win_prob(0.85) > 0.95


def test_g_clipping():
    assert game_win_prob(0.10) == game_win_prob(0.35)
    assert game_win_prob(0.99) == game_win_prob(0.85)


def test_t_symmetry():
    assert math.isclose(
        tiebreak_win_prob(0.65, 0.65, True), 0.5, abs_tol=0.02
    )


def test_set_terminal():
    clear_set_cache()
    assert set_win_prob(0.65, 0.65, 6, 4, True) == 1.0
    clear_set_cache()
    assert set_win_prob(0.65, 0.65, 4, 6, True) == 0.0


def test_set_symmetry():
    clear_set_cache()
    prob = set_win_prob(0.65, 0.65, 0, 0, True)
    assert 0.5 <= prob < 0.6


def test_set_complementary():
    samples = [
        (0.65, 0.60, 3, 2, True),
        (0.70, 0.65, 5, 4, False),
        (0.62, 0.68, 6, 5, True),
    ]
    for p_a, p_b, ga, gb, s in samples:
        clear_set_cache()
        a_wins = set_win_prob(p_a, p_b, ga, gb, s)
        clear_set_cache()
        b_wins = set_win_prob(p_b, p_a, gb, ga, not s)
        assert math.isclose(a_wins + b_wins, 1.0, abs_tol=0.001), (
            f"P(A) + P(B) = {a_wins + b_wins} for state {(p_a, p_b, ga, gb, s)}"
        )


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def con():
    if not DB_PATH.exists():
        pytest.skip(f"DB not found at {DB_PATH}")
    import duckdb
    c = duckdb.connect(str(DB_PATH), read_only=True)
    has_p0 = c.execute(
        """
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = 'core' AND table_name = 'player_p0'
        """
    ).fetchone()[0]
    if has_p0 == 0:
        c.close()
        pytest.skip("core.player_p0 not built yet — run scripts/dev/build_markov_feature.py")
    yield c
    c.close()


def test_p0_top_server_high(con):
    rows = con.execute(
        """
        SELECT player_name, p0
        FROM core.player_p0
        WHERE player_name IN ('John Isner', 'Ivo Karlovic', 'Milos Raonic', 'Andy Roddick')
          AND p0 > 0.7
        """
    ).fetchall()
    assert len(rows) >= 1, f"no top server with p0 > 0.7 found; got {rows}"


def test_p0_league_average_in_range(con):
    rows = con.execute(
        """
        SELECT DISTINCT p0
        FROM core.player_p0
        WHERE match_method = 'league_average'
        """
    ).fetchall()
    assert len(rows) >= 1, "no league_average rows found"
    for (p0,) in rows:
        assert 0.62 <= p0 <= 0.69, f"league_average p0={p0} outside [0.62, 0.69]"


def test_p0_fuzzy_match_count_positive(con):
    n = con.execute(
        "SELECT COUNT(*) FROM core.player_p0 WHERE match_method = 'fuzzy'"
    ).fetchone()[0]
    assert n > 0, "expected at least one fuzzy match"


def test_markov_column_complete(con):
    nulls = con.execute(
        "SELECT COUNT(*) FROM core.ml_game_level WHERE markov_set_win_prob_A IS NULL"
    ).fetchone()[0]
    assert nulls == 0, f"{nulls} rows have NULL markov_set_win_prob_A"

    mn, mx = con.execute(
        "SELECT MIN(markov_set_win_prob_A), MAX(markov_set_win_prob_A) FROM core.ml_game_level"
    ).fetchone()
    assert 0.0 <= mn <= 1.0
    assert 0.0 <= mx <= 1.0
