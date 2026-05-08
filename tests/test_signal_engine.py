"""Tests for the signal engine and its output in core.ml_game_level.

Tests 1-7, 12 query the persisted core.ml_game_level (skip if missing).
Tests 8-11 drive SignalEngine directly with synthetic point streams.
Test 13 reads the build-report runtime line.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import duckdb
import pytest

from src.signal_engine import (
    SIGNAL_COLUMNS,
    SignalEngine,
)

DB_PATH = "data/processed/tennis.duckdb"
REPORT_PATH = "data/processed/signals_build_report.txt"
TABLE = "core.ml_game_level"
EXPECTED_ROW_COUNT = 120_718
EXPECTED_TOTAL_COLS = 80
EXPECTED_SIGNAL_COLS = 52
ORIGINAL_COL_COUNT = 28


@dataclass
class Pt:
    """Synthetic point for SignalEngine tests."""
    set_number: int
    game_number_in_set: int
    Pt: int
    score_before: str
    Svr: int
    PtWinner: int
    is_tiebreak: bool = False


@pytest.fixture(scope="module")
def con():
    if not os.path.exists(DB_PATH):
        pytest.skip(f"DuckDB not found at {DB_PATH}")
    c = duckdb.connect(DB_PATH, read_only=True)
    has_signals = c.execute(f"""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema='core' AND table_name='ml_game_level'
          AND column_name='bpi_bp_rate_ws_a'
    """).fetchone()[0]
    if not has_signals:
        c.close()
        pytest.skip("signal columns not built yet — run scripts/dev/build_signals.py")
    yield c
    c.close()


# ────────── 1. column count ──────────
def test_column_count(con):
    n = con.execute(f"""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema='core' AND table_name='ml_game_level'
    """).fetchone()[0]
    assert n == EXPECTED_TOTAL_COLS, (
        f"expected {EXPECTED_TOTAL_COLS} columns "
        f"(= {ORIGINAL_COL_COUNT} original + {EXPECTED_SIGNAL_COLS} new), got {n}"
    )


# ────────── 2. column names ──────────
def test_column_names(con):
    cols = {r[0] for r in con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='core' AND table_name='ml_game_level'
    """).fetchall()}
    missing = [c for c in SIGNAL_COLUMNS if c not in cols]
    assert not missing, f"missing signal columns: {missing[:10]} ({len(missing)} total)"
    assert len(SIGNAL_COLUMNS) == EXPECTED_SIGNAL_COLS


# ────────── 3. row count unchanged ──────────
def test_row_count_unchanged(con):
    n = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    assert n == EXPECTED_ROW_COUNT, (
        f"expected {EXPECTED_ROW_COUNT} rows, got {n}"
    )


# ────────── 4. set 1 ws == cm ──────────
def test_set1_ws_equals_cm(con):
    """In set 1 every ws sub-component must equal its cm counterpart exactly."""
    pairs = []
    for c in SIGNAL_COLUMNS:
        if "_ws_" in c:
            cm = c.replace("_ws_", "_cm_", 1)
            pairs.append((c, cm))
    diffs = ", ".join(
        f'SUM(CASE WHEN ABS("{w}" - "{m}") > 1e-9 THEN 1 ELSE 0 END) AS d_{i}'
        for i, (w, m) in enumerate(pairs)
    )
    row = con.execute(f"""
        SELECT {diffs} FROM {TABLE} WHERE set_number = 1
    """).fetchone()
    bad = [(pairs[i][0], pairs[i][1], n) for i, n in enumerate(row) if n]
    assert not bad, (
        f"in set 1 the following ws/cm pairs differ on at least one row: {bad[:5]}"
    )


# ────────── 5. no unexpected nulls ──────────
def test_no_unexpected_nulls(con):
    """Every signal column must be non-null on every row that has matching points."""
    n_unmatched = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE} g
        WHERE NOT EXISTS (
            SELECT 1 FROM core.atp_points_enhanced ape
            WHERE ape.match_id = g.match_id_string
        )
    """).fetchone()[0]
    parts = ", ".join(
        f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END) AS "{c}"'
        for c in SIGNAL_COLUMNS
    )
    row = con.execute(f"""
        SELECT {parts} FROM {TABLE}
        WHERE match_id_string IN (
            SELECT DISTINCT match_id FROM core.atp_points_enhanced
        )
    """).fetchone()
    nulls = {c: n for c, n in zip(SIGNAL_COLUMNS, row) if n}
    assert not nulls, (
        f"signal columns with NULL on points-backed rows: {nulls} "
        f"(unmatched ml rows = {n_unmatched})"
    )


# ────────── 6. tiebreak freezes BPI / SDS / RES ──────────
def _frozen_signal_cols() -> list[str]:
    return [c for c in SIGNAL_COLUMNS if c.split("_")[0] in ("bpi", "sds", "res")]


def _moving_signal_cols() -> list[str]:
    return [c for c in SIGNAL_COLUMNS if c.split("_")[0] in ("cpi", "mrs")]


def test_tiebreak_freeze_bpi(con):
    """For tiebreak rows BPI/SDS/RES must equal the values on game 12 of the same set."""
    sample = con.execute(f"""
        SELECT match_id_int, set_number, game_number_in_set
        FROM {TABLE}
        WHERE is_tiebreak = TRUE AND game_number_in_set = 13
        ORDER BY match_id_int, set_number
        LIMIT 5
    """).fetchall()
    assert len(sample) >= 5, f"need ≥5 tiebreak rows; found {len(sample)}"
    frozen = _frozen_signal_cols()
    for match_id_int, set_number, _ in sample:
        cols_sql = ", ".join(f'"{c}"' for c in frozen)
        tb_vals = con.execute(f"""
            SELECT {cols_sql} FROM {TABLE}
            WHERE match_id_int=? AND set_number=? AND game_number_in_set=13
        """, [match_id_int, set_number]).fetchone()
        g12_vals = con.execute(f"""
            SELECT {cols_sql} FROM {TABLE}
            WHERE match_id_int=? AND set_number=? AND game_number_in_set=12
        """, [match_id_int, set_number]).fetchone()
        assert g12_vals is not None, (
            f"missing game-12 row for match_id_int={match_id_int}, set={set_number}"
        )
        for col, v_tb, v_12 in zip(frozen, tb_vals, g12_vals):
            assert v_tb == pytest.approx(v_12, abs=1e-9), (
                f"BPI/SDS/RES not frozen on tiebreak row: "
                f"match_id_int={match_id_int} set={set_number} col={col} "
                f"tb={v_tb} g12={v_12}"
            )


# ────────── 7. tiebreak advances CPI / MRS ──────────
def test_tiebreak_advance_cpi_mrs(con):
    """For tiebreak rows at least one CPI/MRS value should differ from game 12."""
    sample = con.execute(f"""
        SELECT match_id_int, set_number, game_number_in_set
        FROM {TABLE}
        WHERE is_tiebreak = TRUE AND game_number_in_set = 13
        ORDER BY match_id_int, set_number
        LIMIT 5
    """).fetchall()
    assert len(sample) >= 5
    moving = _moving_signal_cols()
    cols_sql = ", ".join(f'"{c}"' for c in moving)
    for match_id_int, set_number, _ in sample:
        tb_vals = con.execute(f"""
            SELECT {cols_sql} FROM {TABLE}
            WHERE match_id_int=? AND set_number=? AND game_number_in_set=13
        """, [match_id_int, set_number]).fetchone()
        g12_vals = con.execute(f"""
            SELECT {cols_sql} FROM {TABLE}
            WHERE match_id_int=? AND set_number=? AND game_number_in_set=12
        """, [match_id_int, set_number]).fetchone()
        assert any(
            abs((v_tb or 0) - (v_12 or 0)) > 1e-9
            for v_tb, v_12 in zip(tb_vals, g12_vals)
        ), (
            f"no CPI/MRS value advanced between game 12 and tiebreak for "
            f"match_id_int={match_id_int} set={set_number}"
        )


# ────────── 8. smoothing applied to BPI ──────────
def test_smoothing_applied():
    """Synthetic single game: A holds after exactly one BP at 30-40.

    Sequence: 0-0, 15-0, 30-0, 30-15, 30-30, 30-40 (BP +1, deuce_reached=False
    yet), 40-40 (deuce reached, BP saved), AD-40, game.

    After this game player B's accumulators: return_games_played=1,
    bp_states_faced=1. Smoothed rate = 1 / (1 + 2) = 1/3.  Without smoothing
    the rate would be 1 / 1 = 1.0, so the 1/3 result proves the +2 is in
    the denominator.
    """
    pts = [
        Pt(1, 1, 1, "0-0",   1, 1),  # → 15-0
        Pt(1, 1, 2, "15-0",  1, 1),  # → 30-0
        Pt(1, 1, 3, "30-0",  1, 2),  # → 30-15
        Pt(1, 1, 4, "30-15", 1, 2),  # → 30-30
        Pt(1, 1, 5, "30-30", 1, 2),  # → 30-40 (BP +1; pressure point)
        Pt(1, 1, 6, "30-40", 1, 1),  # → 40-40 (deuce reached, BP saved)
        Pt(1, 1, 7, "40-40", 1, 1),  # → AD-40
        Pt(1, 1, 8, "AD-40", 1, 1),  # game holds
    ]
    rows = SignalEngine().process_match(42, iter(pts))
    assert len(rows) == 1
    r = rows[0]
    # Smoothed denominator is 1 + 2 = 3.
    assert r["bpi_bp_rate_ws_b"] == pytest.approx(1 / 3, abs=1e-12)
    assert r["bpi_bp_rate_cm_b"] == pytest.approx(1 / 3, abs=1e-12)
    # 30-40 is a BP but not deep (only 0-40, 15-40 are deep).
    assert r["bpi_deep_pressure_rate_ws_b"] == 0.0
    # Deuce was reached, but a BP state also reached, so NOT near pressure.
    assert r["bpi_near_pressure_rate_ws_b"] == 0.0
    # Player A had zero return games this match: 0 / 2 with smoothing = 0.
    assert r["bpi_bp_rate_ws_a"] == 0.0
    # Without smoothing the rate would be 1.0 — confirm we're well under that.
    assert r["bpi_bp_rate_ws_b"] < 0.5


# ────────── 9. pressure-point definition ──────────
def test_pressure_point_definition():
    """Synthetic match with known pressure-point states; check CPI math."""
    # One game. Server = A (1). Scores: 0-0 (not pressure), 15-0 (no), 30-0 (no),
    # 30-15 (no), 30-30 (PRESSURE — A wins), 40-30 (PRESSURE — A loses),
    # 40-40 (deuce; PRESSURE — A wins), AD-40 (PRESSURE; A loses),
    # 40-40 (PRESSURE; A wins), AD-40 (PRESSURE; A wins → game).
    pts = [
        Pt(1, 1, 1,  "0-0",   1, 1),
        Pt(1, 1, 2,  "15-0",  1, 1),
        Pt(1, 1, 3,  "30-0",  1, 2),
        Pt(1, 1, 4,  "30-15", 1, 2),
        Pt(1, 1, 5,  "30-30", 1, 1),
        Pt(1, 1, 6,  "40-30", 1, 2),
        Pt(1, 1, 7,  "40-40", 1, 1),
        Pt(1, 1, 8,  "AD-40", 1, 2),
        Pt(1, 1, 9,  "40-40", 1, 1),
        Pt(1, 1, 10, "AD-40", 1, 1),
    ]
    rows = SignalEngine().process_match(99, iter(pts))
    r = rows[0]
    # Pressure-point points (filtering against PRESSURE_STATES set):
    #   30-30 (A served, A won)        → A-serve-press: played 1, won 1
    #   40-30 (A served, A lost)       → A-serve-press: played 2, won 1
    #   40-40 (A served, A won)        → A-serve-press: played 3, won 2
    #   AD-40 (A served, A lost)       → A-serve-press: played 4, won 2
    #   40-40 (A served, A won)        → A-serve-press: played 5, won 3
    #   AD-40 (A served, A won)        → A-serve-press: played 6, won 4
    # cpi_serve_pressure_pct_ws_a = 4 / 6
    assert r["cpi_serve_pressure_pct_ws_a"] == pytest.approx(4 / 6, abs=1e-12)
    # B is the returner on each pressure point.
    # B-return-press: played 6, won = times B won at a pressure state = 2.
    # cpi_return_pressure_pct_ws_b = 2 / 6
    assert r["cpi_return_pressure_pct_ws_b"] == pytest.approx(2 / 6, abs=1e-12)
    # A never returned, B never served — both should be 0.
    assert r["cpi_return_pressure_pct_ws_a"] == 0.0
    assert r["cpi_serve_pressure_pct_ws_b"] == 0.0


# ────────── 10. game streak resets ──────────
def test_game_streak_resets():
    """Player wins 3 games then loses 1; streak should go 1, 2, 3, 0."""
    # Helper: build a single-server hold that does not touch deuce.
    def hold_pts(set_n: int, game_n: int, base_pt: int, server: int):
        # 0-0, 15-0, 30-0, 40-0 — server wins all 4.
        return [
            Pt(set_n, game_n, base_pt + 0, "0-0",  server, server),
            Pt(set_n, game_n, base_pt + 1, "15-0", server, server),
            Pt(set_n, game_n, base_pt + 2, "30-0", server, server),
            Pt(set_n, game_n, base_pt + 3, "40-0", server, server),
        ]

    def break_pts(set_n: int, game_n: int, base_pt: int, server: int):
        # 0-0, 0-15, 0-30, 0-40 (deep BP), 1-40 = 15-40 (deep), 30-40 (BP),
        # break converts: returner wins next.
        # Simpler — just have returner win all 4: 0-0, 0-15, 0-30, 0-40
        # then on next point at 0-40 returner wins the break.
        ret = 1 if server == 2 else 2
        return [
            Pt(set_n, game_n, base_pt + 0, "0-0",   server, ret),
            Pt(set_n, game_n, base_pt + 1, "0-15",  server, ret),
            Pt(set_n, game_n, base_pt + 2, "0-30",  server, ret),
            Pt(set_n, game_n, base_pt + 3, "0-40",  server, ret),
        ]

    pts: list[Pt] = []
    pts += hold_pts(1, 1, 1,  server=1)   # A holds          → A streak 1, B 0
    pts += break_pts(1, 2, 5, server=2)   # A breaks B       → A streak 2
    pts += hold_pts(1, 3, 9,  server=1)   # A holds          → A streak 3
    pts += hold_pts(1, 4, 13, server=2)   # B holds          → A streak 0, B 1

    rows = SignalEngine().process_match(7, iter(pts))
    assert len(rows) == 4
    assert rows[0]["mrs_game_streak_ws_a"] == 1
    assert rows[0]["mrs_game_streak_ws_b"] == 0
    assert rows[1]["mrs_game_streak_ws_a"] == 2
    assert rows[1]["mrs_game_streak_ws_b"] == 0
    assert rows[2]["mrs_game_streak_ws_a"] == 3
    assert rows[2]["mrs_game_streak_ws_b"] == 0
    assert rows[3]["mrs_game_streak_ws_a"] == 0
    assert rows[3]["mrs_game_streak_ws_b"] == 1


# ────────── 11. pwr_10 sliding-window ──────────
def test_pwr10_window():
    """Construct >10 points in a single set; pwr_10 must reflect only the last 10."""
    # 12 points total. First 2 are A wins, last 10 are B wins.
    # Game 1 ends at point 4 (A serves, all 4 won by A): mid-game A-A-A-A.
    # Wait we need a clean break. Simplest: stitch together short games.
    pts = [
        # Game 1 — A serves, A wins all 4 (points 1-4): A,A,A,A
        Pt(1, 1, 1, "0-0",  1, 1),
        Pt(1, 1, 2, "15-0", 1, 1),
        Pt(1, 1, 3, "30-0", 1, 1),
        Pt(1, 1, 4, "40-0", 1, 1),
        # Game 2 — B serves, A wins all 4 (5-8): A,A,A,A  (break)
        Pt(1, 2, 5, "0-0",  2, 1),
        Pt(1, 2, 6, "0-15", 2, 1),
        Pt(1, 2, 7, "0-30", 2, 1),
        Pt(1, 2, 8, "0-40", 2, 1),
        # Game 3 — A serves, B wins all 4 (9-12): B,B,B,B  (break-back)
        Pt(1, 3, 9,  "0-0",  1, 2),
        Pt(1, 3, 10, "0-15", 1, 2),
        Pt(1, 3, 11, "0-30", 1, 2),
        Pt(1, 3, 12, "0-40", 1, 2),
    ]
    rows = SignalEngine().process_match(13, iter(pts))
    assert len(rows) == 3
    # End-of-game-1 deque has 4 entries (all A): pwr_10_ws_a = 4/4 = 1.0
    assert rows[0]["mrs_pwr_10_ws_a"] == pytest.approx(1.0)
    # End-of-game-2 deque has 8 entries (all A): pwr_10_ws_a = 8/8 = 1.0
    assert rows[1]["mrs_pwr_10_ws_a"] == pytest.approx(1.0)
    # End-of-game-3 deque has 12 entries; last 10 = positions 3..12 inclusive.
    # Positions 3-4 of game 1 are A; 5-8 of game 2 are A; 9-12 of game 3 are B.
    # So last 10 = [A,A,A,A,A,A,B,B,B,B] → A-fraction 6/10 = 0.6.
    assert rows[2]["mrs_pwr_10_ws_a"] == pytest.approx(0.6, abs=1e-12)
    assert rows[2]["mrs_pwr_10_ws_b"] == pytest.approx(0.4, abs=1e-12)
    # mrs_pwr_30 looks at all 12 points: A=8/12, B=4/12.
    assert rows[2]["mrs_pwr_30_ws_a"] == pytest.approx(8 / 12, abs=1e-12)
    assert rows[2]["mrs_pwr_30_ws_b"] == pytest.approx(4 / 12, abs=1e-12)


# ────────── 12. carryover ws resets, cm doesn't ──────────
def test_carryover_ws_resets():
    """Multi-set match: ws values at game 1 of set 2 reflect only set 2 points."""
    def hold_pts(set_n: int, game_n: int, base_pt: int, server: int):
        return [
            Pt(set_n, game_n, base_pt + 0, "0-0",  server, server),
            Pt(set_n, game_n, base_pt + 1, "15-0", server, server),
            Pt(set_n, game_n, base_pt + 2, "30-0", server, server),
            Pt(set_n, game_n, base_pt + 3, "40-0", server, server),
        ]

    pts: list[Pt] = []
    # Set 1, game 1: A holds (4 pts).
    pts += hold_pts(1, 1, 1, server=1)
    # Set 2, game 1: B holds (4 pts).
    pts += hold_pts(2, 1, 5, server=2)

    rows = SignalEngine().process_match(101, iter(pts))
    assert len(rows) == 2

    # Row for (set=2, game=1):
    r2 = rows[1]
    # ws view for set 2: only B's serve has happened. A has zero serve points.
    assert r2["sds_serve_win_pct_ws_a"] == 0.0
    assert r2["sds_serve_win_pct_ws_b"] == pytest.approx(1.0)
    # cm view: across both sets, both A and B have served exactly 4 points
    # and won them all → both 1.0.
    assert r2["sds_serve_win_pct_cm_a"] == pytest.approx(1.0)
    assert r2["sds_serve_win_pct_cm_b"] == pytest.approx(1.0)


# ────────── 13. total runtime acceptable ──────────
def test_total_runtime_acceptable():
    """Build report's 'Total runtime' line must be under 15 minutes."""
    if not os.path.exists(REPORT_PATH):
        pytest.skip(f"build report not present at {REPORT_PATH}")
    with open(REPORT_PATH) as f:
        text = f.read()
    m = re.search(r"Total runtime:\s+([\d.]+)s", text)
    assert m, "could not find 'Total runtime: Xs' line in build report"
    runtime_sec = float(m.group(1))
    assert runtime_sec < 15 * 60, (
        f"build runtime {runtime_sec:.1f}s exceeds 15-minute sanity ceiling"
    )
