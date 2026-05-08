"""
Tests for the aggressive scheduler + pre-spawn worker pipeline.

All TennisFeed and MatchCollector behaviour is mocked or constructed
without HTTP/DB so the suite runs without DATABASE_URL or RAPIDAPI_KEY.
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

# Ensure RAPIDAPI_KEY is set for any TennisFeed instantiation that imports
# the env at construction; tests that hit the network mock _get directly.
os.environ.setdefault("RAPIDAPI_KEY", "test-key")

from src.live.scheduler import (
    MatchScheduler,
    _SCHEDULE_CHECK_ACTIVE,
    _SCHEDULE_CHECK_IDLE,
)


# ---------------------------------------------------------------------------
# Helpers — raw events shaped like the upstream API
# ---------------------------------------------------------------------------

def _raw_upcoming(
    match_id: int = 1,
    seconds_from_now: int = 120,
    category_slug: str = "atp",
    home: str = "Alcaraz",
    away: str = "Sinner",
) -> dict:
    return {
        "id": match_id,
        "startTimestamp": int(time.time()) + seconds_from_now,
        "homeTeam": {"name": home, "country": {"alpha2": "ES"}},
        "awayTeam": {"name": away, "country": {"alpha2": "IT"}},
        "tournament": {
            "name": "Test Open",
            "category": {"slug": category_slug},
            "uniqueTournament": {"id": 1},
        },
        "eventFilters": {"category": "singles"},
    }


def _raw_live(match_id: int = 1) -> dict:
    return {
        "id": match_id,
        "homeTeam": {"name": "Alcaraz"},
        "awayTeam": {"name": "Sinner"},
        "tournament": {
            "name": "Test Open",
            "category": {"slug": "atp"},
            "uniqueTournament": {"id": 1},
        },
        "eventFilters": {"category": "singles"},
        "status": {"type": "inprogress"},
    }


# ---------------------------------------------------------------------------
# TennisFeed.get_upcoming_matches_raw filters correctly
# ---------------------------------------------------------------------------

def test_get_upcoming_matches_raw_filters_to_atp_wta_singles_with_start_ts():
    from src.live.tennis_feed import TennisFeed

    atp_match = _raw_upcoming(match_id=10, category_slug="atp")
    wta_match = _raw_upcoming(match_id=11, category_slug="wta")
    challenger = _raw_upcoming(match_id=12, category_slug="challenger")
    no_start = _raw_upcoming(match_id=13)
    no_start.pop("startTimestamp")
    doubles = _raw_upcoming(match_id=14)
    doubles["eventFilters"]["category"] = "Doubles"
    no_id = _raw_upcoming(match_id=15)
    no_id.pop("id")

    feed = TennisFeed(api_key="test")
    with patch.object(
        feed,
        "_get",
        return_value={"events": [atp_match, wta_match, challenger, no_start, doubles, no_id]},
    ):
        result = feed.get_upcoming_matches_raw(days_ahead=0)

    ids = [e["id"] for e in result]
    assert 10 in ids and 11 in ids
    assert 12 not in ids
    assert 14 not in ids
    assert all(e.get("startTimestamp") is not None for e in result)
    assert all(e.get("id") is not None for e in result)


# ---------------------------------------------------------------------------
# MatchCollector.spawn_pre_match_worker is idempotent
# ---------------------------------------------------------------------------

def test_spawn_pre_match_worker_is_idempotent():
    """Two calls for the same match_id must result in a single MatchWorker."""
    from src.live.collector import MatchCollector

    collector = MatchCollector.__new__(MatchCollector)
    import threading
    collector._rapidapi_key = "test"
    collector._discovery_interval = 60
    collector._worker_poll_interval = 15
    collector._poll_logger = MagicMock()
    collector._active_lock = threading.Lock()
    collector._active = {}
    collector._running = False
    collector._thread = None

    raw = _raw_upcoming(match_id=99)

    fake_worker_instances: list = []

    class FakeWorker:
        def __init__(self, event, rapidapi_key, poll_interval, poll_logger):
            self.event = event
            self._running = True
            fake_worker_instances.append(self)

        def run(self):
            pass

        def stop(self):
            self._running = False

    with patch("src.live.collector.MatchWorker", FakeWorker):
        collector.spawn_pre_match_worker(raw)
        collector.spawn_pre_match_worker(raw)
        collector.spawn_pre_match_worker(raw)

    assert len(collector._active) == 1
    assert 99 in collector._active
    # Second/third calls return early before constructing a worker.
    assert len(fake_worker_instances) == 1


def test_spawn_pre_match_worker_logs_match_discovered_with_pre_spawn_detail():
    from src.live.collector import MatchCollector
    import threading

    collector = MatchCollector.__new__(MatchCollector)
    collector._rapidapi_key = "test"
    collector._discovery_interval = 60
    collector._worker_poll_interval = 15
    collector._poll_logger = MagicMock()
    collector._active_lock = threading.Lock()
    collector._active = {}
    collector._running = False
    collector._thread = None

    raw = _raw_upcoming(match_id=77)

    class FakeWorker:
        def __init__(self, *a, **kw):
            self._running = True

        def run(self):
            pass

        def stop(self):
            self._running = False

    with patch("src.live.collector.MatchWorker", FakeWorker):
        collector.spawn_pre_match_worker(raw)

    calls = collector._poll_logger.log.call_args_list
    discovered = [c for c in calls if c.kwargs.get("event_type") == "MATCH_DISCOVERED"]
    assert len(discovered) == 1
    assert discovered[0].kwargs.get("detail") == "pre_spawn"
    assert discovered[0].kwargs.get("match_id") == 77


# ---------------------------------------------------------------------------
# MatchScheduler reschedules the job interval on state transitions
# ---------------------------------------------------------------------------

def _build_scheduler(upcoming_raw: list[dict] | None = None,
                    live_events: list[dict] | None = None) -> tuple[MatchScheduler, MagicMock]:
    feed = MagicMock()
    feed.get_upcoming_matches_raw.return_value = upcoming_raw or []
    feed.get_live_matches_raw.return_value = live_events or []
    collector = MagicMock()
    sched = MatchScheduler(feed=feed, collector=collector, logger=MagicMock())
    sched._scheduler = MagicMock()
    return sched, collector


def test_reschedule_called_with_active_interval_on_pre_match_entry():
    sched, _collector = _build_scheduler(
        upcoming_raw=[_raw_upcoming(match_id=1, seconds_from_now=600)],
    )
    sched._check_schedule()
    assert sched.state == "PRE_MATCH"
    sched._scheduler.reschedule_job.assert_any_call(
        "check_schedule", trigger="interval", seconds=_SCHEDULE_CHECK_ACTIVE,
    )


def test_reschedule_called_with_active_interval_on_live_entry():
    sched, collector = _build_scheduler(
        live_events=[_raw_live()],
    )
    sched._check_schedule()
    assert sched.state == "LIVE"
    collector.start.assert_called_once()
    sched._scheduler.reschedule_job.assert_any_call(
        "check_schedule", trigger="interval", seconds=_SCHEDULE_CHECK_ACTIVE,
    )


def test_reschedule_called_with_idle_interval_on_idle_entry():
    sched, _collector = _build_scheduler(live_events=[_raw_live()])
    sched._check_schedule()  # → LIVE
    assert sched.state == "LIVE"
    # Now nothing live and nothing imminent.
    sched._feed.get_live_matches_raw.return_value = []
    sched._feed.get_upcoming_matches_raw.return_value = []
    sched._scheduler.reschedule_job.reset_mock()
    sched._check_schedule()
    assert sched.state == "IDLE"
    sched._scheduler.reschedule_job.assert_any_call(
        "check_schedule", trigger="interval", seconds=_SCHEDULE_CHECK_IDLE,
    )


# ---------------------------------------------------------------------------
# Pre-spawn happens for matches within the 5-minute window, regardless of state
# ---------------------------------------------------------------------------

def test_pre_spawn_invokes_collector_for_match_within_5min():
    sched, collector = _build_scheduler(
        upcoming_raw=[_raw_upcoming(match_id=42, seconds_from_now=120)],
    )
    sched._check_schedule()
    collector.spawn_pre_match_worker.assert_called_once()
    spawned_event = collector.spawn_pre_match_worker.call_args.args[0]
    assert spawned_event["id"] == 42


def test_pre_spawn_skipped_for_match_beyond_5min_window():
    sched, collector = _build_scheduler(
        upcoming_raw=[_raw_upcoming(match_id=43, seconds_from_now=600)],  # 10min
    )
    sched._check_schedule()
    collector.spawn_pre_match_worker.assert_not_called()


def test_pre_spawn_runs_when_already_live():
    """If a match is live AND another match is within 5min, pre-spawn the latter."""
    sched, collector = _build_scheduler(
        upcoming_raw=[_raw_upcoming(match_id=44, seconds_from_now=180)],
        live_events=[_raw_live(match_id=99)],
    )
    sched._check_schedule()
    assert sched.state == "LIVE"
    collector.spawn_pre_match_worker.assert_called_once()
    assert collector.spawn_pre_match_worker.call_args.args[0]["id"] == 44


# ---------------------------------------------------------------------------
# MatchWorker terminal-status and runtime-cap stops
# ---------------------------------------------------------------------------

def _bare_worker(match_id: int = 1, status_terminal=("canceled", "postponed", "walkover")):
    """Construct a MatchWorker without invoking __init__ (avoids HTTP/DB)."""
    from src.live.collector import MatchWorker

    w = MatchWorker.__new__(MatchWorker)
    w._match_id = match_id
    w._player_a = "A"
    w._player_b = "B"
    w._country_a = None
    w._country_b = None
    w._tournament_name = "T"
    w._category = "atp"
    w._poll_interval = 15
    w._running = True
    w._poll_logger = MagicMock()
    w._spawned_at = time.time()
    w._max_runtime_seconds = 6 * 3600
    w._terminal_non_finished = set(status_terminal)
    w._cumulative_points = 0
    w._last_score_state = None
    w._first_server = None
    w._first_server_attempted_count = 0
    w._first_server_max_attempts = 40
    w._feed = MagicMock()
    w._logger = MagicMock()
    return w


def test_worker_stops_on_canceled_status():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=101)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "canceled"}}}
    w._feed.parse_match_detail.return_value = {
        "match_id": 101, "status": "canceled", "winner_code": None,
    }

    MatchWorker._poll(w)
    assert w._running is False


def test_worker_stops_on_postponed_status():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=102)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "postponed"}}}
    w._feed.parse_match_detail.return_value = {
        "match_id": 102, "status": "postponed", "winner_code": None,
    }

    MatchWorker._poll(w)
    assert w._running is False


def test_worker_stops_after_max_runtime_exceeded():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=103)
    # Pretend the worker spawned 7 hours ago.
    w._spawned_at = time.time() - (7 * 3600)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = {
        "match_id": 103, "status": "inprogress", "winner_code": None,
        "home_sets": 0, "away_sets": 0,
    }

    MatchWorker._poll(w)
    assert w._running is False


def test_worker_runs_under_max_runtime():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=104)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = {
        "match_id": 104, "status": "inprogress", "winner_code": None,
        "home_sets": 0, "away_sets": 0,
        "home_period1": 0, "away_period1": 0,
        "home_period2": None, "away_period2": None,
        "home_period3": None, "away_period3": None,
        "home_current_point": "0", "away_current_point": "0",
    }

    MatchWorker._poll(w)
    assert w._running is True


# ---------------------------------------------------------------------------
# Status-gated POINTS_RECEIVED logging — pre-match polls don't log points
# ---------------------------------------------------------------------------

def test_notstarted_status_does_not_log_points_received():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=105)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "notstarted"}}}
    w._feed.parse_match_detail.return_value = {
        "match_id": 105, "status": "notstarted", "winner_code": None,
        "home_sets": 0, "away_sets": 0,
        "home_period1": 0, "away_period1": 0,
        "home_period2": None, "away_period2": None,
        "home_period3": None, "away_period3": None,
        "home_current_point": "0", "away_current_point": "0",
    }

    MatchWorker._poll(w)

    event_types = [
        c.kwargs.get("event_type") for c in w._poll_logger.log.call_args_list
    ]
    assert "POINTS_RECEIVED" not in event_types
    assert "NO_NEW_POINTS" not in event_types


def test_inprogress_status_logs_points_received_on_state_change():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=106)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = {
        "match_id": 106, "status": "inprogress", "winner_code": None,
        "home_sets": 0, "away_sets": 0,
        "home_period1": 1, "away_period1": 0,
        "home_period2": None, "away_period2": None,
        "home_period3": None, "away_period3": None,
        "home_current_point": "15", "away_current_point": "0",
    }

    MatchWorker._poll(w)

    event_types = [
        c.kwargs.get("event_type") for c in w._poll_logger.log.call_args_list
    ]
    assert "POINTS_RECEIVED" in event_types


# ---------------------------------------------------------------------------
# first_server fetch / cache / backfill behaviour in MatchWorker._poll
# ---------------------------------------------------------------------------


def _inprogress_parsed(match_id: int) -> dict:
    return {
        "match_id": match_id, "status": "inprogress", "winner_code": None,
        "home_sets": 0, "away_sets": 0,
        "home_period1": 1, "away_period1": 0,
        "home_period2": None, "away_period2": None,
        "home_period3": None, "away_period3": None,
        "home_current_point": "15", "away_current_point": "0",
    }


def test_worker_fetches_first_server_and_passes_it_to_upsert():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=201)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = _inprogress_parsed(201)
    w._feed.get_first_server.return_value = "away"

    MatchWorker._poll(w)

    assert w._first_server == "away"
    assert w._first_server_attempted_count == 1
    w._feed.get_first_server.assert_called_once()
    # The cached value must be threaded into the upsert call.
    kwargs = w._logger.upsert_match_detail_points.call_args.kwargs
    assert kwargs.get("first_server") == "away"
    # Backfill triggered once first_server was learned.
    w._logger.backfill_first_server.assert_called_once_with(201, "away")


def test_worker_stops_attempting_after_first_success():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=202)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = _inprogress_parsed(202)
    w._feed.get_first_server.return_value = "home"

    MatchWorker._poll(w)
    MatchWorker._poll(w)
    MatchWorker._poll(w)

    # After the first successful fetch, no further point-by-point calls.
    assert w._feed.get_first_server.call_count == 1
    assert w._first_server == "home"


def test_worker_keeps_attempting_until_first_server_returned():
    """Early in a match get_first_server returns None; the worker must keep
    trying on subsequent polls."""
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=203)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = _inprogress_parsed(203)
    w._feed.get_first_server.side_effect = [None, None, "home"]

    MatchWorker._poll(w)
    MatchWorker._poll(w)
    MatchWorker._poll(w)
    MatchWorker._poll(w)  # this poll should NOT call get_first_server again

    assert w._feed.get_first_server.call_count == 3
    assert w._first_server == "home"
    assert w._first_server_attempted_count == 3


def test_worker_stops_attempting_after_cap_reached():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=204)
    w._first_server_max_attempts = 3
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = _inprogress_parsed(204)
    w._feed.get_first_server.return_value = None  # always indeterminate

    for _ in range(5):
        MatchWorker._poll(w)

    # Capped at 3 attempts even though we polled 5 times.
    assert w._feed.get_first_server.call_count == 3
    assert w._first_server is None
    assert w._first_server_attempted_count == 3


def test_worker_does_not_crash_if_get_first_server_throws():
    from src.live.collector import MatchWorker

    w = _bare_worker(match_id=205)
    w._feed.get_match_detail.return_value = {"event": {"status": {"type": "inprogress"}}}
    w._feed.parse_match_detail.return_value = _inprogress_parsed(205)
    w._feed.get_first_server.side_effect = RuntimeError("network down")

    # Must not raise.
    MatchWorker._poll(w)

    assert w._first_server is None
    # The exception is swallowed but the upsert should still happen.
    w._logger.upsert_match_detail_points.assert_called_once()
    kwargs = w._logger.upsert_match_detail_points.call_args.kwargs
    assert kwargs.get("first_server") is None
