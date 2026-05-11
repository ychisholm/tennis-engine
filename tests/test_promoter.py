"""
Tests for src/book/promoter.py

DB-integration tests gated on DATABASE_URL. Each test uses synthetic
match_ids in the 999999xxx range and player_ids in the 999999xxx range.
Setup injects the required rows into audit.* and live.* directly via
SQL; teardown deletes everything touching those ids from book.*, audit.*,
and live.* so the suite is side-effect-free.

A MockTennisFeed (same as test_player_resolver) is wired through
promote_match so no real API calls happen.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
_DATABASE_URL = os.getenv("DATABASE_URL")

if not _DATABASE_URL:
    pytest.skip(
        "DATABASE_URL not set — skipping promoter integration tests",
        allow_module_level=True,
    )

import psycopg2  # noqa: E402
from psycopg2.extras import Json  # noqa: E402

from src.book.promoter import promote_match  # noqa: E402


# Synthetic ids
TEST_MATCH_IDS = (999999991, 999999992, 999999993)
TEST_PLAYER_IDS = (999999801, 999999802, 999999803, 999999804)


class MockTennisFeed:
    """Minimal stub — fail every API call so the resolver uses name_fallback."""

    def __init__(self):
        self.call_count = 0

    def get_team_detail(self, team_id, **kwargs):
        self.call_count += 1
        raise RuntimeError("MockTennisFeed: API not available in tests")


# ---------------------------------------------------------------------------
# Fixture: connection with global teardown
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = psycopg2.connect(_DATABASE_URL)
    c.autocommit = False
    try:
        yield c
    finally:
        try:
            with c.cursor() as cur:
                # book.* — cascades through player_career_stats, points
                cur.execute(
                    "DELETE FROM book.points WHERE match_id = ANY(%s)",
                    (list(TEST_MATCH_IDS),),
                )
                cur.execute(
                    "DELETE FROM book.matches WHERE match_id = ANY(%s)",
                    (list(TEST_MATCH_IDS),),
                )
                cur.execute(
                    "DELETE FROM book.player_career_stats "
                    "WHERE player_id = ANY(%s)",
                    (list(TEST_PLAYER_IDS),),
                )
                cur.execute(
                    "DELETE FROM book.players WHERE player_id = ANY(%s)",
                    (list(TEST_PLAYER_IDS),),
                )
                # audit.*
                cur.execute(
                    "DELETE FROM audit.api_response_archive "
                    "WHERE match_id = ANY(%s)",
                    ([str(m) for m in TEST_MATCH_IDS],),
                )
                cur.execute(
                    "DELETE FROM audit.verification_reports "
                    "WHERE match_id = ANY(%s)",
                    ([str(m) for m in TEST_MATCH_IDS],),
                )
                cur.execute(
                    "DELETE FROM audit.gap_reports "
                    "WHERE match_id = ANY(%s)",
                    ([str(m) for m in TEST_MATCH_IDS],),
                )
                # live.*
                cur.execute(
                    "DELETE FROM live.backfill_points "
                    "WHERE match_id = ANY(%s)",
                    (list(TEST_MATCH_IDS),),
                )
                cur.execute(
                    "DELETE FROM live.match_polls "
                    "WHERE match_id = ANY(%s)",
                    (list(TEST_MATCH_IDS),),
                )
            c.commit()
        finally:
            c.close()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _inject_match_details_json(cur, match_id, home_id, home_name,
                               away_id, away_name, ground_type="Hard"):
    body = {
        "event": {
            "id": match_id,
            "homeTeam": {"id": home_id, "name": home_name},
            "awayTeam": {"id": away_id, "name": away_name},
            "groundType": ground_type,
        }
    }
    cur.execute(
        """
        INSERT INTO audit.api_response_archive
            (endpoint, match_id, raw_json, byte_size)
        VALUES ('match_details', %s, %s, %s)
        """,
        (str(match_id), Json(body), len(json.dumps(body))),
    )


def _inject_match_polls(cur, match_id, home_name, away_name,
                        tournament, sets_a, sets_b):
    """Two rows: one inprogress, one finished."""
    cur.execute(
        """
        INSERT INTO live.match_polls (
            match_id, player_a, player_b, polled_at, status,
            home_sets, away_sets,
            home_period1, away_period1, home_period2, away_period2,
            tournament_name, category
        ) VALUES
          (%s, %s, %s, NOW() - INTERVAL '60 min', 'inprogress',
           0, 0, 3, 0, 0, 0, %s, 'atp'),
          (%s, %s, %s, NOW(),                  'finished',
           %s, %s, 6, 0, 6, 0, %s, 'atp')
        """,
        (match_id, home_name, away_name, tournament,
         match_id, home_name, away_name, sets_a, sets_b, tournament),
    )


def _inject_verification_report(
    cur, match_id, run_id, verdict, score_match,
    final_score="6-0 6-0",
):
    cur.execute(
        """
        INSERT INTO audit.verification_reports (
            verification_run_id, match_id, source,
            live_point_count, inferred_missing_points,
            live_final_score, recorded_final_score, final_score_match,
            total_sets, clean_set_count, gapped_set_count,
            gap_count, severity_max, verdict, set_breakdown
        ) VALUES (
            %s, %s, 'backfill',
            0, 0,
            %s, %s, %s,
            2, 2, 0,
            0, NULL, %s, %s
        )
        """,
        (str(run_id), str(match_id),
         final_score, final_score, score_match,
         verdict, Json([])),
    )


def _inject_backfill_points(cur, match_id):
    """A 6-0 6-0 home-sweep match: 2 sets × 6 games × 4 shown points each
    = 48 shown points. Each game home wins 4-0 (server alternates as
    expected). Set 1 starts with away serving."""
    point_num = 0
    for set_num in range(1, 3):
        for game_num in range(1, 7):
            # In set 1: game 1 away serves, game 2 home, etc.
            # In set 2: continuation — game 1 of set 2 is whoever didn't
            # serve last game of set 1. Set 1 has 6 games → game 6 was
            # home (since 1 away, 2 home, …, 6 home). Set 2 g1 → away.
            # Simpler: server alternates by global game count.
            global_game = (set_num - 1) * 6 + game_num
            server = "away" if global_game % 2 == 1 else "home"
            # Home wins every game 4-0.
            scores = [("15", "0"), ("30", "0"), ("40", "0")]
            for hp, ap in scores:
                cur.execute(
                    """
                    INSERT INTO live.backfill_points (
                        ts, match_id, point_num, set_num, game_num,
                        home_point, away_point, server, point_winner,
                        is_ace, is_double_fault, ingestion_source
                    ) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, 'home',
                              FALSE, FALSE, 'test_fixture')
                    """,
                    (match_id, point_num, set_num, game_num,
                     hp, ap, server),
                )
                point_num += 1


def _setup_eligible_match(
    cur, match_id, home_id, home_name, away_id, away_name,
    run_id, verdict="clean", score_match=True,
    sets_a=2, sets_b=0,
):
    _inject_match_details_json(
        cur, match_id, home_id, home_name, away_id, away_name
    )
    _inject_match_polls(
        cur, match_id, home_name, away_name,
        "Test Tournament", sets_a, sets_b,
    )
    _inject_verification_report(
        cur, match_id, run_id, verdict, score_match,
        final_score="6-0 6-0",
    )
    _inject_backfill_points(cur, match_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_happy_path_promotes_clean_match(conn):
    """Eligible clean match → promote_match returns True; all four tables
    populated correctly."""
    mid = TEST_MATCH_IDS[0]
    hid, aid = TEST_PLAYER_IDS[0], TEST_PLAYER_IDS[1]
    run_id = uuid.uuid4()

    with conn.cursor() as cur:
        _setup_eligible_match(cur, mid, hid, "Home Player", aid, "Away Player", run_id)
    conn.commit()

    feed = MockTennisFeed()
    result = promote_match(
        conn, match_id=mid, verification_run_id=run_id, tennis_feed=feed
    )
    conn.commit()

    assert result is True

    with conn.cursor() as cur:
        # book.matches
        cur.execute(
            "SELECT final_score, winner_id, sets_a, sets_b, "
            "surface, tournament FROM book.matches WHERE match_id = %s",
            (mid,),
        )
        m = cur.fetchone()
        assert m == ("6-0 6-0", hid, 2, 0, "hard", "Test Tournament")

        # book.points — fixture has 3 shown per game × 12 games = 36 shown,
        # plus 12 synthesized game-winning points = 48 total
        cur.execute(
            "SELECT COUNT(*) FROM book.points WHERE match_id = %s", (mid,)
        )
        assert cur.fetchone()[0] == 48

        # Home wins every point in every game (fixture is a clean sweep)
        cur.execute(
            "SELECT COUNT(*) FROM book.points "
            "WHERE match_id = %s AND point_winner_id = %s",
            (mid, hid),
        )
        assert cur.fetchone()[0] == 48

        # Career stats: home wins 4/4 every game, half the games is home's
        # service (6 games × 4 pts = 24), half away's (24). So:
        #   home serve_played=24 serve_won=24 return_played=24 return_won=24
        #   away serve_played=24 serve_won=0  return_played=24 return_won=0
        # matches_played=1 for both, matches_won=1 only for home.
        cur.execute(
            "SELECT player_id, serve_points_played, serve_points_won, "
            "return_points_played, return_points_won, matches_played, matches_won "
            "FROM book.player_career_stats WHERE player_id IN (%s, %s) "
            "ORDER BY player_id",
            (hid, aid),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        home_stats = next(r for r in rows if r[0] == hid)
        away_stats = next(r for r in rows if r[0] == aid)
        assert home_stats == (hid, 24, 24, 24, 24, 1, 1)
        assert away_stats == (aid, 24, 0, 24, 0, 1, 0)

        # book.players resolved via name_fallback (MockTennisFeed raises)
        cur.execute(
            "SELECT player_id, name FROM book.players "
            "WHERE player_id IN (%s, %s) ORDER BY player_id",
            (hid, aid),
        )
        prows = cur.fetchall()
        assert prows == [(hid, "Home Player"), (aid, "Away Player")]


def test_re_promotion_returns_false_and_no_duplicate_rows(conn):
    mid = TEST_MATCH_IDS[1]
    hid, aid = TEST_PLAYER_IDS[2], TEST_PLAYER_IDS[3]
    run_id = uuid.uuid4()

    with conn.cursor() as cur:
        _setup_eligible_match(cur, mid, hid, "H2", aid, "A2", run_id)
    conn.commit()

    feed = MockTennisFeed()
    first = promote_match(conn, match_id=mid, verification_run_id=run_id, tennis_feed=feed)
    conn.commit()
    second = promote_match(conn, match_id=mid, verification_run_id=run_id, tennis_feed=feed)
    conn.commit()

    assert first is True
    assert second is False

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM book.matches WHERE match_id = %s", (mid,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM book.points WHERE match_id = %s", (mid,))
        assert cur.fetchone()[0] == 48


def test_ineligible_verdict_returns_false_no_writes(conn):
    mid = TEST_MATCH_IDS[0]
    hid, aid = TEST_PLAYER_IDS[0], TEST_PLAYER_IDS[1]
    run_id = uuid.uuid4()

    with conn.cursor() as cur:
        _setup_eligible_match(
            cur, mid, hid, "H", aid, "A", run_id, verdict="material_gaps"
        )
    conn.commit()

    feed = MockTennisFeed()
    result = promote_match(
        conn, match_id=mid, verification_run_id=run_id, tennis_feed=feed
    )
    conn.commit()

    assert result is False
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM book.matches WHERE match_id = %s", (mid,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM book.points WHERE match_id = %s", (mid,))
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM book.players WHERE player_id IN (%s, %s)",
            (hid, aid),
        )
        assert cur.fetchone()[0] == 0


def test_score_mismatch_returns_false_no_writes(conn):
    mid = TEST_MATCH_IDS[0]
    hid, aid = TEST_PLAYER_IDS[0], TEST_PLAYER_IDS[1]
    run_id = uuid.uuid4()

    with conn.cursor() as cur:
        _setup_eligible_match(
            cur, mid, hid, "H", aid, "A", run_id, score_match=False
        )
    conn.commit()

    feed = MockTennisFeed()
    assert promote_match(
        conn, match_id=mid, verification_run_id=run_id, tennis_feed=feed
    ) is False
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM book.matches WHERE match_id = %s", (mid,))
        assert cur.fetchone()[0] == 0


def test_stats_accumulate_across_matches(conn):
    """Promote match A (player X wins) then match B (player X loses).
    Player X's stats should sum correctly across both."""
    mid_a, mid_b = TEST_MATCH_IDS[0], TEST_MATCH_IDS[1]
    px, py = TEST_PLAYER_IDS[0], TEST_PLAYER_IDS[1]
    run_id = uuid.uuid4()
    feed = MockTennisFeed()

    with conn.cursor() as cur:
        # Match A: X is home, X wins 6-0 6-0
        _setup_eligible_match(cur, mid_a, px, "X", py, "Y", run_id)
        # Match B: X is away (so away_id=px), X loses 6-0 6-0 (home wins).
        # Fixture always has home winning, so to make X lose we swap:
        # home_id=py, away_id=px.
        _setup_eligible_match(cur, mid_b, py, "Y", px, "X", run_id)
    conn.commit()

    assert promote_match(conn, match_id=mid_a, verification_run_id=run_id, tennis_feed=feed)
    assert promote_match(conn, match_id=mid_b, verification_run_id=run_id, tennis_feed=feed)
    conn.commit()

    with conn.cursor() as cur:
        # X: won match A (as home), lost match B (as away).
        # Match A as home: serve_played=24, serve_won=24, return_played=24,
        #                  return_won=24, matches_played=1, matches_won=1
        # Match B as away: serve_played=24, serve_won=0, return_played=24,
        #                  return_won=0, matches_played=1, matches_won=0
        # Cumulative for X: 48, 24, 48, 24, 2, 1
        cur.execute(
            "SELECT serve_points_played, serve_points_won, "
            "return_points_played, return_points_won, matches_played, matches_won "
            "FROM book.player_career_stats WHERE player_id = %s",
            (px,),
        )
        assert cur.fetchone() == (48, 24, 48, 24, 2, 1)

        # Y: won match B (as home), lost match A (as away).
        # Cumulative for Y: 48, 24, 48, 24, 2, 1
        cur.execute(
            "SELECT serve_points_played, serve_points_won, "
            "return_points_played, return_points_won, matches_played, matches_won "
            "FROM book.player_career_stats WHERE player_id = %s",
            (py,),
        )
        assert cur.fetchone() == (48, 24, 48, 24, 2, 1)
