#!/usr/bin/env python3
"""
Component 5C — LiveMatch Orchestrator

Wires every prior component together into a single entry point.
Call process_point() for each point of the match and receive a full
probability + dominance snapshot.

Usage
-----
    from src.live_match import LiveMatch

    match = LiveMatch(
        player_a={"name": "Federer",  "p0_hard": 0.64, "p0_clay": 0.60, "p0_grass": 0.67,
                  "archetype": {"sd": 75, "ba": 80, "pe": 75, "tv": 95}},
        player_b={"name": "Djokovic", "p0_hard": 0.63, "p0_clay": 0.62, "p0_grass": 0.63,
                  "archetype": {"sd": 60, "ba": 70, "pe": 90, "tv": 85}},
        surface="hard",
        best_of=3,
    )
    result = match.process_point({"winner": "A", "rally_length": 3, "serve_speed": 208.0})
"""

from __future__ import annotations

import datetime

from src.engine.signals.nmi import NMICalculator
from src.engine.signals.sms import SMSCalculator
from src.engine.signals.rms import RMSCalculator
from src.engine.signals.pms import PMSCalculator
from src.engine.signals.gps import GPSCalculator
from src.engine.temporal_engine import TemporalEngine
from src.engine.phat_adjuster import PhatAdjuster
from src.engine.markov_engine import (
    compute_live_probabilities,
    tiebreak_win_prob,
)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_POINT_NAMES = {0: "0", 1: "15", 2: "30", 3: "40"}


def _tb_first_serves(point_number: int) -> bool:
    """True when the tiebreak's first server serves point *point_number*."""
    if point_number == 0:
        return True
    return ((point_number - 1) // 2) % 2 == 1


def _game_state_str(sp: int, rp: int) -> str:
    """Convert (server_pts, receiver_pts) integers to an NMI score-state string."""
    if sp >= 3 and rp >= 3:
        diff = sp - rp
        if diff == 0:
            return "deuce"
        if diff > 0:
            return "ad-server"
        return "deuce-ad-receiver"
    s = _POINT_NAMES[min(sp, 3)]
    r = _POINT_NAMES[min(rp, 3)]
    return f"{s}-{r}"


def _is_break_point(sp: int, rp: int) -> bool:
    """True when the receiver winning this point would end the game in their favour."""
    new_r = rp + 1
    return new_r >= 4 and (new_r - sp) >= 2


def _clip_weight(w: float) -> float:
    return max(0.5, min(2.0, w))


def _other(player: str) -> str:
    return "B" if player == "A" else "A"


# ---------------------------------------------------------------------------
# LiveMatch
# ---------------------------------------------------------------------------

class LiveMatch:
    """
    Top-level match orchestrator.

    Parameters
    ----------
    player_a, player_b : dict
        Each must have keys:
          name       : str
          p0_hard    : float  — career serve-win % on hard courts
          p0_clay    : float
          p0_grass   : float
          archetype  : dict with keys sd, ba, pe, tv  (0–100 each)
    surface : str
        "hard", "clay", or "grass".  Default "hard".
    best_of : int
        3 or 5.  Default 3.
    lambda_decay : float
        Recency half-life (points) passed to TemporalEngine.  Default 4.0.
    k : float
        Max p-hat adjustment magnitude passed to PhatAdjuster.  Default 0.08.
    sigma : float
        Sigmoid sensitivity passed to PhatAdjuster.  Default 25.0.
    """

    def __init__(
        self,
        player_a: dict,
        player_b: dict,
        surface: str = "hard",
        best_of: int = 3,
        lambda_decay: float = 4.0,
        k: float = 0.08,
        sigma: float = 25.0,
    ) -> None:
        self._pa = player_a
        self._pb = player_b
        self._surface = surface.lower()
        self._best_of = best_of
        self._sets_to_win = (best_of + 1) // 2

        # ---- Signal calculators ----
        # NMI: one per player *as receiver*
        self._nmi_a = NMICalculator()
        self._nmi_b = NMICalculator()

        # SMS: one per player *as server*
        self._sms_a = SMSCalculator()
        self._sms_b = SMSCalculator()

        # RMS: shares NMI reference (same object, not a copy)
        self._rms_a = RMSCalculator(nmi=self._nmi_a)
        self._rms_b = RMSCalculator(nmi=self._nmi_b)

        # PMS: archetype-aware
        self._pms_a = PMSCalculator(
            archetype_profile={"SD": player_a["archetype"]["sd"],
                               "PE": player_a["archetype"]["pe"]}
        )
        self._pms_b = PMSCalculator(
            archetype_profile={"SD": player_b["archetype"]["sd"],
                               "PE": player_b["archetype"]["pe"]}
        )

        # GPS: one per player *as server*
        self._gps_a = GPSCalculator()
        self._gps_b = GPSCalculator()

        # ---- Higher-level engines ----
        self._temporal = TemporalEngine(lambda_decay=lambda_decay)
        self._phat = PhatAdjuster(k=k, sigma=sigma)

        # ---- Archetype weights ----
        self._weights_a = self._derive_weights(player_a["archetype"])
        self._weights_b = self._derive_weights(player_b["archetype"])

        # ---- Match state ----
        self._sets_a = 0
        self._sets_b = 0
        self._games_a = 0          # games won in current set
        self._games_b = 0
        self._sp = 0               # server's points in current game
        self._rp = 0               # receiver's points in current game
        self._set_number = 1
        self._point_index = 0
        self._serving = "A"        # server of the current game/point
        self._set_first_server = "A"

        # ---- Tiebreak state ----
        self._in_tiebreak = False
        self._tb_pts_a = 0
        self._tb_pts_b = 0
        self._tb_points_played = 0  # used to determine whose serve in TB
        self._tb_first_server = "A"

        # ---- Per-game accumulation (reset each game) ----
        self._game_states: list[str] = []   # score-state strings for NMI
        self._game_points_played = 0        # total points for GPS/SMS

        # ---- Match outcome ----
        self._match_over = False
        self._winner: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_point(self, point_data: dict) -> dict:
        """
        Process one completed point and return a full snapshot.

        Parameters
        ----------
        point_data : dict
            winner         : "A" or "B"
            serving        : "A" or "B"  (optional, defaults to tracked server)
            rally_length   : int          (default 1)
            serve_speed    : float|None   (km/h, optional)
            is_first_serve : bool         (default True)

        Returns
        -------
        dict — full output schema (see module docstring).
        """
        if self._match_over:
            raise RuntimeError("Match is already over.")

        # ---- Unpack point data ----
        winner = point_data["winner"]

        if self._in_tiebreak:
            tb_server = (self._tb_first_server
                         if _tb_first_serves(self._tb_points_played)
                         else _other(self._tb_first_server))
            serving = point_data.get("serving", tb_server)
        else:
            serving = point_data.get("serving", self._serving)

        receiver     = _other(serving)
        rally_length = point_data.get("rally_length", 1)
        serve_speed  = point_data.get("serve_speed", None)
        is_first     = point_data.get("is_first_serve", True)
        server_won   = (winner == serving)
        receiver_won = not server_won

        # ---- Select calculators by role ----
        nmi_rcv = self._nmi_b if serving == "A" else self._nmi_a
        sms_srv = self._sms_a if serving == "A" else self._sms_b
        rms_rcv = self._rms_b if serving == "A" else self._rms_a
        pms_srv = self._pms_a if serving == "A" else self._pms_b
        pms_rcv = self._pms_b if serving == "A" else self._pms_a
        gps_srv = self._gps_a if serving == "A" else self._gps_b

        # ---- Accumulate game score state for NMI ----
        if not self._in_tiebreak:
            self._game_states.append(_game_state_str(self._sp, self._rp))
        self._game_points_played += 1

        # ---- Update signal calculators ----
        sms_srv.add_serve_point({
            "serve_number": 1 if is_first else 2,
            "won": server_won,
            "speed_kmh": serve_speed,
            "point_index": self._point_index,
        })

        rms_rcv.add_return_point({"won": receiver_won, "point_index": self._point_index})
        if not self._in_tiebreak and _is_break_point(self._sp, self._rp):
            rms_rcv.record_break_point_opportunity(converted=receiver_won)

        pms_srv.add_rally_point({
            "won": server_won,
            "rally_length": rally_length,
            "point_index": self._point_index,
        })
        pms_rcv.add_rally_point({
            "won": receiver_won,
            "rally_length": rally_length,
            "point_index": self._point_index,
        })
        if serve_speed is not None:
            pms_srv.add_serve_speed(serve_speed, is_set1=(self._set_number == 1))

        # ---- Read signal scores ----
        game_idx = self._games_a + self._games_b
        signals_a, signals_b = self._read_signals(game_idx)

        # ---- Temporal engine ----
        t_result = self._temporal.update(
            signals_a, signals_b, self._point_index,
            self._weights_a, self._weights_b,
        )
        D_A, D_B, delta = t_result["D_A"], t_result["D_B"], t_result["delta"]

        # ---- p-hat adjustment ----
        p0_A = self._p0("A")
        p0_B = self._p0("B")
        ph = self._phat.adjust(p0_A, p0_B, delta)
        p_hat_A, p_hat_B = ph["p_hat_A"], ph["p_hat_B"]

        # ---- Markov probabilities (state BEFORE advancing score) ----
        probs = self._markov_probs(p_hat_A, p_hat_B, serving)

        # ---- Advance score ----
        set_over   = False
        set_winner = None

        if self._in_tiebreak:
            set_over, set_winner = self._advance_tiebreak(winner, serving)
        else:
            set_over, set_winner = self._advance_game(
                server_won, receiver_won, serving, receiver,
                nmi_rcv, sms_srv, gps_srv,
            )

        # ---- Set boundary ----
        if set_over:
            if set_winner == "A":
                self._sets_a += 1
            else:
                self._sets_b += 1

            if self._sets_a >= self._sets_to_win or self._sets_b >= self._sets_to_win:
                self._match_over = True
                self._winner = "A" if self._sets_a >= self._sets_to_win else "B"
            else:
                self._handle_set_boundary()

        self._point_index += 1

        # ---- Build output ----
        return self._build_output(
            signals_a, signals_b, D_A, D_B, delta,
            p_hat_A, p_hat_B, probs,
        )

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _advance_game(
        self,
        server_won: bool,
        receiver_won: bool,
        serving: str,
        receiver: str,
        nmi_rcv,
        sms_srv,
        gps_srv,
    ) -> tuple[bool, str | None]:
        """Advance point score; handle game completion. Returns (set_over, set_winner)."""
        if server_won:
            self._sp += 1
        else:
            self._rp += 1

        game_over = (
            (self._sp >= 4 and self._sp - self._rp >= 2) or
            (self._rp >= 4 and self._rp - self._sp >= 2)
        )

        set_over   = False
        set_winner = None

        if game_over:
            game_winner = serving if server_won else receiver

            # Inform signal calculators
            game_idx = self._games_a + self._games_b
            gps_srv.add_completed_game({"points_played": self._game_points_played,
                                         "game_index": game_idx})
            nmi_rcv.add_game({"points":    list(self._game_states),
                               "game_index": game_idx,
                               "converted": receiver_won})
            sms_srv.add_completed_game({"points_played": self._game_points_played,
                                         "held": server_won})

            if game_winner == "A":
                self._games_a += 1
            else:
                self._games_b += 1

            self._sp = 0
            self._rp = 0
            self._game_states = []
            self._game_points_played = 0
            self._serving = receiver        # serve flips each game

            ga, gb = self._games_a, self._games_b

            if ga == 6 and gb == 6:
                self._in_tiebreak = True
                self._tb_first_server = self._serving  # current server starts TB
            else:
                set_won = (
                    (ga >= 6 and ga - gb >= 2) or
                    (gb >= 6 and gb - ga >= 2) or
                    (ga == 7 and gb <= 5) or
                    (gb == 7 and ga <= 5)
                )
                if set_won:
                    set_over = True
                    set_winner = "A" if ga > gb else "B"

        return set_over, set_winner

    def _advance_tiebreak(
        self, winner: str, serving: str
    ) -> tuple[bool, str | None]:
        """Advance tiebreak score. Returns (set_over, set_winner)."""
        if winner == "A":
            self._tb_pts_a += 1
        else:
            self._tb_pts_b += 1
        self._tb_points_played += 1

        # Update serving for the next TB point
        next_serves = (self._tb_first_server
                       if _tb_first_serves(self._tb_points_played)
                       else _other(self._tb_first_server))
        self._serving = next_serves

        tb_over = (
            (self._tb_pts_a >= 7 and self._tb_pts_a - self._tb_pts_b >= 2) or
            (self._tb_pts_b >= 7 and self._tb_pts_b - self._tb_pts_a >= 2)
        )

        set_over   = False
        set_winner = None

        if tb_over:
            tb_winner = "A" if self._tb_pts_a > self._tb_pts_b else "B"
            if tb_winner == "A":
                self._games_a += 1
            else:
                self._games_b += 1

            set_over   = True
            set_winner = tb_winner

            # After a tiebreak, the NON-first-server serves the next set
            self._serving = _other(self._tb_first_server)

            self._in_tiebreak       = False
            self._tb_pts_a          = 0
            self._tb_pts_b          = 0
            self._tb_points_played  = 0
            self._game_points_played = 0
            self._game_states        = []
            self._sp = 0
            self._rp = 0

        return set_over, set_winner

    def _handle_set_boundary(self) -> None:
        """Reset all set-scoped state and tell every component a set ended."""
        self._temporal.handle_set_boundary()

        self._nmi_a.reset();  self._nmi_b.reset()
        self._sms_a.reset_set(); self._sms_b.reset_set()
        self._rms_a.reset_set(); self._rms_b.reset_set()
        self._pms_a.reset_set(); self._pms_b.reset_set()
        self._gps_a.reset();  self._gps_b.reset()

        self._games_a = 0
        self._games_b = 0
        self._sp = 0
        self._rp = 0
        self._game_states = []
        self._game_points_played = 0
        self._set_number += 1
        self._set_first_server = self._serving

    # ------------------------------------------------------------------
    # Probability helpers
    # ------------------------------------------------------------------

    def _markov_probs(
        self, p_hat_A: float, p_hat_B: float, serving: str
    ) -> dict:
        if self._in_tiebreak:
            # Use correct tiebreak formula for P_game
            if self._tb_first_server == "A":
                p_game_A = tiebreak_win_prob(
                    p_hat_A, 1.0 - p_hat_B,
                    self._tb_pts_a, self._tb_pts_b,
                )
            else:
                p_game_B = tiebreak_win_prob(
                    p_hat_B, 1.0 - p_hat_A,
                    self._tb_pts_b, self._tb_pts_a,
                )
                p_game_A = 1.0 - p_game_B

            # For set/match, pass game-level state with tiebreak in progress
            # Pass clipped tiebreak points so game_win_prob doesn't break
            markov_state = {
                "sets_A": self._sets_a,
                "sets_B": self._sets_b,
                "games_A": self._games_a,
                "games_B": self._games_b,
                "points_A": min(self._tb_pts_a, 3),
                "points_B": min(self._tb_pts_b, 3),
                "serving_player": serving,
                "best_of": self._best_of,
            }
            full = compute_live_probabilities(p_hat_A, p_hat_B, markov_state)
            return {
                "P_game_A": p_game_A,
                "P_set_A":  full["P_set_A"],
                "P_match_A": full["P_match_A"],
            }

        if serving == "A":
            points_A_m = self._sp
            points_B_m = self._rp
        else:
            points_A_m = self._rp
            points_B_m = self._sp

        markov_state = {
            "sets_A": self._sets_a,
            "sets_B": self._sets_b,
            "games_A": self._games_a,
            "games_B": self._games_b,
            "points_A": points_A_m,
            "points_B": points_B_m,
            "serving_player": serving,
            "best_of": self._best_of,
        }
        return compute_live_probabilities(p_hat_A, p_hat_B, markov_state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_weights(arch: dict) -> dict:
        return {
            "nmi": _clip_weight(1.0 + (arch["ba"] - 50) / 100),
            "sms": _clip_weight(1.0 + (arch["sd"] - 50) / 100),
            "rms": _clip_weight(1.0 + (50 - arch["sd"]) / 100),
            "pms": _clip_weight(1.0 + (arch["pe"] - 50) / 100),
            "gps": _clip_weight(1.0 + (arch["tv"] - 50) / 100),
        }

    def _p0(self, player: str) -> float:
        data = self._pa if player == "A" else self._pb
        return {"hard": data["p0_hard"],
                "clay": data["p0_clay"],
                "grass": data["p0_grass"]}.get(self._surface, data["p0_hard"])

    def _read_signals(self, game_idx: int) -> tuple[dict, dict]:
        sig_a = {
            "nmi": self._nmi_a.compute(game_idx),
            "sms": self._sms_a.compute(),
            "rms": self._rms_a.compute(game_idx),
            "pms": self._pms_a.compute(),
            "gps": self._gps_a.compute(game_idx),
        }
        sig_b = {
            "nmi": self._nmi_b.compute(game_idx),
            "sms": self._sms_b.compute(),
            "rms": self._rms_b.compute(game_idx),
            "pms": self._pms_b.compute(),
            "gps": self._gps_b.compute(game_idx),
        }
        return sig_a, sig_b

    def _build_output(
        self,
        signals_a: dict, signals_b: dict,
        D_A: float, D_B: float, delta: float,
        p_hat_A: float, p_hat_B: float,
        probs: dict,
    ) -> dict:
        # points_A/B: show from each player's perspective
        if self._in_tiebreak:
            pts_a = self._tb_pts_a
            pts_b = self._tb_pts_b
        elif self._serving == "A":
            pts_a, pts_b = self._sp, self._rp
        else:
            pts_a, pts_b = self._rp, self._sp

        return {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "match_state": {
                "sets_A":         self._sets_a,
                "sets_B":         self._sets_b,
                "games_A":        self._games_a,
                "games_B":        self._games_b,
                "points_A":       pts_a,
                "points_B":       pts_b,
                "serving_player": self._serving,
                "set_number":     self._set_number,
                "game_number":    self._games_a + self._games_b + 1,
            },
            "dominance": {
                "D_A":         D_A,
                "D_B":         D_B,
                "delta":       delta,
                "breakdown_A": signals_a,
                "breakdown_B": signals_b,
            },
            "adjusted_p":  {"p_hat_A": p_hat_A, "p_hat_B": p_hat_B},
            "probabilities": probs,
            "match_over":  self._match_over,
            "winner":      self._winner,
        }
