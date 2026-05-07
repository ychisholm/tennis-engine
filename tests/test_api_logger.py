"""
Tests for src/live/api_logger.py

Two test classes:
  - Summarizer tests: pure functions, no DB, always run.
  - DB-backed tests: require DATABASE_URL, skipped otherwise (mirrors the
    convention in tests/test_logger.py). The MatchLogger fixture is built
    first to ensure live_raw.api_call_log and live_raw.api_response_archive
    exist (their DDL lives in src/live/logger.py:_SETUP_STMTS).
  - Mock-based failure tests: hermetic, always run, exercise the swallow-on-
    error contract without needing a real Postgres instance.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.live.api_logger import (
    ApiLogger,
    summarize_events_by_date,
    summarize_live_matches,
    summarize_match_details,
    summarize_point_by_point,
    summarize_response,
)

_HAS_DB = bool(os.environ.get("DATABASE_URL"))
_skip_no_db = pytest.mark.skipif(
    not _HAS_DB, reason="DATABASE_URL not set — skipping PostgreSQL ApiLogger tests"
)


# ===========================================================================
# Fixtures — sample responses
# ===========================================================================

def _atp_singles_event(event_id: int, status: str = "inprogress") -> dict:
    return {
        "id": event_id,
        "tournament": {"category": {"slug": "atp"}},
        "eventFilters": {"category": "singles"},
        "status": {"type": status},
    }


def _wta_doubles_event(event_id: int, status: str = "inprogress") -> dict:
    return {
        "id": event_id,
        "tournament": {"category": {"slug": "wta"}},
        "eventFilters": {"category": "Doubles"},
        "status": {"type": status},
    }


def _challenger_event(event_id: int, status: str = "inprogress") -> dict:
    return {
        "id": event_id,
        "tournament": {"category": {"slug": "challenger"}},
        "eventFilters": {"category": "singles"},
        "status": {"type": status},
    }


_LIVE_RESPONSE = {
    "events": [
        _atp_singles_event(101, "inprogress"),
        _atp_singles_event(102, "inprogress"),
        _wta_doubles_event(201, "inprogress"),
        _challenger_event(301, "inprogress"),
        _atp_singles_event(401, "finished"),
    ]
}

_EVENTS_BY_DATE_RESPONSE = {
    "events": [
        _atp_singles_event(501, "notstarted"),
        _atp_singles_event(502, "inprogress"),
        _atp_singles_event(503, "finished"),
        _wta_doubles_event(601, "notstarted"),
        _challenger_event(701, "notstarted"),
    ]
}

_MATCH_DETAILS_RESPONSE = {
    "event": {
        "id": 14232981,
        "status": {"type": "inprogress"},
        "winnerCode": None,
        "homeScore": {
            "current": 1,
            "period1": 6,
            "period2": 3,
            "period3": 0,
            "point": "30",
        },
        "awayScore": {
            "current": 0,
            "period1": 4,
            "period2": 6,
            "period3": 0,
            "point": "15",
        },
    }
}

_POINT_BY_POINT_RESPONSE = {
    "pointByPoint": [
        {
            "set": 1,
            "games": [
                {"game": 1, "points": [{}, {}, {}, {}]},
                {"game": 2, "points": [{}, {}, {}]},
            ],
        },
        {
            "set": 2,
            "games": [
                {"game": 1, "points": [{}, {}]},
            ],
        },
    ]
}


# ===========================================================================
# Summarizer tests — pure, no DB
# ===========================================================================


class TestSummarizeLiveMatches:
    def test_valid_response(self):
        out = summarize_live_matches(_LIVE_RESPONSE)
        assert out["total_events"] == 5
        # 4 inprogress (101, 102, 201, 301), 1 finished (401)
        assert out["inprogress_count"] == 4
        # qualifying = ATP/WTA singles + inprogress → 101, 102
        assert out["qualifying_count"] == 2
        assert sorted(out["qualifying_match_ids"]) == [101, 102]

    def test_empty_dict(self):
        out = summarize_live_matches({})
        assert out == {
            "total_events": 0,
            "inprogress_count": 0,
            "qualifying_count": 0,
            "qualifying_match_ids": [],
        }

    def test_none(self):
        out = summarize_live_matches(None)
        assert out["total_events"] == 0
        assert out["qualifying_match_ids"] == []

    def test_bare_list_response(self):
        out = summarize_live_matches([_atp_singles_event(99, "inprogress")])
        assert out["total_events"] == 1
        assert out["qualifying_match_ids"] == [99]

    def test_skips_finished_match(self):
        out = summarize_live_matches({"events": [_atp_singles_event(7, "finished")]})
        assert out["inprogress_count"] == 0
        assert out["qualifying_count"] == 0


class TestSummarizeEventsByDate:
    def test_valid_response(self):
        out = summarize_events_by_date(_EVENTS_BY_DATE_RESPONSE)
        assert out["total_events"] == 5
        # status breakdown across all 5 events
        assert out["status_breakdown"]["notstarted"] == 3
        assert out["status_breakdown"]["inprogress"] == 1
        assert out["status_breakdown"]["finished"] == 1
        # qualifying = ATP/WTA singles regardless of status → 501, 502, 503
        assert out["qualifying_count"] == 3
        assert sorted(out["qualifying_match_ids"]) == [501, 502, 503]

    def test_empty_dict(self):
        out = summarize_events_by_date({})
        assert out == {
            "total_events": 0,
            "status_breakdown": {},
            "qualifying_count": 0,
            "qualifying_match_ids": [],
        }

    def test_none(self):
        out = summarize_events_by_date(None)
        assert out["total_events"] == 0
        assert out["status_breakdown"] == {}

    def test_unknown_status_bucket(self):
        out = summarize_events_by_date({"events": [{"id": 1, "tournament": {}}]})
        assert out["status_breakdown"] == {"unknown": 1}


class TestSummarizeMatchDetails:
    def test_valid_response(self):
        out = summarize_match_details(_MATCH_DETAILS_RESPONSE)
        assert out["match_id"] == 14232981
        assert out["status"] == "inprogress"
        assert out["winner_code"] is None
        assert out["home_sets_won"] == 1
        assert out["away_sets_won"] == 0
        assert out["home_current_point"] == "30"
        assert out["away_current_point"] == "15"
        assert out["home_period1"] == 6
        assert out["away_period1"] == 4
        assert out["home_period2"] == 3
        assert out["away_period2"] == 6
        assert out["home_period3"] == 0
        assert out["away_period3"] == 0
        assert out["is_finished"] is False

    def test_finished_match_sets_flag(self):
        resp = {"event": {"id": 1, "status": {"type": "finished"}, "winnerCode": 1}}
        out = summarize_match_details(resp)
        assert out["is_finished"] is True
        assert out["winner_code"] == 1

    def test_empty_dict(self):
        out = summarize_match_details({})
        assert out["match_id"] is None
        assert out["status"] is None
        assert out["is_finished"] is False
        assert out["home_sets_won"] is None

    def test_none(self):
        out = summarize_match_details(None)
        assert out["match_id"] is None
        assert out["is_finished"] is False


class TestSummarizePointByPoint:
    def test_valid_response(self):
        out = summarize_point_by_point(_POINT_BY_POINT_RESPONSE)
        # set 1: 4 + 3 = 7; set 2: 2 → 9 total
        assert out["total_points"] == 9
        assert out["set_count"] == 2
        assert out["latest_set"] == 2

    def test_empty_dict(self):
        out = summarize_point_by_point({})
        assert out == {"total_points": 0, "set_count": 0, "latest_set": 0}

    def test_none(self):
        out = summarize_point_by_point(None)
        assert out["total_points"] == 0
        assert out["set_count"] == 0

    def test_empty_set_no_games(self):
        out = summarize_point_by_point({"pointByPoint": [{"set": 1, "games": []}]})
        assert out["total_points"] == 0
        assert out["set_count"] == 1
        assert out["latest_set"] == 1


# ===========================================================================
# Dispatcher tests
# ===========================================================================


class TestSummarizeResponse:
    def test_dispatches_live_matches(self):
        out = summarize_response("live_matches", _LIVE_RESPONSE)
        assert out["total_events"] == 5
        assert out["qualifying_count"] == 2

    def test_dispatches_events_by_date(self):
        out = summarize_response("events_by_date", _EVENTS_BY_DATE_RESPONSE)
        assert "status_breakdown" in out

    def test_dispatches_match_details(self):
        out = summarize_response("match_details", _MATCH_DETAILS_RESPONSE)
        assert out["match_id"] == 14232981

    def test_dispatches_point_by_point(self):
        out = summarize_response("point_by_point", _POINT_BY_POINT_RESPONSE)
        assert out["total_points"] == 9

    def test_none_response_returns_none(self):
        assert summarize_response("live_matches", None) is None

    def test_unknown_endpoint(self):
        out = summarize_response("nope", {"events": []})
        assert out == {"error": "unknown_endpoint", "endpoint": "nope"}

    def test_summarizer_exception_caught(self, monkeypatch):
        def boom(_response):
            raise ValueError("kaboom")

        monkeypatch.setitem(
            __import__("src.live.api_logger", fromlist=["_SUMMARIZERS"])._SUMMARIZERS,
            "live_matches",
            boom,
        )
        out = summarize_response("live_matches", {"events": []})
        assert out == {"error": "kaboom"}


# ===========================================================================
# Mock-based failure-path tests — hermetic, no DB needed
# ===========================================================================


def _build_mocked_logger(execute_side_effect=None):
    """Construct an ApiLogger backed by a MagicMock psycopg2 connection."""
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    # cursor() is used as a context manager
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_conn.cursor.return_value.__exit__.return_value = False
    if execute_side_effect is not None:
        fake_cursor.execute.side_effect = execute_side_effect

    with patch("src.live.api_logger.psycopg2.connect", return_value=fake_conn):
        logger = ApiLogger(db_url="postgresql://fake")
    return logger, fake_conn, fake_cursor


class TestApiLoggerFailureHandling:
    def test_returns_none_on_db_exception(self):
        logger, fake_conn, _ = _build_mocked_logger(
            execute_side_effect=RuntimeError("connection lost")
        )
        result = logger.log_call(
            endpoint="live_matches",
            request_path="/api/tennis/events/live",
        )
        assert result is None
        fake_conn.rollback.assert_called_once()

    def test_close_releases_connection(self):
        logger, fake_conn, _ = _build_mocked_logger()
        logger.close()
        fake_conn.close.assert_called_once()

    def test_context_manager_closes_on_exit(self):
        logger, fake_conn, _ = _build_mocked_logger()
        with logger as ctx:
            assert ctx is logger
        fake_conn.close.assert_called_once()


# ===========================================================================
# DB-backed integration tests — require DATABASE_URL
# ===========================================================================

# Sentinel match_id used to scope and clean up rows from these tests.
_SENTINEL_MID = "99999_apilogger_test"


@pytest.fixture
def db_logger():
    """Yield an ApiLogger; ensure schema exists by booting MatchLogger first.

    Cleans up sentinel rows in api_call_log and api_response_archive on
    teardown so tests are side-effect-free against a shared database.
    """
    if not _HAS_DB:
        pytest.skip("DATABASE_URL not set")

    # MatchLogger.__init__ runs the live_raw DDL including api_call_log /
    # api_response_archive. Construct it (and discard) so the tables exist.
    from src.live.logger import MatchLogger
    schema_boot = MatchLogger()
    schema_boot.close()

    al = ApiLogger()
    yield al
    # Teardown
    try:
        with al._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM live_raw.api_call_log WHERE match_id = %s",
                [_SENTINEL_MID],
            )
            cur.execute(
                "DELETE FROM live_raw.api_response_archive WHERE match_id = %s",
                [_SENTINEL_MID],
            )
        al._conn.commit()
    except Exception:
        al._conn.rollback()
    al.close()


def _fetchone(al: ApiLogger, sql: str, params=None):
    with al._conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()


@_skip_no_db
class TestApiLoggerDB:
    def test_basic_insert_returns_id(self, db_logger):
        call_id = db_logger.log_call(
            endpoint="live_matches",
            request_path="/api/tennis/events/live",
            request_params={"foo": "bar"},
            match_id=_SENTINEL_MID,
            http_status=200,
            latency_ms=123,
            response_summary={"total_events": 0},
        )
        assert isinstance(call_id, int)
        assert call_id > 0

        row = _fetchone(
            db_logger,
            """
            SELECT endpoint, request_path, match_id, http_status, latency_ms,
                   raw_response_id, error
            FROM live_raw.api_call_log WHERE id = %s
            """,
            [call_id],
        )
        assert row[0] == "live_matches"
        assert row[1] == "/api/tennis/events/live"
        assert row[2] == _SENTINEL_MID
        assert row[3] == 200
        assert row[4] == 123
        assert row[5] is None  # no raw_response provided → no archive row
        assert row[6] is None

    def test_archive_endpoint_with_raw_response_archives(self, db_logger):
        raw = {"events": [{"id": 1, "tournament": {"category": {"slug": "atp"}}}]}
        call_id = db_logger.log_call(
            endpoint="live_matches",
            request_path="/api/tennis/events/live",
            match_id=_SENTINEL_MID,
            http_status=200,
            latency_ms=50,
            response_summary={"total_events": 1},
            raw_response=raw,
        )
        assert call_id is not None

        row = _fetchone(
            db_logger,
            "SELECT raw_response_id FROM live_raw.api_call_log WHERE id = %s",
            [call_id],
        )
        archive_id = row[0]
        assert archive_id is not None

        archive_row = _fetchone(
            db_logger,
            """
            SELECT endpoint, match_id, byte_size
            FROM live_raw.api_response_archive WHERE id = %s
            """,
            [archive_id],
        )
        assert archive_row[0] == "live_matches"
        assert archive_row[1] == _SENTINEL_MID
        assert archive_row[2] > 0

    def test_point_by_point_does_not_archive(self, db_logger):
        raw = {"pointByPoint": [{"set": 1, "games": []}]}
        call_id = db_logger.log_call(
            endpoint="point_by_point",
            request_path="/api/tennis/event/123/point-by-point",
            match_id=_SENTINEL_MID,
            http_status=200,
            latency_ms=80,
            response_summary={"total_points": 0},
            raw_response=raw,
        )
        assert call_id is not None
        row = _fetchone(
            db_logger,
            "SELECT raw_response_id FROM live_raw.api_call_log WHERE id = %s",
            [call_id],
        )
        assert row[0] is None

    def test_raw_response_none_never_archives(self, db_logger):
        for endpoint in ("live_matches", "events_by_date", "match_details"):
            call_id = db_logger.log_call(
                endpoint=endpoint,
                request_path=f"/api/tennis/{endpoint}",
                match_id=_SENTINEL_MID,
                raw_response=None,
            )
            assert call_id is not None
            row = _fetchone(
                db_logger,
                "SELECT raw_response_id FROM live_raw.api_call_log WHERE id = %s",
                [call_id],
            )
            assert row[0] is None, f"{endpoint} archived despite raw_response=None"

    def test_error_recorded_with_null_status(self, db_logger):
        call_id = db_logger.log_call(
            endpoint="match_details",
            request_path="/api/tennis/event/999",
            match_id=_SENTINEL_MID,
            http_status=None,
            latency_ms=None,
            error="ConnectionResetError: peer closed",
        )
        assert call_id is not None
        row = _fetchone(
            db_logger,
            """
            SELECT http_status, latency_ms, error
            FROM live_raw.api_call_log WHERE id = %s
            """,
            [call_id],
        )
        assert row[0] is None
        assert row[1] is None
        assert row[2] == "ConnectionResetError: peer closed"

    def test_jsonb_roundtrip(self, db_logger):
        params = {"date": "2026-05-07", "page": 1}
        summary = {"total_events": 3, "qualifying_match_ids": [1, 2, 3]}
        call_id = db_logger.log_call(
            endpoint="events_by_date",
            request_path="/api/tennis/events/7/5/2026",
            request_params=params,
            match_id=_SENTINEL_MID,
            http_status=200,
            latency_ms=42,
            response_summary=summary,
        )
        assert call_id is not None
        row = _fetchone(
            db_logger,
            """
            SELECT request_params, response_summary
            FROM live_raw.api_call_log WHERE id = %s
            """,
            [call_id],
        )
        # psycopg2 returns JSONB as parsed Python objects.
        assert row[0] == params
        assert row[1] == summary

    def test_poll_cycle_id_persisted(self, db_logger):
        cycle = uuid.uuid4()
        call_id = db_logger.log_call(
            endpoint="match_details",
            request_path="/api/tennis/event/1",
            match_id=_SENTINEL_MID,
            http_status=200,
            latency_ms=10,
            poll_cycle_id=cycle,
        )
        assert call_id is not None
        row = _fetchone(
            db_logger,
            "SELECT poll_cycle_id FROM live_raw.api_call_log WHERE id = %s",
            [call_id],
        )
        assert str(row[0]) == str(cycle)

    def test_match_id_int_coerced_to_str(self, db_logger):
        call_id = db_logger.log_call(
            endpoint="match_details",
            request_path="/api/tennis/event/14232981",
            match_id=14232981,  # int
            http_status=200,
            latency_ms=5,
        )
        # Restrict cleanup to our sentinel; this row has a different match_id,
        # so delete it explicitly here.
        try:
            assert call_id is not None
            row = _fetchone(
                db_logger,
                "SELECT match_id FROM live_raw.api_call_log WHERE id = %s",
                [call_id],
            )
            assert row[0] == "14232981"
        finally:
            with db_logger._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM live_raw.api_call_log WHERE id = %s", [call_id]
                )
            db_logger._conn.commit()


# ===========================================================================
# Phase 3.1 — process-wide singleton accessor
# ===========================================================================

import logging  # noqa: E402
import threading  # noqa: E402

import src.live.api_logger as api_logger_mod  # noqa: E402
from src.live.api_logger import (  # noqa: E402
    _reset_default_logger_for_testing,
    get_default_logger,
)


@pytest.fixture(autouse=False)
def _reset_singleton():
    """Each singleton test starts and ends with a clean global state."""
    _reset_default_logger_for_testing()
    yield
    _reset_default_logger_for_testing()


class TestSingletonIdentity:
    def test_two_calls_return_same_instance(self, monkeypatch, _reset_singleton):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        fake = MagicMock(spec=ApiLogger)
        ctor = MagicMock(return_value=fake)
        monkeypatch.setattr(api_logger_mod, "ApiLogger", ctor)

        first = get_default_logger()
        second = get_default_logger()

        assert first is fake
        assert first is second
        assert ctor.call_count == 1

    def test_reset_yields_new_instance(self, monkeypatch, _reset_singleton):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        instances = [MagicMock(spec=ApiLogger), MagicMock(spec=ApiLogger)]
        ctor = MagicMock(side_effect=instances)
        monkeypatch.setattr(api_logger_mod, "ApiLogger", ctor)

        first = get_default_logger()
        _reset_default_logger_for_testing()
        second = get_default_logger()

        assert first is instances[0]
        assert second is instances[1]
        assert first is not second
        assert ctor.call_count == 2


class TestSingletonDatabaseUrlHandling:
    def test_no_database_url_returns_none_silently(
        self, monkeypatch, caplog, _reset_singleton
    ):
        monkeypatch.delenv("DATABASE_URL", raising=False)

        ctor = MagicMock()
        monkeypatch.setattr(api_logger_mod, "ApiLogger", ctor)

        with caplog.at_level(logging.WARNING, logger="src.live.api_logger"):
            result = get_default_logger()

        assert result is None
        ctor.assert_not_called()
        assert not any(
            "ApiLogger" in r.getMessage() for r in caplog.records
        )

    def test_construction_failure_logs_warning_and_returns_none(
        self, monkeypatch, caplog, _reset_singleton
    ):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        ctor = MagicMock(side_effect=RuntimeError("connection refused"))
        monkeypatch.setattr(api_logger_mod, "ApiLogger", ctor)

        with caplog.at_level(logging.WARNING, logger="src.live.api_logger"):
            result = get_default_logger()

        assert result is None
        ctor.assert_called_once()
        assert any(
            "Default ApiLogger construction failed" in r.getMessage()
            for r in caplog.records
        )

    def test_failed_init_is_memoized(self, monkeypatch, _reset_singleton):
        """After construction fails, subsequent calls do NOT retry."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        ctor = MagicMock(side_effect=RuntimeError("nope"))
        monkeypatch.setattr(api_logger_mod, "ApiLogger", ctor)

        for _ in range(5):
            assert get_default_logger() is None

        assert ctor.call_count == 1


class TestSingletonThreadSafety:
    def test_concurrent_callers_share_one_instance(
        self, monkeypatch, _reset_singleton
    ):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        # Slow constructor so threads pile up at the lock and we exercise the
        # double-checked path inside the critical section.
        construct_event = threading.Event()

        def slow_ctor(*a, **kw):
            construct_event.wait(timeout=2.0)
            return MagicMock(spec=ApiLogger)

        ctor = MagicMock(side_effect=slow_ctor)
        monkeypatch.setattr(api_logger_mod, "ApiLogger", ctor)

        N = 10
        barrier = threading.Barrier(N)
        results: list = [None] * N

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = get_default_logger()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()

        # Let the constructor complete after all threads have queued up at
        # the lock. A small sleep is the simplest synchronization here.
        import time
        time.sleep(0.1)
        construct_event.set()

        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive()

        assert ctor.call_count == 1
        first = results[0]
        assert first is not None
        for r in results:
            assert r is first
