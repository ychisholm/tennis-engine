#!/usr/bin/env python3
"""
Component 5A — Temporal Engine

A pure mathematical layer that combines per-point signal scores into two
player dominance scores and a differential.  It owns no signal calculators
and calls no reset() methods on them — it only consumes the numeric values
it is given.

Architecture
------------
Two time-horizon layers are blended into a final dominance score D:

  RecencyLayer (60%)
    Exponentially-decayed weighted average of composite signal scores.
    Captures short-term momentum shifts.

  SetLayer (40%)
    Simple mean of composite signal scores since the current set started.
    Captures set-level trend.

  D(player) = 0.60 * RecencyLayer + 0.40 * SetLayer  ∈ [0, 100]
  delta = D_A - D_B  ∈ [-100, 100]

GPS Perspective Flip
--------------------
GPS is measured from the server's perspective (high = server under pressure).
When computing a player's dominance, the *opponent's* GPS is used as that
player's GPS contribution — a struggling opponent's long service games are a
positive signal for the receiver.

Usage
-----
    engine = TemporalEngine()

    result = engine.update(
        signals_a={"nmi": 60, "sms": 75, "rms": 55, "pms": 65, "gps": 30},
        signals_b={"nmi": 40, "sms": 50, "rms": 45, "pms": 50, "gps": 70},
        point_index=0,
    )
    # result = {"D_A": float, "D_B": float, "delta": float}

    engine.handle_set_boundary()   # at set transitions
    engine.reset()                 # at match start / full reset
"""

from __future__ import annotations

import math


_SIGNALS = ("nmi", "sms", "rms", "pms", "gps")
_DEFAULT_WEIGHTS = {s: 1.0 for s in _SIGNALS}


def _default_weights() -> dict:
    return dict(_DEFAULT_WEIGHTS)


class TemporalEngine:
    """
    Temporal Engine: blends five signal scores per player into dominance values.

    Parameters
    ----------
    lambda_decay : float
        Recency half-life in points.  Default 4.0.
    w_recency : float
        Weight of the recency layer in the final dominance score.  Default 0.60.
    w_set : float
        Weight of the set layer in the final dominance score.  Default 0.40.
    gamma : float
        Set-boundary carryover fraction.  The final recency score is multiplied
        by gamma and seeded into the new set layer.  Default 0.20.
    """

    def __init__(
        self,
        lambda_decay: float = 4.0,
        w_recency: float = 0.60,
        w_set: float = 0.40,
        gamma: float = 0.20,
    ) -> None:
        self.lambda_decay = lambda_decay
        self.w_recency = w_recency
        self.w_set = w_set
        self.gamma = gamma

        # History entries: list of (point_index, composite_score)
        self._history_a: list[tuple[int, float]] = []
        self._history_b: list[tuple[int, float]] = []

        # Set-layer accumulators: list of composite scores
        self._set_a: list[float] = []
        self._set_b: list[float] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        signals_a: dict,
        signals_b: dict,
        point_index: int,
        archetype_weights_a: dict | None = None,
        archetype_weights_b: dict | None = None,
    ) -> dict:
        """
        Process one point and return updated dominance scores.

        GPS perspective flip is applied here: each player's dominance uses
        the *opponent's* GPS value.

        Parameters
        ----------
        signals_a : dict
            Signal scores for Player A. Keys: nmi, sms, rms, pms, gps (0–100).
        signals_b : dict
            Signal scores for Player B.
        point_index : int
            Monotonically increasing match-point counter (0-based).
        archetype_weights_a : dict or None
            Per-signal archetype multipliers for Player A (0.0–2.0).
            Defaults to 1.0 for all signals.
        archetype_weights_b : dict or None
            Per-signal archetype multipliers for Player B.

        Returns
        -------
        dict with keys:
            D_A   float — Player A dominance score [0, 100]
            D_B   float — Player B dominance score [0, 100]
            delta float — D_A − D_B  [-100, 100]
        """
        wa = archetype_weights_a or _default_weights()
        wb = archetype_weights_b or _default_weights()

        comp_a = self._composite(signals_a, signals_b, wa, player="a")
        comp_b = self._composite(signals_b, signals_a, wb, player="b")

        self._history_a.append((point_index, comp_a))
        self._history_b.append((point_index, comp_b))
        self._set_a.append(comp_a)
        self._set_b.append(comp_b)

        D_A = self._dominance(self._history_a, self._set_a, point_index)
        D_B = self._dominance(self._history_b, self._set_b, point_index)

        return {
            "D_A": D_A,
            "D_B": D_B,
            "delta": D_A - D_B,
        }

    def handle_set_boundary(self) -> None:
        """
        Transition between sets.

        1. Captures the final RecencyLayer score for each player from the
           last point processed.
        2. Clears both players' point histories (recency layer).
        3. Clears both players' set-layer accumulators.
        4. Seeds the new set layer with one synthetic data point:
               carryover = gamma * final_recency_score
           stored at a notional point_index of 0 (age-0 in the new set).
        """
        # Determine the last point index seen so we can compute recency scores
        last_idx = (
            self._history_a[-1][0]
            if self._history_a
            else 0
        )

        final_rec_a = self._recency_layer(self._history_a, last_idx)
        final_rec_b = self._recency_layer(self._history_b, last_idx)

        carryover_a = self.gamma * final_rec_a
        carryover_b = self.gamma * final_rec_b

        # Reset histories
        self._history_a = []
        self._history_b = []

        # Seed new set layer with carryover at synthetic index 0
        self._set_a = [carryover_a]
        self._set_b = [carryover_b]

    def reset(self) -> None:
        """
        Full match reset — clears all history.

        After reset(), the engine returns to cold-start defaults:
        D_A = D_B = 50.0, delta = 0.
        """
        self._history_a = []
        self._history_b = []
        self._set_a = []
        self._set_b = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _composite(
        self,
        signals_own: dict,
        signals_opp: dict,
        weights: dict,
        player: str,
    ) -> float:
        """
        Compute a weighted composite signal score for one player.

        GPS perspective flip: use the *opponent's* GPS value as this player's
        GPS input (a struggling opponent's long service games help the receiver).

        Formula:   sum(alpha_i * S_i) / sum(alpha_i)

        Parameters
        ----------
        signals_own : dict   Own signal scores (nmi, sms, rms, pms, gps).
        signals_opp : dict   Opponent's signal scores (only gps is used).
        weights : dict       Archetype weight multipliers.
        player : str         Unused except for clarity; kept for symmetry.

        Returns
        -------
        float  Composite score (0–100).
        """
        effective = dict(signals_own)
        effective["gps"] = signals_opp.get("gps", 50.0)

        total_weight = 0.0
        weighted_sum = 0.0
        for sig in _SIGNALS:
            alpha = weights.get(sig, 1.0)
            score = effective.get(sig, 50.0)
            weighted_sum += alpha * score
            total_weight += alpha

        if total_weight == 0.0:
            return 50.0
        return weighted_sum / total_weight

    def _recency_layer(
        self,
        history: list[tuple[int, float]],
        current_point_index: int,
    ) -> float:
        """
        Exponentially-decayed weighted average of stored composite scores.

        w(tau) = exp(-ln2 * tau / lambda_decay)
        where tau = current_point_index - historical_point_index

        Returns 50.0 if history is empty (cold-start default).
        """
        if not history:
            return 50.0

        total_weight = 0.0
        weighted_sum = 0.0
        for idx, score in history:
            tau = current_point_index - idx
            w = math.exp(-math.log(2) * tau / self.lambda_decay)
            weighted_sum += w * score
            total_weight += w

        if total_weight == 0.0:
            return 50.0
        return weighted_sum / total_weight

    def _set_layer(self, set_scores: list[float]) -> float:
        """
        Simple mean of all composite scores recorded since set start.

        Returns 50.0 if no scores exist (cold-start default).
        """
        if not set_scores:
            return 50.0
        return sum(set_scores) / len(set_scores)

    def _dominance(
        self,
        history: list[tuple[int, float]],
        set_scores: list[float],
        current_point_index: int,
    ) -> float:
        """
        Blend recency and set layers into a final dominance score, clipped to [0, 100].
        """
        rec = self._recency_layer(history, current_point_index)
        st = self._set_layer(set_scores)
        raw = self.w_recency * rec + self.w_set * st
        return max(0.0, min(100.0, raw))
