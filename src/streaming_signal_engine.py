"""Streaming twin of :class:`src.signal_engine.SignalEngine`.

``SignalEngine`` walks an entire match's point stream in one call and
returns a list of per-game signal rows. ``StreamingMatchState`` exposes
the same per-point logic in a stateful, point-at-a-time form: feed one
point via :meth:`process_point` and receive the just-closed game's row
dict (or ``None`` if the current game has not ended yet); call
:meth:`finalize` once the match is over to flush the final game.

The point-loop body, accumulator types, score-state sets and game-close
formulas are reused verbatim from ``src.signal_engine`` so that, given
the same point sequence, this engine emits dicts equal to those produced
by ``SignalEngine.process_match``. See
``docs/streaming_signal_engine_recon.md`` (especially sections 3, 4, 5,
8 and 10) for the recon that motivated this design.
"""
from __future__ import annotations

from collections import deque

from src.signal_engine import (
    BP_STATES,
    DEEP_BP_STATES,
    DEUCE_STATE,
    PRESSURE_STATES,
    SignalEngine,
    _GameTracker,
    _PlayerAcc,
    _new_acc_set,
)


class StreamingMatchState:
    """Stateful per-match streaming twin of :class:`SignalEngine`."""

    def __init__(self, match_id_int: int) -> None:
        self.match_id_int: int = match_id_int
        self.acc: dict[str, dict[int, _PlayerAcc]] = {
            "ws": _new_acc_set(),
            "cm": _new_acc_set(),
        }
        self.mrs_deque: dict[str, deque] = {
            "ws": deque(maxlen=30),
            "cm": deque(maxlen=30),
        }
        self.in_game: _GameTracker | None = None
        self.prev_set: int | None = None
        self.prev_game: int | None = None
        self._finalized: bool = False

    def process_point(self, pt) -> dict | None:
        """Feed one point; return the just-closed game's row dict, or ``None``.

        A row is returned only when this point is the first point of a
        new ``(set_number, game_number_in_set)`` pair — i.e. the previous
        game has just ended. Otherwise ``None`` is returned.
        """
        if self._finalized:
            raise RuntimeError(
                "process_point called after finalize(); "
                "StreamingMatchState is single-use per match"
            )

        emitted: dict | None = None

        cur_set = int(pt.set_number)
        cur_game = int(pt.game_number_in_set)

        if (cur_set, cur_game) != (self.prev_set, self.prev_game):
            if self.in_game is not None:
                SignalEngine._close_game(self.in_game, self.acc)
                emitted = SignalEngine._emit_row(
                    self.match_id_int,
                    self.prev_set,
                    self.prev_game,
                    self.acc,
                    self.mrs_deque,
                )
            if self.prev_set is not None and cur_set != self.prev_set:
                self.acc["ws"] = _new_acc_set()
                self.mrs_deque["ws"] = deque(maxlen=30)
            self.in_game = _GameTracker(
                is_tiebreak=bool(pt.is_tiebreak),
                server=int(pt.Svr),
            )
            self.prev_set, self.prev_game = cur_set, cur_game

        score = pt.score_before
        server_int = int(pt.Svr)
        returner_int = 2 if server_int == 1 else 1
        winner_int = int(pt.PtWinner)
        point_is_tiebreak = bool(pt.is_tiebreak)

        self.mrs_deque["ws"].append(winner_int)
        self.mrs_deque["cm"].append(winner_int)

        # Known quirk preserved from SignalEngine: during tiebreaks the
        # incoming score_before is numeric (e.g. "6-6", "7-5") and almost
        # never matches PRESSURE_STATES, so CPI effectively rarely updates
        # mid-tiebreak. This is the batch engine's behaviour and we match it.
        if score in PRESSURE_STATES:
            for ver in ("ws", "cm"):
                a = self.acc[ver]
                a[server_int].serve_pressure_played += 1
                a[returner_int].return_pressure_played += 1
                if winner_int == server_int:
                    a[server_int].serve_pressure_won += 1
                else:
                    a[returner_int].return_pressure_won += 1

        if not point_is_tiebreak:
            for ver in ("ws", "cm"):
                a = self.acc[ver]
                a[server_int].serve_pts_played += 1
                a[returner_int].return_pts_played += 1
                if winner_int == server_int:
                    a[server_int].serve_pts_won += 1
                else:
                    a[returner_int].return_pts_won += 1
            if score in BP_STATES:
                self.in_game.bp_count += 1
                self.in_game.bp_state_reached = True
                if score in DEEP_BP_STATES:
                    self.in_game.deep_reached = True
            if score == DEUCE_STATE:
                self.in_game.deuce_reached = True
            self.in_game.points_played += 1

        self.in_game.is_tiebreak = self.in_game.is_tiebreak or point_is_tiebreak
        self.in_game.last_winner = winner_int

        return emitted

    def finalize(self) -> dict | None:
        """Flush and return the final game's row dict; idempotent after first call."""
        if self._finalized:
            return None
        self._finalized = True
        if self.in_game is None:
            return None
        SignalEngine._close_game(self.in_game, self.acc)
        return SignalEngine._emit_row(
            self.match_id_int,
            self.prev_set,
            self.prev_game,
            self.acc,
            self.mrs_deque,
        )
