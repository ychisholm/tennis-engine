"""Tests for Component 5A: data_loader.

Skipped if the DuckDB database doesn't exist yet.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from src.data_loader import (
    EXPECTED_TEST_ROWS,
    EXPECTED_TRAIN_ROWS,
    SURFACE_DUMMIES,
    load_ml_data,
)

DB_PATH = "data/processed/tennis.duckdb"

pytestmark = pytest.mark.skipif(
    not os.path.exists(DB_PATH),
    reason=f"DuckDB not found at {DB_PATH}",
)


@pytest.fixture(scope="module")
def bundle():
    return load_ml_data(DB_PATH)


def test_data_loads_without_error(bundle):
    for key in (
        "X_train",
        "y_train",
        "X_test",
        "y_test",
        "feature_names",
        "train_meta",
        "test_meta",
    ):
        assert key in bundle, f"missing key {key!r}"


def test_feature_count_is_63(bundle):
    assert len(bundle["feature_names"]) == 63
    assert bundle["X_train"].shape[1] == 63
    assert bundle["X_test"].shape[1] == 63


def test_no_nulls_in_features(bundle):
    assert not np.isnan(bundle["X_train"]).any()
    assert not np.isnan(bundle["X_test"]).any()


def test_surface_onehot_exclusive(bundle):
    feature_names = bundle["feature_names"]
    surface_idx = [feature_names.index(c) for c in SURFACE_DUMMIES]
    for arr in (bundle["X_train"], bundle["X_test"]):
        sub = arr[:, surface_idx]
        assert ((sub == 0) | (sub == 1)).all(), "surface dummies must be 0/1"
        assert (sub.sum(axis=1) == 1).all(), "exactly one surface dummy must be 1 per row"


def test_target_is_binary(bundle):
    for y in (bundle["y_train"], bundle["y_test"]):
        unique = set(np.unique(y).tolist())
        assert unique == {0, 1}, f"expected {{0, 1}}, got {unique}"


def test_train_test_row_counts(bundle):
    n_train = bundle["X_train"].shape[0]
    n_test = bundle["X_test"].shape[0]
    assert abs(n_train - EXPECTED_TRAIN_ROWS) <= EXPECTED_TRAIN_ROWS * 0.01
    assert abs(n_test - EXPECTED_TEST_ROWS) <= EXPECTED_TEST_ROWS * 0.01


def test_feature_names_match_X_columns(bundle):
    assert len(bundle["feature_names"]) == bundle["X_train"].shape[1]
    assert len(bundle["feature_names"]) == bundle["X_test"].shape[1]
    assert len(bundle["feature_names"]) == len(set(bundle["feature_names"])), (
        "feature_names must be unique"
    )


def test_meta_has_required_columns(bundle):
    required = {
        "match_id_int",
        "set_number",
        "game_number_in_set",
        "player_A",
        "player_B",
        "server_was_A",
    }
    for meta in (bundle["train_meta"], bundle["test_meta"]):
        missing = required - set(meta.columns)
        assert not missing, f"meta missing columns: {missing}"
        assert len(meta) > 0
