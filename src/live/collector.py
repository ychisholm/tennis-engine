from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

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
    and writing them to live.match_polls + live.match_states.
    Stops when the API reports a winner_code.
    """

    def __init__(
        self,
        event: dict,
        rapidapi_key: str,
        poll_interval: int = 10,
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
        self._spawned_at = time.time()
        self._max_runtime_seconds = 6 * 3600
        self._terminal_non_finished = {"canceled", "postponed", "walkover"}
        # Counts score-state changes observed since this worker started polling,
        # NOT total points played in the match. Used as the points_count value
        # logged with POINTS_RECEIVED audit events.
        self._cumulative_points: int = 0
        self._last_score_state: tuple | None = None
        # Once learned from /point-by-point this is cached for the worker's
        # lifetime and persisted on every match_states row.
        self._first_server: Optional[str] = None
        self._first_server_attempted_count: int = 0
        # Cap how many times we'll poll point-by-point before giving up. 40
        # polls × 10s ≈ 6.5 minutes of attempts — well past when any real match
        # produces its first point.
        self._first_server_max_attempts: int = 40

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
        cycle_id = uuid.uuid4()
        polled_at = datetime.now(timezone.utc)
        try:
            raw_detail = self._feed.get_match_detail(
                self._match_id, poll_cycle_id=cycle_id,
            )
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
                    poll_cycle_id=cycle_id,
                )
            parsed_detail = None

        if parsed_detail:
            status = parsed_detail.get("status")
            if status in self._terminal_non_finished:
                _log.warning(
                    "%s vs %s (match %s) — terminal non-finished status %s; stopping worker.",
                    self._player_a, self._player_b, self._match_id, status,
                )
                with _ACTIVE_IDS_LOCK:
                    ACTIVE_MATCH_IDS.discard(self._match_id)
                self._running = False
                return

            if (time.time() - self._spawned_at) > self._max_runtime_seconds:
                _log.warning(
                    "%s vs %s (match %s) — exceeded max runtime (%ds); stopping worker.",
                    self._player_a, self._player_b, self._match_id,
                    self._max_runtime_seconds,
                )
                with _ACTIVE_IDS_LOCK:
                    ACTIVE_MATCH_IDS.discard(self._match_id)
                self._running = False
                return

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
            if status == "inprogress":
                if poll_logger is not None:
                    if score_state != getattr(self, "_last_score_state", None):
                        self._cumulative_points = getattr(self, "_cumulative_points", 0) + 1
                        poll_logger.log(
                            event_type="POINTS_RECEIVED",
                            match_id=self._match_id,
                            points_count=self._cumulative_points,
                            poll_cycle_id=cycle_id,
                        )
                    else:
                        poll_logger.log(
                            event_type="NO_NEW_POINTS",
                            match_id=self._match_id,
                            poll_cycle_id=cycle_id,
                        )
                self._last_score_state = score_state

            parsed_detail["country_a"] = getattr(self, "_country_a", None)
            parsed_detail["country_b"] = getattr(self, "_country_b", None)

            if (
                self._first_server is None
                and self._first_server_attempted_count < self._first_server_max_attempts
            ):
                try:
                    self._first_server_attempted_count += 1
                    result = self._feed.get_first_server(
                        self._match_id, poll_cycle_id=cycle_id,
                    )
                    if result is not None:
                        self._first_server = result
                        self._logger.backfill_first_server(self._match_id, result)
                except Exception:
                    # Already audit-logged via TennisFeed; continue with poll.
                    pass

            try:
                self._logger.upsert_match_detail_points(
                    parsed_detail, polled_at, first_server=self._first_server,
                )
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
                        poll_cycle_id=cycle_id,
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
        worker_poll_interval: int = 10,
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
        cycle_id = uuid.uuid4()
        poll_logger = getattr(self, "_poll_logger", None)
        try:
            raw_events = self._feed.get_live_matches_raw(poll_cycle_id=cycle_id)
        except Exception as exc:
            _log.warning("collector failed to fetch live matches: %s", exc)
            if poll_logger is not None:
                poll_logger.log(
                    event_type="POLL_ERROR",
                    detail=str(exc)[:200],
                    poll_cycle_id=cycle_id,
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
                        poll_cycle_id=cycle_id,
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
            finished = [
                mid for mid, w in self._active.items() if not w._running
            ]
            for mid in finished:
                self._active[mid].stop()
                del self._active[mid]
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="MATCH_ENDED",
                        match_id=mid,
                        poll_cycle_id=cycle_id,
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

    def spawn_pre_match_worker(self, raw_event: dict) -> None:
        """Idempotently spin up a MatchWorker for a not-yet-live match so that
        match_detail polling is already running when the API flips status to
        inprogress. Safe to call repeatedly for the same match — if a worker
        already exists for this match_id, this is a no-op."""
        match_id = raw_event.get("id")
        if match_id is None:
            return

        with self._active_lock:
            if match_id in self._active:
                return

        poll_logger = getattr(self, "_poll_logger", None)
        worker = MatchWorker(
            event=raw_event,
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
            # Re-check under lock to prevent two pre-spawns from racing the
            # _active check above.
            if match_id in self._active:
                worker.stop()
                return
            self._active[match_id] = worker
        _log.info("pre-spawn worker for match %s", match_id)
        if poll_logger is not None:
            poll_logger.log(
                event_type="MATCH_DISCOVERED",
                match_id=match_id,
                detail="pre_spawn",
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
