"""Tests for Component 5A: baseline predictors."""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import brier_score_loss

from src.baseline import (
    load_p0_lookup,
    predict_hard_pick,
    predict_soft_markov,
)

DB_PATH = "data/processed/tennis.duckdb"


def _meta(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


@pytest.mark.skipif(not os.path.exists(DB_PATH), reason="DuckDB not found")
def test_p0_lookup_loads():
    lookup = load_p0_lookup(DB_PATH)
    assert isinstance(lookup, dict)
    assert len(lookup) >= 500
    for name, p0 in list(lookup.items())[:10]:
        assert isinstance(name, str)
        assert 0.0 < p0 < 1.0


def test_hard_pick_outputs_valid():
    p0 = {"A1": 0.70, "A2": 0.55, "B1": 0.65, "B2": 0.65}
    df = _meta([
        dict(match_id_int=1, set_number=1, game_number_in_set=1, player_A="A1", player_B="A2", server_was_A=True),
        dict(match_id_int=1, set_number=1, game_number_in_set=2, player_A="A2", player_B="A1", server_was_A=False),
        dict(match_id_int=2, set_number=1, game_number_in_set=1, player_A="B1", player_B="B2", server_was_A=True),
    ])
    preds = predict_hard_pick(df, p0)
    assert preds.shape == (3,)
    assert set(np.unique(preds).tolist()) <= {0.0, 0.5, 1.0}
    assert preds[0] == 1.0
    assert preds[1] == 0.0
    assert preds[2] == 0.5


def test_soft_markov_outputs_valid():
    p0 = {"Strong": 0.72, "Weak": 0.55}
    df = _meta([
        dict(match_id_int=1, set_number=1, game_number_in_set=1, player_A="Strong", player_B="Weak", server_was_A=True),
        dict(match_id_int=1, set_number=1, game_number_in_set=2, player_A="Strong", player_B="Weak", server_was_A=False),
        dict(match_id_int=1, set_number=2, game_number_in_set=1, player_A="Strong", player_B="Weak", server_was_A=False),
        dict(match_id_int=2, set_number=1, game_number_in_set=1, player_A="Weak", player_B="Strong", server_was_A=True),
    ])
    preds = predict_soft_markov(df, p0)
    assert preds.shape == (4,)
    assert (preds > 0.0).all()
    assert (preds < 1.0).all()


def test_soft_markov_constant_within_set():
    p0 = {"Alpha": 0.68, "Beta": 0.60}
    df = _meta([
        dict(match_id_int=10, set_number=1, game_number_in_set=1, player_A="Alpha", player_B="Beta", server_was_A=True),
        dict(match_id_int=10, set_number=1, game_number_in_set=2, player_A="Alpha", player_B="Beta", server_was_A=False),
        dict(match_id_int=10, set_number=1, game_number_in_set=3, player_A="Alpha", player_B="Beta", server_was_A=True),
        dict(match_id_int=10, set_number=2, game_number_in_set=1, player_A="Alpha", player_B="Beta", server_was_A=False),
        dict(match_id_int=10, set_number=2, game_number_in_set=2, player_A="Alpha", player_B="Beta", server_was_A=True),
    ])
    preds = predict_soft_markov(df, p0)

    set1 = preds[(df["set_number"] == 1).to_numpy()]
    set2 = preds[(df["set_number"] == 2).to_numpy()]
    assert np.allclose(set1, set1[0])
    assert np.allclose(set2, set2[0])


def test_soft_markov_symmetry():
    p0 = {"Even1": 0.65, "Even2": 0.65}
    df = _meta([
        dict(match_id_int=1, set_number=1, game_number_in_set=1, player_A="Even1", player_B="Even2", server_was_A=True),
        dict(match_id_int=2, set_number=1, game_number_in_set=1, player_A="Even1", player_B="Even2", server_was_A=False),
    ])
    preds = predict_soft_markov(df, p0)
    assert preds.shape == (2,)
    assert abs(preds[0] - 0.5) < 1e-9
    assert abs(preds[1] - 0.5) < 1e-9


def test_baseline_brier_better_than_floor():
    p0 = {"Strong": 0.75, "Weak": 0.50}
    rows = []
    for i in range(6):
        rows.append(dict(
            match_id_int=i + 1,
            set_number=1,
            game_number_in_set=1,
            player_A="Strong",
            player_B="Weak",
            server_was_A=(i % 2 == 0),
        ))
    df = _meta(rows)
    y_true = np.ones(len(df), dtype=np.int64)

    hard = predict_hard_pick(df, p0)
    soft = predict_soft_markov(df, p0)
    floor = brier_score_loss(y_true, np.full_like(y_true, 0.5, dtype=np.float64))

    brier_hard = brier_score_loss(y_true, hard)
    brier_soft = brier_score_loss(y_true, soft)
    assert brier_hard < floor, f"hard={brier_hard:.4f} not better than floor={floor:.4f}"
    assert brier_soft < floor, f"soft={brier_soft:.4f} not better than floor={floor:.4f}"
