from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from src.live.tennis_feed import TennisFeed
from src.engine.live_match import LiveMatch

if TYPE_CHECKING:
    from src.live.logger import MatchLogger

load_dotenv()

_POLL_INTERVAL = 15  # seconds
_COL = (22, 14, 14, 10)
_HEADER = (
    f"{'Score (A-B)':<{_COL[0]}}"
    f"{'Model P(A)':<{_COL[1]}}"
    f"{'Bookie P(A)':<{_COL[2]}}"
    f"{'Edge':<{_COL[3]}}"
)


class MatchRunner:
    """
    Orchestrates live polling, LiveMatch updates, and odds comparison.

    Parameters
    ----------
    match_id   : API event ID to track
    player_a   : LiveMatch player_a dict (home player maps to A)
    player_b   : LiveMatch player_b dict (away player maps to B)
    surface    : "hard", "clay", or "grass"
    best_of    : 3 or 5
    api_key    : override RAPIDAPI_KEY from .env
    """

    def __init__(
        self,
        match_id: int | str,
        player_a: dict,
        player_b: dict,
        surface: str = "hard",
        best_of: int = 3,
        api_key: str | None = None,
        log_fn: Callable[..., Any] = print,
        logger: "MatchLogger | None" = None,
        tournament_id: int | None = None,
    ) -> None:
        self._match_id = match_id
        self._player_a_name: str = player_a.get("name", "Home")
        self._player_b_name: str = player_b.get("name", "Away")
        self._player_a_dict = player_a
        self._player_b_dict = player_b
        self._surface = surface
        self._best_of = best_of
        self._tournament_id: int | None = tournament_id
        self._feed = TennisFeed(api_key=api_key)
        self._engine = LiveMatch(
            player_a=player_a,
            player_b=player_b,
            surface=surface,
            best_of=best_of,
        )
        self._points_seen = 0
        self._last_point_processed: dict | None = None
        self._log = log_fn
        self._logger = logger

    @classmethod
    def list_matches(cls, api_key: str | None = None) -> list[dict]:
        return TennisFeed(api_key=api_key).get_live_matches()

    def run(self, max_duration_seconds: int | None = None) -> None:
        deadline = time.monotonic() + max_duration_seconds if max_duration_seconds else None
        self._log(f"Tracking match {self._match_id}. Polling every {_POLL_INTERVAL}s.")
        self._log(_HEADER)
        self._log("-" * sum(_COL))

        while True:
            if deadline and time.monotonic() >= deadline:
                self._log("Max duration reached. Stopping.")
                break
            try:
                self._poll()
                if self._engine._match_over:
                    break
            except KeyboardInterrupt:
                self._log("Stopped.")
                break
            except Exception as exc:
                self._log(f"[poll error] {exc}")
            remaining = (deadline - time.monotonic()) if deadline else _POLL_INTERVAL
            if remaining <= 0:
                self._log("Max duration reached. Stopping.")
                break
            time.sleep(min(_POLL_INTERVAL, remaining))

    def _poll(self) -> None:
        raw = self._feed.get_point_by_point(self._match_id)
        all_points = self._feed.translate_to_engine_format(raw)

        if self._points_seen > 0:
            is_shrinkage = len(all_points) < self._points_seen
            is_mutation = (
                not is_shrinkage
                and self._last_point_processed is not None
                and all_points[self._points_seen - 1] != self._last_point_processed
            )
            if is_shrinkage or is_mutation:
                self._log(
                    f"[fingerprint] API rollback/mutation detected — "
                    f"resetting engine and replaying all {len(all_points)} points."
                )
                self._reset_engine()

        new_points = all_points[self._points_seen:]

        for pt in new_points:
            if self._engine._match_over:
                self._log("Match complete.")
                return

            if self._logger is not None:
                try:
                    self._logger.log_raw_point(
                        match_id=self._match_id,
                        player_a=self._player_a_name,
                        player_b=self._player_b_name,
                        point_dict=pt,
                        point_num=self._points_seen,
                    )
                except Exception as exc:
                    self._log(f"[log_raw_point error] {exc}")

            winner_engine  = "A" if pt["point_winner"] == "home" else "B"
            serving_engine = "A" if pt["server"]       == "home" else "B"

            result = self._engine.process_point({
                "winner": winner_engine,
                "serving": serving_engine,
            })

            self._print_row(result, None)
            if self._logger is not None:
                try:
                    self._logger.log_processed_state(
                        match_id=self._match_id,
                        player_a=self._player_a_name,
                        player_b=self._player_b_name,
                        point_dict=pt,
                        prob_output=result,
                        last_odds=None,
                        point_num=self._points_seen,
                    )
                except Exception as exc:
                    self._log(f"[log_processed_state error] {exc}")
            self._last_point_processed = pt
            self._points_seen += 1

    def _reset_engine(self) -> None:
        """Rebuild the engine and rewind so the next poll replays all points."""
        self._engine = LiveMatch(
            player_a=self._player_a_dict,
            player_b=self._player_b_dict,
            surface=self._surface,
            best_of=self._best_of,
        )
        self._points_seen = 0
        self._last_point_processed = None

    def _print_row(self, result: dict, odds: dict | None) -> None:
        ms = result["match_state"]
        model_p = result["probabilities"].get("P_match_A", float("nan"))
        score_str = (
            f"{ms['sets_A']}-{ms['sets_B']} "
            f"{ms['games_A']}-{ms['games_B']} "
            f"{ms['points_A']}-{ms['points_B']}"
        )

        if odds:
            bookie_p = odds["home_implied_prob"]
            edge = model_p - bookie_p
            bookie_str = f"{bookie_p:.3f}"
            edge_str = f"{edge:+.3f}"
        else:
            bookie_str = "N/A"
            edge_str = "N/A"

        self._log(
            f"{score_str:<{_COL[0]}}"
            f"{model_p:<{_COL[1]}.3f}"
            f"{bookie_str:<{_COL[2]}}"
            f"{edge_str:<{_COL[3]}}"
        )
