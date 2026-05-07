"""
Tests for TennisFeed.translate_to_engine_format using hardcoded fixture.
No live API calls are made.
"""

import pytest
from src.live.tennis_feed import TennisFeed

# ---------------------------------------------------------------------------
# Fixture — match 14232981
# Set 1, Game 1: home serving (serving=1), home wins game (scoring=1)
#   Point 0: home wins on an ace           (pointDescription=1)
#   Point 1: away wins on a double fault   (pointDescription=2)
#   Point 2: home wins normally            (pointDescription=0)
#   Point 3: home wins normally            (pointDescription=0)
# Set 2, Game 1: away serving (serving=2), away wins game (scoring=2)
#   Point 0: away wins normally            (pointDescription=0)
# ---------------------------------------------------------------------------

FIXTURE_14232981 = {
    "pointByPoint": [
        {
            "set": 2,
            "games": [
                {
                    "game": 1,
                    "score": {"serving": 2, "scoring": 2},
                    "points": [
                        {
                            "homePoint": "0",
                            "awayPoint": "15",
                            "homePointType": 5,
                            "awayPointType": 1,
                            "pointDescription": 0,
                        },
                    ],
                }
            ],
        },
        {
            "set": 1,
            "games": [
                {
                    "game": 1,
                    "score": {"serving": 1, "scoring": 1},
                    "points": [
                        {
                            "homePoint": "15",
                            "awayPoint": "0",
                            "homePointType": 1,
                            "awayPointType": 5,
                            "pointDescription": 1,
                        },
                        {
                            "homePoint": "15",
                            "awayPoint": "15",
                            "homePointType": 5,
                            "awayPointType": 1,
                            "pointDescription": 2,
                        },
                        {
                            "homePoint": "30",
                            "awayPoint": "15",
                            "homePointType": 1,
                            "awayPointType": 5,
                            "pointDescription": 0,
                        },
                        {
                            "homePoint": "40",
                            "awayPoint": "15",
                            "homePointType": 1,
                            "awayPointType": 5,
                            "pointDescription": 0,
                        },
                    ],
                }
            ],
        },
    ]
}


@pytest.fixture
def feed(monkeypatch):
    # api_key won't be used — no HTTP calls in these tests.
    # Hide DATABASE_URL so TennisFeed.__init__ skips its default
    # ApiLogger construction and stays hermetic regardless of the
    # developer's shell environment.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return TennisFeed(api_key="test-key")


@pytest.fixture
def points(feed):
    return feed.translate_to_engine_format(FIXTURE_14232981)


# ---------------------------------------------------------------------------
# Chronological ordering
# ---------------------------------------------------------------------------

def test_set1_comes_before_set2(points):
    set_numbers = [p["set_number"] for p in points]
    assert set_numbers[0] == 1
    assert set_numbers[-1] == 2


def test_total_point_count(points):
    assert len(points) == 5  # 4 in set1/game1 + 1 in set2/game1


# ---------------------------------------------------------------------------
# Set 1, Game 1 — first point
# ---------------------------------------------------------------------------

def test_first_point_server_is_home(points):
    assert points[0]["server"] == "home"


def test_first_point_winner_is_home(points):
    assert points[0]["point_winner"] == "home"


def test_first_point_scores(points):
    assert points[0]["home_point_score"] == "15"
    assert points[0]["away_point_score"] == "0"


def test_first_point_set_and_game_numbers(points):
    assert points[0]["set_number"] == 1
    assert points[0]["game_number"] == 1


# ---------------------------------------------------------------------------
# Ace detection
# ---------------------------------------------------------------------------

def test_ace_on_first_point(points):
    assert points[0]["is_ace"] is True
    assert points[0]["is_double_fault"] is False


def test_no_false_ace_on_normal_point(points):
    assert points[2]["is_ace"] is False


# ---------------------------------------------------------------------------
# Double-fault detection
# ---------------------------------------------------------------------------

def test_double_fault_on_second_point(points):
    assert points[1]["is_double_fault"] is True
    assert points[1]["is_ace"] is False


def test_double_fault_point_winner_is_away(points):
    # Server (home) committed the double fault — away wins the point
    assert points[1]["point_winner"] == "away"


# ---------------------------------------------------------------------------
# Set 2 point
# ---------------------------------------------------------------------------

def test_set2_server_is_away(points):
    set2_point = points[4]
    assert set2_point["server"] == "away"
    assert set2_point["set_number"] == 2
    assert set2_point["point_winner"] == "away"


# ===========================================================================
# Phase 3 — TennisFeed._get instrumentation with ApiLogger
# ===========================================================================

import logging  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import requests as _requests  # noqa: E402


def _ok_response(body):
    resp = MagicMock(status_code=200)
    resp.json.return_value = body
    return resp


def _err_response(status, text="server error"):
    return MagicMock(status_code=status, text=text)


# ---------------------------------------------------------------------------
# A. ApiLogger plumbing
# ---------------------------------------------------------------------------


import src.live.api_logger as api_logger_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_default_logger_singleton():
    """Each test starts with a clean process-wide ApiLogger singleton."""
    api_logger_mod._reset_default_logger_for_testing()
    yield
    api_logger_mod._reset_default_logger_for_testing()


def test_init_default_singleton_is_none_yields_none_logger(monkeypatch):
    monkeypatch.setattr(api_logger_mod, "get_default_logger", lambda: None)
    feed = TennisFeed(api_key="x")
    assert feed._api_logger is None


def test_init_default_singleton_is_used_when_set(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(api_logger_mod, "get_default_logger", lambda: fake)
    feed = TennisFeed(api_key="x")
    assert feed._api_logger is fake


def test_two_feeds_share_same_singleton(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(api_logger_mod, "get_default_logger", lambda: fake)
    feed1 = TennisFeed(api_key="x")
    feed2 = TennisFeed(api_key="y")
    assert feed1._api_logger is feed2._api_logger
    assert feed1._api_logger is fake


def test_init_explicit_apilogger_bypasses_singleton(monkeypatch):
    """Explicit api_logger= must override the singleton entirely."""
    singleton = MagicMock(name="singleton")
    explicit = MagicMock(name="explicit")
    monkeypatch.setattr(api_logger_mod, "get_default_logger", lambda: singleton)

    feed = TennisFeed(api_key="x", api_logger=explicit)
    assert feed._api_logger is explicit
    assert feed._api_logger is not singleton


def test_get_works_with_no_logger(monkeypatch):
    """Regression: existing call sites (no logger configured) keep working."""
    monkeypatch.setattr(api_logger_mod, "get_default_logger", lambda: None)
    feed = TennisFeed(api_key="x")
    assert feed._api_logger is None

    monkeypatch.setattr(
        "src.live.tennis_feed.requests.get",
        lambda *a, **kw: _ok_response({"events": []}),
    )
    assert feed.get_live_matches_raw() == []


# ---------------------------------------------------------------------------
# B. Successful call path — endpoint dispatch and field correctness
# ---------------------------------------------------------------------------


def _build_logged_feed(monkeypatch, response_body):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_logger = MagicMock()
    feed = TennisFeed(api_key="x", api_logger=fake_logger)
    monkeypatch.setattr(
        "src.live.tennis_feed.requests.get",
        lambda *a, **kw: _ok_response(response_body),
    )
    return feed, fake_logger


def test_live_matches_logs_once_with_correct_fields(monkeypatch):
    body = {"events": [{"id": 1, "tournament": {"category": {"slug": "atp"}},
                        "eventFilters": {"category": "singles"},
                        "status": {"type": "inprogress"}}]}
    feed, fake_logger = _build_logged_feed(monkeypatch, body)

    feed.get_live_matches_raw()

    fake_logger.log_call.assert_called_once()
    kwargs = fake_logger.log_call.call_args.kwargs
    assert kwargs["endpoint"] == "live_matches"
    assert kwargs["request_path"] == "/api/tennis/events/live"
    assert kwargs["http_status"] == 200
    assert kwargs["raw_response"] == body
    assert kwargs["error"] is None
    assert kwargs["match_id"] is None
    assert kwargs["request_params"] == {}


def test_get_match_detail_endpoint_and_match_id(monkeypatch):
    body = {"event": {"id": 123, "status": {"type": "inprogress"}}}
    feed, fake_logger = _build_logged_feed(monkeypatch, body)

    feed.get_match_detail(123)

    kwargs = fake_logger.log_call.call_args.kwargs
    assert kwargs["endpoint"] == "match_details"
    assert kwargs["request_path"] == "/api/tennis/event/123"
    assert kwargs["match_id"] == "123"
    assert kwargs["request_params"] == {"match_id": "123"}
    assert kwargs["http_status"] == 200


def test_get_point_by_point_endpoint_and_match_id(monkeypatch):
    body = {"pointByPoint": []}
    feed, fake_logger = _build_logged_feed(monkeypatch, body)

    feed.get_point_by_point(456)

    kwargs = fake_logger.log_call.call_args.kwargs
    assert kwargs["endpoint"] == "point_by_point"
    assert kwargs["request_path"] == "/api/tennis/event/456/point-by-point"
    assert kwargs["match_id"] == "456"
    assert kwargs["request_params"] == {"match_id": "456"}


def test_get_upcoming_matches_logs_one_per_day(monkeypatch):
    body = {"events": []}
    feed, fake_logger = _build_logged_feed(monkeypatch, body)

    feed.get_upcoming_matches(days_ahead=1)  # today + tomorrow → 2 calls

    assert fake_logger.log_call.call_count == 2
    for call in fake_logger.log_call.call_args_list:
        kwargs = call.kwargs
        assert kwargs["endpoint"] == "events_by_date"
        params = kwargs["request_params"]
        assert set(params.keys()) == {"date_day", "date_month", "date_year"}
        assert all(isinstance(v, int) for v in params.values())


# ---------------------------------------------------------------------------
# C. Error and retry path
# ---------------------------------------------------------------------------


def test_http_500_logs_three_attempts_and_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_logger = MagicMock()
    feed = TennisFeed(api_key="x", api_logger=fake_logger)

    monkeypatch.setattr(
        "src.live.tennis_feed.requests.get",
        lambda *a, **kw: _err_response(500, "boom"),
    )
    monkeypatch.setattr("src.live.tennis_feed.time.sleep", lambda *_: None)

    with pytest.raises(ValueError):
        feed.get_live_matches_raw()

    assert fake_logger.log_call.call_count == 3
    for call in fake_logger.log_call.call_args_list:
        kwargs = call.kwargs
        assert kwargs["http_status"] == 500
        assert kwargs["error"] is not None
        assert kwargs["raw_response"] is None


def test_request_exception_logs_three_attempts(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_logger = MagicMock()
    feed = TennisFeed(api_key="x", api_logger=fake_logger)

    def boom(*a, **kw):
        raise _requests.ConnectionError("connection lost")

    monkeypatch.setattr("src.live.tennis_feed.requests.get", boom)
    monkeypatch.setattr("src.live.tennis_feed.time.sleep", lambda *_: None)

    with pytest.raises(_requests.RequestException):
        feed.get_live_matches_raw()

    assert fake_logger.log_call.call_count == 3
    for call in fake_logger.log_call.call_args_list:
        kwargs = call.kwargs
        assert kwargs["http_status"] is None
        assert kwargs["error"] is not None
        assert "connection lost" in kwargs["error"]
        assert isinstance(kwargs["latency_ms"], int)
        assert kwargs["latency_ms"] >= 0


def test_two_failures_then_success(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_logger = MagicMock()
    feed = TennisFeed(api_key="x", api_logger=fake_logger)

    success_body = {"events": []}
    counter = {"n": 0}

    def fake_get(*a, **kw):
        counter["n"] += 1
        if counter["n"] <= 2:
            raise _requests.ConnectionError("transient")
        return _ok_response(success_body)

    monkeypatch.setattr("src.live.tennis_feed.requests.get", fake_get)
    monkeypatch.setattr("src.live.tennis_feed.time.sleep", lambda *_: None)

    result = feed.get_live_matches_raw()
    assert result == []
    assert fake_logger.log_call.call_count == 3

    for i in range(2):
        kwargs = fake_logger.log_call.call_args_list[i].kwargs
        assert kwargs["http_status"] is None
        assert kwargs["error"] is not None
    final = fake_logger.log_call.call_args_list[2].kwargs
    assert final["http_status"] == 200
    assert final["error"] is None
    assert final["raw_response"] == success_body


# ---------------------------------------------------------------------------
# D. Defensive logging — exceptions in audit path must not break callers
# ---------------------------------------------------------------------------


def test_log_call_exception_does_not_break_request(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_logger = MagicMock()
    fake_logger.log_call.side_effect = RuntimeError("DB exploded")
    feed = TennisFeed(api_key="x", api_logger=fake_logger)

    body = {"events": []}
    monkeypatch.setattr(
        "src.live.tennis_feed.requests.get",
        lambda *a, **kw: _ok_response(body),
    )

    # Must not raise even though log_call raises.
    assert feed.get_live_matches_raw() == []


def test_summarize_response_exception_does_not_break_request(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_logger = MagicMock()
    feed = TennisFeed(api_key="x", api_logger=fake_logger)

    def boom(*a, **kw):
        raise ValueError("summarizer failure")

    import src.live.api_logger as api_logger_mod
    monkeypatch.setattr(api_logger_mod, "summarize_response", boom)

    monkeypatch.setattr(
        "src.live.tennis_feed.requests.get",
        lambda *a, **kw: _ok_response({"events": []}),
    )

    assert feed.get_live_matches_raw() == []


# ---------------------------------------------------------------------------
# E. Latency
# ---------------------------------------------------------------------------


def test_latency_ms_non_negative_int_on_success(monkeypatch):
    body = {"events": []}
    feed, fake_logger = _build_logged_feed(monkeypatch, body)
    feed.get_live_matches_raw()
    kwargs = fake_logger.log_call.call_args.kwargs
    assert isinstance(kwargs["latency_ms"], int)
    assert kwargs["latency_ms"] >= 0


def test_latency_ms_non_negative_int_on_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake_logger = MagicMock()
    feed = TennisFeed(api_key="x", api_logger=fake_logger)

    def boom(*a, **kw):
        raise _requests.ConnectionError("nope")

    monkeypatch.setattr("src.live.tennis_feed.requests.get", boom)
    monkeypatch.setattr("src.live.tennis_feed.time.sleep", lambda *_: None)

    with pytest.raises(_requests.RequestException):
        feed.get_live_matches_raw()

    for call in fake_logger.log_call.call_args_list:
        kwargs = call.kwargs
        assert isinstance(kwargs["latency_ms"], int)
        assert kwargs["latency_ms"] >= 0
