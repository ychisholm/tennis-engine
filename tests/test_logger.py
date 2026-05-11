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


# ---------------------------------------------------------------------------
# first_server column — schema, upsert plumbing, and backfill
# ---------------------------------------------------------------------------

from datetime import datetime, timezone, timedelta  # noqa: E402


def test_match_states_has_first_server_column(logger):
    """The migration ALTER must run on every MatchLogger.__init__."""
    with logger._conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'live' AND table_name = 'match_states'
        """)
        cols = {r[0] for r in cur.fetchall()}
    assert "first_server" in cols


def _upsert_state(logger, polled_at, *, home_pt="15", away_pt="0",
                  home_period1=1, away_period1=0, first_server=None):
    parsed = {
        "match_id": _MID,
        "player_a": "Federer",
        "player_b": "Djokovic",
        "status": "inprogress",
        "home_sets": 0,
        "away_sets": 0,
        "home_period1": home_period1,
        "away_period1": away_period1,
        "home_period2": None, "away_period2": None,
        "home_period3": None, "away_period3": None,
        "home_current_point": home_pt,
        "away_current_point": away_pt,
        "winner_code": None,
        "tournament_name": "Wimbledon",
        "category": "atp",
    }
    logger.upsert_match_detail_points(parsed, polled_at, first_server=first_server)


def test_upsert_writes_first_server(logger):
    ts = datetime.now(timezone.utc)
    _upsert_state(logger, ts, first_server="home")
    val = _fetchval(
        logger,
        "SELECT first_server FROM live.match_states WHERE match_id = %s",
        [_MID],
    )
    assert val == "home"


def test_upsert_writes_null_when_first_server_omitted(logger):
    """Default behaviour — early polls before we know the server."""
    ts = datetime.now(timezone.utc)
    _upsert_state(logger, ts)  # no first_server kwarg
    val = _fetchval(
        logger,
        "SELECT first_server FROM live.match_states WHERE match_id = %s",
        [_MID],
    )
    assert val is None


def test_backfill_first_server_fills_null_rows(logger):
    """Two NULL rows + one already-set row → backfill leaves the set row alone
    and fills the NULLs."""
    base = datetime.now(timezone.utc)
    _upsert_state(logger, base, home_pt="15", away_pt="0", first_server=None)
    _upsert_state(
        logger, base + timedelta(seconds=1),
        home_pt="30", away_pt="0", first_server=None,
    )
    # Manually plant a row already labelled 'away' to make sure backfill
    # respects existing values.
    with logger._conn.cursor() as cur:
        cur.execute("""
            UPDATE live.match_states SET first_server = 'away'
             WHERE match_id = %s AND home_current_point = '30'
        """, [_MID])
    logger._conn.commit()

    logger.backfill_first_server(_MID, "home")

    with logger._conn.cursor() as cur:
        cur.execute("""
            SELECT home_current_point, first_server FROM live.match_states
             WHERE match_id = %s ORDER BY polled_at
        """, [_MID])
        rows = cur.fetchall()
    by_pt = {r[0]: r[1] for r in rows}
    assert by_pt["15"] == "home"   # was NULL → backfilled
    assert by_pt["30"] == "away"   # already set → preserved


def test_backfill_first_server_is_idempotent(logger):
    base = datetime.now(timezone.utc)
    _upsert_state(logger, base, first_server=None)
    logger.backfill_first_server(_MID, "home")
    logger.backfill_first_server(_MID, "home")  # second call is a no-op
    val = _fetchval(
        logger,
        "SELECT first_server FROM live.match_states WHERE match_id = %s",
        [_MID],
    )
    assert val == "home"


def test_backfill_first_server_swallows_errors(logger, monkeypatch):
    """A DB hiccup must not raise — first_server is best-effort metadata."""
    import src.live.logger as logger_mod
    monkeypatch.setattr(
        logger_mod, "_BACKFILL_FIRST_SERVER",
        "UPDATE live.this_table_does_not_exist SET first_server = %s WHERE match_id = %s",
    )
    # Must not raise — failure is logged and swallowed.
    logger.backfill_first_server(_MID, "home")


# ---------------------------------------------------------------------------
# _infer_first_row_winner — pure unit tests, no DB needed
# (kept inside the test_logger module since they exercise logger internals)
# ---------------------------------------------------------------------------

def test_infer_first_row_winner_home_at_15_zero():
    """Worker's first observation is 15-0 → home scored the opening point."""
    from src.live.logger import _infer_first_row_winner
    curr = {"home_current_point": "15", "away_current_point": "0"}
    assert _infer_first_row_winner(curr) == "home"


def test_infer_first_row_winner_away_at_zero_15():
    """Worker's first observation is 0-15 → away scored the opening point.
    This is the Medvedev/Ruiz case: worker spun up after Medvedev won the
    first point, score was already 0-15 when we started polling."""
    from src.live.logger import _infer_first_row_winner
    curr = {"home_current_point": "0", "away_current_point": "15"}
    assert _infer_first_row_winner(curr) == "away"


def test_infer_first_row_winner_returns_none_at_zero_zero():
    """0-0 means no point played yet — nothing to infer."""
    from src.live.logger import _infer_first_row_winner
    curr = {"home_current_point": "0", "away_current_point": "0"}
    assert _infer_first_row_winner(curr) is None


def test_infer_first_row_winner_returns_none_when_both_nonzero():
    """15-15 is ambiguous (two points played, order unknown) — don't guess."""
    from src.live.logger import _infer_first_row_winner
    curr = {"home_current_point": "15", "away_current_point": "15"}
    assert _infer_first_row_winner(curr) is None


def test_infer_first_row_winner_handles_higher_first_observations():
    """If we missed several points and the first observation is 30-0, home
    still won the most-recent point (the one that produced this row)."""
    from src.live.logger import _infer_first_row_winner
    curr = {"home_current_point": "30", "away_current_point": "0"}
    assert _infer_first_row_winner(curr) == "home"


# ---------------------------------------------------------------------------
# _derive_point_winner — tiebreak score handling
# Tiebreak point scores are integers ("0","1","2",...,"7"), not the regular
# "0/15/30/40/AD" tokens. The pre-fix code dict-looked-up every score and
# returned rank 0 for any integer string, so deltas inside a tiebreak were
# indistinguishable and point_winner was NULL for every tiebreak point.
# ---------------------------------------------------------------------------

def _tb_prev_curr(prev_h, prev_a, curr_h, curr_a):
    return (
        {"home_sets_won": 0, "away_sets_won": 0,
         "home_current_games": 6, "away_current_games": 6,
         "home_current_point": prev_h, "away_current_point": prev_a},
        {"home_sets_won": 0, "away_sets_won": 0,
         "home_current_games": 6, "away_current_games": 6,
         "home_current_point": curr_h, "away_current_point": curr_a},
    )


def test_derive_point_winner_tiebreak_away_scores_first():
    """0-0 → 0-1 in a tiebreak: away won the point."""
    from src.live.logger import _derive_point_winner
    prev, curr = _tb_prev_curr("0", "0", "0", "1")
    assert _derive_point_winner(prev, curr) == "away"


def test_derive_point_winner_tiebreak_home_scores_after_away():
    """0-3 → 1-3 in a tiebreak: home scored."""
    from src.live.logger import _derive_point_winner
    prev, curr = _tb_prev_curr("0", "3", "1", "3")
    assert _derive_point_winner(prev, curr) == "home"


def test_derive_point_winner_tiebreak_at_5_5_home_to_6_5():
    """Mid-tiebreak transition through 5-5 keeps working at higher scores."""
    from src.live.logger import _derive_point_winner
    prev, curr = _tb_prev_curr("5", "5", "6", "5")
    assert _derive_point_winner(prev, curr) == "home"


def test_derive_point_winner_tiebreak_above_10():
    """Match tiebreaks (and long tiebreaks) can reach double-digit scores."""
    from src.live.logger import _derive_point_winner
    prev, curr = _tb_prev_curr("9", "10", "9", "11")
    assert _derive_point_winner(prev, curr) == "away"


def test_derive_point_winner_regular_game_still_works():
    """Pre-existing regular-game logic must keep working after the rank fix."""
    from src.live.logger import _derive_point_winner
    prev = {"home_sets_won": 0, "away_sets_won": 0,
            "home_current_games": 2, "away_current_games": 1,
            "home_current_point": "15", "away_current_point": "30"}
    curr = {"home_sets_won": 0, "away_sets_won": 0,
            "home_current_games": 2, "away_current_games": 1,
            "home_current_point": "15", "away_current_point": "40"}
    assert _derive_point_winner(prev, curr) == "away"


def test_derive_point_winner_regular_deuce_reset_still_works():
    """AD → 40 deuce reset: away scored on home's AD."""
    from src.live.logger import _derive_point_winner
    prev = {"home_sets_won": 0, "away_sets_won": 0,
            "home_current_games": 1, "away_current_games": 1,
            "home_current_point": "AD", "away_current_point": "40"}
    curr = {"home_sets_won": 0, "away_sets_won": 0,
            "home_current_games": 1, "away_current_games": 1,
            "home_current_point": "40", "away_current_point": "40"}
    assert _derive_point_winner(prev, curr) == "away"
