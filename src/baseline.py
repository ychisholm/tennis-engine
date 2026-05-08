"""Component 5A — naive baselines for set-win prediction.

Two predictors XGBoost has to beat in 5B/5C:

* ``predict_hard_pick`` : 1.0 / 0.0 / 0.5 by raw p0 comparison (a step function).
* ``predict_soft_markov`` : Markov set-win probability evaluated once per set
  from 0-0 using career p0 values.

A small ``load_p0_lookup`` helper returns the ``{player_name: p0}`` mapping
read from ``core.player_p0``.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from src.engine.markov_engine import game_win_prob, set_win_prob


def load_p0_lookup(db_path: str = "data/processed/tennis.duckdb") -> dict[str, float]:
    """Return ``{player_name: p0}`` from core.player_p0."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute("SELECT player_name, p0 FROM core.player_p0").fetchall()
    finally:
        con.close()
    return {name: float(p0) for name, p0 in rows}


def clear_set_cache() -> None:
    """Clear the markov_engine ``set_win_prob`` lru_cache.

    Needed between matches so the per-(p, q) memo does not grow without bound
    across many distinct p0 pairs.
    """
    set_win_prob.cache_clear()


def _set_win_prob_a(p_a: float, p_b: float, first_server_is_a: bool) -> float:
    """P(player A wins the set) at 0-0 given career p0 of each player.

    Reframes the Markov set engine — which is written from the set server's
    perspective — to always return the probability for player A.
    """
    g_a = game_win_prob(p_a)
    g_b = game_win_prob(p_b)
    if first_server_is_a:
        p = g_a
        q = 1.0 - g_b
        return set_win_prob(p, q, 0, 0)
    p = g_b
    q = 1.0 - g_a
    return 1.0 - set_win_prob(p, q, 0, 0)


def predict_hard_pick(meta_df: pd.DataFrame, p0_lookup: dict[str, float]) -> np.ndarray:
    """Step-function pick by p0: 1.0 if A's p0 > B's, 0.0 if less, 0.5 on tie."""
    a = meta_df["player_A"].map(p0_lookup).to_numpy(dtype=np.float64)
    b = meta_df["player_B"].map(p0_lookup).to_numpy(dtype=np.float64)
    out = np.full(len(meta_df), 0.5, dtype=np.float64)
    out[a > b] = 1.0
    out[a < b] = 0.0
    return out


def predict_soft_markov(meta_df: pd.DataFrame, p0_lookup: dict[str, float]) -> np.ndarray:
    """Markov set-win probability from 0-0, broadcast to every row in each set.

    For each (match_id_int, set_number) group we read ``server_was_A`` on the
    ``game_number_in_set == 1`` row to determine who served the set, look up
    p0 for each player, compute set_win_prob once and apply it to every row.
    The lru_cache on markov_engine.set_win_prob is cleared between matches.
    """
    df = meta_df.reset_index(drop=True)
    n = len(df)
    out = np.empty(n, dtype=np.float64)

    last_match_id: int | None = None
    set_cache: dict[tuple, float] = {}

    for (match_id, _set_no), group in df.groupby(
        ["match_id_int", "set_number"], sort=False
    ):
        if match_id != last_match_id:
            clear_set_cache()
            set_cache.clear()
            last_match_id = match_id

        first_game = group[group["game_number_in_set"] == 1]
        if first_game.empty:
            raise RuntimeError(
                f"set (match {match_id}, set {_set_no}) has no game_number_in_set==1 row"
            )
        first_row = first_game.iloc[0]
        p_a = p0_lookup[first_row["player_A"]]
        p_b = p0_lookup[first_row["player_B"]]
        first_server_is_a = bool(first_row["server_was_A"])

        key = (round(p_a, 6), round(p_b, 6), first_server_is_a)
        prob = set_cache.get(key)
        if prob is None:
            prob = _set_win_prob_a(p_a, p_b, first_server_is_a)
            set_cache[key] = prob

        out[group.index.to_numpy()] = prob

    return out
