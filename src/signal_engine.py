"""Signal engine — compute the 52 signal sub-components per game.

Walks point-level data sequentially per match, maintaining per-player
within-set (ws) and cumulative-match (cm) accumulators, and emits one
set of 52 signal sub-component values per completed game.

Signal definitions live in the project reference guide Section 5; this
module implements them exactly. Tiebreak handling: BPI / SDS / RES
freeze (no updates), CPI / MRS continue normally. ws accumulators reset
at each set boundary; cm accumulators persist across the entire match.

Public API:
    SignalEngine().process_match(match_id_int, points_iter) -> list[dict]
    SIGNAL_COLUMNS  — the 52 column names in canonical order
    BP_STATES, DEEP_BP_STATES, PRESSURE_STATES, DEUCE_STATE — score-state sets
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable


# ── Score-state sets ──
BP_STATES = frozenset({"0-40", "15-40", "30-40", "40-AD"})
DEEP_BP_STATES = frozenset({"0-40", "15-40"})
PRESSURE_STATES = frozenset({
    "30-30", "40-40", "30-40", "40-30",
    "40-AD", "AD-40", "0-40", "15-40",
})
DEUCE_STATE = "40-40"


def _signal_column_names() -> list[str]:
    base = [
        ("bpi", ["bp_rate", "deep_pressure_rate", "near_pressure_rate"]),
        ("sds", ["serve_win_pct", "hold_rate", "avg_pts_per_game"]),
        ("res", ["return_win_pct", "bp_conv_rate"]),
        ("cpi", ["serve_pressure_pct", "return_pressure_pct"]),
        ("mrs", ["pwr_10", "pwr_30", "game_streak"]),
    ]
    cols: list[str] = []
    for sig, subs in base:
        for sub in subs:
            for ver in ("ws", "cm"):
                for player in ("a", "b"):
                    cols.append(f"{sig}_{sub}_{ver}_{player}")
    return cols


SIGNAL_COLUMNS: list[str] = _signal_column_names()
assert len(SIGNAL_COLUMNS) == 52, f"expected 52 columns, got {len(SIGNAL_COLUMNS)}"


@dataclass
class _PlayerAcc:
    """Per-player signal accumulators for one carryover version."""
    serve_pts_played: int = 0
    serve_pts_won: int = 0
    service_games_played: int = 0
    service_games_held: int = 0
    service_game_total_points: int = 0
    return_pts_played: int = 0
    return_pts_won: int = 0
    return_games_played: int = 0
    breaks_won_with_bp: int = 0
    bp_states_faced: int = 0
    deep_pressure_games: int = 0
    near_pressure_games: int = 0
    serve_pressure_played: int = 0
    serve_pressure_won: int = 0
    return_pressure_played: int = 0
    return_pressure_won: int = 0
    game_streak: int = 0


@dataclass
class _GameTracker:
    """Within-game running state, reset at each new game."""
    is_tiebreak: bool = False
    server: int = 0  # 1 or 2; 0 means uninitialised
    bp_count: int = 0
    deep_reached: bool = False
    bp_state_reached: bool = False
    deuce_reached: bool = False
    points_played: int = 0
    last_winner: int = 0  # 1 or 2; 0 means uninitialised


def _new_acc_set() -> dict[int, _PlayerAcc]:
    return {1: _PlayerAcc(), 2: _PlayerAcc()}


class SignalEngine:
    """Walks one match's points and emits per-game signal rows."""

    def process_match(
        self, match_id_int: int, points_iter: Iterable
    ) -> list[dict]:
        """Process one match's points (sorted by Pt ascending).

        Each point object must expose: set_number, game_number_in_set,
        score_before, Svr, PtWinner, is_tiebreak. Returns a list of
        dicts — one per completed game — containing match_id_int,
        set_number, game_number_in_set, plus the 52 signal columns.
        """
        acc = {"ws": _new_acc_set(), "cm": _new_acc_set()}
        mrs_deque = {"ws": deque(maxlen=30), "cm": deque(maxlen=30)}

        rows: list[dict] = []
        in_game: _GameTracker | None = None
        prev_set: int | None = None
        prev_game: int | None = None

        for pt in points_iter:
            cur_set = int(pt.set_number)
            cur_game = int(pt.game_number_in_set)

            if (cur_set, cur_game) != (prev_set, prev_game):
                if in_game is not None:
                    self._close_game(in_game, acc)
                    rows.append(self._emit_row(
                        match_id_int, prev_set, prev_game, acc, mrs_deque
                    ))
                if prev_set is not None and cur_set != prev_set:
                    acc["ws"] = _new_acc_set()
                    mrs_deque["ws"] = deque(maxlen=30)
                in_game = _GameTracker(
                    is_tiebreak=bool(pt.is_tiebreak),
                    server=int(pt.Svr),
                )
                prev_set, prev_game = cur_set, cur_game

            score = pt.score_before
            server_int = int(pt.Svr)
            returner_int = 2 if server_int == 1 else 1
            winner_int = int(pt.PtWinner)
            point_is_tiebreak = bool(pt.is_tiebreak)

            mrs_deque["ws"].append(winner_int)
            mrs_deque["cm"].append(winner_int)

            if score in PRESSURE_STATES:
                for ver in ("ws", "cm"):
                    a = acc[ver]
                    a[server_int].serve_pressure_played += 1
                    a[returner_int].return_pressure_played += 1
                    if winner_int == server_int:
                        a[server_int].serve_pressure_won += 1
                    else:
                        a[returner_int].return_pressure_won += 1

            if not point_is_tiebreak:
                for ver in ("ws", "cm"):
                    a = acc[ver]
                    a[server_int].serve_pts_played += 1
                    a[returner_int].return_pts_played += 1
                    if winner_int == server_int:
                        a[server_int].serve_pts_won += 1
                    else:
                        a[returner_int].return_pts_won += 1
                if score in BP_STATES:
                    in_game.bp_count += 1
                    in_game.bp_state_reached = True
                    if score in DEEP_BP_STATES:
                        in_game.deep_reached = True
                if score == DEUCE_STATE:
                    in_game.deuce_reached = True
                in_game.points_played += 1

            in_game.is_tiebreak = in_game.is_tiebreak or point_is_tiebreak
            in_game.last_winner = winner_int

        if in_game is not None:
            self._close_game(in_game, acc)
            rows.append(self._emit_row(
                match_id_int, prev_set, prev_game, acc, mrs_deque
            ))

        return rows

    @staticmethod
    def _close_game(g: _GameTracker, acc: dict) -> None:
        if g.last_winner == 0:
            return
        winner = g.last_winner
        server = g.server
        returner = 2 if server == 1 else 1

        for ver in ("ws", "cm"):
            if winner == 1:
                acc[ver][1].game_streak += 1
                acc[ver][2].game_streak = 0
            else:
                acc[ver][2].game_streak += 1
                acc[ver][1].game_streak = 0

        if g.is_tiebreak:
            return

        held = (winner == server)
        for ver in ("ws", "cm"):
            srv = acc[ver][server]
            ret = acc[ver][returner]
            srv.service_games_played += 1
            srv.service_game_total_points += g.points_played
            if held:
                srv.service_games_held += 1
            ret.return_games_played += 1
            ret.bp_states_faced += g.bp_count
            if g.deep_reached:
                ret.deep_pressure_games += 1
            if g.deuce_reached and not g.bp_state_reached:
                ret.near_pressure_games += 1
            if (not held) and g.bp_state_reached:
                ret.breaks_won_with_bp += 1

    @staticmethod
    def _emit_row(
        match_id_int: int,
        set_number: int,
        game_number_in_set: int,
        acc: dict,
        mrs_deque: dict,
    ) -> dict:
        row: dict = {
            "match_id_int": match_id_int,
            "set_number": set_number,
            "game_number_in_set": game_number_in_set,
        }
        for ver in ("ws", "cm"):
            dq = list(mrs_deque[ver])
            last10 = dq[-10:]
            last30 = dq
            n10 = len(last10)
            n30 = len(last30)
            for player_int, ltr in ((1, "a"), (2, "b")):
                a = acc[ver][player_int]
                denom = a.return_games_played + 2
                row[f"bpi_bp_rate_{ver}_{ltr}"] = a.bp_states_faced / denom
                row[f"bpi_deep_pressure_rate_{ver}_{ltr}"] = a.deep_pressure_games / denom
                row[f"bpi_near_pressure_rate_{ver}_{ltr}"] = a.near_pressure_games / denom

                row[f"sds_serve_win_pct_{ver}_{ltr}"] = (
                    a.serve_pts_won / a.serve_pts_played
                ) if a.serve_pts_played else 0.0
                row[f"sds_hold_rate_{ver}_{ltr}"] = (
                    a.service_games_held / a.service_games_played
                ) if a.service_games_played else 0.0
                row[f"sds_avg_pts_per_game_{ver}_{ltr}"] = (
                    a.service_game_total_points / a.service_games_played
                ) if a.service_games_played else 0.0

                row[f"res_return_win_pct_{ver}_{ltr}"] = (
                    a.return_pts_won / a.return_pts_played
                ) if a.return_pts_played else 0.0
                row[f"res_bp_conv_rate_{ver}_{ltr}"] = (
                    a.breaks_won_with_bp / a.bp_states_faced
                ) if a.bp_states_faced else 0.0

                row[f"cpi_serve_pressure_pct_{ver}_{ltr}"] = (
                    a.serve_pressure_won / a.serve_pressure_played
                ) if a.serve_pressure_played else 0.0
                row[f"cpi_return_pressure_pct_{ver}_{ltr}"] = (
                    a.return_pressure_won / a.return_pressure_played
                ) if a.return_pressure_played else 0.0

                row[f"mrs_pwr_10_{ver}_{ltr}"] = (
                    sum(1 for w in last10 if w == player_int) / n10
                ) if n10 else 0.0
                row[f"mrs_pwr_30_{ver}_{ltr}"] = (
                    sum(1 for w in last30 if w == player_int) / n30
                ) if n30 else 0.0
                row[f"mrs_game_streak_{ver}_{ltr}"] = a.game_streak
        return row
