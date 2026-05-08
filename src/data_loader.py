"""Component 5A — load core.ml_game_level into train/test arrays for modelling.

Single entry point: ``load_ml_data(db_path)``.

Output layout:
    X_train, y_train, X_test, y_test : numpy arrays
    feature_names : list[str] (length 63, deterministic column order)
    train_meta, test_meta : pandas DataFrames with id columns for grouping/joins

Feature composition (63 total):
    6  numeric context features
    4  surface one-hot dummies (hard, clay, grass, unknown)
   52  signal sub-components (bpi_/sds_/res_/cpi_/mrs_)
    1  markov_set_win_prob_A

The function is read-only: it never writes to the DuckDB.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd


NUMERIC_CONTEXT = [
    "games_A",
    "games_B",
    "set_number",
    "sets_won_A",
    "sets_won_B",
    "game_number_in_set",
]

SURFACE_CATEGORIES = ["hard", "clay", "grass", "unknown"]
SURFACE_DUMMIES = [f"surface_{s}" for s in SURFACE_CATEGORIES]

SIGNAL_PREFIXES = ("bpi_", "sds_", "res_", "cpi_", "mrs_")

META_COLUMNS = [
    "match_id_int",
    "set_number",
    "game_number_in_set",
    "player_A",
    "player_B",
    "server_was_A",
]

EXPECTED_TRAIN_ROWS = 86_698
EXPECTED_TEST_ROWS = 34_020


def _signal_columns(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return signal sub-component columns in DuckDB's ordinal order (deterministic)."""
    rows = con.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'core'
          AND table_name = 'ml_game_level'
        ORDER BY ordinal_position
        """
    ).fetchall()
    all_cols = [r[0] for r in rows]
    return [c for c in all_cols if c.startswith(SIGNAL_PREFIXES)]


def load_ml_data(db_path: str = "data/processed/tennis.duckdb") -> dict:
    con = duckdb.connect(db_path, read_only=True)
    try:
        signal_cols = _signal_columns(con)
        if len(signal_cols) != 52:
            raise RuntimeError(
                f"expected 52 signal sub-components, found {len(signal_cols)}: {signal_cols}"
            )

        select_cols = (
            ["split", "set_winner_is_A", "surface"]
            + META_COLUMNS
            + NUMERIC_CONTEXT
            + signal_cols
            + ["markov_set_win_prob_A"]
        )

        df = con.execute(
            f"SELECT {', '.join(select_cols)} FROM core.ml_game_level"
        ).fetchdf()
    finally:
        con.close()

    surface_lower = df["surface"].astype(str).str.lower()
    unknown_surfaces = set(surface_lower.unique()) - set(SURFACE_CATEGORIES)
    if unknown_surfaces:
        raise RuntimeError(
            f"unexpected surface values in ml_game_level: {sorted(unknown_surfaces)}"
        )
    for cat in SURFACE_CATEGORIES:
        df[f"surface_{cat}"] = (surface_lower == cat).astype(np.int64)

    feature_names = NUMERIC_CONTEXT + SURFACE_DUMMIES + signal_cols + ["markov_set_win_prob_A"]
    if len(feature_names) != 63:
        raise RuntimeError(f"expected 63 feature columns, got {len(feature_names)}")

    X = df[feature_names].to_numpy(dtype=np.float64)
    y = df["set_winner_is_A"].to_numpy(dtype=np.int64)

    train_mask = (df["split"] == "train").to_numpy()
    test_mask = (df["split"] == "test").to_numpy()
    if not (train_mask | test_mask).all():
        unknown_splits = set(df["split"].unique()) - {"train", "test"}
        raise RuntimeError(f"rows with unrecognised split values: {unknown_splits}")

    X_train, X_test = X[train_mask], X[test_mask]
    y_train, y_test = y[train_mask], y[test_mask]

    for name, arr in (("X_train", X_train), ("X_test", X_test)):
        nan_mask = np.isnan(arr)
        if nan_mask.any():
            r, c = np.argwhere(nan_mask)[0]
            col = feature_names[c]
            raise AssertionError(
                f"NaN found in {name} at row={r}, column='{col}' "
                f"(total NaNs in {name}: {int(nan_mask.sum())})"
            )

    train_meta = df.loc[train_mask, META_COLUMNS].reset_index(drop=True)
    test_meta = df.loc[test_mask, META_COLUMNS].reset_index(drop=True)

    n_train, n_test = len(X_train), len(X_test)
    if abs(n_train - EXPECTED_TRAIN_ROWS) > EXPECTED_TRAIN_ROWS * 0.01:
        print(
            f"[data_loader] WARN train rows={n_train} differs from expected "
            f"{EXPECTED_TRAIN_ROWS} by more than 1%"
        )
    else:
        print(f"[data_loader] train rows={n_train} (expected ~{EXPECTED_TRAIN_ROWS})")
    if abs(n_test - EXPECTED_TEST_ROWS) > EXPECTED_TEST_ROWS * 0.01:
        print(
            f"[data_loader] WARN test rows={n_test} differs from expected "
            f"{EXPECTED_TEST_ROWS} by more than 1%"
        )
    else:
        print(f"[data_loader] test rows={n_test} (expected ~{EXPECTED_TEST_ROWS})")

    return {
        "X_train": X_train,
        "y_train": y_train,
        "X_test": X_test,
        "y_test": y_test,
        "feature_names": feature_names,
        "train_meta": train_meta,
        "test_meta": test_meta,
    }


if __name__ == "__main__":
    bundle = load_ml_data()
    print(f"X_train shape: {bundle['X_train'].shape}")
    print(f"X_test  shape: {bundle['X_test'].shape}")
    print(f"feature_names ({len(bundle['feature_names'])}): {bundle['feature_names'][:6]} ...")
