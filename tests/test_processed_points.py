"""
Tests for the upsert_processed_points pipeline in src/live/logger.py.

Terminal-score detection tests run without DATABASE_URL.
End-to-end DB tests are gated on DATABASE_URL and use match_id=99998 as a
sentinel so they're side-effect-free on a shared instance.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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
            "live_processed.match_detail_points",
        ):
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE match_id = %s", [_MID])
            except Exception:
                ml._conn.rollback()
    ml._conn.commit()


def _insert_raw_point_at(
    ml: MatchLogger,
    *,
    point_num: int,
    set_num: int,
    game_num: int,
    home_pt: str,
    away_pt: str,
    server: str,
    point_winner: str,
    ts: datetime,
) -> None:
    """Insert a raw point with an explicit ts (log_raw_point uses now())."""
    with ml._conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO live_raw.tennisapi_points (
                ts, match_id, player_a, player_b,
                point_num, set_num, game_num,
                home_point, away_point, server, point_winner,
                is_ace, is_double_fault,
                ingestion_source, tournament_name, category
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                ts, _MID, _PA, _PB,
                point_num, set_num, game_num,
                home_pt, away_pt, server, point_winner,
                False, False,
                "test", "Test Open", "atp",
            ],
        )
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
                   home_games_won, away_games_won,
                   source
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
    """A game's last point at 40-40 means we lost data. With no fill data
    available, has_gap=TRUE is set on the surrounding boundary rows (the last
    point of game 1 and the first point of game 2)."""
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
    g2 = [r for r in rows if r[2] == 2]
    assert len(g1) == 6
    # Only the last point of game 1 (40-40) is flagged — it's prev_row of an
    # unfilled gap. Earlier game-1 points have a clean score progression.
    last_g1 = max(g1, key=lambda r: r[0])
    assert last_g1[7] is True
    assert sum(1 for r in g1 if r[7] is True) == 1
    # Game 2's first point is the curr_row of the same unfilled gap.
    assert all(r[7] is True for r in g2)


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


# ---------------------------------------------------------------------------
# Gap detection / match_details fill — new pipeline behavior
# ---------------------------------------------------------------------------


def _md(home_pt: str, away_pt: str, *, set_num: int = 1,
        home_sets: int = 0, away_sets: int = 0,
        home_games: int = 0, away_games: int = 0) -> dict:
    """Build a parsed match_detail dict at a given live score state."""
    d = _empty_match_detail()
    d["home_sets"] = home_sets
    d["away_sets"] = away_sets
    d[f"home_period{set_num}"] = home_games
    d[f"away_period{set_num}"] = away_games
    d["home_current_point"] = home_pt
    d["away_current_point"] = away_pt
    return d


@pytestmark_db
def test_mid_game_gap_detection_fires(logger):
    """home_point jumps 15 → 40 within the same game with no fill available
    → has_gap=TRUE on the two surrounding rows."""
    pn = 0
    for hp, ap in [("15", "0"), ("40", "0"), ("40", "30")]:
        _insert_raw_point(
            logger, point_num=pn, set_num=1, game_num=1,
            home_pt=hp, away_pt=ap, server="home", point_winner="home",
        )
        pn += 1
    # Force game-1 to be a "completed" group by adding a game-2 point.
    _insert_raw_point(
        logger, point_num=pn, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away",
    )
    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)

    pt_15_0 = next(r for r in rows if r[3] == "15" and r[4] == "0")
    pt_40_0 = next(r for r in rows if r[3] == "40" and r[4] == "0")
    pt_40_30 = next(r for r in rows if r[3] == "40" and r[4] == "30")
    # 15-0 (prev) and 40-0 (curr) flank the mid-game gap.
    assert pt_15_0[7] is True
    assert pt_40_0[7] is True
    # 40-30 follows 40-0 cleanly (sort 3 → 5? actually 3+1=4 then 3+2=5 = +2 jump → also a gap).
    # That's a second gap: 40-0 → 40-30. So 40-30 also flagged.
    assert pt_40_30[7] is True


@pytestmark_db
def test_multiple_gaps_in_same_game(logger):
    """Two independent jumps within one game produce two gaps; all four
    surrounding rows are flagged has_gap=TRUE when no fill is available."""
    pn = 0
    # 15-0 (1) → 40-0 (3)  ← gap A (jump of 2)
    # 40-0 (3) → 40-30 (5) ← gap B (jump of 2)
    for hp, ap in [("15", "0"), ("40", "0"), ("40", "30")]:
        _insert_raw_point(
            logger, point_num=pn, set_num=1, game_num=1,
            home_pt=hp, away_pt=ap, server="home", point_winner="home",
        )
        pn += 1
    # Then close out the game with a terminal point and start game 2.
    _insert_raw_point(
        logger, point_num=pn, set_num=1, game_num=1,
        home_pt="40", away_pt="40", server="home", point_winner="home",
    )
    pn += 1
    _insert_raw_point(
        logger, point_num=pn, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away",
    )
    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)

    flagged_g1 = [r for r in rows if r[2] == 1 and r[7] is True]
    # Both gap A's prev (15-0) and curr (40-0), and gap B's curr (40-30)
    # must be flagged. 40-0 is shared between gap A and gap B.
    flagged_scores = {(r[3], r[4]) for r in flagged_g1}
    assert ("15", "0") in flagged_scores
    assert ("40", "0") in flagged_scores
    assert ("40", "30") in flagged_scores


@pytestmark_db
def test_match_detail_points_dedup(logger):
    """Two upserts with the same score state yield one row, with polled_at
    advanced to the most recent timestamp."""
    t1 = datetime.now(timezone.utc) - timedelta(seconds=30)
    t2 = t1 + timedelta(seconds=10)
    detail = _md("30", "15", set_num=1, home_games=2, away_games=1)
    logger.upsert_match_detail_points(detail, t1)
    logger.upsert_match_detail_points(detail, t2)
    with logger._conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), MAX(polled_at) "
            "FROM live_processed.match_detail_points WHERE match_id = %s",
            [_MID],
        )
        count, latest = cur.fetchone()
    assert count == 1
    assert abs((latest - t2).total_seconds()) < 1


@pytestmark_db
def test_gap_fill_inserts_rows_from_match_details(logger):
    """Mid-game gap (15-0 → 40-0) fills from a match_detail_points row
    captured in the gap window. The fill row appears with source='match_details'
    and has_gap=TRUE."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    t1 = t0 + timedelta(seconds=15)
    t2 = t1 + timedelta(seconds=15)
    t3 = t2 + timedelta(seconds=15)

    _insert_raw_point_at(
        logger, point_num=0, set_num=1, game_num=1,
        home_pt="15", away_pt="0", server="home", point_winner="home", ts=t0,
    )
    logger.upsert_match_detail_points(
        _md("30", "0", set_num=1, home_games=0, away_games=0),
        t1,
    )
    _insert_raw_point_at(
        logger, point_num=1, set_num=1, game_num=1,
        home_pt="40", away_pt="0", server="home", point_winner="home", ts=t2,
    )
    _insert_raw_point_at(
        logger, point_num=2, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away", ts=t3,
    )

    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)
    fill = [r for r in rows if r[12] == "match_details"]
    assert len(fill) == 1
    fill_row = fill[0]
    assert (fill_row[3], fill_row[4]) == ("30", "0")
    assert fill_row[7] is True
    # Sits between the two pbp rows.
    pt_15_0 = next(r for r in rows if r[3] == "15" and r[4] == "0")
    pt_40_0 = next(r for r in rows if r[3] == "40" and r[4] == "0")
    assert float(pt_15_0[0]) < float(fill_row[0]) < float(pt_40_0[0])
    # Inferred winner: home went 15 → 30.
    assert fill_row[6] == "home"


@pytestmark_db
def test_gap_fill_preserves_score_progression_order(logger):
    """When match_details rows arrive with polled_at out of progression order,
    fill rows are still inserted in correct score order."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    t_pbp_first = t0
    t_md_a = t0 + timedelta(seconds=10)   # later poll captured 30-0
    t_md_b = t0 + timedelta(seconds=20)   # earlier-in-game state polled later: 15-0? no
    # Construct a clearer ordering issue: the LATER poll captures the EARLIER
    # score state (which can happen if the API briefly regresses). We then
    # rely on score_sort_key, not polled_at, for ordering.
    t_pbp_last = t0 + timedelta(seconds=40)

    _insert_raw_point_at(
        logger, point_num=0, set_num=1, game_num=1,
        home_pt="0", away_pt="0", server="home", point_winner="home",
        ts=t_pbp_first,
    )
    # Insert md fills in REVERSE score order (later score first by polled_at).
    logger.upsert_match_detail_points(
        _md("30", "0", set_num=1, home_games=0, away_games=0),
        t_md_a,
    )
    logger.upsert_match_detail_points(
        _md("15", "0", set_num=1, home_games=0, away_games=0),
        t_md_b,
    )
    _insert_raw_point_at(
        logger, point_num=1, set_num=1, game_num=1,
        home_pt="40", away_pt="0", server="home", point_winner="home",
        ts=t_pbp_last,
    )
    # Trigger boundary
    _insert_raw_point_at(
        logger, point_num=2, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away",
        ts=t_pbp_last + timedelta(seconds=20),
    )
    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)
    fills = [r for r in rows if r[12] == "match_details"]
    # Sorted by score_sort_key: 15-0 then 30-0
    assert [(r[3], r[4]) for r in fills] == [("15", "0"), ("30", "0")]
    # And their point_nums sit between pbp 0 and pbp 1
    assert all(0 < float(r[0]) < 1 for r in fills)


@pytestmark_db
def test_set_boundary_gap_uses_match_detail_set_num(logger):
    """Gap spans a set boundary: prev_row is last point of set 1, curr_row
    is first point of set 2. Each fill row's set_num comes from
    match_detail_points itself, not from prev or curr."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    t1 = t0 + timedelta(seconds=15)
    t2 = t1 + timedelta(seconds=15)
    t3 = t2 + timedelta(seconds=15)

    # Last point of set 1 — non-terminal so a gap is detected at the boundary.
    _insert_raw_point_at(
        logger, point_num=0, set_num=1, game_num=10,
        home_pt="40", away_pt="40", server="home", point_winner="home", ts=t0,
    )
    # An md row in set 1 in the gap window (after last set-1 pbp point).
    logger.upsert_match_detail_points(
        _md("0", "0", set_num=1,
            home_sets=0, away_sets=0, home_games=6, away_games=4),
        t1,
    )
    # And an md row in set 2.
    logger.upsert_match_detail_points(
        _md("15", "0", set_num=2,
            home_sets=1, away_sets=0, home_games=0, away_games=0),
        t2,
    )
    # First point of set 2.
    _insert_raw_point_at(
        logger, point_num=1, set_num=2, game_num=1,
        home_pt="30", away_pt="0", server="home", point_winner="home", ts=t3,
    )

    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)
    fills = [r for r in rows if r[12] == "match_details"]
    fill_sets = [(r[1], r[3], r[4]) for r in fills]
    assert (1, "0", "0") in fill_sets
    assert (2, "15", "0") in fill_sets


@pytestmark_db
def test_no_gap_fill_available_flags_surrounding_rows(logger):
    """Gap detected, no match_detail_points row in window → no inserts,
    surrounding rows have has_gap=TRUE."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    _insert_raw_point_at(
        logger, point_num=0, set_num=1, game_num=1,
        home_pt="15", away_pt="0", server="home", point_winner="home", ts=t0,
    )
    _insert_raw_point_at(
        logger, point_num=1, set_num=1, game_num=1,
        home_pt="40", away_pt="0", server="home", point_winner="home",
        ts=t0 + timedelta(seconds=30),
    )
    _insert_raw_point_at(
        logger, point_num=2, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away",
        ts=t0 + timedelta(seconds=60),
    )
    logger.upsert_processed_points(_MID, _PA, _PB, _empty_match_detail())
    rows = _fetch_processed(logger)
    assert all(r[12] == "point_by_point" for r in rows)
    pt_15_0 = next(r for r in rows if r[3] == "15" and r[4] == "0")
    pt_40_0 = next(r for r in rows if r[3] == "40" and r[4] == "0")
    assert pt_15_0[7] is True
    assert pt_40_0[7] is True


@pytestmark_db
def test_running_scores_monotonic_with_gap_fill(logger):
    """After merging fill rows into pbp rows, running games_won should be
    monotonically non-decreasing across the sequence and reflect the final
    completed-game tally for the current set."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    pn = 0
    # Game 1: home wins to love, all four points clean.
    for j, (hp, ap) in enumerate([("15", "0"), ("30", "0"), ("40", "0")]):
        _insert_raw_point_at(
            logger, point_num=pn, set_num=1, game_num=1,
            home_pt=hp, away_pt=ap, server="home", point_winner="home",
            ts=t0 + timedelta(seconds=j * 5),
        )
        pn += 1
    # Game 2: gap mid-game, fill via match_detail_points.
    _insert_raw_point_at(
        logger, point_num=pn, set_num=1, game_num=2,
        home_pt="0", away_pt="15", server="away", point_winner="away",
        ts=t0 + timedelta(seconds=60),
    )
    pn += 1
    logger.upsert_match_detail_points(
        _md("0", "30", set_num=1, home_games=1, away_games=0),
        t0 + timedelta(seconds=70),
    )
    _insert_raw_point_at(
        logger, point_num=pn, set_num=1, game_num=2,
        home_pt="0", away_pt="40", server="away", point_winner="away",
        ts=t0 + timedelta(seconds=90),
    )
    # Trigger a boundary so game 2 is "completed".
    pn += 1
    _insert_raw_point_at(
        logger, point_num=pn, set_num=1, game_num=3,
        home_pt="15", away_pt="0", server="home", point_winner="home",
        ts=t0 + timedelta(seconds=120),
    )

    detail = _empty_match_detail()
    detail["home_period1"] = 1
    detail["away_period1"] = 1
    logger.upsert_processed_points(_MID, _PA, _PB, detail)
    rows = _fetch_processed(logger)

    h_games = [r[10] for r in rows]
    a_games = [r[11] for r in rows]
    assert all(h_games[i] <= h_games[i + 1] for i in range(len(h_games) - 1))
    assert all(a_games[i] <= a_games[i + 1] for i in range(len(a_games) - 1))
    # Final pbp row is in game 3 → before-game state of (1, 1).
    last = rows[-1]
    assert (last[10], last[11]) == (1, 1)
