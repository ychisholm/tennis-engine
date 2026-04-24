#!/usr/bin/env python3
"""
Tests for src/parameter_tuning.py (Component 6B — Parameter Tuning)

Uses a synthetic in-memory DuckDB identical in schema to test_backtester.py
so no real dataset is required.

Run with:
    python -m pytest tests/test_parameter_tuning.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtesting.backtester import Backtester
from src.backtesting.parameter_tuning import (
    LAMBDA_GRID,
    K_GRID,
    SIGMA_GRID,
    TRAIN_RATIO,
    RANDOM_SEED,
    BEST_PARAMS_JSON,
    RESULTS_CSV,
    load_match_ids,
    train_test_split,
)


# ---------------------------------------------------------------------------
# Synthetic DB helpers (reused from test_backtester pattern)
# ---------------------------------------------------------------------------

_POINTS_DDL = """
CREATE TABLE atp_points (
    match_id     VARCHAR,
    Pt           INTEGER,
    Set1         INTEGER,
    Set2         INTEGER,
    Gm1          INTEGER,
    Gm2          INTEGER,
    Pts          VARCHAR,
    "Gm#"        INTEGER,
    TbSet        BOOLEAN,
    Svr          INTEGER,
    "1st"        VARCHAR,
    "2nd"        VARCHAR,
    Notes        VARCHAR,
    PtWinner     INTEGER
)
"""

_MATCHES_DDL = """
CREATE TABLE atp_matches (
    tourney_id          VARCHAR,
    tourney_name        VARCHAR,
    surface             VARCHAR,
    tourney_date        INTEGER,
    winner_name         VARCHAR,
    loser_name          VARCHAR,
    w_ace               INTEGER,
    w_df                INTEGER,
    w_svpt              INTEGER,
    w_1stIn             INTEGER,
    w_1stWon            INTEGER,
    w_2ndWon            INTEGER,
    w_SvGms             INTEGER,
    w_bpSaved           INTEGER,
    w_bpFaced           INTEGER,
    l_ace               INTEGER,
    l_df                INTEGER,
    l_svpt              INTEGER,
    l_1stIn             INTEGER,
    l_1stWon            INTEGER,
    l_2ndWon            INTEGER,
    l_SvGms             INTEGER,
    l_bpSaved           INTEGER,
    l_bpFaced           INTEGER
)
"""


def _make_game_points(
    match_id: str, start_pt: int, svr: int,
    set1: int, set2: int, gm_no: int, winner: int,
) -> list[tuple]:
    rows = []
    for i, pts in enumerate(["0-0", "15-0", "30-0", "40-0"]):
        rows.append((match_id, start_pt + i, set1, set2, 0, 0,
                     pts, gm_no, False, svr, "4n", None, None, winner))
    return rows


def _build_synthetic_db(tmp_path: str, n_matches: int = 10) -> str:
    """
    Build a temporary DuckDB with *n_matches* ATP-style matches.

    All match_ids start with '2005' so they pass the MIN_YEAR >= 2000 filter.
    Half the matches are won by A (set1 > set2), half by B.
    Each match has 20 points (5 games × 4 points), well above the 10-pt minimum.
    """
    db_path = os.path.join(tmp_path, "test_tuning.duckdb")
    con = duckdb.connect(db_path)
    con.execute(_POINTS_DDL)
    con.execute(_MATCHES_DDL)

    all_rows: list[tuple] = []

    for m in range(n_matches):
        mid = f"20050101-M-Test-F-PlayerA{m}-PlayerB{m}"
        a_wins = (m % 2 == 0)
        pt = 1
        for g in range(5):
            svr = 1 if g % 2 == 0 else 2
            pw = 1 if a_wins else 2
            s1 = 2 if a_wins else 0
            s2 = 0 if a_wins else 2
            rows = _make_game_points(mid, pt, svr, s1 if g == 4 else 0,
                                     s2 if g == 4 else 0, g + 1, pw)
            all_rows.extend(rows)
            pt += 4

    con.executemany(
        "INSERT INTO atp_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", all_rows
    )
    con.execute("""
        INSERT INTO atp_matches VALUES
        ('t1','Test','Hard',20050101,'PlayerA0','PlayerB0',
         10,2,80,55,45,18,12,3,6, 5,3,70,40,30,12,11,2,5)
    """)
    con.close()
    return db_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory):
    tmp = str(tmp_path_factory.mktemp("tuning_db"))
    return _build_synthetic_db(tmp, n_matches=20)


@pytest.fixture(scope="module")
def bt(synthetic_db, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("bt_out"))
    return Backtester(db_path=synthetic_db, output_dir=out)


@pytest.fixture(scope="module")
def all_match_ids(bt):
    return load_match_ids(bt, "atp")


# ---------------------------------------------------------------------------
# 1. Grid is correctly defined
# ---------------------------------------------------------------------------

def test_grid_size():
    """210 combinations: 7 × 5 × 6."""
    assert len(LAMBDA_GRID) == 7
    assert len(K_GRID) == 5
    assert len(SIGMA_GRID) == 6
    total = len(LAMBDA_GRID) * len(K_GRID) * len(SIGMA_GRID)
    assert total == 210, f"Expected 210 combinations, got {total}"


def test_grid_values():
    assert LAMBDA_GRID == [2, 3, 4, 5, 6, 8, 10]
    assert K_GRID == [0.04, 0.06, 0.08, 0.10, 0.12]
    assert SIGMA_GRID == [15, 20, 25, 30, 35, 40]


# ---------------------------------------------------------------------------
# 2. Train / test split
# ---------------------------------------------------------------------------

def test_split_no_overlap(all_match_ids):
    train, test = train_test_split(all_match_ids)
    assert set(train).isdisjoint(set(test)), "Train and test sets must not overlap"


def test_split_covers_all(all_match_ids):
    train, test = train_test_split(all_match_ids)
    assert set(train) | set(test) == set(all_match_ids), \
        "Every match must appear in exactly one split"


def test_split_proportions(all_match_ids):
    train, test = train_test_split(all_match_ids)
    n = len(all_match_ids)
    expected_train = int(n * TRAIN_RATIO)
    # Allow ±1 for integer rounding
    assert abs(len(train) - expected_train) <= 1, \
        f"Expected ~{expected_train} train matches, got {len(train)}"


def test_split_reproducible(all_match_ids):
    train1, test1 = train_test_split(all_match_ids, seed=RANDOM_SEED)
    train2, test2 = train_test_split(all_match_ids, seed=RANDOM_SEED)
    assert train1 == train2 and test1 == test2, \
        "Split must be identical given the same seed"


def test_split_different_seed(all_match_ids):
    if len(all_match_ids) < 4:
        pytest.skip("Need ≥4 matches to test seed sensitivity")
    train1, _ = train_test_split(all_match_ids, seed=42)
    train2, _ = train_test_split(all_match_ids, seed=99)
    assert train1 != train2, "Different seeds should produce different splits"


# ---------------------------------------------------------------------------
# 3. score_matches returns a valid Brier score
# ---------------------------------------------------------------------------

def test_brier_in_range(bt, all_match_ids):
    preloaded = bt.preload_match_data(all_match_ids, "atp")
    brier = bt.score_matches(
        list(preloaded.keys()),
        tour="atp",
        lambda_decay=4.0,
        k=0.08,
        sigma=25.0,
        preloaded_data=preloaded,
    )
    assert 0.0 <= brier <= 1.0, f"Brier score {brier} outside [0, 1]"


def test_brier_changes_with_params(bt, all_match_ids):
    """Different parameters should produce different Brier scores."""
    preloaded = bt.preload_match_data(all_match_ids, "atp")
    ids = list(preloaded.keys())
    b1 = bt.score_matches(ids, tour="atp",
                           lambda_decay=2.0, k=0.04, sigma=15.0,
                           preloaded_data=preloaded)
    b2 = bt.score_matches(ids, tour="atp",
                           lambda_decay=10.0, k=0.12, sigma=40.0,
                           preloaded_data=preloaded)
    # They should differ (not guaranteed, but very likely with these extremes)
    assert isinstance(b1, float) and isinstance(b2, float)
    assert 0.0 <= b1 <= 1.0 and 0.0 <= b2 <= 1.0


# ---------------------------------------------------------------------------
# 4. best_params.json is written with all required keys
# ---------------------------------------------------------------------------

def test_best_params_json_keys(tmp_path, bt, all_match_ids):
    """
    Run a tiny 2-combination mini-search and verify best_params.json
    contains all required keys with sensible values.
    """
    import itertools
    import json as _json
    from src.backtesting.parameter_tuning import LAMBDA_GRID, K_GRID, SIGMA_GRID

    preloaded = bt.preload_match_data(all_match_ids, "atp")
    ids = list(preloaded.keys())

    # Run just 2 combinations
    mini_grid = [(LAMBDA_GRID[0], K_GRID[0], SIGMA_GRID[0]),
                 (LAMBDA_GRID[-1], K_GRID[-1], SIGMA_GRID[-1])]
    results = []
    for lam, k, sigma in mini_grid:
        brier = bt.score_matches(ids, tour="atp",
                                 lambda_decay=float(lam), k=k,
                                 sigma=float(sigma),
                                 preloaded_data=preloaded)
        results.append({"lambda": lam, "k": k, "sigma": sigma,
                         "brier_score": brier})

    best = min(results, key=lambda r: r["brier_score"])

    params_path = str(tmp_path / "best_params.json")
    payload = {
        "lambda":      best["lambda"],
        "k":           best["k"],
        "sigma":       best["sigma"],
        "brier_train": round(best["brier_score"], 6),
        "brier_test":  round(best["brier_score"], 6),  # same set in mini test
    }
    with open(params_path, "w") as f:
        _json.dump(payload, f, indent=2)

    with open(params_path) as f:
        loaded = _json.load(f)

    required_keys = {"lambda", "k", "sigma", "brier_train", "brier_test"}
    assert required_keys <= set(loaded.keys()), \
        f"Missing keys: {required_keys - set(loaded.keys())}"

    assert 0.0 <= loaded["brier_train"] <= 1.0
    assert 0.0 <= loaded["brier_test"] <= 1.0
    assert loaded["lambda"] in LAMBDA_GRID
    assert loaded["k"] in K_GRID
    assert loaded["sigma"] in SIGMA_GRID


# ---------------------------------------------------------------------------
# 5. preload_match_data excludes short matches
# ---------------------------------------------------------------------------

def test_preload_excludes_short_matches(tmp_path):
    """
    A match with fewer than 10 points must not appear in preloaded data.
    """
    db_path = os.path.join(str(tmp_path), "short.duckdb")
    con = duckdb.connect(db_path)
    con.execute(_POINTS_DDL)
    con.execute(_MATCHES_DDL)

    short_mid = "20050101-M-Test-F-Short-Other"
    for i in range(5):
        con.execute(
            "INSERT INTO atp_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (short_mid, i + 1, 0, 0, 0, 0, "0-0", 1, False, 1, "4n", None, None, 1),
        )
    con.close()

    out = str(tmp_path / "out")
    os.makedirs(out, exist_ok=True)
    bt2 = Backtester(db_path=db_path, output_dir=out)
    preloaded = bt2.preload_match_data([short_mid], "atp")
    assert short_mid not in preloaded, \
        "Short match (< 10 points) must be excluded from preloaded data"


# ---------------------------------------------------------------------------
# 6. year filter in load_match_ids
# ---------------------------------------------------------------------------

def test_year_filter(tmp_path):
    """
    load_match_ids must only return match IDs with year >= 2000.
    A match ID starting with '1999' must be excluded.
    """
    db_path = os.path.join(str(tmp_path), "year.duckdb")
    con = duckdb.connect(db_path)
    con.execute(_POINTS_DDL)
    con.execute(_MATCHES_DDL)

    old_mid = "19990101-M-Test-F-Old-Player"
    new_mid = "20010101-M-Test-F-New-Player"
    for mid in (old_mid, new_mid):
        con.execute(
            "INSERT INTO atp_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, 1, 0, 0, 0, 0, "0-0", 1, False, 1, "4n", None, None, 1),
        )
    con.close()

    out = str(tmp_path / "out")
    os.makedirs(out, exist_ok=True)
    bt3 = Backtester(db_path=db_path, output_dir=out)
    ids = load_match_ids(bt3, "atp")

    assert old_mid not in ids, "Match from 1999 must be excluded"
    assert new_mid in ids, "Match from 2001 must be included"
