"""
Tests for src/live/logger.py (PostgreSQL backend)

Requires DATABASE_URL to be set.  All tests are skipped automatically when
the env-var is absent so the suite stays green in environments without a
Postgres instance.

Uses match_id=99999 as a sentinel and deletes those rows in teardown so the
tests are side-effect-free on a shared database.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Skip-gate: every test in this module is skipped without DATABASE_URL
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping PostgreSQL logger tests",
)

from src.live.logger import MatchLogger  # noqa: E402  (import after skip-gate)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------
_MID = 99999  # sentinel match_id — cleaned up in teardown


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def logger():
    """Create a MatchLogger and delete sentinel rows on teardown."""
    ml = MatchLogger()
    yield ml
    with ml._conn.cursor() as cur:
        for tbl in (
            "live.match_polls",
            "live.match_states",
            "live.backfill_odds_polls",
        ):
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE match_id = %s", [_MID])
            except Exception:
                ml._conn.rollback()
    ml._conn.commit()
    ml.close()


def _fetchone(ml: MatchLogger, sql: str, params=None):
    with ml._conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()


def _fetchval(ml: MatchLogger, sql: str, params=None):
    row = _fetchone(ml, sql, params)
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_tables_created(logger):
    """All expected tables in live and audit schemas exist after MatchLogger init."""
    for schema, table in [
        ("live",  "match_polls"),
        ("live",  "match_states"),
        ("live",  "backfill_points"),
        ("live",  "backfill_odds_polls"),
        ("audit", "api_call_log"),
        ("audit", "api_response_archive"),
    ]:
        count = _fetchval(logger, """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        """, [schema, table])
        assert count == 1, f"{schema}.{table} was not created"


def test_match_polls_columns(logger):
    with logger._conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'live' AND table_name = 'match_polls'
        """)
        cols = {r[0] for r in cur.fetchall()}
    expected = {
        "match_id", "player_a", "player_b", "polled_at", "status",
        "home_sets", "away_sets",
        "home_period1", "away_period1",
        "home_period2", "away_period2",
        "home_period3", "away_period3",
        "home_current_point", "away_current_point",
        "winner_code", "tournament_name", "category",
    }
    assert expected <= cols


# ---------------------------------------------------------------------------
# Insertion tests — log_raw_odds
# ---------------------------------------------------------------------------

def test_raw_odds_inserts_one_row(logger):
    logger.log_raw_odds(
        match_id=_MID,
        player_a="Federer",
        player_b="Djokovic",
        odds_result={"bookmaker_implied_prob": 0.65, "num_bookmakers": 3,
                     "api_credits_remaining": 500},
    )
    count = _fetchval(
        logger,
        "SELECT COUNT(*) FROM live.backfill_odds_polls WHERE match_id = %s",
        [_MID],
    )
    assert count == 1


# ---------------------------------------------------------------------------
# Insertion tests — log_match_detail
# ---------------------------------------------------------------------------

_PARSED_DETAIL = {
    "match_id": _MID,
    "player_a": "Federer",
    "player_b": "Djokovic",
    "status": "inprogress",
    "home_sets": 0,
    "away_sets": 0,
    "home_period1": 1,
    "away_period1": 0,
    "home_period2": 0,
    "away_period2": 0,
    "home_period3": 0,
    "away_period3": 0,
    "home_current_point": "15",
    "away_current_point": "0",
    "winner_code": None,
    "tournament_name": "Wimbledon",
    "category": "atp",
}


def test_log_match_detail_inserts_one_row(logger):
    logger.log_match_detail(_PARSED_DETAIL)
    count = _fetchval(
        logger,
        "SELECT COUNT(*) FROM live.match_polls WHERE match_id = %s",
        [_MID],
    )
    assert count == 1


def test_log_match_detail_fields(logger):
    logger.log_match_detail(_PARSED_DETAIL)
    row = _fetchone(logger, """
        SELECT match_id, player_a, player_b, status,
               home_period1, away_period1,
               home_current_point, away_current_point,
               tournament_name, category
        FROM live.match_polls
        WHERE match_id = %s
    """, [_MID])
    assert row[0] == _MID
    assert row[1] == "Federer"
    assert row[2] == "Djokovic"
    assert row[3] == "inprogress"
    assert row[4] == 1
    assert row[5] == 0
    assert row[6] == "15"
    assert row[7] == "0"
    assert row[8] == "Wimbledon"
    assert row[9] == "atp"
