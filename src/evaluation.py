"""Component 5C — full evaluation suite for the trained model.

Six top-level functions:

* ``load_trained_model`` — joblib-load the calibrated XGBoost from disk.
* ``compute_calibration_curve`` — sklearn ``calibration_curve`` (uniform bins).
* ``compute_confidence_buckets`` — accuracy / Brier inside fixed confidence
  buckets defined on ``max(p, 1 - p)``.
* ``compute_performance_by_game_number`` — Brier / accuracy by
  ``game_number_in_set``, plus a decided-vs-undecided split with apples-to-
  apples Soft Markov Brier on each subset.
* ``compute_feature_importance`` — average ``feature_importances_`` across the
  five XGBClassifier base estimators inside the CalibratedClassifierCV.
* ``make_plots`` — write four PNG plots (calibration, confidence buckets,
  Brier-by-game-number, top-20 feature importances).
"""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.metrics import brier_score_loss


CONFIDENCE_BUCKET_EDGES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.01]
CONFIDENCE_BUCKET_LABELS = [
    "0.50-0.55",
    "0.55-0.60",
    "0.60-0.65",
    "0.65-0.70",
    "0.70-0.75",
    "0.75-0.80",
    "0.80+",
]

SIGNAL_PREFIX_TO_GROUP = {
    "bpi_": "BPI",
    "sds_": "SDS",
    "res_": "RES",
    "cpi_": "CPI",
    "mrs_": "MRS",
}

SCORE_CONTEXT_FEATURES = {
    "games_A",
    "games_B",
    "set_number",
    "sets_won_A",
    "sets_won_B",
    "game_number_in_set",
}

VALID_SIGNAL_GROUPS = {"score_context", "BPI", "SDS", "RES", "CPI", "MRS", "Markov"}


def load_trained_model(model_path: str = "data/processed/model_v1.joblib"):
    return joblib.load(model_path)


def compute_calibration_curve(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10
) -> dict:
    frac_pos, mean_pred = calibration_curve(
        y_true, y_prob, n_bins=n_bins, strategy="uniform"
    )
    return {
        "fraction_of_positives": frac_pos,
        "mean_predicted_value": mean_pred,
        "n_bins": n_bins,
    }


def compute_confidence_buckets(y_true: np.ndarray, y_prob: np.ndarray) -> pd.DataFrame:
    """Accuracy and Brier inside fixed confidence buckets.

    Confidence is ``max(p, 1 - p)``; the predicted winner is A if ``p > 0.5``,
    else B. Buckets are lower-inclusive, upper-exclusive (final bucket
    [0.80, 1.01) absorbs everything from 0.80 up).
    """
    y_true = np.asarray(y_true)
    y_prob = np.asarray(y_prob, dtype=np.float64)
    confidence = np.maximum(y_prob, 1.0 - y_prob)
    predicted_winner_a = y_prob > 0.5
    actual_winner_a = y_true == 1
    correct = (predicted_winner_a == actual_winner_a).astype(np.float64)

    rows = []
    for label, lo, hi in zip(
        CONFIDENCE_BUCKET_LABELS,
        CONFIDENCE_BUCKET_EDGES[:-1],
        CONFIDENCE_BUCKET_EDGES[1:],
    ):
        mask = (confidence >= lo) & (confidence < hi)
        n = int(mask.sum())
        if n == 0:
            rows.append(
                {
                    "bucket_label": label,
                    "count": 0,
                    "accuracy": float("nan"),
                    "mean_confidence": float("nan"),
                    "mean_brier": float("nan"),
                }
            )
            continue
        rows.append(
            {
                "bucket_label": label,
                "count": n,
                "accuracy": float(correct[mask].mean()),
                "mean_confidence": float(confidence[mask].mean()),
                "mean_brier": float(brier_score_loss(y_true[mask], y_prob[mask])),
            }
        )
    return pd.DataFrame(rows)


def _decided_mask(meta_df: pd.DataFrame) -> np.ndarray:
    """Boolean mask, same length as meta_df, True where the row is the last
    game of its (match_id_int, set_number) group."""
    df = meta_df.reset_index(drop=True).copy()
    df["__orig_idx"] = np.arange(len(df))
    df_sorted = df.sort_values(
        ["match_id_int", "set_number", "game_number_in_set"], kind="mergesort"
    )
    last_orig = (
        df_sorted.groupby(["match_id_int", "set_number"], sort=False)
        .tail(1)["__orig_idx"]
        .to_numpy()
    )
    mask = np.zeros(len(df), dtype=bool)
    mask[last_orig] = True
    return mask


def compute_performance_by_game_number(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    meta_df: pd.DataFrame,
    *,
    p0_lookup: dict | None = None,
) -> dict:
    """Brier / accuracy by game number plus decided-vs-undecided split.

    The optional ``p0_lookup`` kwarg lets tests inject a mock; when None we
    load it from the live DuckDB. Soft Markov is computed on the full
    meta_df (one prediction per set, broadcast to every row in that set) and
    indexed onto the decided/undecided subsets — predict_soft_markov needs
    ``game_number_in_set == 1`` per set, which the subsets don't always
    contain on their own.
    """
    y_true = np.asarray(y_true)
    y_prob = np.asarray(y_prob, dtype=np.float64)

    df = meta_df.reset_index(drop=True).copy()
    df["y_true"] = y_true
    df["y_prob"] = y_prob

    by_game_rows = []
    for game_no, group in df.groupby("game_number_in_set", sort=True):
        n = len(group)
        yt = group["y_true"].to_numpy()
        yp = group["y_prob"].to_numpy()
        pred_class = (yp > 0.5).astype(int)
        by_game_rows.append(
            {
                "game_number_in_set": int(game_no),
                "count": n,
                "mean_predicted_prob": float(yp.mean()),
                "brier": float(brier_score_loss(yt, yp)),
                "accuracy": float((pred_class == yt).mean()),
            }
        )
    by_game_df = pd.DataFrame(by_game_rows).set_index("game_number_in_set")

    decided_mask = _decided_mask(meta_df)

    if p0_lookup is None:
        from src.baseline import load_p0_lookup
        p0_lookup = load_p0_lookup()
    from src.baseline import predict_soft_markov

    soft_full = predict_soft_markov(meta_df, p0_lookup)

    def _bucket(mask: np.ndarray) -> dict:
        if not mask.any():
            return {
                "count": 0,
                "brier": float("nan"),
                "accuracy": float("nan"),
                "soft_markov_brier_on_same_subset": float("nan"),
            }
        yt = y_true[mask]
        yp = y_prob[mask]
        pred_class = (yp > 0.5).astype(int)
        return {
            "count": int(mask.sum()),
            "brier": float(brier_score_loss(yt, yp)),
            "accuracy": float((pred_class == yt).mean()),
            "soft_markov_brier_on_same_subset": float(
                brier_score_loss(yt, soft_full[mask])
            ),
        }

    decided_undecided = pd.DataFrame(
        [_bucket(decided_mask), _bucket(~decided_mask)],
        index=["decided", "undecided"],
    )

    return {
        "by_game_number": by_game_df,
        "decided_vs_undecided": decided_undecided,
    }


def _signal_group_for(name: str) -> str:
    if name in SCORE_CONTEXT_FEATURES or name.startswith("surface_"):
        return "score_context"
    if name == "markov_set_win_prob_A":
        return "Markov"
    for prefix, label in SIGNAL_PREFIX_TO_GROUP.items():
        if name.startswith(prefix):
            return label
    raise RuntimeError(f"unknown signal group for feature: {name!r}")


def compute_feature_importance(model, feature_names: list) -> pd.DataFrame:
    """Average XGB feature_importances_ across the K calibrated base estimators."""
    imps = np.vstack(
        [cc.estimator.feature_importances_ for cc in model.calibrated_classifiers_]
    )
    avg = imps.mean(axis=0)
    if len(avg) != len(feature_names):
        raise RuntimeError(
            f"feature_importances_ length {len(avg)} != len(feature_names) {len(feature_names)}"
        )
    rows = [
        {
            "feature_name": name,
            "importance": float(imp),
            "signal_group": _signal_group_for(name),
        }
        for name, imp in zip(feature_names, avg)
    ]
    return (
        pd.DataFrame(rows).sort_values("importance", ascending=False).reset_index(drop=True)
    )


def make_plots(
    calib_curve_data: dict,
    confidence_df: pd.DataFrame,
    perf_dict: dict,
    feature_importance_df: pd.DataFrame,
    output_dir: str,
) -> dict:
    """Write four PNG plots into ``output_dir``. Returns paths keyed by name."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    paths = {}

    # 1. Calibration curve
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="Perfect calibration")
    ax.plot(
        calib_curve_data["mean_predicted_value"],
        calib_curve_data["fraction_of_positives"],
        "o-",
        color="steelblue",
        label="Model",
    )
    ax.set_xlabel("Mean Predicted Probability")
    ax.set_ylabel("Fraction of Positives")
    ax.set_title("Calibration Curve")
    ax.set_xlim([0, 1])
    ax.set_ylim([0, 1])
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = out / "calibration_curve.png"
    fig.savefig(p, dpi=100)
    plt.close(fig)
    paths["calibration_curve"] = p

    # 2. Confidence buckets
    df = confidence_df.dropna(subset=["accuracy"]).reset_index(drop=True)
    fig, ax1 = plt.subplots(figsize=(8, 5))
    x = np.arange(len(df))
    ax1.bar(x, df["accuracy"], color="steelblue", alpha=0.7, label="Accuracy")
    ax1.set_xticks(x)
    ax1.set_xticklabels(df["bucket_label"], rotation=45)
    ax1.set_ylabel("Accuracy", color="steelblue")
    ax1.set_xlabel("Confidence Bucket")
    ax1.set_ylim([0, 1.05])
    ax2 = ax1.twinx()
    ax2.plot(
        x, df["mean_confidence"], "ro-", label="Mean Confidence"
    )
    ax2.set_ylabel("Mean Confidence", color="red")
    ax2.set_ylim([0.5, 1.05])
    ax1.set_title("Accuracy by Confidence Bucket")
    fig.tight_layout()
    p = out / "confidence_buckets.png"
    fig.savefig(p, dpi=100)
    plt.close(fig)
    paths["confidence_buckets"] = p

    # 3. Brier by game number
    by_game = perf_dict["by_game_number"]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(by_game.index.to_numpy(), by_game["brier"].to_numpy(), "o-", color="steelblue")
    ax.set_xlabel("Game Number in Set")
    ax.set_ylabel("Brier Score")
    ax.set_title("Brier Score by Game Number in Set")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = out / "brier_by_game_number.png"
    fig.savefig(p, dpi=100)
    plt.close(fig)
    paths["brier_by_game_number"] = p

    # 4. Feature importance — top 20, color by signal group
    top = feature_importance_df.head(20).iloc[::-1].reset_index(drop=True)
    groups_present = list(dict.fromkeys(top["signal_group"]))
    cmap = plt.get_cmap("tab10")
    color_map = {g: cmap(i % 10) for i, g in enumerate(groups_present)}
    bar_colors = [color_map[g] for g in top["signal_group"]]
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.barh(top["feature_name"], top["importance"], color=bar_colors)
    ax.set_xlabel("Importance")
    ax.set_title("Top 20 Feature Importances")
    handles = [plt.Rectangle((0, 0), 1, 1, color=color_map[g]) for g in groups_present]
    ax.legend(handles, groups_present, title="Signal Group", loc="lower right")
    fig.tight_layout()
    p = out / "feature_importance.png"
    fig.savefig(p, dpi=100)
    plt.close(fig)
    paths["feature_importance"] = p

    return paths
