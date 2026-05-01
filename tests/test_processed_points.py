"""
Tests for the upsert_processed_points pipeline in src/live/logger.py.

Terminal-score detection tests run without DATABASE_URL.
End-to-end DB tests are gated on DATABASE_URL and use match_id=99998 as a
sentinel so they're side-effect-free on a shared instance.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from src.live.logger import MatchLogger

_MID = 99998
_PA = "PlayerA"
_PB = "PlayerB"


# ---------------------------------------------------------------------------
# Pure-Python tests for terminal-score detection (no DB)
# ---------------------------------------------------------------------------

class TestTerminalScore:
    def test_standard_game_server_won_terminal(self):
        # 40-0 with home serving, home (server) wins → terminal
        assert MatchLogger._is_terminal_score("40", "0", "home", "home") is True
        assert MatchLogger._is_terminal_score("40", "15", "home", "home") is True
        assert MatchLogger._is_terminal_score("40", "30", "home", "home") is True
        assert MatchLogger._is_terminal_score("AD", "40", "home", "home") is True

    def test_standard_game_server_won_when_away_serves(self):
        # In server-receiver convention "40-0 server won" means away=40, home=0,
        # away serves and away wins.
        assert MatchLogger._is_terminal_score("0", "40", "away", "away") is True
        assert MatchLogger._is_terminal_score("15", "40", "away", "away") is True
        assert MatchLogger._is_terminal_score("30", "40", "away", "away") is True
        assert MatchLogger._is_terminal_score("40", "AD", "away", "away") is True

    def test_standard_game_receiver_won_terminal(self):
        # 0-40 (server-receiver) with home serving, away (receiver) wins → terminal
        assert MatchLogger._is_terminal_score("0", "40", "home", "away") is True
        assert MatchLogger._is_terminal_score("15", "40", "home", "away") is True
        assert MatchLogger._is_terminal_score("30", "40", "home", "away") is True
        assert MatchLogger._is_terminal_score("40", "AD", "home", "away") is True

    def test_deuce_not_terminal(self):
        # 40-40 is deuce — game cannot end here
        assert MatchLogger._is_terminal_score("40", "40", "home", "home") is False
        assert MatchLogger._is_terminal_score("40", "40", "home", "away") is False

    def test_terminal_score_but_wrong_winner(self):
        # 40-0 server-receiver, home serves, but receiver wins the point → game NOT over
        assert MatchLogger._is_terminal_score("40", "0", "home", "away") is False
        # 0-40 server-receiver, home serves, but server wins → no game over
        assert MatchLogger._is_terminal_score("0", "40", "home", "home") is False

    def test_tiebreak_terminal_seven_five(self):
        # 7-5 tiebreak → terminal regardless of winner field
        assert MatchLogger._is_terminal_score("7", "5", "home", "home") is True
        assert MatchLogger._is_terminal_score("5", "7", "home", "away") is True

    def test_tiebreak_terminal_extended(self):
        # 10-8, 12-10 etc. terminal (>=7 with 2+ lead)
        assert MatchLogger._is_terminal_score("10", "8", "home", "home") is True
        assert MatchLogger._is_terminal_score("12", "10", "away", "away") is True

    def test_tiebreak_not_terminal_one_apart(self):
        # 7-6 not terminal (lead < 2)
        assert MatchLogger._is_terminal_score("7", "6", "home", "home") is False
        assert MatchLogger._is_terminal_score("6", "7", "home", "away") is False

    def test_tiebreak_not_terminal_below_seven(self):
        # 6-4 not terminal (max < 7)
        assert MatchLogger._is_terminal_score("6", "4", "home", "home") is False

    def test_none_inputs(self):
        assert MatchLogger._is_terminal_score(None, "0", "home", "home") is False
        assert MatchLogger._is_terminal_score("40", None, "home", "home") is False


# ---------------------------------------------------------------------------
# DB-backed tests — skipped when DATABASE_URL is absent
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping PostgreSQL processed-points tests",
)


@pytest.fixture
def logger():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    ml = MatchLogger()
    _cleanup(ml)
    yield ml
    _cleanup(ml)
    ml.close()


def _cleanup(ml: MatchLogger) -> None:
    with ml._conn.cursor() as cur:
        for tbl in (
            "live_raw.tennisapi_points",
            "live_raw.match_details",
            "live_processed.points",
        ):
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE match_id = %s", [_MID])
            except Exception:
                ml._conn.rollback()
    ml._conn.commit()


def _insert_raw_point(
    ml: MatchLogger,
    *,
    point_num: int,
    set_num: int,
    game_num: int,
    home_pt: str,
    away_pt: str,
    server: str,
    point_winner: str,
) -> None:
    ml.log_raw_point(
        match_id=_MID,
        player_a=_PA,
        player_b=_PB,
        point_dict={
            "set_number": set_num,
            "game_number": game_num,
            "home_point_score": home_pt,
            "away_point_score": away_pt,
            "server": server,
            "point_winner": point_winner,
            "is_ace": False,
            "is_double_fault": False,
        },
        point_num=point_num,
        tournament_name="Test Open",
        category="atp",
    )


def _fetch_processed(ml: MatchLogger) -> list[tuple]:
    with ml._conn.cursor() as cur:
        cur.execute(
            """
            SELECT point_num, set_num, game_num, home_point, away_point,
                   server, point_winner, has_gap,
                   home_sets_won, away_sets_won,
                   home_games_won, away_games_won
            FROM live_processed.points
            WHERE match_id = %s
            ORDER BY point_num
            """,
            [_MID],
        )
        return cur.fetchall()


def _empty_match_detail() -> dict:
    return {
        "match_id": _MID,
        "player_a": _PA,
        "player_b": _PB,
        "status": "inprogress",
        "home_sets": 0, "away_sets": 0,
        "home_period1": 0, "away_period1": 0,
        "home_period2": 0, "away_period2": 0,
        "home_period3": 0, "away_period3": 0,
        "home_current_point": "0", "away_current_point": "0",
        "winner_code": None,
        "tournament_name": "Test Open",
        "category": "atp",
    }


@pytestmark_db
def test_dedup_same_point_num_twice(logger):
    """Same point_num inserted twice in raw — only one row in processed."""
    # Two raw rows with point_num=0
    _insert_raw_point(
        logger, point_num=0, set_num=1, game_num=1,
        home_pt="15", away_pt="0", server="home", point_winner="home",
    )
    _insert_raw_point(
        logger, point_num=0, set_num=1, game_num=1,
        home_pt="15", away_pt="0", server="home", point_winner="home",
    )
    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)
    assert len(rows) == 1
    assert rows[0][0] == 0  # point_num


@pytestmark_db
def test_clean_three_game_sequence(logger):
    """
    Three completed games in set 1, all clean wins:
      Game 1 (home serves): home wins to love     → games 1-0
      Game 2 (away serves): home breaks (0-40)    → games 2-0
      Game 3 (home serves): home wins to love     → games 3-0
    """
    pn = 0
    # Game 1 — home serves, home wins 4 straight (3 logged points: 15-0, 30-0, 40-0)
    for hp, ap in [("15", "0"), ("30", "0"), ("40", "0")]:
        _insert_raw_point(
            logger, point_num=pn, set_num=1, game_num=1,
            home_pt=hp, away_pt=ap, server="home", point_winner="home",
        )
        pn += 1
    # Game 2 — away serves, home breaks. Server-receiver convention from API:
    # home is receiver. We log scores: 0-15, 0-30, 0-40 (home wins each as receiver)
    for hp, ap in [("0", "15"), ("0", "30"), ("0", "40")]:
        _insert_raw_point(
            logger, point_num=pn, set_num=1, game_num=2,
            home_pt=hp, away_pt=ap, server="away", point_winner="home",
        )
        pn += 1
    # Game 3 — home serves, home wins to love
    for hp, ap in [("15", "0"), ("30", "0"), ("40", "0")]:
        _insert_raw_point(
            logger, point_num=pn, set_num=1, game_num=3,
            home_pt=hp, away_pt=ap, server="home", point_winner="home",
        )
        pn += 1

    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)
    assert len(rows) == 9

    # No gaps anywhere
    assert all(r[7] is False for r in rows)

    # Game 1 points: counters reflect state DURING the game (0-0 prior)
    g1 = [r for r in rows if r[2] == 1]
    assert all(r[10] == 0 and r[11] == 0 for r in g1)

    # Game 2 points: home has won game 1 already
    g2 = [r for r in rows if r[2] == 2]
    assert all(r[10] == 1 and r[11] == 0 for r in g2)

    # Game 3 points: home has won 2 games
    g3 = [r for r in rows if r[2] == 3]
    assert all(r[10] == 2 and r[11] == 0 for r in g3)

    # Sets stay 0 throughout (set is not finished yet)
    assert all(r[8] == 0 and r[9] == 0 for r in rows)


@pytestmark_db
def test_gap_detection_at_deuce(logger):
    """A game's last point at 40-40 means we lost data → has_gap=TRUE."""
    pn = 0
    # Incomplete game 1 — last visible score is 40-40 (deuce)
    for hp, ap in [("15", "0"), ("30", "0"), ("40", "0"), ("40", "15"),
                   ("40", "30"), ("40", "40")]:
        _insert_raw_point(
            logger, point_num=pn, set_num=1, game_num=1,
            home_pt=hp, away_pt=ap, server="home",
            point_winner="home" if hp >= ap else "away",
        )
        pn += 1
    # Game 2 — first point so the game-1 boundary triggers
    _insert_raw_point(
        logger, point_num=pn, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away",
    )

    detail = _empty_match_detail()
    detail["home_period1"] = 1   # match_detail says home won game 1
    detail["away_period1"] = 0

    logger.upsert_processed_points(_MID, _PA, _PB, detail)
    rows = _fetch_processed(logger)

    g1 = [r for r in rows if r[2] == 1]
    assert len(g1) == 6
    assert all(r[7] is True for r in g1), "all game-1 points should be flagged has_gap"

    # Game 2 (still in progress) should NOT be flagged gap
    g2 = [r for r in rows if r[2] == 2]
    assert all(r[7] is False for r in g2)


@pytestmark_db
def test_gap_resolution_uses_match_detail_period_scores(logger):
    """
    Game ends at 40-40 (gap). match_detail says home_period1=1 → home should
    get the game-1 increment so game-2 points show home_games_won=1.
    """
    pn = 0
    # Gapped game 1 — last point at 40-40
    for hp, ap in [("40", "30"), ("40", "40")]:
        _insert_raw_point(
            logger, point_num=pn, set_num=1, game_num=1,
            home_pt=hp, away_pt=ap, server="home", point_winner="home",
        )
        pn += 1
    # Game 2 - first point, triggers the boundary
    _insert_raw_point(
        logger, point_num=pn, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away",
    )

    detail = _empty_match_detail()
    detail["home_period1"] = 1
    detail["away_period1"] = 0

    logger.upsert_processed_points(_MID, _PA, _PB, detail)
    rows = _fetch_processed(logger)

    g2 = [r for r in rows if r[2] == 2]
    assert len(g2) == 1
    # Counters reflect: home won game 1, away has 0
    assert g2[0][10] == 1, "home_games_won should be 1 (gap resolved via period scores)"
    assert g2[0][11] == 0


@pytestmark_db
def test_upsert_idempotent(logger):
    """Calling upsert twice should not create duplicate rows (PK conflict)."""
    _insert_raw_point(
        logger, point_num=0, set_num=1, game_num=1,
        home_pt="15", away_pt="0", server="home", point_winner="home",
    )
    detail = _empty_match_detail()
    logger.upsert_processed_points(_MID, _PA, _PB, detail)
    logger.upsert_processed_points(_MID, _PA, _PB, detail)
    rows = _fetch_processed(logger)
    assert len(rows) == 1


@pytestmark_db
def test_log_match_detail(logger):
    """log_match_detail inserts one row in live_raw.match_details."""
    detail = _empty_match_detail()
    detail["home_sets"] = 1
    detail["away_sets"] = 0
    detail["home_period1"] = 6
    detail["away_period1"] = 4
    logger.log_match_detail(detail, polled_at=datetime.now(timezone.utc))
    with logger._conn.cursor() as cur:
        cur.execute(
            "SELECT home_sets, away_sets, home_period1, away_period1 "
            "FROM live_raw.match_details WHERE match_id = %s",
            [_MID],
        )
        row = cur.fetchone()
    assert row == (1, 0, 6, 4)
