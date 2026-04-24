#!/usr/bin/env python3
"""
Component 4D — Physical Module Score (PMS)

Measures rally-length dominance for a player.  Rally wins are bucketed by
length because short, medium and long rallies mean different things for
different archetypes:

  PMS_1  Short  (1–3 shots)   — favours big servers / quick-point winners
  PMS_2  Medium (4–8 shots)   — general baseline quality
  PMS_3  Long   (9+ shots)    — favours counter-punchers / grinders
  PMS_4  Fatigue signal       — compares current-set speed to set-1 baseline

Bucket weights are adjusted based on the player's archetype profile before
being renormalised to sum to 1.0.

Usage
-----
    from src.signals.pms import PMSCalculator

    profile = {"SD": 35, "PE": 85}
    calc = PMSCalculator(archetype_profile=profile)

    calc.add_rally_point({"won": True,  "rally_length": 12, "point_index": 0})
    calc.add_rally_point({"won": True,  "rally_length": 15, "point_index": 1})
    calc.add_rally_point({"won": False, "rally_length":  2, "point_index": 2})

    calc.add_serve_speed(195.0, is_set1=True)
    calc.add_serve_speed(188.0)

    print(calc.compute())
"""

from __future__ import annotations


class PMSCalculator:
    """
    Physical Module Score calculator.

    Parameters
    ----------
    archetype_weight : float
        Multiplier sourced from the Archetype Engine (3A).  Default 1.0.
    archetype_profile : dict or None
        Dict with keys ``SD`` (serve dominance, 0–100) and ``PE`` (physical
        endurance, 0–100).  Used to tilt bucket weights.  Pass None to use
        base weights.
    """

    def __init__(
        self,
        archetype_weight: float = 1.0,
        archetype_profile: dict | None = None,
    ) -> None:
        self._archetype_weight: float = archetype_weight
        self._archetype_profile: dict | None = archetype_profile
        self._rally_points: list[dict] = []
        self._set1_speeds: list[float] = []
        self._set1_frozen: bool = False
        self._current_set_speeds: list[float] = []

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_archetype_weight(self, weight: float) -> None:
        """Update the archetype multiplier during the match."""
        self._archetype_weight = weight

    def set_archetype_profile(self, profile: dict) -> None:
        """
        Replace the archetype profile used for bucket-weight adjustment.

        Parameters
        ----------
        profile : dict
            Must contain at least ``SD`` and ``PE`` keys (floats, 0–100).
        """
        self._archetype_profile = profile

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def add_rally_point(self, point: dict) -> None:
        """
        Append a completed rally point to the history.

        Parameters
        ----------
        point : dict
            Must contain:
            - ``won``          : bool — True if this player won the point
            - ``rally_length`` : int  — number of shots in the rally
            - ``point_index``  : int  — sequential position in the match
        """
        self._rally_points.append(point)

    def add_serve_speed(self, speed: float, is_set1: bool = False) -> None:
        """
        Record a serve speed reading.

        Parameters
        ----------
        speed : float
            Serve speed in km/h.
        is_set1 : bool
            If True, append to the set-1 baseline list.  Only meaningful
            before the first ``reset_set()`` call.  Default False.
        """
        if is_set1:
            if not self._set1_frozen:
                self._set1_speeds.append(speed)
        else:
            self._current_set_speeds.append(speed)

    # ------------------------------------------------------------------
    # Reset helpers
    # ------------------------------------------------------------------

    def reset_set(self) -> None:
        """
        Clear rally history and current-set speeds at a set boundary.

        The set-1 speed baseline is frozen on the first call and never
        overwritten by subsequent calls.
        """
        if not self._set1_frozen and self._set1_speeds:
            self._set1_frozen = True
        self._rally_points = []
        self._current_set_speeds = []

    def reset_match(self) -> None:
        """Clear all history including set-1 speed baseline."""
        self._rally_points = []
        self._set1_speeds = []
        self._set1_frozen = False
        self._current_set_speeds = []

    # ------------------------------------------------------------------
    # Weight calculation
    # ------------------------------------------------------------------

    def _get_bucket_weights(self) -> dict:
        """
        Return archetype-adjusted, normalised bucket weights.

        Base weights
        ------------
        short=0.25, medium=0.35, long=0.30, fatigue=0.10

        Archetype adjustments (applied if profile is provided)
        ------------------------------------------------------
        PE >= 70 (counter-puncher): long=0.50, short=0.10, medium=0.30, fatigue=0.10
        SD >= 70 (big server):      short=0.40, fatigue=0.25, medium=0.25, long=0.10

        When both conditions are met, SD dominance takes priority (as in the
        Archetype Engine's PMS rules — apply whichever single dimension is higher).

        Weights are renormalised to sum to 1.0 before returning.

        Returns
        -------
        dict with keys: ``short``, ``medium``, ``long``, ``fatigue``
        """
        weights = {
            "short":   0.25,
            "medium":  0.35,
            "long":    0.30,
            "fatigue": 0.10,
        }

        if self._archetype_profile is not None:
            sd = self._archetype_profile.get("SD", 0.0)
            pe = self._archetype_profile.get("PE", 0.0)

            if sd >= 70 or pe >= 70:
                if sd >= pe:
                    # Big Server profile
                    weights["short"]   = 0.40
                    weights["fatigue"] = 0.25
                    weights["medium"]  = 0.25
                    weights["long"]    = 0.10
                else:
                    # Counter-Puncher profile
                    weights["long"]    = 0.50
                    weights["short"]   = 0.10
                    weights["medium"]  = 0.30
                    weights["fatigue"] = 0.10

        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()}

    # ------------------------------------------------------------------
    # Sub-signal helpers (exposed for testability)
    # ------------------------------------------------------------------

    def _bucket_win_pct(self, lo: int, hi: int | None) -> float:
        """
        Win % for rally points whose length falls in [lo, hi] (inclusive).

        Parameters
        ----------
        lo  : int           — minimum rally length (inclusive)
        hi  : int or None   — maximum rally length (inclusive); None = no upper bound

        Returns 50.0 (neutral) if no points fall in the bucket.
        """
        bucket = [
            p for p in self._rally_points
            if p["rally_length"] >= lo and (hi is None or p["rally_length"] <= hi)
        ]
        if not bucket:
            return 50.0
        return sum(1 for p in bucket if p["won"]) / len(bucket) * 100.0

    def _pms4(self) -> float:
        """
        Fatigue signal: compares current-set mean serve speed to set-1 baseline.

        Formula
        -------
        ratio = -(max(0, mean_set1 - mean_current) / mean_set1)

        Mapped from [-1, 0] → [0, 100]:
            pms4 = (ratio + 1) * 100   →   0 fatigue (ratio=0) → 100
                                            max fatigue (ratio=-1) → 0

        Returns 0.0 if set1_speeds is empty or fewer than 3 current-set
        speed readings exist (insufficient data).
        """
        if not self._set1_speeds or len(self._current_set_speeds) < 3:
            return 0.0

        mean_set1 = sum(self._set1_speeds) / len(self._set1_speeds)
        if mean_set1 == 0:
            return 0.0

        mean_current = sum(self._current_set_speeds) / len(self._current_set_speeds)
        ratio = -(max(0.0, mean_set1 - mean_current) / mean_set1)
        return (ratio + 1.0) * 100.0

    # ------------------------------------------------------------------
    # Main computation
    # ------------------------------------------------------------------

    def compute(self) -> float:
        """
        Compute the Physical Module Score.

        Sub-signals
        -----------
        PMS_1  Short rally win %   (length 1–3)
        PMS_2  Medium rally win %  (length 4–8)
        PMS_3  Long rally win %    (length 9+)
        PMS_4  Fatigue signal

        Weights come from ``_get_bucket_weights()`` and are archetype-adjusted.

        Returns
        -------
        float  PMS score in [0, 100].
        """
        pms1 = self._bucket_win_pct(1, 3)
        pms2 = self._bucket_win_pct(4, 8)
        pms3 = self._bucket_win_pct(9, None)
        pms4 = self._pms4()

        w = self._get_bucket_weights()
        pms = (
            w["short"]   * pms1
            + w["medium"]  * pms2
            + w["long"]    * pms3
            + w["fatigue"] * pms4
        )

        result = pms * self._archetype_weight
        return max(0.0, min(100.0, result))
