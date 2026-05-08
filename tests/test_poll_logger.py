"""
Tests for src/poll_logger.py — covers Phase 4's poll_cycle_id wiring.

Mirrors tests/test_logger.py and tests/test_api_logger.py conventions:
DB-backed tests are skip-gated on DATABASE_URL; signature-level tests are
hermetic via mocking psycopg2.connect.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

_HAS_DB = bool(os.environ.get("DATABASE_URL"))
_skip_no_db = pytest.mark.skipif(
    not _HAS_DB, reason="DATABASE_URL not set — skipping PostgreSQL PollLogger tests"
)


# ===========================================================================
# Hermetic signature tests — no DB required
# ===========================================================================

def _mocked_pollogger():
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_conn.cursor.return_value.__exit__.return_value = False
    with patch("src.poll_logger.psycopg2.connect", return_value=fake_conn):
        from src.poll_logger import PollLogger
        pl = PollLogger(db_url="postgresql://fake")
    return pl, fake_conn, fake_cursor


class TestPollLoggerSignature:
    def test_log_accepts_poll_cycle_id_kwarg(self):
        pl, _, fake_cursor = _mocked_pollogger()
        cycle = uuid.uuid4()
        pl.log(event_type="TICK_START", poll_cycle_id=cycle)
        fake_cursor.execute.assert_called_once()
        sql, params = fake_cursor.execute.call_args.args
        assert "poll_cycle_id" in sql
        assert str(cycle) in params

    def test_log_without_poll_cycle_id_passes_none(self):
        pl, _, fake_cursor = _mocked_pollogger()
        pl.log(event_type="TICK_START")
        fake_cursor.execute.assert_called_once()
        sql, params = fake_cursor.execute.call_args.args
        assert "poll_cycle_id" in sql
        assert params[-1] is None

    def test_log_swallows_db_failure(self):
        pl, fake_conn, fake_cursor = _mocked_pollogger()
        fake_cursor.execute.side_effect = RuntimeError("DB went away")
        # log returns None on failure (per the swallow-on-error contract).
        result = pl.log(event_type="TICK_START", poll_cycle_id=uuid.uuid4())
        assert result is None
        fake_conn.rollback.assert_called_once()


# ===========================================================================
# DB-backed integration tests
# ===========================================================================

_SENTINEL_MID = "99999_polllogger_test"


@pytest.fixture
def pl():
    if not _HAS_DB:
        pytest.skip("DATABASE_URL not set")
    from src.poll_logger import PollLogger
    pl = PollLogger()
    pl.setup()  # idempotent — ensures the new poll_cycle_id column exists
    yield pl
    try:
        with pl._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit.poll_audit_log WHERE match_id = %s",
                [_SENTINEL_MID],
            )
        pl._conn.commit()
    except Exception:
        pl._conn.rollback()
    pl.close()


def _fetchone(pl, sql, params=None):
    with pl._conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()


@_skip_no_db
class TestPollLoggerDB:
    def test_poll_cycle_id_persisted_as_uuid(self, pl):
        cycle = uuid.uuid4()
        pl.log(
            event_type="TICK_START",
            match_id=_SENTINEL_MID,
            poll_cycle_id=cycle,
        )
        row = _fetchone(
            pl,
            """
            SELECT poll_cycle_id FROM audit.poll_audit_log
            WHERE match_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            [_SENTINEL_MID],
        )
        assert row[0] is not None
        assert str(row[0]) == str(cycle)

    def test_omitted_poll_cycle_id_writes_null(self, pl):
        pl.log(
            event_type="TICK_START",
            match_id=_SENTINEL_MID,
        )
        row = _fetchone(
            pl,
            """
            SELECT poll_cycle_id FROM audit.poll_audit_log
            WHERE match_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            [_SENTINEL_MID],
        )
        assert row[0] is None

    def test_poll_cycle_id_links_multiple_events(self, pl):
        """Two events with the same cycle_id can be joined back together."""
        cycle = uuid.uuid4()
        for evt in ("TICK_START", "STATE_TRANSITION"):
            pl.log(
                event_type=evt,
                match_id=_SENTINEL_MID,
                poll_cycle_id=cycle,
            )
        with pl._conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM audit.poll_audit_log
                WHERE match_id = %s AND poll_cycle_id = %s
                """,
                [_SENTINEL_MID, str(cycle)],
            )
            count = cur.fetchone()[0]
        assert count == 2
