#!/usr/bin/env python3
"""
Component 6B — Parameter Tuning

Grid-searches three key parameters of the tennis prediction engine to find
the combination that minimises Brier score on a held-out test set.

Parameters searched
-------------------
  lambda  : TemporalEngine.lambda_decay  — recency half-life (points)
  k       : PhatAdjuster.k               — max p-hat adjustment magnitude
  sigma   : PhatAdjuster.sigma           — sigmoid sensitivity

Grid
----
  lambda : [2, 3, 4, 5, 6, 8, 10]    (7 values)
  k      : [0.04, 0.06, 0.08, 0.10, 0.12]  (5 values)
  sigma  : [15, 20, 25, 30, 35, 40]   (6 values)
  Total  : 210 combinations

Usage
-----
    python -m src.parameter_tuning
    python -m src.parameter_tuning --max-matches 500  # faster smoke test

Outputs
-------
  data/tuning_results.csv  — full ranked results table
  data/best_params.json    — best parameters for downstream components
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import logging
import os
import random
import sys

from src.backtesting.backtester import Backtester
from src.engine.markov_engine import (
    game_win_prob, tiebreak_win_prob, set_win_prob, match_win_prob,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# ---------------------------------------------------------------------------
# Grid definition
# ---------------------------------------------------------------------------

LAMBDA_GRID: list[int]   = [2, 3, 4, 5, 6, 8, 10]
K_GRID:      list[float] = [0.04, 0.06, 0.08, 0.10, 0.12]
SIGMA_GRID:  list[int]   = [15, 20, 25, 30, 35, 40]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH          = "data/processed/tennis.duckdb"
OUTPUT_DIR       = "data/backtesting"
RESULTS_CSV      = "data/tuning_results.csv"
BEST_PARAMS_JSON = "data/best_params.json"
RANDOM_SEED      = 42
TRAIN_RATIO      = 0.80
TOUR             = "atp"
MIN_YEAR         = 2000


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_match_ids(bt: Backtester, tour: str) -> list[str]:
    """
    Load all distinct match IDs from the points table, filtered to
    matches from MIN_YEAR onwards (determined by the YYYYMMDD prefix
    in the match_id string).
    """
    points_table = f"{tour}_points"
    rows = bt._con.execute(
        f"SELECT DISTINCT match_id FROM {points_table} ORDER BY match_id"
    ).fetchall()

    filtered: list[str] = []
    for (mid,) in rows:
        try:
            if int(mid[:4]) >= MIN_YEAR:
                filtered.append(mid)
        except (ValueError, IndexError):
            pass

    return filtered


def train_test_split(
    match_ids: list[str],
    train_ratio: float = TRAIN_RATIO,
    seed: int = RANDOM_SEED,
) -> tuple[list[str], list[str]]:
    """
    Randomly shuffle match IDs and split into train / test sets.

    The split is by match (not by point) so no match appears in both sets.
    The fixed random seed ensures reproducibility.
    """
    rng = random.Random(seed)
    shuffled = list(match_ids)
    rng.shuffle(shuffled)
    n_train = int(len(shuffled) * train_ratio)
    return shuffled[:n_train], shuffled[n_train:]


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def run_grid_search(
    bt: Backtester,
    train_ids: list[str],
    preloaded_data: dict,
) -> list[dict]:
    """
    Evaluate every (lambda, k, sigma) combination on the training set.

    Returns a list of result dicts, unsorted, one per combination.
    """
    grid = list(itertools.product(LAMBDA_GRID, K_GRID, SIGMA_GRID))
    total = len(grid)
    results: list[dict] = []

    for i, (lam, k, sigma) in enumerate(grid, 1):
        brier = bt.score_matches(
            train_ids,
            tour=TOUR,
            lambda_decay=float(lam),
            k=k,
            sigma=float(sigma),
            preloaded_data=preloaded_data,
        )

        # Clear Markov LRU caches between combinations to prevent
        # unbounded memory growth from unique floating-point keys.
        game_win_prob.cache_clear()
        tiebreak_win_prob.cache_clear()
        set_win_prob.cache_clear()
        match_win_prob.cache_clear()

        results.append({
            "lambda":      lam,
            "k":           k,
            "sigma":       sigma,
            "brier_score": brier,
        })

        if i % 10 == 0 or i == total:
            best_so_far = min(r["brier_score"] for r in results)
            print(
                f"  [{i:>3}/{total}]  lambda={lam:>2}  k={k:.2f}  sigma={sigma:>2}"
                f"  brier={brier:.6f}  (best so far: {best_so_far:.6f})"
            )

    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(max_matches: int | None = None) -> None:
    print("=" * 62)
    print("Component 6B — Parameter Tuning")
    print("=" * 62)

    # ---- Initialise backtester ----
    bt = Backtester(db_path=DB_PATH, output_dir=OUTPUT_DIR)

    # ---- Load & filter match IDs ----
    print(f"\nLoading match IDs (year >= {MIN_YEAR}, tour={TOUR.upper()})...")
    all_ids = load_match_ids(bt, TOUR)
    print(f"Total charted matches found: {len(all_ids)}")

    if max_matches is not None and max_matches < len(all_ids):
        rng = random.Random(RANDOM_SEED)
        all_ids = rng.sample(all_ids, max_matches)
        print(f"Capped to {max_matches} matches (--max-matches flag).")

    # ---- Train / test split ----
    train_ids, test_ids = train_test_split(all_ids)
    print(f"Train set : {len(train_ids):>5} matches")
    print(f"Test set  : {len(test_ids):>5} matches")

    # ---- Preload training data (one DB pass) ----
    print("\nPreloading training match data from DB...")
    train_data = bt.preload_match_data(train_ids, TOUR)
    usable_train = list(train_data.keys())
    print(f"Usable training matches (≥10 pts): {len(usable_train)}")

    # ---- Grid search ----
    n_combos = len(LAMBDA_GRID) * len(K_GRID) * len(SIGMA_GRID)
    print(f"\nRunning grid search: {len(LAMBDA_GRID)}×{len(K_GRID)}×{len(SIGMA_GRID)} = {n_combos} combinations")
    print("-" * 62)
    results = run_grid_search(bt, usable_train, train_data)

    # ---- Sort & rank ----
    results_sorted = sorted(results, key=lambda r: r["brier_score"])
    for rank, r in enumerate(results_sorted, 1):
        r["ranked"] = rank

    # ---- Print top 10 ----
    print("\nTop 10 parameter combinations (lowest Brier score first):")
    print(f"  {'Rank':>4}  {'lambda':>6}  {'k':>6}  {'sigma':>5}  {'Brier':>10}")
    print("  " + "-" * 38)
    for r in results_sorted[:10]:
        print(
            f"  {r['ranked']:>4}  {r['lambda']:>6}  {r['k']:>6.2f}"
            f"  {r['sigma']:>5}  {r['brier_score']:>10.6f}"
        )

    best = results_sorted[0]
    print(
        f"\nBest combination: lambda={best['lambda']},  k={best['k']},  sigma={best['sigma']}"
    )
    print(f"Brier score (train): {best['brier_score']:.6f}")

    # ---- Save full results CSV ----
    os.makedirs("data", exist_ok=True)
    with open(RESULTS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["lambda", "k", "sigma", "brier_score", "ranked"]
        )
        writer.writeheader()
        writer.writerows(results_sorted)
    print(f"\nFull results table saved → {RESULTS_CSV}")

    # ---- Final evaluation on held-out test set ----
    print("\nPreloading held-out test data from DB...")
    test_data = bt.preload_match_data(test_ids, TOUR)
    usable_test = list(test_data.keys())
    print(f"Usable test matches (≥10 pts): {len(usable_test)}")

    print("Evaluating best parameters on held-out test set...")
    brier_test = bt.score_matches(
        usable_test,
        tour=TOUR,
        lambda_decay=float(best["lambda"]),
        k=best["k"],
        sigma=float(best["sigma"]),
        preloaded_data=test_data,
    )
    brier_train = best["brier_score"]
    gap = abs(brier_test - brier_train)

    if gap <= 0.01:
        fit_status = f"GOOD — gap {gap:.4f} is within 0.01"
    else:
        fit_status = f"POSSIBLE OVERFITTING — gap {gap:.4f} exceeds 0.01"

    print(f"\n  Brier score (train):     {brier_train:.6f}")
    print(f"  Brier score (test) :     {brier_test:.6f}")
    print(f"  Train/test gap     :     {gap:.6f}  →  {fit_status}")

    # ---- Save best_params.json ----
    os.makedirs("config", exist_ok=True)
    best_params = {
        "lambda":      best["lambda"],
        "k":           best["k"],
        "sigma":       best["sigma"],
        "brier_train": round(brier_train, 6),
        "brier_test":  round(brier_test, 6),
    }
    with open(BEST_PARAMS_JSON, "w") as f:
        json.dump(best_params, f, indent=2)
    print(f"\nBest parameters saved      → {BEST_PARAMS_JSON}")

    # ---- Summary ----
    print("\n" + "=" * 62)
    print("SUMMARY")
    print("=" * 62)
    print(f"  Best lambda  : {best['lambda']}")
    print(f"  Best k       : {best['k']}")
    print(f"  Best sigma   : {best['sigma']}")
    print(f"  Brier train  : {brier_train:.6f}")
    print(f"  Brier test   : {brier_test:.6f}")
    print(f"  Gap          : {gap:.6f}  →  {fit_status}")
    print("=" * 62)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="6B — Parameter Tuning grid search")
    parser.add_argument(
        "--max-matches", type=int, default=None,
        help="Cap total matches before splitting (useful for quick smoke tests).",
    )
    args = parser.parse_args()
    main(max_matches=args.max_matches)
