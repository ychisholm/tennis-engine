from __future__ import annotations

import logging
import threading
import time
from typing import Any

from src.live.logger import MatchLogger
from src.live.odds_fetcher import get_match_odds
from src.live.tennis_feed import TennisFeed
from src.engine.live_match import LiveMatch

_log = logging.getLogger(__name__)

_DEFAULT_PLAYER: dict = {
    "p0_hard": 0.63,
    "p0_clay": 0.60,
    "p0_grass": 0.63,
    "archetype": {"sd": 60, "ba": 60, "pe": 60, "tv": 60},
}

_ODDS_MIN_INTERVAL = 300.0  # seconds between successive odds fetches per match

# Module-level lock guards all reads and writes to ACTIVE_MATCH_IDS.
_ACTIVE_IDS_LOCK = threading.Lock()
ACTIVE_MATCH_IDS: set[int] = set()

# Maps match_id → (country_a_alpha2, country_b_alpha2) for the live-match detail merge.
COUNTRY_MAP: dict[int, tuple[str | None, str | None]] = {}


class MatchWorker:
    """
    Polls one live match in a daemon thread: feeds points through the
    LiveMatch engine, logs to DuckDB, and triggers a rate-gated odds
    fetch whenever new points are seen.
    """

    def __init__(
        self,
        event: dict,
        rapidapi_key: str,
        poll_interval: int = 15,
        get_odds_fn=get_match_odds,
    ) -> None:
        self._match_id        = event["id"]
        self._player_a        = event["homeTeam"]["name"]
        self._player_b        = event["awayTeam"]["name"]
        self._country_a       = ((event.get("homeTeam") or {}).get("country") or {}).get("alpha2")
        self._country_b       = ((event.get("awayTeam") or {}).get("country") or {}).get("alpha2")
        COUNTRY_MAP[self._match_id] = (self._country_a, self._country_b)
        self._tournament_id   = (
            (event.get("tournament") or {})
            .get("uniqueTournament", {})
            .get("id")
        )
        self._tournament_name = (event.get("tournament") or {}).get("name", "Unknown")
        self._category        = (
            (event.get("tournament") or {})
            .get("category", {})
            .get("slug", "unknown")
        )
        self._poll_interval = poll_interval
        self._running       = True
        self._points_seen   = 0
        self._last_point_processed: dict | None = None
        self._last_odds_fetch_time: float = 0.0
        self._get_odds_fn = get_odds_fn

        self._match_metadata = {
            "homeTeam":   {"name": self._player_a},
            "awayTeam":   {"name": self._player_b},
            "tournament": {"uniqueTournament": {"id": self._tournament_id}},
        }

        player_a_dict = {**_DEFAULT_PLAYER, "name": self._player_a}
        player_b_dict = {**_DEFAULT_PLAYER, "name": self._player_b}

        self._engine = LiveMatch(
            player_a=player_a_dict,
            player_b=player_b_dict,
            surface="hard",
            best_of=3,
        )
        self._feed   = TennisFeed(api_key=rapidapi_key)
        self._logger = MatchLogger()
        with _ACTIVE_IDS_LOCK:
            ACTIVE_MATCH_IDS.add(self._match_id)

    def run(self) -> None:
        _log.info(
            "worker START: %s vs %s (%s, %s)",
            self._player_a, self._player_b, self._tournament_name, self._match_id,
        )
        try:
            while self._running:
                try:
                    self._poll()
                except Exception as exc:
                    _log.warning(
                        "worker error %s vs %s: %s",
                        self._player_a, self._player_b, exc,
                    )
                if self._running:
                    time.sleep(self._poll_interval)
        finally:
            with _ACTIVE_IDS_LOCK:
                ACTIVE_MATCH_IDS.discard(self._match_id)
            self._logger.close()
            _log.info(
                "worker STOP: %s vs %s (%s) — match finished",
                self._player_a, self._player_b, self._match_id,
            )

    def _poll(self) -> None:
        raw        = self._feed.get_point_by_point(self._match_id)
        all_points = self._feed.translate_to_engine_format(raw)

        if self._points_seen > 0:
            is_shrinkage = len(all_points) < self._points_seen
            is_mutation = (
                not is_shrinkage
                and self._last_point_processed is not None
                and all_points[self._points_seen - 1] != self._last_point_processed
            )
            if is_shrinkage or is_mutation:
                _log.warning(
                    "API rollback/mutation detected for %s vs %s (match %s) — "
                    "resetting engine and replaying all %d points.",
                    self._player_a, self._player_b, self._match_id, len(all_points),
                )
                self._reset_engine()

        if len(all_points) == self._points_seen:
            return  # no new data this cycle

        new_points = all_points[self._points_seen:]
        new_points_found = False

        for pt in new_points:
            if self._engine._match_over:
                if not self._api_confirms_finished():
                    _log.warning(
                        "engine declared %s vs %s finished but API shows match still live. Resetting engine.",
                        self._player_a, self._player_b,
                    )
                    self._reset_engine()
                    break
                _log.info(
                    "%s vs %s — match complete, stopping.",
                    self._player_a, self._player_b,
                )
                with _ACTIVE_IDS_LOCK:
                    ACTIVE_MATCH_IDS.discard(self._match_id)
                self._running = False
                return

            try:
                self._logger.log_raw_point(
                    match_id=self._match_id,
                    player_a=self._player_a,
                    player_b=self._player_b,
                    point_dict=pt,
                    point_num=self._points_seen,
                    tournament_name=self._tournament_name,
                    category=self._category,
                )
            except Exception as exc:
                _log.warning("log_raw_point error: %s", exc)

            winner_engine  = "A" if pt["point_winner"] == "home" else "B"
            serving_engine = "A" if pt["server"]       == "home" else "B"

            result = self._engine.process_point({
                "winner": winner_engine,
                "serving": serving_engine,
            })

            try:
                self._logger.log_processed_state(
                    match_id=self._match_id,
                    player_a=self._player_a,
                    player_b=self._player_b,
                    point_dict=pt,
                    prob_output=result,
                    last_odds=None,
                    point_num=self._points_seen,
                    tournament_name=self._tournament_name,
                    category=self._category,
                )
            except Exception as exc:
                _log.warning("log_processed_state error: %s", exc)

            self._last_point_processed = pt
            self._points_seen += 1
            new_points_found = True

        _log.debug(
            "%s vs %s — %d new points processed",
            self._player_a, self._player_b, len(new_points),
        )

        if new_points_found:
            self._maybe_fetch_odds()

    def _maybe_fetch_odds(self) -> None:
        now = time.time()
        elapsed = now - self._last_odds_fetch_time
        if elapsed < _ODDS_MIN_INTERVAL:
            _log.debug(
                "odds skip for %s vs %s: only %.1fs since last fetch",
                self._player_a, self._player_b, elapsed,
            )
            return
        try:
            odds = self._get_odds_fn(self._match_metadata)
        except Exception as exc:
            _log.warning(
                "odds fetch error for %s vs %s: %s",
                self._player_a, self._player_b, exc,
            )
            return
        if odds is None:
            _log.debug(
                "odds fetch returned None for %s vs %s",
                self._player_a, self._player_b,
            )
            return
        try:
            self._logger.log_raw_odds(
                match_id=self._match_id,
                player_a=self._player_a,
                player_b=self._player_b,
                odds_result=odds,
            )
        except Exception as exc:
            _log.warning("log_raw_odds error: %s", exc)
            return
        self._last_odds_fetch_time = now
        _log.debug(
            "odds logged for %s vs %s (implied=%.3f)",
            self._player_a, self._player_b,
            odds.get("bookmaker_implied_prob", float("nan")),
        )

    def _api_confirms_finished(self) -> bool:
        """Return True only when this match_id is gone from the live feed."""
        try:
            live_events = self._feed.get_live_matches_raw()
            return not any(e.get("id") == self._match_id for e in live_events)
        except Exception:
            return False  # can't verify → assume still live

    def _reset_engine(self) -> None:
        """Rebuild the engine and rewind points_seen so next poll replays all points."""
        player_a_dict = {**_DEFAULT_PLAYER, "name": self._player_a}
        player_b_dict = {**_DEFAULT_PLAYER, "name": self._player_b}
        self._engine = LiveMatch(
            player_a=player_a_dict,
            player_b=player_b_dict,
            surface="hard",
            best_of=3,
        )
        self._points_seen = 0
        self._last_point_processed = None

    def stop(self) -> None:
        self._running = False


class MatchCollector:
    """
    Discovers all qualifying live ATP/WTA singles matches every
    ``discovery_interval`` seconds and manages a MatchWorker daemon thread
    for each one. Controlled by an external scheduler via start()/stop().
    """

    def __init__(
        self,
        rapidapi_key: str,
        odds_api_key: str,
        discovery_interval: int = 60,
        worker_poll_interval: int = 15,
    ) -> None:
        self._rapidapi_key        = rapidapi_key
        self._odds_api_key        = odds_api_key
        self._discovery_interval  = discovery_interval
        self._worker_poll_interval = worker_poll_interval
        self._active_lock   = threading.Lock()
        self._active: dict[Any, MatchWorker] = {}
        self._feed = TennisFeed(api_key=rapidapi_key)
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="collector-loop",
        )
        self._thread.start()
        _log.info("MatchCollector started")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        with self._active_lock:
            workers = list(self._active.values())
            self._active.clear()
        for w in workers:
            w.stop()
        _log.info("MatchCollector stopped (%d workers signalled)", len(workers))

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._cycle()
            except Exception as exc:
                _log.warning("collector cycle error: %s", exc)
            # Interruptible sleep so stop() takes effect promptly.
            for _ in range(self._discovery_interval):
                if not self._running:
                    return
                time.sleep(1)

    def _cycle(self) -> None:
        try:
            raw_events = self._feed.get_live_matches_raw()
        except Exception as exc:
            _log.warning("collector failed to fetch live matches: %s", exc)
            return

        live_ids: set = set()
        for event in raw_events:
            if not self._is_qualifying(event):
                continue
            match_id = event.get("id")
            live_ids.add(match_id)

            with self._active_lock:
                already_active = match_id in self._active

            if not already_active:
                worker = MatchWorker(
                    event=event,
                    rapidapi_key=self._rapidapi_key,
                    poll_interval=self._worker_poll_interval,
                )
                thread = threading.Thread(
                    target=worker.run,
                    daemon=True,
                    name=f"worker-{match_id}",
                )
                thread.start()
                with self._active_lock:
                    self._active[match_id] = worker

        with self._active_lock:
            finished = [mid for mid in self._active if mid not in live_ids]
            for mid in finished:
                self._active[mid].stop()
                del self._active[mid]

        with self._active_lock:
            match_labels = ", ".join(
                f"{w._player_a} vs {w._player_b}" for w in self._active.values()
            )
            active_count = len(self._active)

        _log.info(
            "collector cycle complete | active: %d matches | monitoring: %s",
            active_count, match_labels or "none",
        )

    @staticmethod
    def _is_qualifying(event: dict) -> bool:
        # Rule 1: ATP or WTA only — excludes Challenger, ITF, exhibition
        try:
            category_slug = event["tournament"]["category"]["slug"]
        except (KeyError, TypeError):
            return False
        if category_slug not in ("atp", "wta"):
            return False

        # Rule 2: singles only — reject if "doubles" appears in eventFilters.category
        try:
            ef_category = event["eventFilters"]["category"]
        except (KeyError, TypeError):
            ef_category = ""
        if "doubles" in str(ef_category).lower():
            return False

        # Rule 3: match must currently be live
        try:
            status_type = event["status"]["type"]
        except (KeyError, TypeError):
            return False
        if status_type != "inprogress":
            return False

        return True
