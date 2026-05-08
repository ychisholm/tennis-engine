"""Component 5A — orchestrator: compute and report baseline Brier scores.

Usage:
    python3 scripts/dev/run_baseline.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.metrics import brier_score_loss

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.baseline import load_p0_lookup, predict_hard_pick, predict_soft_markov
from src.data_loader import load_ml_data


REPORT_PATH = Path("data/processed/baseline_report.txt")


def hard_pick_accuracy(predictions: np.ndarray, y_true: np.ndarray) -> float:
    """Accuracy with 0.5 ties counted as half-correct."""
    correct = (predictions == y_true.astype(np.float64)).astype(np.float64)
    correct[predictions == 0.5] = 0.5
    return float(correct.mean())


def main() -> None:
    print("[run_baseline] loading ml_game_level …")
    bundle = load_ml_data()
    test_meta = bundle["test_meta"]
    y_test = bundle["y_test"]

    print("[run_baseline] loading player p0 lookup …")
    p0_lookup = load_p0_lookup()

    print("[run_baseline] computing hard-pick predictions …")
    hard_preds = predict_hard_pick(test_meta, p0_lookup)

    print("[run_baseline] computing soft-Markov predictions …")
    soft_preds = predict_soft_markov(test_meta, p0_lookup)

    constant_preds = np.full_like(y_test, 0.5, dtype=np.float64)
    brier_floor = brier_score_loss(y_test, constant_preds)
    brier_hard = brier_score_loss(y_test, hard_preds)
    brier_soft = brier_score_loss(y_test, soft_preds)

    n_one = int((hard_preds == 1.0).sum())
    n_zero = int((hard_preds == 0.0).sum())
    n_tie = int((hard_preds == 0.5).sum())
    hard_acc = hard_pick_accuracy(hard_preds, y_test)

    soft_mean = float(soft_preds.mean())
    soft_min = float(soft_preds.min())
    soft_max = float(soft_preds.max())
    n_close = int(((soft_preds >= 0.45) & (soft_preds <= 0.55)).sum())

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "Tennis Engine — Step 5A Baseline Report",
        f"Generated: {timestamp}",
        f"Test set rows: {len(y_test)}",
        "",
        "Brier scores (lower = better):",
        f"  Constant 0.5 (sanity floor):  {brier_floor:.4f}",
        f"  Hard pick by p0:              {brier_hard:.4f}",
        f"  Soft Markov from 0-0:         {brier_soft:.4f}",
        "",
        "Hard pick details:",
        f"  Predictions = 1.0:  {n_one}",
        f"  Predictions = 0.0:  {n_zero}",
        f"  Predictions = 0.5 (tie):  {n_tie}",
        f"  Accuracy:  {hard_acc * 100:.2f}%",
        "",
        "Soft Markov details:",
        f"  Mean predicted prob:  {soft_mean:.4f}",
        f"  Min predicted prob:   {soft_min:.4f}",
        f"  Max predicted prob:   {soft_max:.4f}",
        f"  Predictions in [0.45, 0.55] (close calls):  {n_close}",
        "",
        "Numbers for XGBoost to beat in 5B/5C:",
        f"  Primary target: Soft Markov Brier = {brier_soft:.4f}",
        f"  Floor:          Hard pick Brier = {brier_hard:.4f}",
    ]
    report = "\n".join(lines) + "\n"

    print()
    print(report)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report)
    print(f"[run_baseline] report saved to {REPORT_PATH}")


if __name__ == "__main__":
    main()
