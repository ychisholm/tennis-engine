#!/usr/bin/env python3
"""
Component 4A — Near-Miss Index (NMI)

The NMI captures break-point pressure created by the receiver even when the
break wasn't converted.  A receiver who repeatedly reaches 0-40 or 15-40 is
exerting real pressure that won't show up in a simple break-conversion rate.

Usage
-----
    from src.signals.nmi import NMICalculator

    calc = NMICalculator(archetype_weight=1.2)

    calc.add_game({
        "points": ["0-0", "15-0", "15-15", "15-30", "15-40", "game"],
        "game_index": 0,
        "converted": False,
    })

    score = calc.compute(current_game_index=3)
"""

from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Score-state pressure weights
# ---------------------------------------------------------------------------

_PRESSURE_TABLE: dict[str, float] = {
    "0-40":             1.0,
    "15-40":            0.8,
    "30-40":            0.5,
    "deuce-ad-receiver": 0.5,
    "40-ad":            0.5,
    "30-30":            0.2,
    "deuce":            0.2,
}


class NMICalculator:
    """
    Near-Miss Index calculator for a single player's return games.

    Parameters
    ----------
    archetype_weight : float
        Multiplier sourced from the Archetype Engine (3A).  Scales how much
        this signal matters for the specific player matchup.  Default 1.0.
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
    # Core helpers
    # ------------------------------------------------------------------

    def pressure_weight(self, score_state: str) -> float:
        """
        Return the pressure weight for a single score state.

        Weights
        -------
        "0-40"                         → 1.0
        "15-40"                        → 0.8
        "30-40" / "deuce-ad-receiver"
                / "40-ad"              → 0.5
        "30-30" / "deuce"              → 0.2
        anything else                  → 0.0

        Parameters
        ----------
        score_state : str
            The score state string exactly as stored in a game's points list.

        Returns
        -------
        float
        """
        return _PRESSURE_TABLE.get(score_state, 0.0)

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

        A game played at the same index as *current_game_index* (tau=0) has
        weight 1.0.  A game played *lambda_val* games ago has weight ~0.5.

        Parameters
        ----------
        game_index : int
            The index of the historical game being weighted.
        current_game_index : int
            The index of the game currently being evaluated (present moment).
        lambda_val : float
            Half-life in games.  Default 4.0.

        Returns
        -------
        float
        """
        tau = current_game_index - game_index
        return math.exp(-math.log(2) * tau / lambda_val)

    def compute_pressure_score(self, game: dict) -> float:
        """
        Sum the pressure weights for every score state in a game.

        Parameters
        ----------
        game : dict
            A game dict with at least a ``points`` key (list of score-state
            strings).

        Returns
        -------
        float
            Total pressure score for this game.
        """
        return sum(self.pressure_weight(state) for state in game["points"])

    # ------------------------------------------------------------------
    # Game management
    # ------------------------------------------------------------------

    def add_game(self, game: dict) -> None:
        """
        Append a completed service game to the internal history.

        Parameters
        ----------
        game : dict
            Must contain:
            - ``points``     : list[str] — score states visited
            - ``game_index`` : int       — position in the set (0-based)
            - ``converted``  : bool      — whether the receiver broke serve
        """
        self._games.append(game)

    def reset(self) -> None:
        """
        Clear all stored games and reset the running max.

        Call at set boundaries so NMI reflects only the current set's pressure.
        """
        self._games = []
        self._running_max = 0.0

    # ------------------------------------------------------------------
    # NMI computation
    # ------------------------------------------------------------------

    def compute(
        self,
        current_game_index: int,
        lambda_val: float = 4.0,
    ) -> float:
        """
        Compute the Near-Miss Index as of *current_game_index*.

        Algorithm
        ---------
        1. For each stored game:
               weighted = pressure_score(game) * recency_weight(game)
        2. NMI_raw = sum of all weighted scores
        3. Update the running max with NMI_raw
        4. Normalise: NMI_norm = NMI_raw / running_max * 100
           (returns 0.0 if running_max == 0)
        5. Apply archetype multiplier and clip to [0, 100]

        Parameters
        ----------
        current_game_index : int
            Index of the game being evaluated (used for recency decay).
        lambda_val : float
            Recency half-life in games.  Default 4.0.

        Returns
        -------
        float  NMI score in [0, 100]
        """
        if not self._games:
            return 0.0

        nmi_raw = sum(
            self.compute_pressure_score(g)
            * self.recency_weight(g["game_index"], current_game_index, lambda_val)
            for g in self._games
        )

        if nmi_raw > self._running_max:
            self._running_max = nmi_raw

        if self._running_max == 0.0:
            return 0.0

        nmi_norm = (nmi_raw / self._running_max) * 100.0
        result = nmi_norm * self._archetype_weight
        return max(0.0, min(100.0, result))
