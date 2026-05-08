"""Tests for Component 5B: model_training.

Mix of synthetic-data tests (fast) and one real-data smoke pipeline that runs
on a subset of the train split with a 2-combination grid. The smoke pipeline
output is reused by three tests via a module-scoped fixture.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import joblib
import numpy as np
import pytest

from src.model_training import (
    DEFAULT_PARAM_GRID,
    evaluate_on_test,
    train_calibrated_models,
    tune_hyperparameters,
)

DB_PATH = "data/processed/tennis.duckdb"
SUBSET_TRAIN_ROWS = 5000


def _make_synthetic_classification(
    n_samples: int = 200,
    n_features: int = 6,
    n_groups: int = 20,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A small linearly-separable-ish dataset with `n_groups` distinct groups.

    Each group spans n_samples // n_groups rows so GroupKFold(5) has at least
    5 distinct groups to split on.
    """
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n_samples, n_features))
    w = rng.standard_normal(n_features)
    logits = X @ w
    p = 1.0 / (1.0 + np.exp(-logits))
    y = (rng.random(n_samples) < p).astype(np.int64)
    rows_per_group = n_samples // n_groups
    groups = np.repeat(np.arange(n_groups), rows_per_group)
    if len(groups) < n_samples:
        groups = np.concatenate([groups, np.full(n_samples - len(groups), n_groups - 1)])
    return X.astype(np.float64), y, groups


@pytest.fixture(scope="module")
def synthetic_data():
    return _make_synthetic_classification(n_samples=200, n_features=6, n_groups=20)


@pytest.fixture(scope="module")
def smoke_pipeline(tmp_path_factory):
    """Run the full pipeline on a real-data subset with a 2-combo grid.

    Reused by:
        - test_full_pipeline_smoke
        - test_metadata_json_schema
        - test_test_brier_below_floor
    """
    if not os.path.exists(DB_PATH):
        pytest.skip(f"DuckDB not found at {DB_PATH}")

    from src.data_loader import load_ml_data

    bundle = load_ml_data(DB_PATH)
    X_train_full = bundle["X_train"]
    y_train_full = bundle["y_train"]
    X_test = bundle["X_test"]
    y_test = bundle["y_test"]
    feature_names = bundle["feature_names"]
    train_meta_full = bundle["train_meta"]
    groups_full = train_meta_full["match_id_int"].to_numpy()

    n = min(SUBSET_TRAIN_ROWS, len(X_train_full))
    X_train = X_train_full[:n]
    y_train = y_train_full[:n]
    groups = groups_full[:n]

    small_grid = {
        "max_depth": [4],
        "learning_rate": [0.1],
        "n_estimators": [100, 200],
        "reg_lambda": [1.0],
    }

    start = time.time()
    tune_result = tune_hyperparameters(
        X_train, y_train, groups, verbose=0, param_grid=small_grid
    )
    models = train_calibrated_models(
        X_train, y_train, groups, tune_result["best_params"], verbose=0
    )
    test_briers = evaluate_on_test(models, X_test, y_test)
    elapsed = time.time() - start

    selected_name = min(test_briers, key=test_briers.get)
    selected_model = models[selected_name]
    selected_brier = test_briers[selected_name]
    calibration_method = {"uncalibrated": "none", "platt": "sigmoid", "isotonic": "isotonic"}[
        selected_name
    ]

    out_dir = tmp_path_factory.mktemp("smoke_artifacts")
    model_path = out_dir / "model_v1.joblib"
    metadata_path = out_dir / "model_v1_metadata.json"

    joblib.dump(selected_model, model_path)
    metadata = {
        "timestamp": "2026-05-06T00:00:00",
        "best_params": tune_result["best_params"],
        "calibration_method": calibration_method,
        "test_brier": selected_brier,
        "feature_names": feature_names,
        "train_size": int(X_train.shape[0]),
        "test_size": int(X_test.shape[0]),
        "total_runtime_seconds": float(elapsed),
    }
    with metadata_path.open("w") as f:
        json.dump(metadata, f, indent=2)

    return {
        "tune_result": tune_result,
        "models": models,
        "test_briers": test_briers,
        "selected_name": selected_name,
        "selected_brier": selected_brier,
        "model_path": model_path,
        "metadata_path": metadata_path,
        "metadata": metadata,
        "X_test": X_test,
        "y_test": y_test,
    }


def test_tune_returns_best_params_in_grid(synthetic_data):
    X, y, groups = synthetic_data
    grid = {
        "max_depth": [3, 5],
        "learning_rate": [0.1],
        "n_estimators": [10],
        "reg_lambda": [1.0, 5.0],
    }
    result = tune_hyperparameters(X, y, groups, verbose=0, param_grid=grid)

    assert set(result.keys()) == {"best_params", "best_cv_brier", "cv_results_summary"}
    best_params = result["best_params"]
    assert set(best_params.keys()) == set(grid.keys())
    for k, choices in grid.items():
        assert best_params[k] in choices, f"{k}={best_params[k]} not in grid choices {choices}"
    assert isinstance(result["best_cv_brier"], float)
    assert result["best_cv_brier"] >= 0.0
    assert isinstance(result["cv_results_summary"], list)
    assert len(result["cv_results_summary"]) == 4
    for row in result["cv_results_summary"]:
        assert set(row.keys()) == {"params", "mean_brier", "std_brier"}


def test_train_calibrated_models_returns_three(synthetic_data):
    X, y, groups = synthetic_data
    best_params = {
        "max_depth": 3,
        "learning_rate": 0.1,
        "n_estimators": 10,
        "reg_lambda": 1.0,
    }
    models = train_calibrated_models(X, y, groups, best_params, verbose=0)

    assert set(models.keys()) == {"uncalibrated", "platt", "isotonic"}
    for name, model in models.items():
        assert hasattr(model, "predict_proba"), f"{name} missing predict_proba"
        proba = model.predict_proba(X[:5])
        assert proba.shape == (5, 2)


def test_evaluate_returns_dict_of_briers(synthetic_data):
    X, y, groups = synthetic_data
    best_params = {
        "max_depth": 3,
        "learning_rate": 0.1,
        "n_estimators": 10,
        "reg_lambda": 1.0,
    }
    models = train_calibrated_models(X, y, groups, best_params, verbose=0)
    briers = evaluate_on_test(models, X, y)

    assert set(briers.keys()) == {"uncalibrated", "platt", "isotonic"}
    for name, val in briers.items():
        assert isinstance(val, float), f"{name} brier not a float: {type(val)}"
        assert 0.0 <= val <= 1.0, f"{name} brier outside [0,1]: {val}"


def test_predicted_probs_in_range(synthetic_data):
    X, y, groups = synthetic_data
    best_params = {
        "max_depth": 3,
        "learning_rate": 0.1,
        "n_estimators": 10,
        "reg_lambda": 1.0,
    }
    models = train_calibrated_models(X, y, groups, best_params, verbose=0)
    for name, model in models.items():
        proba = model.predict_proba(X)[:, 1]
        assert proba.min() >= 0.0, f"{name}: min prob {proba.min()} < 0"
        assert proba.max() <= 1.0, f"{name}: max prob {proba.max()} > 1"


def test_save_and_load_model(synthetic_data, tmp_path):
    X, y, groups = synthetic_data
    best_params = {
        "max_depth": 3,
        "learning_rate": 0.1,
        "n_estimators": 10,
        "reg_lambda": 1.0,
    }
    models = train_calibrated_models(X, y, groups, best_params, verbose=0)

    for name, model in models.items():
        path = tmp_path / f"{name}.joblib"
        joblib.dump(model, path)
        loaded = joblib.load(path)
        original_proba = model.predict_proba(X)
        loaded_proba = loaded.predict_proba(X)
        np.testing.assert_array_equal(original_proba, loaded_proba)


def test_full_pipeline_smoke(smoke_pipeline):
    """End-to-end pipeline run on real-data subset with 2-combo grid."""
    sp = smoke_pipeline
    assert sp["model_path"].exists(), "model joblib was not written"
    assert sp["metadata_path"].exists(), "metadata json was not written"
    assert set(sp["test_briers"].keys()) == {"uncalibrated", "platt", "isotonic"}
    for name, val in sp["test_briers"].items():
        assert 0.0 <= val <= 1.0, f"{name} brier outside [0,1]: {val}"
    assert sp["selected_name"] in {"uncalibrated", "platt", "isotonic"}
    assert "best_params" in sp["tune_result"]
    assert "best_cv_brier" in sp["tune_result"]


def test_metadata_json_schema(smoke_pipeline):
    """Metadata sidecar must contain every documented field."""
    with smoke_pipeline["metadata_path"].open() as f:
        meta = json.load(f)
    required_keys = {
        "timestamp",
        "best_params",
        "calibration_method",
        "test_brier",
        "feature_names",
        "train_size",
        "test_size",
        "total_runtime_seconds",
    }
    assert required_keys.issubset(meta.keys()), (
        f"missing keys: {required_keys - meta.keys()}"
    )
    assert isinstance(meta["best_params"], dict)
    assert meta["calibration_method"] in {"none", "sigmoid", "isotonic"}
    assert isinstance(meta["test_brier"], float)
    assert isinstance(meta["feature_names"], list)
    assert len(meta["feature_names"]) == 63
    assert meta["train_size"] > 0
    assert meta["test_size"] > 0


def test_test_brier_below_floor(smoke_pipeline):
    """Selected model must beat the constant-0.5 floor (0.25)."""
    assert smoke_pipeline["selected_brier"] < 0.25, (
        f"selected model Brier {smoke_pipeline['selected_brier']:.4f} did not "
        f"beat the constant-0.5 floor of 0.25"
    )
