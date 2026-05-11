"""
Tests for src/book/player_resolver.py

Uses a MockTennisFeed (no real API calls). DB-integration tests gated
on DATABASE_URL. Synthetic player_ids in the 999999000-block; each
test deletes its rows in teardown.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
_DATABASE_URL = os.getenv("DATABASE_URL")

if not _DATABASE_URL:
    pytest.skip(
        "DATABASE_URL not set — skipping player_resolver integration tests",
        allow_module_level=True,
    )

import psycopg2  # noqa: E402

from src.book.player_resolver import resolve_player  # noqa: E402


# ---------------------------------------------------------------------------
# Mock TennisFeed
# ---------------------------------------------------------------------------

class MockTennisFeed:
    """Counts get_team_detail calls and returns a canned response or raises."""

    def __init__(self, response=None, raise_exc=None, side_effect=None):
        self.response = response
        self.raise_exc = raise_exc
        self.side_effect = side_effect
        self.call_count = 0
        self.call_args: list[int] = []

    def get_team_detail(self, team_id, **kwargs):
        self.call_count += 1
        self.call_args.append(int(team_id))
        if self.side_effect is not None:
            self.side_effect(team_id)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _sample_team_response(name="Test Player", alpha3="USA",
                          plays="right-handed", birth_ts=631152000):
    """birth_ts default = 1990-01-01 UTC."""
    return {
        "team": {
            "id": 999999000,
            "name": name,
            "country": {"alpha2": alpha3[:2], "alpha3": alpha3},
            "playerTeamInfo": {
                "plays": plays,
                "birthDateTimestamp": birth_ts,
            },
        }
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Synthetic IDs in 999999xxx range — guaranteed not to collide with API IDs.
TEST_IDS = (999999001, 999999002, 999999003, 999999004, 999999005)


@pytest.fixture
def conn():
    c = psycopg2.connect(_DATABASE_URL)
    c.autocommit = False
    try:
        yield c
    finally:
        try:
            with c.cursor() as cur:
                cur.execute(
                    "DELETE FROM book.players WHERE player_id = ANY(%s)",
                    (list(TEST_IDS),),
                )
            c.commit()
        finally:
            c.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_existing_player_returns_without_api_call(conn):
    """If book.players already has a row, the API must not be called."""
    pid = 999999001
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO book.players (player_id, name, country) "
            "VALUES (%s, %s, %s)",
            (pid, "Pre-Existing", "ITA"),
        )
    conn.commit()

    feed = MockTennisFeed()
    row = resolve_player(conn, pid, tennis_feed=feed)

    assert feed.call_count == 0
    assert row["player_id"] == pid
    assert row["name"] == "Pre-Existing"
    assert row["country"] == "ITA"


def test_missing_player_fetches_from_api_and_inserts(conn):
    """API returns metadata; resolver inserts row with all fields populated."""
    pid = 999999002
    feed = MockTennisFeed(
        response=_sample_team_response(
            name="Mock Sinner",
            alpha3="ITA",
            plays="right-handed",
            birth_ts=997920000,  # 2001-08-16
        )
    )
    row = resolve_player(conn, pid, tennis_feed=feed)
    conn.commit()

    assert feed.call_count == 1
    assert feed.call_args == [pid]
    assert row["player_id"] == pid
    assert row["name"] == "Mock Sinner"
    assert row["country"] == "ITA"
    assert row["hand"] == "right"
    assert row["dob"] == date(2001, 8, 16)


def test_api_failure_with_name_fallback_inserts_minimal_row(conn):
    """API raises; resolver falls back to (player_id, name_fallback) with NULLs."""
    pid = 999999003
    feed = MockTennisFeed(raise_exc=RuntimeError("API unreachable"))
    row = resolve_player(
        conn, pid, name_fallback="Fallback Name", tennis_feed=feed
    )
    conn.commit()

    assert feed.call_count == 1
    assert row["player_id"] == pid
    assert row["name"] == "Fallback Name"
    assert row["dob"] is None
    assert row["hand"] is None
    assert row["country"] is None


def test_api_failure_without_name_fallback_raises(conn):
    """API raises and no name_fallback — must raise ValueError."""
    pid = 999999004
    feed = MockTennisFeed(raise_exc=RuntimeError("API down"))
    with pytest.raises(ValueError, match="not in book.players"):
        resolve_player(conn, pid, tennis_feed=feed)
    # And no row should have been written.
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM book.players WHERE player_id = %s", (pid,)
        )
        assert cur.fetchone()[0] == 0


def test_concurrent_insert_race(conn):
    """Simulated race: a concurrent inserter writes the row while resolver
    is fetching. Resolver's INSERT hits ON CONFLICT DO NOTHING and the
    final SELECT returns the racer's row — no error."""
    pid = 999999005

    def race_insert(_team_id):
        # Use a separate connection to commit the racing insert independently.
        with psycopg2.connect(_DATABASE_URL) as race_conn:
            with race_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO book.players (player_id, name, country) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (pid, "Racer Won", "FRA"),
                )

    feed = MockTennisFeed(
        response=_sample_team_response(name="API Lost"),
        side_effect=race_insert,
    )
    row = resolve_player(conn, pid, tennis_feed=feed)
    conn.commit()

    assert feed.call_count == 1
    # Racer's row wins; our INSERT was a no-op.
    assert row["name"] == "Racer Won"
    assert row["country"] == "FRA"


def test_empty_api_response_with_name_fallback(conn):
    """API returns a response with no usable name → treated as failure;
    fallback used."""
    pid = 999999001
    feed = MockTennisFeed(response={"team": {}})  # no name
    row = resolve_player(
        conn, pid, name_fallback="Empty-Resp Fallback", tennis_feed=feed
    )
    conn.commit()
    assert row["name"] == "Empty-Resp Fallback"
    assert row["dob"] is None


def test_left_handed_normalization(conn):
    """plays='left-handed' → hand='left'."""
    pid = 999999002
    feed = MockTennisFeed(response=_sample_team_response(plays="left-handed"))
    row = resolve_player(conn, pid, tennis_feed=feed)
    conn.commit()
    assert row["hand"] == "left"
