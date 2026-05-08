"""Tests for Component 5C: evaluation suite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.calibration import CalibratedClassifierCV

from src.evaluation import (
    CONFIDENCE_BUCKET_LABELS,
    VALID_SIGNAL_GROUPS,
    compute_calibration_curve,
    compute_confidence_buckets,
    compute_feature_importance,
    compute_performance_by_game_number,
    load_trained_model,
    make_plots,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = "data/processed/tennis.duckdb"
MODEL_PATH = "data/processed/model_v1.joblib"


def _synth(n: int = 300, n_matches: int = 30, seed: int = 0):
    """Synthetic test bundle: predictions, truths, meta_df, p0_lookup."""
    rng = np.random.default_rng(seed)
    rows = []
    truths = []
    probs = []
    for m in range(n_matches):
        for s in (1, 2):
            n_games = rng.integers(6, 14)
            for g in range(1, n_games + 1):
                rows.append(
                    {
                        "match_id_int": m,
                        "set_number": s,
                        "game_number_in_set": int(g),
                        "player_A": f"P{m}A",
                        "player_B": f"P{m}B",
                        "server_was_A": bool(g % 2),
                    }
                )
                truths.append(int(rng.random() < 0.55))
                probs.append(float(np.clip(rng.beta(2, 2), 0.0, 1.0)))
                if len(rows) >= n:
                    break
            if len(rows) >= n:
                break
        if len(rows) >= n:
            break
    meta = pd.DataFrame(rows[:n])
    y_true = np.array(truths[:n], dtype=np.int64)
    y_prob = np.array(probs[:n], dtype=np.float64)
    p0_lookup = {f"P{m}A": 0.62 for m in range(n_matches)}
    p0_lookup.update({f"P{m}B": 0.58 for m in range(n_matches)})
    return y_true, y_prob, meta, p0_lookup


@pytest.mark.skipif(not os.path.exists(MODEL_PATH), reason="trained model not found")
def test_load_trained_model():
    model = load_trained_model(MODEL_PATH)
    assert isinstance(model, CalibratedClassifierCV)
    assert hasattr(model, "calibrated_classifiers_")
    assert len(model.calibrated_classifiers_) == 5


def test_calibration_curve_shape():
    rng = np.random.default_rng(0)
    y_true = (rng.random(1000) < 0.5).astype(int)
    y_prob = rng.random(1000)
    out = compute_calibration_curve(y_true, y_prob, n_bins=10)
    assert set(out.keys()) == {"fraction_of_positives", "mean_predicted_value", "n_bins"}
    assert out["n_bins"] == 10
    # populated bins ≤ n_bins; arrays are equal length
    assert len(out["fraction_of_positives"]) == len(out["mean_predicted_value"])
    assert len(out["fraction_of_positives"]) <= 10
    assert len(out["fraction_of_positives"]) > 0


def test_confidence_buckets_partition():
    y_true, y_prob, _, _ = _synth(n=400)
    df = compute_confidence_buckets(y_true, y_prob)
    assert list(df["bucket_label"]) == CONFIDENCE_BUCKET_LABELS
    assert int(df["count"].sum()) == len(y_true)


def test_confidence_bucket_accuracy_in_unit_range():
    y_true, y_prob, _, _ = _synth(n=400)
    df = compute_confidence_buckets(y_true, y_prob)
    populated = df.dropna(subset=["accuracy"])
    assert (populated["accuracy"] >= 0.0).all()
    assert (populated["accuracy"] <= 1.0).all()


def test_decided_vs_undecided_split():
    y_true, y_prob, meta, p0_lookup = _synth(n=400, n_matches=20)
    perf = compute_performance_by_game_number(y_true, y_prob, meta, p0_lookup=p0_lookup)
    dvu = perf["decided_vs_undecided"]
    total = int(dvu.loc["decided", "count"] + dvu.loc["undecided", "count"])
    assert total == len(y_true)
    expected_decided = meta.groupby(["match_id_int", "set_number"]).ngroups
    assert int(dvu.loc["decided", "count"]) == expected_decided


@pytest.mark.skipif(not os.path.exists(MODEL_PATH), reason="trained model not found")
def test_feature_importance_count():
    from src.data_loader import load_ml_data

    bundle = load_ml_data(DB_PATH)
    feature_names = bundle["feature_names"]
    model = load_trained_model(MODEL_PATH)
    df = compute_feature_importance(model, feature_names)
    assert len(df) == 63
    assert df["importance"].sum() == pytest.approx(1.0, abs=1e-3)
    assert set(df["signal_group"].unique()).issubset(VALID_SIGNAL_GROUPS)


def test_plots_render(tmp_path):
    y_true, y_prob, meta, p0_lookup = _synth(n=400, n_matches=20)
    calib = compute_calibration_curve(y_true, y_prob, n_bins=10)
    conf = compute_confidence_buckets(y_true, y_prob)
    perf = compute_performance_by_game_number(y_true, y_prob, meta, p0_lookup=p0_lookup)
    feature_names = (
        ["games_A", "games_B", "set_number", "sets_won_A", "sets_won_B", "game_number_in_set"]
        + ["surface_hard", "surface_clay", "surface_grass", "surface_unknown"]
        + [f"bpi_f{i}" for i in range(10)]
        + [f"sds_f{i}" for i in range(10)]
        + [f"res_f{i}" for i in range(10)]
        + [f"cpi_f{i}" for i in range(11)]
        + [f"mrs_f{i}" for i in range(11)]
        + ["markov_set_win_prob_A"]
    )
    assert len(feature_names) == 63

    rng = np.random.default_rng(0)
    raw = rng.random(63)
    imp = raw / raw.sum()

    class _FakeBase:
        def __init__(self, fi):
            self.feature_importances_ = fi

    class _FakeCal:
        def __init__(self, fi):
            self.estimator = _FakeBase(fi)

    class _FakeModel:
        def __init__(self):
            self.calibrated_classifiers_ = [_FakeCal(imp) for _ in range(5)]

    feat_df = compute_feature_importance(_FakeModel(), feature_names)
    out = tmp_path / "plots"
    paths = make_plots(calib, conf, perf, feat_df, str(out))
    expected_files = [
        "calibration_curve.png",
        "confidence_buckets.png",
        "brier_by_game_number.png",
        "feature_importance.png",
    ]
    for fname in expected_files:
        p = out / fname
        assert p.exists(), f"{fname} not created"
        assert p.stat().st_size > 1000, f"{fname} suspiciously small ({p.stat().st_size} bytes)"


@pytest.mark.skipif(
    not os.path.exists(DB_PATH) or not os.path.exists(MODEL_PATH),
    reason="DB or trained model not found",
)
def test_full_pipeline_smoke(tmp_path, monkeypatch):
    """End-to-end orchestrator run; verifies report + 4 plots are written."""
    monkeypatch.chdir(REPO_ROOT)
    script = REPO_ROOT / "scripts/dev/evaluate_model.py"
    venv_python = REPO_ROOT / ".venv/bin/python"
    py = str(venv_python) if venv_python.exists() else sys.executable

    result = subprocess.run(
        [py, str(script)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"orchestrator failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    report = REPO_ROOT / "data/processed/evaluation_report.txt"
    assert report.exists()
    text = report.read_text()
    assert "Step 5C Evaluation Report" in text
    assert "HEADLINE BRIER" in text
    assert "CALIBRATION CURVE" in text
    assert "CONFIDENCE-BUCKETED ACCURACY" in text
    assert "PERFORMANCE BY POSITION IN SET" in text
    assert "FEATURE IMPORTANCE" in text
    assert "SUMMARY" in text

    for fname in (
        "calibration_curve.png",
        "confidence_buckets.png",
        "brier_by_game_number.png",
        "feature_importance.png",
    ):
        p = REPO_ROOT / "data/processed" / fname
        assert p.exists(), f"{fname} not created by orchestrator"
        assert p.stat().st_size > 1000
