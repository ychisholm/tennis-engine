#!/usr/bin/env python3
"""
Component 4E — Game Length Pressure Score (GPS)

Measures how much pressure a server is under based on how long their service
games are running.  A clean 4-point hold = zero pressure.  Every extra point
beyond 4 signals pressure that the scoreline doesn't show.

GPS is computed from the SERVER's perspective as a negative signal: long games
indicate the server is being pushed.  The combining layer reads GPS as a
positive signal from the RECEIVER's perspective.

Normalisation follows the same running-max pattern as NMI: the score is always
relative to the longest-game pressure seen so far this set, so a single
deuce game becomes 100 until a worse game is observed.

Usage
-----
    from src.signals.gps import GPSCalculator

    calc = GPSCalculator()

    calc.add_completed_game({"points_played": 6, "game_index": 0})
    calc.add_completed_game({"points_played": 4, "game_index": 2})
    calc.add_completed_game({"points_played": 8, "game_index": 4})

    print(calc.compute(current_game_index=5))
"""

from __future__ import annotations

import math


class GPSCalculator:
    """
    Game Length Pressure Score calculator.

    Parameters
    ----------
    archetype_weight : float
        Multiplier sourced from the Archetype Engine (3A).  Default 1.0.
    """

    def __init__(self, archetype_weight: float = 1.0) -> None:
        self._archetype_weight: float = archetype_weight
        self._games: list[dict] = []
        self._running_max: float = 0.0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_archetype_weight(self, weight: float) -> None:
        """Update the archetype multiplier during the match."""
        self._archetype_weight = weight

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def add_completed_game(self, game: dict) -> None:
        """
        Append a completed service game to the set history.

        Parameters
        ----------
        game : dict
            Must contain:
            - ``points_played`` : int — total points played in the game
            - ``game_index``    : int — position in the set (0-based)
        """
        self._games.append(game)

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """
        Clear all stored games and reset the running max.

        Call at set boundaries so GPS reflects only the current set's pressure.
        """
        self._games = []
        self._running_max = 0.0

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    def game_pressure(self, points_played: int) -> float:
        """
        Return the raw pressure contribution of a single service game.

        A clean hold (4 points) contributes zero.  Every additional point
        beyond 4 adds 1.0 of pressure.

        Examples
        --------
        4 points  →  0.0
        6 points  →  2.0   (deuce)
        8 points  →  4.0   (double deuce)

        Parameters
        ----------
        points_played : int

        Returns
        -------
        float
        """
        return float(max(0, points_played - 4))

    def recency_weight(
        self,
        game_index: int,
        current_game_index: int,
        lambda_val: float = 4.0,
    ) -> float:
        """
        Exponential recency decay at game granularity.

            w = exp(-ln(2) * tau / lambda_val)

        where tau = current_game_index - game_index.

        A game at tau=0 (same index as current) has weight 1.0.
        A game tau=lambda_val games in the past has weight ~0.5.

        Parameters
        ----------
        game_index : int
            Index of the historical game being weighted.
        current_game_index : int
            Index of the game currently being evaluated.
        lambda_val : float
            Half-life in games.  Default 4.0.

        Returns
        -------
        float
        """
        tau = current_game_index - game_index
        return math.exp(-math.log(2) * tau / lambda_val)

    # ------------------------------------------------------------------
    # Main computation
    # ------------------------------------------------------------------

    def compute(
        self,
        current_game_index: int,
        lambda_val: float = 4.0,
    ) -> float:
        """
        Compute the Game Length Pressure Score as of *current_game_index*.

        Algorithm
        ---------
        1. For each stored game:
               weighted = game_pressure(game) * recency_weight(game)
        2. GPS_raw = sum of all weighted values
        3. Update running max with GPS_raw
        4. GPS_norm = GPS_raw / running_max * 100  (0.0 if running_max == 0)
        5. Apply archetype multiplier and clip to [0, 100]

        Parameters
        ----------
        current_game_index : int
            Used for recency decay of each stored game.
        lambda_val : float
            Recency half-life in games.  Default 4.0.

        Returns
        -------
        float  GPS score in [0, 100].
        """
        if not self._games:
            return 0.0

        gps_raw = sum(
            self.game_pressure(g["points_played"])
            * self.recency_weight(g["game_index"], current_game_index, lambda_val)
            for g in self._games
        )

        if gps_raw > self._running_max:
            self._running_max = gps_raw

        if self._running_max == 0.0:
            return 0.0

        gps_norm = (gps_raw / self._running_max) * 100.0
        result = gps_norm * self._archetype_weight
        return max(0.0, min(100.0, result))
