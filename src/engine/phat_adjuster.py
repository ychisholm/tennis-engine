#!/usr/bin/env python3
"""
Component 5B — p-hat Adjuster

Converts a dominance differential (delta) and two career baseline probabilities
into live adjusted win probabilities.

Formula (spec §6.1)
-------------------
  p_hat_A = p0_A + k * (2 * sigmoid( delta / sigma) - 1)
  p_hat_B = p0_B + k * (2 * sigmoid(-delta / sigma) - 1)
  sigmoid(x) = 1 / (1 + exp(-x))
  Both outputs clipped to [0.35, 0.85].

Usage
-----
    from src.phat_adjuster import PhatAdjuster

    adj = PhatAdjuster()
    result = adj.adjust(p0_A=0.52, p0_B=0.48, delta=30.0)
    # result = {"p_hat_A": float, "p_hat_B": float}
"""

from __future__ import annotations

import math


class PhatAdjuster:
    """
    Adjusts career baseline win probabilities using the live dominance differential.

    Parameters
    ----------
    k : float
        Maximum adjustment magnitude (added to or subtracted from p0).
        Default 0.08.
    sigma : float
        Sigmoid sensitivity — controls how quickly the adjustment saturates
        as |delta| grows.  Default 25.0.
    """

    _CLIP_LO: float = 0.35
    _CLIP_HI: float = 0.85

    def __init__(self, k: float = 0.08, sigma: float = 25.0) -> None:
        self.k = k
        self.sigma = sigma

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def adjust(self, p0_A: float, p0_B: float, delta: float) -> dict:
        """
        Compute live adjusted win probabilities for both players.

        Parameters
        ----------
        p0_A : float
            Career baseline win probability for Player A (typically 0–1).
        p0_B : float
            Career baseline win probability for Player B.
        delta : float
            Dominance differential D_A − D_B from the Temporal Engine
            (range −100 to +100).

        Returns
        -------
        dict with keys:
            p_hat_A : float — adjusted probability for Player A, clipped to [0.35, 0.85]
            p_hat_B : float — adjusted probability for Player B, clipped to [0.35, 0.85]
        """
        adj_a = self.k * (2.0 * self._sigmoid( delta / self.sigma) - 1.0)
        adj_b = self.k * (2.0 * self._sigmoid(-delta / self.sigma) - 1.0)

        p_hat_A = max(self._CLIP_LO, min(self._CLIP_HI, p0_A + adj_a))
        p_hat_B = max(self._CLIP_LO, min(self._CLIP_HI, p0_B + adj_b))

        return {"p_hat_A": p_hat_A, "p_hat_B": p_hat_B}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sigmoid(x: float) -> float:
        """Standard logistic sigmoid: 1 / (1 + exp(-x))."""
        return 1.0 / (1.0 + math.exp(-x))
