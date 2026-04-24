#!/usr/bin/env python3
"""
Component 4C — Return Module Score (RMS)

Measures live return quality for the player currently receiving serve.
Three sub-signals are combined into a single 0–100 score:

  RMS_1  Return points won %   (last n return points)
  RMS_2  Break conversion rate  (this set)
  RMS_3  Near-Miss Index        (delegated to NMICalculator)

The NMI is the dominant sub-signal (0.40 weight) because it captures
pressure that isn't visible in break-conversion rate alone.

Usage
-----
    from src.engine.signals.nmi import NMICalculator
    from src.signals.rms import RMSCalculator

    nmi = NMICalculator()
    rms = RMSCalculator(nmi=nmi, archetype_weight=1.1)

    nmi.add_game({"points": ["0-0", "0-15", "0-30", "0-40", "game"],
                  "game_index": 0, "converted": False})
    rms.add_return_point({"won": True,  "point_index": 0})
    rms.add_return_point({"won": False, "point_index": 1})
    rms.record_break_point_opportunity(converted=True)

    print(rms.compute(current_game_index=1))
"""

from __future__ import annotations

from src.engine.signals.nmi import NMICalculator


class RMSCalculator:
    """
    Return Module Score calculator for a single receiving player.

    Parameters
    ----------
    nmi : NMICalculator
        Shared NMI instance.  RMSCalculator reads from it but does not
        own or reset it — lifecycle is managed externally.
    archetype_weight : float
        Multiplier sourced from the Archetype Engine (3A).  Default 1.0.
    """

    def __init__(
        self,
        nmi: NMICalculator,
        archetype_weight: float = 1.0,
    ) -> None:
        self.nmi = nmi
        self._archetype_weight: float = archetype_weight
        self._return_points: list[dict] = []
        self._break_points_faced: int = 0
        self._breaks_converted: int = 0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_archetype_weight(self, weight: float) -> None:
        """Update the archetype multiplier during the match."""
        self._archetype_weight = weight

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def add_return_point(self, point: dict) -> None:
        """
        Append a completed return point to the rolling history.

        Parameters
        ----------
        point : dict
            Must contain:
            - ``won``         : bool — True if the receiver won the point
            - ``point_index`` : int  — sequential position in the match
        """
        self._return_points.append(point)

    def record_break_point_opportunity(self, converted: bool) -> None:
        """
        Record one break point opportunity for this player.

        Parameters
        ----------
        converted : bool
            True if the receiver converted the break point; False if saved.
        """
        self._break_points_faced += 1
        if converted:
            self._breaks_converted += 1

    # ------------------------------------------------------------------
    # Reset helpers
    # ------------------------------------------------------------------

    def reset_set(self) -> None:
        """
        Clear return point history and break-tracking counters at a set boundary.

        The NMI instance is not touched — its lifecycle is managed externally.
        """
        self._return_points = []
        self._break_points_faced = 0
        self._breaks_converted = 0

    # ------------------------------------------------------------------
    # Main computation
    # ------------------------------------------------------------------

    def compute(
        self,
        current_game_index: int,
        n: int = 15,
        lambda_val: float = 4.0,
    ) -> float:
        """
        Compute the Return Module Score.

        Sub-signal weights
        ------------------
        RMS_1  0.35  (return points won %)
        RMS_2  0.25  (break conversion rate)
        RMS_3  0.40  (Near-Miss Index — dominant signal)

        Parameters
        ----------
        current_game_index : int
            Passed through to NMI for recency decay.
        n : int
            Rolling window size for RMS_1.  Default 15.
        lambda_val : float
            NMI recency half-life in games.  Default 4.0.

        Returns
        -------
        float  RMS score in [0, 100].
        """
        rms1 = self._rms1(n)
        rms2 = self._rms2()
        rms3 = self.nmi.compute(current_game_index, lambda_val)

        rms = 0.35 * rms1 + 0.25 * rms2 + 0.40 * rms3

        result = rms * self._archetype_weight
        return max(0.0, min(100.0, result))

    # ------------------------------------------------------------------
    # Sub-signal helpers (exposed for testability)
    # ------------------------------------------------------------------

    def _rms1(self, n: int = 15) -> float:
        """
        Return points won % over the last *n* return points.

        Returns 50.0 (neutral) if no points exist in the window.
        """
        if not self._return_points:
            return 50.0
        window = self._return_points[-n:]
        return sum(1 for p in window if p["won"]) / len(window) * 100.0

    def _rms2(self) -> float:
        """
        Break conversion rate this set, scaled to [0, 100].

        Formula: (breaks_converted / break_points_faced) × 100.
        Returns 50.0 (neutral) if no break points have been faced.
        """
        if self._break_points_faced == 0:
            return 50.0
        return self._breaks_converted / self._break_points_faced * 100.0
