"""Tests for ``src.streaming_signal_engine.StreamingMatchState``.

API-contract tests verify the wrapper's lifecycle (finalize / idempotency
/ post-finalize guard / single-game emission).  Boundary and tiebreak
tests use synthetic point streams.  The two equivalence tests (one
synthetic, one DB-sampled) are the linchpin: they assert that, given
the same point sequence, ``StreamingMatchState`` produces dicts equal
to ``SignalEngine.process_match`` under strict ``==``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields

import duckdb
import pytest

from src.signal_engine import SIGNAL_COLUMNS, SignalEngine
from src.streaming_signal_engine import StreamingMatchState

DB_PATH = "data/processed/tennis.duckdb"


@dataclass
class Pt:
    """Synthetic point for SignalEngine / StreamingMatchState tests."""
    set_number: int
    game_number_in_set: int
    Pt: int
    score_before: str
    Svr: int
    PtWinner: int
    is_tiebreak: bool = False


@pytest.fixture(scope="module")
def con():
    """Module-scoped DuckDB connection; skip cleanly if not available."""
    if not os.path.exists(DB_PATH):
        pytest.skip(f"DuckDB not found at {DB_PATH}")
    c = duckdb.connect(DB_PATH, read_only=True)
    has_points = c.execute("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema='core' AND table_name='atp_points_enhanced'
    """).fetchone()[0]
    if not has_points:
        c.close()
        pytest.skip("core.atp_points_enhanced not present")
    yield c
    c.close()


# ──────────────── point-stream builders ────────────────

def _hold_at_love(set_n: int, game_n: int, base_pt: int, server: int) -> list[Pt]:
    """Server wins all four points: 0-0, 15-0, 30-0, 40-0."""
    return [
        Pt(set_n, game_n, base_pt + 0, "0-0", server, server),
        Pt(set_n, game_n, base_pt + 1, "15-0", server, server),
        Pt(set_n, game_n, base_pt + 2, "30-0", server, server),
        Pt(set_n, game_n, base_pt + 3, "40-0", server, server),
    ]


def _full_set_then_tiebreak() -> list[Pt]:
    """12 alternating love-holds (no BPs) then a 7-0 tiebreak won by player 1."""
    pts: list[Pt] = []
    base = 0
    for i in range(12):
        game_n = i + 1
        server = 1 if game_n % 2 == 1 else 2
        pts.extend(_hold_at_love(1, game_n, base + 1, server))
        base += 4
    # Tiebreak (game 13): player 1 wins 7-0. Server identity is irrelevant
    # to _close_game for tiebreaks; we set Svr=1 throughout for simplicity.
    for i in range(7):
        score = f"{i}-0"
        pts.append(Pt(1, 13, base + 1, score, 1, 1, is_tiebreak=True))
        base += 1
    return pts


def _stream_all(pts: list[Pt], match_id_int: int) -> list[dict]:
    """Feed every point and the finalize() flush, return emitted rows in order."""
    state = StreamingMatchState(match_id_int)
    rows: list[dict] = []
    for pt in pts:
        r = state.process_point(pt)
        if r is not None:
            rows.append(r)
    final = state.finalize()
    if final is not None:
        rows.append(final)
    return rows


# ──────────────── 1. empty match ────────────────
def test_empty_match_finalize_returns_none():
    state = StreamingMatchState(42)
    assert state.finalize() is None


# ──────────────── 2. finalize idempotent ────────────────
def test_finalize_idempotent():
    state = StreamingMatchState(42)
    state.process_point(Pt(1, 1, 1, "0-0", 1, 1))
    first = state.finalize()
    assert first is not None
    assert state.finalize() is None


# ──────────────── 3. process_point after finalize raises ────────────────
def test_process_point_after_finalize_raises():
    state = StreamingMatchState(42)
    state.process_point(Pt(1, 1, 1, "0-0", 1, 1))
    state.finalize()
    with pytest.raises(RuntimeError):
        state.process_point(Pt(1, 1, 2, "15-0", 1, 1))


# ──────────────── 4. single game via finalize ────────────────
def test_single_game_via_finalize():
    state = StreamingMatchState(42)
    pts = _hold_at_love(set_n=1, game_n=1, base_pt=1, server=1)
    for pt in pts:
        assert state.process_point(pt) is None
    row = state.finalize()
    assert row is not None
    assert row["set_number"] == 1
    assert row["game_number_in_set"] == 1
    for col in SIGNAL_COLUMNS:
        assert col in row, f"missing signal column {col!r}"


# ──────────────── 5. emit at game boundary ────────────────
def test_emit_at_game_boundary():
    """Rows are emitted only when the first point of the next game arrives."""
    state = StreamingMatchState(42)
    game1 = _hold_at_love(set_n=1, game_n=1, base_pt=1, server=1)
    first_of_game2 = Pt(1, 2, 5, "0-0", 2, 2)

    for pt in game1:
        assert state.process_point(pt) is None, (
            f"unexpected emit mid-game-1 on Pt={pt.Pt}"
        )

    row = state.process_point(first_of_game2)
    assert row is not None
    assert row["set_number"] == 1
    assert row["game_number_in_set"] == 1, (
        "emitted row should describe the just-closed game (game 1), "
        f"not the new one — got game_number_in_set={row['game_number_in_set']}"
    )


# ──────────────── 6. set boundary resets ws, preserves cm ────────────────
def test_set_boundary_resets_ws_preserves_cm():
    """Crossing a set boundary clears acc['ws'] / mrs_deque['ws']; cm carries."""
    state = StreamingMatchState(7)

    # Set 1: 6 alternating love-holds (server 1 odd games, server 2 even).
    base = 0
    for i in range(6):
        game_n = i + 1
        server = 1 if game_n % 2 == 1 else 2
        for pt in _hold_at_love(1, game_n, base + 1, server):
            state.process_point(pt)
        base += 4

    # First point of set 2 — boundary fires: game 6 row is emitted, then
    # ws is reset before the per-point update is applied.
    first_set2 = Pt(2, 1, base + 1, "0-0", 1, 1)
    emitted = state.process_point(first_set2)
    assert emitted is not None
    assert emitted["set_number"] == 1 and emitted["game_number_in_set"] == 6

    # The fields below are only mutated at game-close (inside _close_game),
    # so after the ws reset + this single in-flight point they MUST be zero
    # for both players. Per-point fields (serve_pts_played etc.) may carry
    # the one point of evidence we just applied and are irrelevant to the
    # "ws was reset" check.
    GAME_CLOSE_FIELDS = (
        "service_games_played", "service_games_held", "service_game_total_points",
        "return_games_played", "breaks_won_with_bp", "bp_states_faced",
        "deep_pressure_games", "near_pressure_games", "game_streak",
    )
    for ver_player in (state.acc["ws"][1], state.acc["ws"][2]):
        for fname in GAME_CLOSE_FIELDS:
            assert getattr(ver_player, fname) == 0, (
                f"ws field {fname} should be 0 after set-boundary reset, "
                f"got {getattr(ver_player, fname)}"
            )

    # cm preserves set-1 totals — at least one player has served games in cm.
    assert (
        state.acc["cm"][1].service_games_played > 0
        or state.acc["cm"][2].service_games_played > 0
    ), "cm should retain set-1 service-game counts across the set boundary"

    # ws mrs_deque was also reset: after exactly one set-2 point it holds
    # exactly that one point. cm carries all set-1 points plus the one set-2
    # point.
    assert len(state.mrs_deque["ws"]) == 1
    assert len(state.mrs_deque["cm"]) > 1


# ──────────────── 7. tiebreak freezes BPI / SDS / RES ────────────────
def _bpi_sds_res_cols() -> list[str]:
    return [c for c in SIGNAL_COLUMNS if c.split("_")[0] in ("bpi", "sds", "res")]


def _cpi_mrs_cols() -> list[str]:
    return [c for c in SIGNAL_COLUMNS if c.split("_")[0] in ("cpi", "mrs")]


def test_tiebreak_freezes_bpi_sds_res():
    """The tiebreak row's BPI/SDS/RES values must match the last normal game's."""
    pts = _full_set_then_tiebreak()
    rows = _stream_all(pts, match_id_int=55)
    by_game = {(r["set_number"], r["game_number_in_set"]): r for r in rows}

    assert (1, 12) in by_game, "expected a row for the last normal game (game 12)"
    assert (1, 13) in by_game, "expected a row for the tiebreak game (game 13)"
    normal = by_game[(1, 12)]
    tiebreak = by_game[(1, 13)]

    for col in _bpi_sds_res_cols():
        assert tiebreak[col] == normal[col], (
            f"BPI/SDS/RES column {col} was not frozen across the tiebreak: "
            f"last-normal={normal[col]} tiebreak={tiebreak[col]}"
        )


# ──────────────── 8. tiebreak advances MRS ────────────────
def test_tiebreak_advances_mrs():
    """The tiebreak row's CPI/MRS values must differ from the last normal game's."""
    pts = _full_set_then_tiebreak()
    rows = _stream_all(pts, match_id_int=55)
    by_game = {(r["set_number"], r["game_number_in_set"]): r for r in rows}
    normal = by_game[(1, 12)]
    tiebreak = by_game[(1, 13)]

    diffs = [col for col in _cpi_mrs_cols() if tiebreak[col] != normal[col]]
    assert diffs, (
        "no CPI/MRS column advanced between the last normal game and the "
        "tiebreak row — MRS deque/game_streak should at minimum have moved"
    )


# ──────────────── 9. synthetic equivalence ────────────────
def test_equivalence_on_synthetic_match():
    """A hand-crafted 4-game match must produce identical batch and streaming rows."""
    # Server is encoded server-returner in score_before. Player 1 = A, Player 2 = B.
    pts: list[Pt] = []

    # Game 1: A serves, A holds at love.
    pts += [
        Pt(1, 1, 1, "0-0",  1, 1),
        Pt(1, 1, 2, "15-0", 1, 1),
        Pt(1, 1, 3, "30-0", 1, 1),
        Pt(1, 1, 4, "40-0", 1, 1),
    ]

    # Game 2: B serves, B holds via deuce after facing one BP.
    # score_before is server-returner (B first), so a BP for A reads "x-40".
    pts += [
        Pt(1, 2, 5,  "0-0",   2, 1),  # → 0-15
        Pt(1, 2, 6,  "0-15",  2, 2),  # → 15-15
        Pt(1, 2, 7,  "15-15", 2, 1),  # → 15-30
        Pt(1, 2, 8,  "15-30", 2, 2),  # → 30-30 (pressure)
        Pt(1, 2, 9,  "30-30", 2, 1),  # → 30-40 (BP for A, pressure)
        Pt(1, 2, 10, "30-40", 2, 2),  # → 40-40 (deuce, pressure, BP saved)
        Pt(1, 2, 11, "40-40", 2, 2),  # → AD-40 (pressure)
        Pt(1, 2, 12, "AD-40", 2, 2),  # game holds
    ]

    # Game 3: A serves, B converts a break from 0-40.
    pts += [
        Pt(1, 3, 13, "0-0",   1, 2),  # → 0-15
        Pt(1, 3, 14, "0-15",  1, 2),  # → 0-30
        Pt(1, 3, 15, "0-30",  1, 2),  # → 0-40 (deep BP, pressure)
        Pt(1, 3, 16, "0-40",  1, 2),  # break converted
    ]

    # Game 4: B serves, B holds reaching deuce without ever facing a BP
    # (i.e. near-pressure but never BP). Score_before is B-A.
    pts += [
        Pt(1, 4, 17, "0-0",   2, 2),  # → 15-0
        Pt(1, 4, 18, "15-0",  2, 1),  # → 15-15
        Pt(1, 4, 19, "15-15", 2, 2),  # → 30-15
        Pt(1, 4, 20, "30-15", 2, 1),  # → 30-30 (pressure)
        Pt(1, 4, 21, "30-30", 2, 2),  # → 40-30 (pressure)
        Pt(1, 4, 22, "40-30", 2, 1),  # → 40-40 (deuce, pressure)
        Pt(1, 4, 23, "40-40", 2, 2),  # → AD-40 (pressure)
        Pt(1, 4, 24, "AD-40", 2, 2),  # game holds without ever a BP
    ]

    batch_rows = SignalEngine().process_match(99, iter(pts))
    stream_rows = _stream_all(pts, match_id_int=99)

    assert stream_rows == batch_rows, (
        f"streaming rows differ from batch rows. "
        f"batch_len={len(batch_rows)} stream_len={len(stream_rows)}"
    )


# ──────────────── 10. DB-sampled equivalence ────────────────
def test_equivalence_on_db_sampled_matches(con):
    """For 10 real matches, streaming and batch rows must be strictly equal."""
    # Pick 10 match_id_int values. atp_points_enhanced keys on match_id (varchar);
    # match_id_int comes from core.match_id_map. Mirror build_signals.load_points.
    sample_ids = [r[0] for r in con.execute("""
        SELECT DISTINCT mim.match_id_int
        FROM core.atp_points_enhanced ape
        JOIN core.match_id_map mim ON mim.match_id_string = ape.match_id
        WHERE ape.match_date BETWEEN DATE '2015-01-01' AND DATE '2025-12-31'
          AND ape.player1_name IS NOT NULL AND ape.player2_name IS NOT NULL
          AND ape.Set1 IS NOT NULL AND ape.Set2 IS NOT NULL
          AND ape.Gm1  IS NOT NULL AND ape.Gm2  IS NOT NULL
          AND ape.Svr  IS NOT NULL AND ape.PtWinner IS NOT NULL
        ORDER BY mim.match_id_int
        LIMIT 10
    """).fetchall()]
    if not sample_ids:
        pytest.skip("no joinable matches in the configured date window")

    # Fetch all in-window points for the sampled matches in one query, then
    # group by match in Python (cheaper than 10 round-trips).
    rows = con.execute("""
        SELECT
            mim.match_id_int,
            (ape.Set1 + ape.Set2 + 1) AS set_number,
            (ape.Gm1 + ape.Gm2 + 1)   AS game_number_in_set,
            ape.Pt,
            ape.score_before,
            CAST(ape.Svr AS INTEGER)      AS Svr,
            CAST(ape.PtWinner AS INTEGER) AS PtWinner,
            ape.is_tiebreak
        FROM core.atp_points_enhanced ape
        JOIN core.match_id_map mim ON mim.match_id_string = ape.match_id
        WHERE mim.match_id_int IN ({})
          AND ape.match_date BETWEEN DATE '2015-01-01' AND DATE '2025-12-31'
          AND ape.player1_name IS NOT NULL AND ape.player2_name IS NOT NULL
          AND ape.Set1 IS NOT NULL AND ape.Set2 IS NOT NULL
          AND ape.Gm1  IS NOT NULL AND ape.Gm2  IS NOT NULL
          AND ape.Svr  IS NOT NULL AND ape.PtWinner IS NOT NULL
        ORDER BY mim.match_id_int, ape.Pt
    """.format(",".join(str(i) for i in sample_ids))).fetchall()

    by_match: dict[int, list[Pt]] = {}
    for r in rows:
        by_match.setdefault(r[0], []).append(Pt(
            set_number=int(r[1]),
            game_number_in_set=int(r[2]),
            Pt=int(r[3]),
            score_before=r[4],
            Svr=int(r[5]),
            PtWinner=int(r[6]),
            is_tiebreak=bool(r[7]),
        ))

    for mid in sample_ids:
        pts = by_match.get(mid, [])
        if not pts:
            continue
        batch_rows = SignalEngine().process_match(mid, iter(pts))
        stream_rows = _stream_all(pts, match_id_int=mid)
        if batch_rows != stream_rows:
            # Find the first divergence and surface it precisely.
            first_diff = None
            for i, (b, s) in enumerate(zip(batch_rows, stream_rows)):
                if b != s:
                    diff_keys = [k for k in b if b[k] != s.get(k)]
                    first_diff = (i, diff_keys[:5])
                    break
            extra = (
                f" first differing row idx={first_diff[0]} "
                f"diff_keys={first_diff[1]}"
                if first_diff is not None
                else f" len(batch)={len(batch_rows)} len(stream)={len(stream_rows)}"
            )
            pytest.fail(
                f"streaming/batch divergence on match_id_int={mid}.{extra}"
            )
