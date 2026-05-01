"""
Tests for MatchScheduler state transitions and MatchWorker poll behaviour.
All TennisFeed and MatchCollector calls are mocked — no real HTTP traffic.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.live.scheduler import MatchScheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_event(
    match_id: int = 1,
    player_a: str = "Alcaraz",
    player_b: str = "Sinner",
    category_slug: str = "atp",
    status_type: str = "inprogress",
) -> dict:
    return {
        "id": match_id,
        "homeTeam": {"name": player_a},
        "awayTeam": {"name": player_b},
        "tournament": {
            "name": "Test Open",
            "category": {"slug": category_slug},
            "uniqueTournament": {"id": 1234},
        },
        "eventFilters": {"category": "singles"},
        "status": {"type": status_type},
    }


def _upcoming(
    seconds_from_now: int,
    match_id: int = 1,
    player_a: str = "Alcaraz",
    player_b: str = "Sinner",
) -> dict:
    return {
        "match_id": match_id,
        "player_a": player_a,
        "player_b": player_b,
        "tournament": "Test Open",
        "scheduled_start_unix": int(time.time()) + seconds_from_now,
    }


def _make_scheduler(
    upcoming: list[dict] | None = None,
    live_events: list[dict] | None = None,
) -> tuple[MatchScheduler, MagicMock, MagicMock]:
    feed = MagicMock()
    feed.get_upcoming_matches.return_value = upcoming or []
    feed.get_live_matches_raw.return_value = live_events or []
    collector = MagicMock()
    logger = MagicMock()
    sched = MatchScheduler(feed=feed, collector=collector, logger=logger)
    return sched, feed, collector


# ---------------------------------------------------------------------------
# Scheduler state transitions
# ---------------------------------------------------------------------------

def test_idle_to_pre_match_when_match_within_15_minutes():
    """A match starting in 10 minutes must drive IDLE → PRE_MATCH."""
    sched, _feed, collector = _make_scheduler(
        upcoming=[_upcoming(seconds_from_now=600)],  # 10 minutes
        live_events=[],
    )
    assert sched.state == "IDLE"

    sched._check_schedule()

    assert sched.state == "PRE_MATCH"
    collector.start.assert_not_called()


def test_pre_match_to_live_when_live_matches_detected():
    """When /live returns a qualifying inprogress event, state must go LIVE."""
    sched, feed, collector = _make_scheduler()
    # Prime into PRE_MATCH first.
    feed.get_upcoming_matches.return_value = [_upcoming(seconds_from_now=300)]
    feed.get_live_matches_raw.return_value = []
    sched._check_schedule()
    assert sched.state == "PRE_MATCH"

    # Now simulate the match going live.
    feed.get_live_matches_raw.return_value = [_live_event()]
    sched._check_schedule()

    assert sched.state == "LIVE"
    collector.start.assert_called_once()


def test_live_to_idle_when_no_live_or_upcoming():
    """When nothing is live and nothing upcoming within 15 min, go to IDLE."""
    sched, feed, collector = _make_scheduler()
    # Force LIVE state.
    feed.get_live_matches_raw.return_value = [_live_event()]
    feed.get_upcoming_matches.return_value = []
    sched._check_schedule()
    assert sched.state == "LIVE"
    collector.start.assert_called_once()

    # Matches all finished, nothing upcoming near term.
    feed.get_live_matches_raw.return_value = []
    feed.get_upcoming_matches.return_value = [_upcoming(seconds_from_now=7200)]
    sched._check_schedule()

    assert sched.state == "IDLE"
    collector.stop.assert_called_once()


# ---------------------------------------------------------------------------
# MatchWorker fingerprint mutation detection
# ---------------------------------------------------------------------------

def _make_worker():
    from src.live.collector import MatchWorker
    event = {
        "id": 42,
        "homeTeam": {"name": "Alcaraz"},
        "awayTeam": {"name": "Sinner"},
        "tournament": {
            "name": "Test Open",
            "category": {"slug": "atp"},
            "uniqueTournament": {"id": 1234},
        },
    }
    # Bypass __init__ so we don't open real HTTP/DB connections.
    w = MatchWorker.__new__(MatchWorker)
    w._match_id = event["id"]
    w._player_a = event["homeTeam"]["name"]
    w._player_b = event["awayTeam"]["name"]
    w._tournament_id = event["tournament"]["uniqueTournament"]["id"]
    w._match_metadata = {
        "homeTeam":   {"name": w._player_a},
        "awayTeam":   {"name": w._player_b},
        "tournament": {"uniqueTournament": {"id": w._tournament_id}},
    }
    w._last_point_processed = None
    w._points_seen = 0
    w._tournament_name = "Test Open"
    w._category = "atp"
    w._logger = MagicMock()
    _neutral = {"p0_hard": 0.63, "p0_clay": 0.60, "p0_grass": 0.65,
                "archetype": {"sd": 60, "ba": 60, "pe": 60, "tv": 60}}
    w._player_a_dict = {**_neutral, "name": w._player_a}
    w._player_b_dict = {**_neutral, "name": w._player_b}
    return w

def _make_points(n: int) -> list[dict]:
    return [
        {
            "server": "home", "home_point_score": "0", "away_point_score": "0",
            "point_winner": "home", "is_ace": False, "is_double_fault": False,
            "game_number": 1, "set_number": 1, "idx": i,
        }
        for i in range(n)
    ]


def test_mutation_shrinkage_resets_engine():
    """When API returns fewer points than _points_seen, engine must be reset."""
    from src.live.collector import MatchWorker

    w = _make_worker()
    original_points = _make_points(5)
    w._points_seen = 5
    w._last_point_processed = original_points[4]

    feed = MagicMock()
    feed.get_point_by_point.return_value = {}
    feed.translate_to_engine_format.return_value = _make_points(3)
    w._feed = feed
    w._engine = MagicMock()
    w._engine._match_over = False

    MatchWorker._poll(w)

    # After reset, the same poll cycle replays the 3 returned points.
    assert w._points_seen == 3


def test_mutation_changed_history_resets_engine():
    """When the last-seen point no longer matches all_points[_points_seen-1], engine must reset."""
    from src.live.collector import MatchWorker

    w = _make_worker()
    original_last = {
        "server": "home", "home_point_score": "15", "away_point_score": "0",
        "point_winner": "home", "is_ace": False, "is_double_fault": False,
        "game_number": 1, "set_number": 1, "idx": 4,
    }
    w._points_seen = 5
    w._last_point_processed = original_last

    mutated_points = _make_points(6)
    mutated_points[4] = {**original_last, "point_winner": "away"}

    feed = MagicMock()
    feed.get_point_by_point.return_value = {}
    feed.translate_to_engine_format.return_value = mutated_points
    w._feed = feed
    w._engine = MagicMock()
    w._engine._match_over = False

    MatchWorker._poll(w)

    # After reset, the same poll cycle replays all 6 returned points.
    assert w._points_seen == 6


def test_stable_history_does_not_reset_engine():
    """When history is unchanged, no reset should occur and points_seen advances."""
    from src.live.collector import MatchWorker

    w = _make_worker()
    stable_points = _make_points(5)
    w._points_seen = 5
    w._last_point_processed = stable_points[4]

    extended_points = _make_points(6)
    extended_points[:5] = stable_points

    feed = MagicMock()
    feed.get_point_by_point.return_value = {}
    feed.translate_to_engine_format.return_value = extended_points
    w._feed = feed
    w._engine = MagicMock()
    w._engine._match_over = False

    MatchWorker._poll(w)

    assert w._points_seen == 6
