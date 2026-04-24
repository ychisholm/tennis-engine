#!/usr/bin/env python3
"""
Component 4B — Serve Module Score (SMS)

Measures live serve quality for the player currently serving.  Four
sub-signals are combined into a single 0–100 score:

  SMS_1  1st-serve win %      (last n points)
  SMS_2  2nd-serve win %      (last n points)
  SMS_3  Serve speed trend    (recent EMA vs match average)
  SMS_4  Effective hold %     (clean holds score higher than grinding ones)

Usage
-----
    from src.signals.sms import SMSCalculator

    calc = SMSCalculator(archetype_weight=1.1)

    calc.add_serve_point({"serve_number": 1, "won": True,  "speed_kmh": 210, "point_index": 0})
    calc.add_serve_point({"serve_number": 1, "won": False, "speed_kmh": 205, "point_index": 1})
    calc.add_serve_point({"serve_number": 2, "won": True,  "speed_kmh": 170, "point_index": 2})

    calc.add_completed_game({"points_played": 4, "held": True})

    print(calc.compute())
"""

from __future__ import annotations


class SMSCalculator:
    """
    Serve Module Score calculator for a single serving player.

    Parameters
    ----------
    archetype_weight : float
        Multiplier sourced from the Archetype Engine (3A).  Scales how much
        this signal matters for the specific player matchup.  Default 1.0.
    """

    def __init__(self, archetype_weight: float = 1.0) -> None:
        self._archetype_weight: float = archetype_weight
        self._serve_points: list[dict] = []
        self._completed_games: list[dict] = []

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_archetype_weight(self, weight: float) -> None:
        """Update the archetype multiplier during the match."""
        self._archetype_weight = weight

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def add_serve_point(self, point: dict) -> None:
        """
        Append a completed serve point to the rolling history.

        Parameters
        ----------
        point : dict
            Must contain:
            - ``serve_number`` : int   — 1 or 2
            - ``won``          : bool  — True if the server won the point
            - ``speed_kmh``    : float or None — serve speed (may be absent)
            - ``point_index``  : int   — sequential position in the match
        """
        self._serve_points.append(point)

    def add_completed_game(self, game: dict) -> None:
        """
        Append a completed service game to the set history.

        Parameters
        ----------
        game : dict
            Must contain:
            - ``points_played`` : int  — total points played in the game
            - ``held``          : bool — True if the server held serve
        """
        self._completed_games.append(game)

    # ------------------------------------------------------------------
    # Reset helpers
    # ------------------------------------------------------------------

    def reset_set(self) -> None:
        """
        Clear the completed-games list at a set boundary.

        Serve point history is intentionally preserved — the rolling window
        for SMS_1/SMS_2/SMS_3 spans set boundaries.
        """
        self._completed_games = []

    def reset_match(self) -> None:
        """Clear all history (serve points and completed games)."""
        self._serve_points = []
        self._completed_games = []

    # ------------------------------------------------------------------
    # Sub-signal helpers (exposed for testability)
    # ------------------------------------------------------------------

    def _sms1(self, window: list[dict]) -> float:
        """
        1st-serve win % over the supplied point window.

        Returns 50.0 (neutral) if there are no 1st-serve points in the window.
        """
        first_serve = [p for p in window if p["serve_number"] == 1]
        if not first_serve:
            return 50.0
        return sum(1 for p in first_serve if p["won"]) / len(first_serve) * 100.0

    def _sms2(self, window: list[dict]) -> float:
        """
        2nd-serve win % over the supplied point window.

        Returns 50.0 (neutral) if there are no 2nd-serve points in the window.
        """
        second_serve = [p for p in window if p["serve_number"] == 2]
        if not second_serve:
            return 50.0
        return sum(1 for p in second_serve if p["won"]) / len(second_serve) * 100.0

    def _sms3(self, all_points: list[dict]) -> float:
        """
        Serve speed trend: recent EMA vs full-match average.

        Formula
        -------
        ratio = 1 - (avg_speed_match - current_speed_ema) / avg_speed_match
              = current_speed_ema / avg_speed_match

        Clipped to [0.5, 1.5], then linearly mapped to [0, 100]
        (0.5 → 0, 1.5 → 100).

        Returns 50.0 if fewer than 3 speed readings exist in *all_points*.
        """
        speeds = [
            p["speed_kmh"]
            for p in all_points
            if p.get("speed_kmh") is not None
        ]
        if len(speeds) < 3:
            return 50.0

        avg_speed = sum(speeds) / len(speeds)
        if avg_speed == 0:
            return 50.0

        # EMA of the most recent 5 speeds
        recent = speeds[-5:]
        alpha = 2.0 / (len(recent) + 1)
        ema = recent[0]
        for s in recent[1:]:
            ema = alpha * s + (1.0 - alpha) * ema

        ratio = ema / avg_speed
        ratio_clipped = max(0.5, min(1.5, ratio))
        return (ratio_clipped - 0.5) / (1.5 - 0.5) * 100.0

    def _sms4(self) -> float:
        """
        Effective hold % this set.

        For each held game: efficiency = 4 / points_played_in_game.
        SMS_4_raw = mean(efficiency across all held games) / total_games_served.

        Scaled to [0, 100] via × 100 then clipped.
        Returns 50.0 if no games have been served yet.

        Rationale: a 4-point hold scores efficiency=1.0 while an 8-point hold
        scores 0.5.  Dividing by total games penalises service breaks (a broken
        game contributes 0 efficiency).
        """
        if not self._completed_games:
            return 50.0

        total_games = len(self._completed_games)
        held_efficiencies = [
            4.0 / g["points_played"]
            for g in self._completed_games
            if g["held"] and g["points_played"] > 0
        ]

        if not held_efficiencies:
            # Served games but held none → 0 efficiency
            return 0.0

        sms4_raw = sum(held_efficiencies) / total_games
        return max(0.0, min(100.0, sms4_raw * 100.0))

    # ------------------------------------------------------------------
    # Main computation
    # ------------------------------------------------------------------

    def compute(self, n: int = 15) -> float:
        """
        Compute the Serve Module Score from the last *n* serve points.

        Sub-signal weights
        ------------------
        SMS_1  0.35  (1st-serve win %)
        SMS_2  0.30  (2nd-serve win %)
        SMS_3  0.20  (speed trend)
        SMS_4  0.15  (effective hold %)

        Parameters
        ----------
        n : int
            Rolling window size (number of most-recent serve points).
            Default 15.

        Returns
        -------
        float  SMS score in [0, 100].
        """
        window = self._serve_points[-n:] if len(self._serve_points) > n else self._serve_points

        sms1 = self._sms1(window)
        sms2 = self._sms2(window)
        sms3 = self._sms3(self._serve_points)   # speed trend uses full history
        sms4 = self._sms4()

        sms = 0.35 * sms1 + 0.30 * sms2 + 0.20 * sms3 + 0.15 * sms4

        result = sms * self._archetype_weight
        return max(0.0, min(100.0, result))
