"""Component 5C — orchestrator: full evaluation suite for the trained model.

Usage:
    python3 scripts/dev/evaluate_model.py

Workflow:
    1. Load test data + feature names + meta via data_loader.load_ml_data.
    2. Load the trained CalibratedClassifierCV from disk.
    3. Score the test set once.
    4. Run all five evaluation functions (calibration, confidence buckets,
       performance by game number + decided/undecided, feature importance).
    5. Render four PNG plots into data/processed/.
    6. Write a human-readable report to data/processed/evaluation_report.txt.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sklearn.metrics import brier_score_loss

from src.data_loader import load_ml_data
from src.evaluation import (
    compute_calibration_curve,
    compute_confidence_buckets,
    compute_feature_importance,
    compute_performance_by_game_number,
    load_trained_model,
    make_plots,
)


MODEL_PATH = "data/processed/model_v1.joblib"
OUTPUT_DIR = Path("data/processed")
REPORT_PATH = OUTPUT_DIR / "evaluation_report.txt"

SOFT_MARKOV_BRIER = 0.2409


def _calibration_block(curve: dict) -> tuple[list[str], str]:
    lines = [
        f"=== CALIBRATION CURVE ({curve['n_bins']} bins) ===",
        "Bin       | Mean Predicted | Actual Fraction | Diff",
    ]
    n = curve["n_bins"]
    edges = np.linspace(0.0, 1.0, n + 1)
    fp = curve["fraction_of_positives"]
    mp = curve["mean_predicted_value"]
    diffs = []
    fp_iter = iter(fp)
    mp_iter = iter(mp)
    for lo, hi in zip(edges[:-1], edges[1:]):
        try:
            mean_pred = next(mp_iter)
            actual = next(fp_iter)
        except StopIteration:
            lines.append(f"{lo:.1f}-{hi:.1f}   | --             | --              | --")
            continue
        if not (lo <= mean_pred < hi or (hi >= 1.0 and mean_pred <= 1.0)):
            lines.append(f"{lo:.1f}-{hi:.1f}   | --             | --              | --")
            mp_iter = iter([mean_pred] + list(mp_iter))
            fp_iter = iter([actual] + list(fp_iter))
            continue
        diff = actual - mean_pred
        diffs.append((lo, hi, diff))
        sign = "+" if diff >= 0 else ""
        lines.append(
            f"{lo:.1f}-{hi:.1f}   | {mean_pred:.4f}         | {actual:.4f}          | {sign}{diff:.4f}"
        )

    if not diffs:
        interp = "no calibration bins populated."
    else:
        max_dev = max(diffs, key=lambda t: abs(t[2]))
        if abs(max_dev[2]) < 0.03:
            interp = (
                f"Predictions track the diagonal closely; max deviation "
                f"{max_dev[2]:+.4f} in bin {max_dev[0]:.1f}-{max_dev[1]:.1f}."
            )
        else:
            direction = "over" if max_dev[2] < 0 else "under"
            interp = (
                f"Model is {direction}-confident in bin {max_dev[0]:.1f}-{max_dev[1]:.1f} "
                f"(deviation {max_dev[2]:+.4f}); other bins within ~{0.03:.2f} of the diagonal."
            )
    return lines, interp


def main() -> None:
    started = datetime.now()
    print("=" * 60)
    print("Tennis Engine — Step 5C Evaluation")
    print(f"Started: {started.isoformat(timespec='seconds')}")
    print("=" * 60)

    print("\n[evaluate_model] loading test data ...")
    bundle = load_ml_data()
    X_test = bundle["X_test"]
    y_test = bundle["y_test"]
    feature_names = bundle["feature_names"]
    test_meta = bundle["test_meta"]

    print("[evaluate_model] loading trained model ...")
    model = load_trained_model(MODEL_PATH)

    print("[evaluate_model] generating predictions ...")
    y_prob = model.predict_proba(X_test)[:, 1]
    test_brier = float(brier_score_loss(y_test, y_prob))
    rel_improve = (SOFT_MARKOV_BRIER - test_brier) / SOFT_MARKOV_BRIER * 100.0

    print("[evaluate_model] computing calibration curve ...")
    calib = compute_calibration_curve(y_test, y_prob, n_bins=10)

    print("[evaluate_model] computing confidence buckets ...")
    conf_df = compute_confidence_buckets(y_test, y_prob)

    print("[evaluate_model] computing performance by game number ...")
    perf = compute_performance_by_game_number(y_test, y_prob, test_meta)

    print("[evaluate_model] computing feature importance ...")
    imp_df = compute_feature_importance(model, feature_names)

    print("[evaluate_model] rendering plots ...")
    plot_paths = make_plots(calib, conf_df, perf, imp_df, str(OUTPUT_DIR))
    for name, p in plot_paths.items():
        print(f"  {name:24s} -> {p}")

    # Build report
    lines: list[str] = []
    lines += [
        "Tennis Engine — Step 5C Evaluation Report",
        f"Generated: {started.isoformat(timespec='seconds')}",
        f"Model: {MODEL_PATH} (isotonic-calibrated XGBoost, from 5B)",
        f"Test set rows: {len(y_test):,}",
        "",
        "=== HEADLINE BRIER (recap from 5B) ===",
        f"Test Brier:        {test_brier:.4f}",
        f"Soft Markov:       {SOFT_MARKOV_BRIER:.4f}",
        f"Improvement:       {rel_improve:.2f}% relative",
        "",
    ]

    calib_lines, calib_interp = _calibration_block(calib)
    lines += calib_lines
    lines += [
        f"Calibration plot saved to {plot_paths['calibration_curve']}",
        f"Interpretation: {calib_interp}",
        "",
    ]

    lines += [
        "=== CONFIDENCE-BUCKETED ACCURACY ===",
        "Bucket    | Count  | Accuracy | Mean Confidence | Mean Brier",
    ]
    for _, row in conf_df.iterrows():
        if row["count"] == 0:
            lines.append(
                f"{row['bucket_label']} | 0      | --       | --              | --"
            )
            continue
        lines.append(
            f"{row['bucket_label']} | {int(row['count']):<6d} | "
            f"{row['accuracy'] * 100:5.2f}%   | {row['mean_confidence']:.4f}          | "
            f"{row['mean_brier']:.4f}"
        )
    lines.append(f"Plot saved to {plot_paths['confidence_buckets']}")

    populated = conf_df.dropna(subset=["accuracy"]).reset_index(drop=True)
    if len(populated) > 0:
        accs = populated["accuracy"].to_numpy()
        is_monotone = bool(np.all(np.diff(accs) >= -1e-9))
        top_bucket = populated.iloc[-1]
        monotone_word = "monotonically" if is_monotone else "with one or more dips"
        lines.append(
            f"Interpretation: accuracy climbs {monotone_word} with confidence; "
            f"the top bucket ({top_bucket['bucket_label']}) has "
            f"{int(top_bucket['count'])} predictions at "
            f"{top_bucket['accuracy'] * 100:.2f}% accuracy — "
            f"{'a viable selective edge' if top_bucket['accuracy'] >= 0.75 else 'modest selective edge'}."
        )
    else:
        lines.append("Interpretation: no confidence buckets populated.")
    lines.append("")

    lines += [
        "=== PERFORMANCE BY POSITION IN SET ===",
        "Game Number | Count  | Mean Pred Prob | Brier  | Accuracy",
    ]
    for game_no, row in perf["by_game_number"].iterrows():
        lines.append(
            f"{int(game_no):<11d} | {int(row['count']):<6d} | "
            f"{row['mean_predicted_prob']:.4f}         | "
            f"{row['brier']:.4f} | {row['accuracy'] * 100:5.2f}%"
        )
    lines.append(f"Plot saved to {plot_paths['brier_by_game_number']}")
    lines.append("")

    dvu = perf["decided_vs_undecided"]
    decided = dvu.loc["decided"]
    undecided = dvu.loc["undecided"]
    lines += [
        "Decided vs Undecided rows:",
        "  Decided rows (last game of each set):",
        f"    Count:  {int(decided['count']):,}",
        f"    Model Brier:        {decided['brier']:.4f}",
        f"    Soft Markov Brier:  {decided['soft_markov_brier_on_same_subset']:.4f}",
        "  Undecided rows (mid-set):",
        f"    Count:  {int(undecided['count']):,}",
        f"    Model Brier:        {undecided['brier']:.4f}",
        f"    Soft Markov Brier:  {undecided['soft_markov_brier_on_same_subset']:.4f}",
    ]
    decided_edge = undecided["brier"] - decided["brier"]
    model_undecided_edge = undecided["soft_markov_brier_on_same_subset"] - undecided["brier"]
    lines.append(
        f"Interpretation: decided rows are easier (Brier {decided['brier']:.4f}) "
        f"than mid-set rows (Brier {undecided['brier']:.4f}, gap {decided_edge:+.4f}); "
        f"the model still beats Soft Markov on undecided rows by {model_undecided_edge:.4f} "
        f"Brier — that's the honest read."
    )
    lines.append("")

    lines += [
        "=== FEATURE IMPORTANCE ===",
        "Top 15 individual features:",
    ]
    for i, (_, row) in enumerate(imp_df.head(15).iterrows(), start=1):
        lines.append(
            f"  {i:>2}. {row['feature_name']:<28s} {row['signal_group']:<14s} {row['importance']:.4f}"
        )

    grouped = (
        imp_df.groupby("signal_group")["importance"].sum().sort_values(ascending=False)
    )
    lines += ["", "Aggregated by signal group (sum of importances):"]
    for grp, val in grouped.items():
        lines.append(f"  {grp:<15s} {val:.4f}")
    lines.append(f"Plot saved to {plot_paths['feature_importance']}")
    lines.append("")

    # Summary
    cal_summary = (
        "good" if "track the diagonal closely" in calib_interp else "needs work"
    )
    high_conf = populated.iloc[-1] if len(populated) > 0 else None
    if high_conf is not None and high_conf["accuracy"] >= 0.75:
        edge_summary = (
            f"viable — {int(high_conf['count'])} predictions at "
            f"{high_conf['accuracy'] * 100:.2f}% in the {high_conf['bucket_label']} bucket"
        )
    elif high_conf is not None:
        edge_summary = (
            f"modest — top bucket ({high_conf['bucket_label']}) at "
            f"{high_conf['accuracy'] * 100:.2f}%"
        )
    else:
        edge_summary = "no high-confidence predictions"

    if not np.isnan(decided["brier"]) and not np.isnan(undecided["brier"]):
        mid_summary = (
            f"undecided rows carry Brier {undecided['brier']:.4f} vs decided "
            f"{decided['brier']:.4f}; the headline {test_brier:.4f} reflects a "
            f"mix of trivial and genuinely uncertain rows"
        )
    else:
        mid_summary = "decided/undecided split could not be computed"

    if len(grouped) > 0:
        top_groups = grouped.head(3)
        signal_summary = ", ".join(
            f"{g} ({v:.3f})" for g, v in top_groups.items()
        )
    else:
        signal_summary = "no signal groups present"

    lines += [
        "=== SUMMARY ===",
        f"- Calibration: {cal_summary}.",
        f"- High-confidence reliability: {edge_summary}.",
        f"- Mid-set vs decided: {mid_summary}.",
        f"- Signal contribution: top families are {signal_summary}.",
        "",
        "Ready to proceed to Step 6 (iteration) and eventually Block 7 "
        "(live integration) gated on 6C odds comparison.",
    ]

    report = "\n".join(lines) + "\n"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report)

    print("\n" + "=" * 60)
    print(report)
    print(f"[evaluate_model] report saved to {REPORT_PATH}")


if __name__ == "__main__":
    main()
