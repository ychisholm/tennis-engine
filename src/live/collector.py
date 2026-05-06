from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from src.live.logger import MatchLogger
from src.live.tennis_feed import TennisFeed

_log = logging.getLogger(__name__)

# Module-level lock guards all reads and writes to ACTIVE_MATCH_IDS.
_ACTIVE_IDS_LOCK = threading.Lock()
ACTIVE_MATCH_IDS: set[int] = set()

# Maps match_id → (country_a_alpha2, country_b_alpha2) for the live-match detail merge.
COUNTRY_MAP: dict[int, tuple[str | None, str | None]] = {}


class MatchWorker:
    """
    Polls one live match in a daemon thread, fetching match-detail snapshots
    and writing them to live_raw.match_details + live_processed.match_detail_points.
    Stops when the API reports a winner_code.
    """

    def __init__(
        self,
        event: dict,
        rapidapi_key: str,
        poll_interval: int = 15,
        poll_logger=None,
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
        self._poll_logger   = poll_logger
        # Counts score-state changes observed since this worker started polling,
        # NOT total points played in the match. Used as the points_count value
        # logged with POINTS_RECEIVED audit events.
        self._cumulative_points: int = 0
        self._last_score_state: tuple | None = None

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
        polled_at = datetime.now(timezone.utc)
        try:
            raw_detail = self._feed.get_match_detail(self._match_id)
            parsed_detail = self._feed.parse_match_detail(
                raw_detail,
                match_id=self._match_id,
                player_a=self._player_a,
                player_b=self._player_b,
                tournament_name=self._tournament_name,
                category=self._category,
            )
            self._logger.log_match_detail(parsed_detail, polled_at=polled_at)
        except Exception as exc:
            _log.warning(
                "match_detail fetch/log error for %s vs %s: %s",
                self._player_a, self._player_b, exc,
            )
            poll_logger = getattr(self, "_poll_logger", None)
            if poll_logger is not None:
                poll_logger.log(
                    event_type="POLL_ERROR",
                    match_id=self._match_id,
                    detail=str(exc)[:200],
                )
            parsed_detail = None

        if parsed_detail:
            score_state = (
                parsed_detail.get("home_sets"),
                parsed_detail.get("away_sets"),
                parsed_detail.get("home_period1"),
                parsed_detail.get("away_period1"),
                parsed_detail.get("home_period2"),
                parsed_detail.get("away_period2"),
                parsed_detail.get("home_period3"),
                parsed_detail.get("away_period3"),
                parsed_detail.get("home_current_point"),
                parsed_detail.get("away_current_point"),
            )
            poll_logger = getattr(self, "_poll_logger", None)
            if poll_logger is not None:
                if score_state != getattr(self, "_last_score_state", None):
                    self._cumulative_points = getattr(self, "_cumulative_points", 0) + 1
                    poll_logger.log(
                        event_type="POINTS_RECEIVED",
                        match_id=self._match_id,
                        points_count=self._cumulative_points,
                    )
                else:
                    poll_logger.log(
                        event_type="NO_NEW_POINTS",
                        match_id=self._match_id,
                    )
            self._last_score_state = score_state

            parsed_detail["country_a"] = getattr(self, "_country_a", None)
            parsed_detail["country_b"] = getattr(self, "_country_b", None)
            try:
                self._logger.upsert_match_detail_points(parsed_detail, polled_at)
            except Exception as exc:
                _log.warning(
                    "upsert_match_detail_points error for %s vs %s (match %s): %s",
                    self._player_a, self._player_b, self._match_id, exc,
                )
                poll_logger = getattr(self, "_poll_logger", None)
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="POLL_ERROR",
                        match_id=self._match_id,
                        detail=str(exc)[:200],
                    )

            if parsed_detail.get("winner_code"):
                _log.info(
                    "%s vs %s (match %s) — complete (winner_code=%s), stopping worker.",
                    self._player_a, self._player_b, self._match_id,
                    parsed_detail["winner_code"],
                )
                with _ACTIVE_IDS_LOCK:
                    ACTIVE_MATCH_IDS.discard(self._match_id)
                self._running = False
                return

        _log.debug(
            "%s vs %s — match detail polled",
            self._player_a, self._player_b,
        )

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
        discovery_interval: int = 60,
        worker_poll_interval: int = 15,
        poll_logger=None,
    ) -> None:
        self._rapidapi_key        = rapidapi_key
        self._discovery_interval  = discovery_interval
        self._worker_poll_interval = worker_poll_interval
        self._poll_logger         = poll_logger
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
        poll_logger = getattr(self, "_poll_logger", None)
        try:
            raw_events = self._feed.get_live_matches_raw()
        except Exception as exc:
            _log.warning("collector failed to fetch live matches: %s", exc)
            if poll_logger is not None:
                poll_logger.log(
                    event_type="POLL_ERROR",
                    detail=str(exc)[:200],
                )
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
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="MATCH_DISCOVERED",
                        match_id=match_id,
                    )
                worker = MatchWorker(
                    event=event,
                    rapidapi_key=self._rapidapi_key,
                    poll_interval=self._worker_poll_interval,
                    poll_logger=poll_logger,
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
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="MATCH_ENDED",
                        match_id=mid,
                    )

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
