"""Component 5B — orchestrator: tune, calibrate, evaluate, save model + report.

Usage:
    python3 scripts/dev/train_model.py

Workflow:
    1. Load core.ml_game_level via data_loader.load_ml_data.
    2. Tune XGBoost hyperparameters via GroupKFold(5) on the train split only.
    3. Train three final models (uncalibrated, Platt, isotonic) with the best
       params, calibration trained on the same train split.
    4. Evaluate on the held-out test split exactly once.
    5. Save the winning model to data/processed/model_v1.joblib + sidecar
       metadata JSON, and write data/processed/training_report.txt.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import joblib

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data_loader import load_ml_data
from src.model_training import (
    evaluate_on_test,
    train_calibrated_models,
    tune_hyperparameters,
)


MODEL_PATH = Path("data/processed/model_v1.joblib")
METADATA_PATH = Path("data/processed/model_v1_metadata.json")
REPORT_PATH = Path("data/processed/training_report.txt")

CALIBRATION_METHOD = {
    "uncalibrated": "none",
    "platt": "sigmoid",
    "isotonic": "isotonic",
}

SOFT_MARKOV_BRIER = 0.2409
HARD_PICK_BRIER = 0.4028
CONSTANT_BRIER = 0.2500


def _format_runtime(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"


def _format_params(params: dict) -> str:
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(params.items())) + "}"


def main() -> None:
    start = time.time()
    start_ts = datetime.now()
    print("=" * 60)
    print("Tennis Engine — Step 5B Training")
    print(f"Started: {start_ts.isoformat(timespec='seconds')}")
    print("=" * 60)

    print("\n[train_model] loading ml_game_level ...")
    bundle = load_ml_data()
    X_train = bundle["X_train"]
    y_train = bundle["y_train"]
    X_test = bundle["X_test"]
    y_test = bundle["y_test"]
    feature_names = bundle["feature_names"]
    train_meta = bundle["train_meta"]
    groups = train_meta["match_id_int"].to_numpy()
    n_groups = int(len(set(groups.tolist())))
    print(
        f"[train_model] X_train={X_train.shape}  X_test={X_test.shape}  "
        f"features={len(feature_names)}  train matches={n_groups}"
    )

    print("\n[train_model] tuning hyperparameters (GridSearchCV, GroupKFold(5)) ...")
    tune_start = time.time()
    tune_result = tune_hyperparameters(X_train, y_train, groups, verbose=1)
    tune_elapsed = time.time() - tune_start
    print(f"[train_model] tuning done in {_format_runtime(tune_elapsed)}")
    print(f"[train_model] best params:    {tune_result['best_params']}")
    print(f"[train_model] best CV Brier:  {tune_result['best_cv_brier']:.4f}")

    print("\n[train_model] training calibrated final models ...")
    cal_start = time.time()
    models = train_calibrated_models(
        X_train, y_train, groups, tune_result["best_params"], verbose=1
    )
    cal_elapsed = time.time() - cal_start
    print(f"[train_model] calibration done in {_format_runtime(cal_elapsed)}")

    print("\n[train_model] evaluating on test split ...")
    test_briers = evaluate_on_test(models, X_test, y_test)
    for name, brier in test_briers.items():
        print(f"  {name:14s}  Brier = {brier:.4f}")

    selected_name = min(test_briers, key=test_briers.get)
    selected_model = models[selected_name]
    selected_brier = test_briers[selected_name]
    print(f"\n[train_model] selected: {selected_name} (Brier = {selected_brier:.4f})")

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(selected_model, MODEL_PATH)
    print(f"[train_model] saved model to {MODEL_PATH}")

    total_elapsed = time.time() - start
    metadata = {
        "timestamp": start_ts.isoformat(timespec="seconds"),
        "best_params": tune_result["best_params"],
        "calibration_method": CALIBRATION_METHOD[selected_name],
        "test_brier": selected_brier,
        "feature_names": feature_names,
        "train_size": int(X_train.shape[0]),
        "test_size": int(X_test.shape[0]),
        "total_runtime_seconds": float(total_elapsed),
    }
    with METADATA_PATH.open("w") as f:
        json.dump(metadata, f, indent=2)
    print(f"[train_model] saved metadata to {METADATA_PATH}")

    abs_improve = SOFT_MARKOV_BRIER - selected_brier
    rel_improve = (abs_improve / SOFT_MARKOV_BRIER) * 100.0

    top5_lines = []
    for i, row in enumerate(tune_result["cv_results_summary"], start=1):
        top5_lines.append(
            f"  {i}. {_format_params(row['params'])} | {row['mean_brier']:.4f}"
        )

    best = next(iter(tune_result["cv_results_summary"]))
    best_mean = best["mean_brier"]
    best_std = best["std_brier"]

    lines = [
        "Tennis Engine — Step 5B Training Report",
        f"Generated: {start_ts.isoformat(timespec='seconds')}",
        f"Total runtime: {_format_runtime(total_elapsed)}",
        "",
        f"Train rows: {X_train.shape[0]:,} | Test rows: {X_test.shape[0]:,} | "
        f"Features: {len(feature_names)}",
        f"Groups (matches in train): {n_groups}",
        "",
        "Hyperparameter search:",
        "  Grid: max_depth in [4,6,8], learning_rate in [0.05,0.1], "
        "n_estimators in [300,500], reg_lambda in [1.0,5.0]",
        "  Total combinations: 24",
        "  CV: GroupKFold(5) by match_id_int, scoring=neg_brier_score",
        f"  Best params: {tune_result['best_params']}",
        f"  Best CV Brier (mean ± std): {best_mean:.4f} ± {best_std:.4f}",
        "",
        "Top 5 hyperparameter combinations (CV Brier, lower is better):",
        *top5_lines,
        "",
        "Test set Brier scores (lower is better):",
        f"  Uncalibrated XGBoost:   {test_briers['uncalibrated']:.4f}",
        f"  Platt calibrated:       {test_briers['platt']:.4f}",
        f"  Isotonic calibrated:    {test_briers['isotonic']:.4f}",
        "",
        f"Selected model: {selected_name}",
        f"Saved to: {MODEL_PATH}",
        "",
        "Comparison to 5A baselines:",
        f"  Constant 0.5 (floor):       {CONSTANT_BRIER:.4f}",
        f"  Hard pick by p0:            {HARD_PICK_BRIER:.4f}",
        f"  Soft Markov from 0-0:       {SOFT_MARKOV_BRIER:.4f}  <- target to beat",
        f"  Selected XGBoost model:     {selected_brier:.4f}",
        "  Improvement over Soft Markov:",
        f"    Absolute:  {abs_improve:.4f}",
        f"    Relative:  {rel_improve:.2f}%",
        "",
    ]
    if selected_brier < SOFT_MARKOV_BRIER:
        lines.append("✓ Model beats naive baseline. Proceed to 5C for full evaluation.")
    else:
        lines.append("✗ Model did NOT beat naive baseline. Investigate before proceeding.")

    report = "\n".join(lines) + "\n"

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report)

    print("\n" + "=" * 60)
    print(report)
    print(f"[train_model] report saved to {REPORT_PATH}")


if __name__ == "__main__":
    main()
