#!/usr/bin/env python3
"""
Tests for src/backtester.py (Component 6A — Backtester)

All tests use a synthetic in-memory DuckDB to avoid dependency on the real
dataset being present in the test environment.

Run with:
    python -m pytest tests/test_backtester.py -v
"""

import csv
import os
import tempfile

import duckdb
import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtesting.backtester import Backtester, _CSV_COLUMNS


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

# Column order must match the INSERT statements below.
_POINTS_DDL = """
CREATE TABLE atp_points (
    match_id     VARCHAR,
    Pt           INTEGER,
    Set1         INTEGER,
    Set2         INTEGER,
    Gm1          INTEGER,
    Gm2          INTEGER,
    Pts          VARCHAR,
    "Gm#"        INTEGER,
    TbSet        BOOLEAN,
    Svr          INTEGER,
    "1st"        VARCHAR,
    "2nd"        VARCHAR,
    Notes        VARCHAR,
    PtWinner     INTEGER
)
"""

_MATCHES_DDL = """
CREATE TABLE atp_matches (
    tourney_id          VARCHAR,
    tourney_name        VARCHAR,
    surface             VARCHAR,
    tourney_date        INTEGER,
    winner_name         VARCHAR,
    loser_name          VARCHAR,
    w_ace               INTEGER,
    w_df                INTEGER,
    w_svpt              INTEGER,
    w_1stIn             INTEGER,
    w_1stWon            INTEGER,
    w_2ndWon            INTEGER,
    w_SvGms             INTEGER,
    w_bpSaved           INTEGER,
    w_bpFaced           INTEGER,
    l_ace               INTEGER,
    l_df                INTEGER,
    l_svpt              INTEGER,
    l_1stIn             INTEGER,
    l_1stWon            INTEGER,
    l_2ndWon            INTEGER,
    l_SvGms             INTEGER,
    l_bpSaved           INTEGER,
    l_bpFaced           INTEGER
)
"""


def _make_game_points(
    match_id: str,
    start_pt: int,
    svr: int,
    set1: int,
    set2: int,
    gm_no: int,
    winner: int,          # 1 or 2 — who wins each point (all same for clean hold)
) -> list[tuple]:
    """Generate 4 points for a clean service hold."""
    score_seq = ["0-0", "15-0", "30-0", "40-0"]
    rows = []
    for i, pts in enumerate(score_seq):
        rows.append((
            match_id, start_pt + i,
            set1, set2,
            0, 0,
            pts,
            gm_no,
            False,
            svr,
            "4n",    # 1st serve
            None,    # 2nd serve (first serve in)
            None,
            winner,
        ))
    return rows


def _build_synthetic_db(tmp_path: str) -> str:
    """
    Build a temporary DuckDB with two complete ATP-style matches and one
    short match (< 10 points) for testing the skip logic.

    Match 1: "20230101-M-Test-F-Player_One-Player_Two" — 20 points, A wins
    Match 2: "20230101-M-Test-F-Player_Three-Player_Four" — 20 points, B wins
    Match 3: "20230101-M-Test-F-Short_Player-Other_Player" — 5 points (skip)

    Returns the path to the DuckDB file.
    """
    db_path = os.path.join(tmp_path, "test.duckdb")
    con = duckdb.connect(db_path)
    con.execute(_POINTS_DDL)
    con.execute(_MATCHES_DDL)

    rows: list[tuple] = []

    # ---- Match 1: Player_One (A) wins, 5 games (A serves 3, B serves 2) ----
    mid1 = "20230101-M-Test-F-Player_One-Player_Two"
    pt = 1
    # 5 games: server alternates A(1)/B(2)/A(1)/B(2)/A(1)
    # A wins all — set1 stays 0, set2 stays 0 for first 20 pts (no full set won)
    # Manually set final Set1=2, Set2=0 on the last few rows to declare A winner
    for g in range(5):
        svr = 1 if g % 2 == 0 else 2    # A serves odd games, B serves even
        pw  = 1                           # A wins all points
        set1 = min(g // 3, 2)            # crude: set1 increments after 3 games
        set2 = 0
        game_rows = _make_game_points(mid1, pt, svr, set1, set2, g + 1, pw)
        rows.extend(game_rows)
        pt += 4
    # Patch the last row to Set1=2, Set2=0 (match decided)
    last = list(rows[-1])
    last[3] = 2   # Set1 index
    last[4] = 0   # Set2 index
    rows[-1] = tuple(last)

    # ---- Match 2: Player_Three loses (B/Player_Four wins) ----
    mid2 = "20230101-M-Test-F-Player_Three-Player_Four"
    pt = 1
    for g in range(5):
        svr = 1 if g % 2 == 0 else 2
        pw  = 2   # B wins all points
        set1 = 0
        set2 = min(g // 3, 2)
        game_rows = _make_game_points(mid2, pt, svr, set1, set2, g + 1, pw)
        rows.extend(game_rows)
        pt += 4
    # Patch the last row to Set1=0, Set2=2 (B wins match)
    last = list(rows[-1])
    last[3] = 0
    last[4] = 2
    rows[-1] = tuple(last)

    # ---- Match 3: Short match — should be skipped (< 10 points) ----
    mid3 = "20230101-M-Test-F-Short_Player-Other_Player"
    for i in range(5):
        rows.append((mid3, i + 1, 0, 0, 0, 0, "0-0", 1, False, 1, "4n", None, None, 1))

    con.executemany(
        "INSERT INTO atp_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )

    # Insert minimal career stats for Player_One (surface=Hard)
    con.execute("""
        INSERT INTO atp_matches VALUES
        ('t1', 'Test', 'Hard', 20230101,
         'Player One', 'Player Two',
         10, 2, 80, 55, 45, 18, 12, 3, 6,
         5,  3, 70, 40, 30, 12, 11, 2, 5)
    """)

    con.close()
    return db_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory):
    tmp = str(tmp_path_factory.mktemp("db"))
    return _build_synthetic_db(tmp)


@pytest.fixture(scope="module")
def output_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("out"))


@pytest.fixture(scope="module")
def bt(synthetic_db, output_dir):
    return Backtester(db_path=synthetic_db, output_dir=output_dir)


@pytest.fixture(scope="module")
def csv_path(bt):
    """Run the backtest once and return the CSV path (shared across tests)."""
    return bt.run(tour="atp")


# ---------------------------------------------------------------------------
# 1. Backtester initialises without error
# ---------------------------------------------------------------------------

def test_init(synthetic_db, tmp_path):
    b = Backtester(db_path=synthetic_db, output_dir=str(tmp_path / "out"))
    assert b is not None


# ---------------------------------------------------------------------------
# 2. run() completes and returns a file path
# ---------------------------------------------------------------------------

def test_run_returns_path(csv_path):
    assert isinstance(csv_path, str)
    assert csv_path.endswith(".csv")


# ---------------------------------------------------------------------------
# 3. Output CSV has correct column headers
# ---------------------------------------------------------------------------

def test_csv_headers(csv_path):
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        assert list(reader.fieldnames) == _CSV_COLUMNS, (
            f"Expected columns {_CSV_COLUMNS}, got {reader.fieldnames}"
        )


# ---------------------------------------------------------------------------
# 4. Row count equals total points across replayed matches
# ---------------------------------------------------------------------------

def test_row_count(csv_path):
    """
    Match 3 is skipped (< 10 pts). Matches 1 and 2 have 20 points each.
    LiveMatch may end early if match_over fires, so row count <= 40.
    There should be at least 1 row per replayed match.
    """
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 2, f"Expected >= 2 rows, got {len(rows)}"
    assert len(rows) <= 40, f"Expected <= 40 rows, got {len(rows)}"


# ---------------------------------------------------------------------------
# 5. P_match_A values are all between 0 and 1
# ---------------------------------------------------------------------------

def test_p_match_in_range(csv_path):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        p = float(row["P_match_A"])
        assert 0.0 <= p <= 1.0, f"P_match_A={p} out of [0,1] for {row['match_id']}"


# ---------------------------------------------------------------------------
# 6. actual_winner contains only 0 or 1
# ---------------------------------------------------------------------------

def test_actual_winner_binary(csv_path):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        w = int(row["actual_winner"])
        assert w in (0, 1), f"actual_winner={w} must be 0 or 1"


# ---------------------------------------------------------------------------
# 7. Match with < 10 points is skipped (not in output)
# ---------------------------------------------------------------------------

def test_short_match_skipped(csv_path):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    short_mid = "20230101-M-Test-F-Short_Player-Other_Player"
    ids_in_output = {r["match_id"] for r in rows}
    assert short_mid not in ids_in_output, (
        "Short match should have been skipped but appears in output"
    )


# ---------------------------------------------------------------------------
# 8. If LiveMatch raises on one match, others still complete
# ---------------------------------------------------------------------------

def test_error_isolation(synthetic_db, tmp_path):
    """
    Inject a p0_lookup_fn that raises for the first match but succeeds
    for the second.  The second match's rows should still appear in output.
    """
    out_dir = str(tmp_path / "isolated_out")
    b = Backtester(db_path=synthetic_db, output_dir=out_dir)

    calls = {"n": 0}
    mid1 = "20230101-M-Test-F-Player_One-Player_Two"

    def flaky_lookup(player_name: str, surface: str) -> float:
        calls["n"] += 1
        if calls["n"] <= 1:   # first match's first lookup — raises; error propagates before 2nd call
            raise RuntimeError("Simulated lookup failure for first match")
        return 0.65

    out = b.run(tour="atp", p0_lookup_fn=flaky_lookup)

    with open(out, newline="") as f:
        rows = list(csv.DictReader(f))

    ids_in_output = {r["match_id"] for r in rows}
    # The first match should have been skipped
    assert mid1 not in ids_in_output, "First match should be skipped on error"
    # At least one other match should still appear
    assert len(ids_in_output - {mid1}) >= 1, "Other matches should still run"
