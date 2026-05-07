"""
State-machine scheduler driving the live data pipeline.

Four states:
  IDLE       — no live matches, none starting within 15 min. Schedule polled
               once/hour, no odds polling.
  PRE_MATCH  — at least one match starts within 15 min. Still no live polling
               or odds polling; system is armed.
  LIVE       — at least one qualifying match is in progress. MatchCollector
               runs its 60s discovery loop; each MatchWorker polls its match
               every 15s and event-triggers odds fetches (rate-gated).
  (completion of single matches is handled inline by MatchCollector; the
   system returns to IDLE only when no matches are live or upcoming.)
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

_log = logging.getLogger(__name__)

_SCHEDULE_CHECK_INTERVAL = 3600  # legacy constant; retained for compatibility
_SCHEDULE_CHECK_IDLE     = 3600  # seconds between schedule polls in IDLE
_SCHEDULE_CHECK_ACTIVE   = 60    # seconds between schedule polls in PRE_MATCH/LIVE
_PRE_MATCH_WINDOW        = 900   # seconds ahead within which PRE_MATCH arms
_PRE_SPAWN_WINDOW        = 300   # seconds ahead within which a worker is pre-spawned


class MatchScheduler:
    def __init__(self, feed, collector, logger, poll_logger=None) -> None:
        self._feed = feed
        self._collector = collector
        self._logger = logger
        self._poll_logger = poll_logger
        self._state: str = "IDLE"
        self._upcoming_matches: list[dict] = []
        self._upcoming_raw_by_id: dict[int, dict] = {}
        self._scheduler = BackgroundScheduler()

    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        self._scheduler.add_job(
            self._check_schedule,
            "interval",
            seconds=_SCHEDULE_CHECK_IDLE,
            id="check_schedule",
            replace_existing=True,
        )
        self._scheduler.start()
        self._check_schedule()

    def stop(self) -> None:
        try:
            self._scheduler.shutdown(wait=False)
        except Exception as exc:
            _log.warning("scheduler shutdown error: %s", exc)

    def _reschedule_job(self, interval_seconds: int) -> None:
        try:
            self._scheduler.reschedule_job(
                "check_schedule",
                trigger="interval",
                seconds=interval_seconds,
            )
        except Exception as exc:
            _log.warning(
                "reschedule_job failed (interval=%ds): %s",
                interval_seconds, exc,
            )

    def _check_schedule(self) -> None:
        """Decide current state by inspecting live and upcoming matches."""
        cycle_id = uuid.uuid4()
        poll_logger = getattr(self, "_poll_logger", None)
        if poll_logger is not None:
            poll_logger.log(
                event_type="TICK_START",
                detail=self._state,
                poll_cycle_id=cycle_id,
            )
        try:
            raw_upcoming = self._feed.get_upcoming_matches_raw(
                poll_cycle_id=cycle_id,
            )
            if not isinstance(raw_upcoming, list):
                raw_upcoming = []
            self._upcoming_matches = [_summarize_upcoming(e) for e in raw_upcoming]
            self._upcoming_raw_by_id = {int(e["id"]): e for e in raw_upcoming}
        except Exception as exc:
            _log.warning("get_upcoming_matches_raw failed: %s", exc)
            self._upcoming_matches = []
            self._upcoming_raw_by_id = {}

        try:
            live_events = self._feed.get_live_matches_raw(poll_cycle_id=cycle_id)
        except Exception as exc:
            _log.warning("get_live_matches_raw failed: %s", exc)
            live_events = []

        live_qualifying = [e for e in live_events if _is_live_qualifying(e)]

        if live_qualifying:
            if self._state != "LIVE":
                self._enter_live(cycle_id=cycle_id)
            self._pre_spawn_imminent(now=time.time())
            return

        now = time.time()
        imminent = [
            m for m in self._upcoming_matches
            if 0 <= (m["scheduled_start_unix"] - now) <= _PRE_MATCH_WINDOW
        ]

        if imminent:
            if self._state != "PRE_MATCH":
                self._enter_pre_match(cycle_id=cycle_id)
            self._pre_spawn_imminent(now=now)
            for m in imminent:
                eta = int(m["scheduled_start_unix"] - now)
                _log.info(
                    "PRE_MATCH upcoming: %s vs %s (%s) in %dm %ds",
                    m["player_a"], m["player_b"], m.get("tournament", ""),
                    eta // 60, eta % 60,
                )
            return

        if self._state != "IDLE":
            self._enter_idle(cycle_id=cycle_id)

        future = [
            m for m in self._upcoming_matches
            if m["scheduled_start_unix"] > now
        ]
        if future:
            soonest = min(future, key=lambda m: m["scheduled_start_unix"])
            secs = int(soonest["scheduled_start_unix"] - now)
            hours, rem = divmod(secs, 3600)
            mins = rem // 60
            _log.info(
                "IDLE — next match: %s vs %s in %dh %dm",
                soonest["player_a"], soonest["player_b"], hours, mins,
            )
        else:
            _log.info("IDLE — no upcoming matches in schedule window")

    def _pre_spawn_imminent(self, *, now: float) -> None:
        """For each upcoming match within _PRE_SPAWN_WINDOW seconds, ask the
        collector to spin up a worker now. Idempotent — collector dedupes by
        match_id, so repeated calls are safe."""
        to_pre_spawn = [
            m for m in self._upcoming_matches
            if 0 <= (m["scheduled_start_unix"] - now) <= _PRE_SPAWN_WINDOW
        ]
        for m in to_pre_spawn:
            raw = self._upcoming_raw_by_id.get(int(m["match_id"]))
            if raw is None:
                continue
            try:
                self._collector.spawn_pre_match_worker(raw)
            except Exception as exc:
                _log.warning(
                    "pre-spawn failed for match %s: %s", m["match_id"], exc,
                )

    def _enter_idle(self, *, cycle_id: "uuid.UUID | None" = None) -> None:
        prev = self._state
        _log.info("Entering IDLE state")
        self._state = "IDLE"
        self._collector.stop()
        self._reschedule_job(_SCHEDULE_CHECK_IDLE)
        poll_logger = getattr(self, "_poll_logger", None)
        if poll_logger is not None:
            poll_logger.log(
                event_type="STATE_TRANSITION",
                detail=f"{prev}->IDLE",
                poll_cycle_id=cycle_id,
            )

    def _enter_pre_match(self, *, cycle_id: "uuid.UUID | None" = None) -> None:
        prev = self._state
        _log.info("Entering PRE_MATCH state — armed for upcoming match")
        self._state = "PRE_MATCH"
        self._reschedule_job(_SCHEDULE_CHECK_ACTIVE)
        poll_logger = getattr(self, "_poll_logger", None)
        if poll_logger is not None:
            poll_logger.log(
                event_type="STATE_TRANSITION",
                detail=f"{prev}->PRE_MATCH",
                poll_cycle_id=cycle_id,
            )

    def _enter_live(self, *, cycle_id: "uuid.UUID | None" = None) -> None:
        prev = self._state
        _log.info("Entering LIVE state — starting live polling")
        self._state = "LIVE"
        self._collector.start()
        self._reschedule_job(_SCHEDULE_CHECK_ACTIVE)
        poll_logger = getattr(self, "_poll_logger", None)
        if poll_logger is not None:
            poll_logger.log(
                event_type="STATE_TRANSITION",
                detail=f"{prev}->LIVE",
                poll_cycle_id=cycle_id,
            )


def _summarize_upcoming(event: dict) -> dict:
    """Reduce a raw events_by_date entry to the parsed-summary shape that
    scheduler logging and `imminent` filtering expect."""
    home = (event.get("homeTeam") or {}).get("name", "Unknown")
    away = (event.get("awayTeam") or {}).get("name", "Unknown")
    tournament = (event.get("tournament") or {}).get("name", "Unknown")
    return {
        "match_id": int(event["id"]),
        "player_a": home,
        "player_b": away,
        "tournament": tournament,
        "scheduled_start_unix": int(event["startTimestamp"]),
    }


def _is_live_qualifying(event: dict) -> bool:
    """ATP/WTA singles, status inprogress. Mirrors MatchCollector._is_qualifying."""
    try:
        category_slug = event["tournament"]["category"]["slug"]
    except (KeyError, TypeError):
        return False
    if category_slug not in ("atp", "wta"):
        return False
    try:
        ef_category = event["eventFilters"]["category"]
    except (KeyError, TypeError):
        ef_category = ""
    if "doubles" in str(ef_category).lower():
        return False
    try:
        status_type = event["status"]["type"]
    except (KeyError, TypeError):
        return False
    return status_type == "inprogress"
