"""Tests for :class:`LivePredictionService` and the private ``_ScoreState``.

Group A exercises ``_ScoreState`` in isolation (no model, no DB).
Groups B-E exercise the service under construction-time and runtime
contracts using an explicit ``p0_lookup`` so the DB is not required.
Group F is the end-to-end feature-equivalence test: feed real points
through the service and assert that ``Prediction.features`` matches
``core.ml_game_level`` row-for-row under strict numerical tolerance.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock

import duckdb
import pytest

from src import markov_engine
from src.live_prediction_service import (
    LivePredictionService,
    Prediction,
    _ScoreState,
)
from src.signal_engine import SIGNAL_COLUMNS

DB_PATH = "data/processed/tennis.duckdb"
MODEL_PATH = "data/processed/model_v1.joblib"
METADATA_PATH = "data/processed/model_v1_metadata.json"


@dataclass
class Pt:
    """Synthetic point for service / score-state tests."""
    set_number: int
    game_number_in_set: int
    Pt: int
    score_before: str
    Svr: int
    PtWinner: int
    is_tiebreak: bool = False


# ──────────────── fixtures ────────────────

@pytest.fixture(autouse=True)
def _no_default_prediction_logger(monkeypatch):
    """Default to no DB-backed PredictionLogger in tests.

    Patches get_default_logger so LivePredictionService's auto-resolution
    returns None, keeping these tests hermetic even if DATABASE_URL is
    set in the local environment. Tests that exercise singleton logic
    override this patch explicitly.
    """
    from src.prediction_logger import _reset_default_logger_for_testing
    _reset_default_logger_for_testing()
    monkeypatch.setattr(
        "src.live_prediction_service.get_default_logger",
        lambda: None,
    )
    yield
    _reset_default_logger_for_testing()


@pytest.fixture(scope="module")
def con():
    if not os.path.exists(DB_PATH):
        pytest.skip(f"DuckDB not found at {DB_PATH}")
    c = duckdb.connect(DB_PATH, read_only=True)
    has_p0 = c.execute("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema='core' AND table_name='player_p0'
    """).fetchone()[0]
    if not has_p0:
        c.close()
        pytest.skip("core.player_p0 not present")
    yield c
    c.close()


@pytest.fixture(scope="module")
def model_files():
    if not os.path.exists(MODEL_PATH):
        pytest.skip(f"model not found at {MODEL_PATH}")
    if not os.path.exists(METADATA_PATH):
        pytest.skip(f"metadata not found at {METADATA_PATH}")
    return {"model": MODEL_PATH, "metadata": METADATA_PATH}


def _make_minimal_service(p0_lookup: dict[str, float]) -> LivePredictionService:
    return LivePredictionService(
        model_path=MODEL_PATH,
        metadata_path=METADATA_PATH,
        p0_lookup=p0_lookup,
    )


# ──────────────── helpers for crafting point streams ────────────────

def _love_hold(set_n: int, game_n: int, base_pt: int, server: int) -> list[Pt]:
    return [
        Pt(set_n, game_n, base_pt + 0, "0-0",  server, server),
        Pt(set_n, game_n, base_pt + 1, "15-0", server, server),
        Pt(set_n, game_n, base_pt + 2, "30-0", server, server),
        Pt(set_n, game_n, base_pt + 3, "40-0", server, server),
    ]


def _love_break(set_n: int, game_n: int, base_pt: int, server: int) -> list[Pt]:
    """Returner wins all 4 points (love break)."""
    ret = 2 if server == 1 else 1
    return [
        Pt(set_n, game_n, base_pt + 0, "0-0",  server, ret),
        Pt(set_n, game_n, base_pt + 1, "0-15", server, ret),
        Pt(set_n, game_n, base_pt + 2, "0-30", server, ret),
        Pt(set_n, game_n, base_pt + 3, "0-40", server, ret),
    ]


# ═════════════════ Group A — _ScoreState contract ═════════════════

def test_scorestate_initial():
    s = _ScoreState()
    assert s.set_number == 1
    assert s.games_a == 0
    assert s.games_b == 0
    assert s.sets_won_a == 0
    assert s.sets_won_b == 0
    assert s.prev_set is None
    assert s.prev_game is None
    assert s.current_game_server is None
    assert s.current_game_pts_a == 0
    assert s.current_game_pts_b == 0
    assert s.finalized is False


def test_scorestate_first_point_sets_server():
    s = _ScoreState()
    out = s.observe_point(Pt(1, 1, 1, "0-0", 1, 1))
    assert out is None
    assert s.current_game_server == 1
    assert s.prev_set == 1
    assert s.prev_game == 1


def test_scorestate_mid_game_returns_none():
    s = _ScoreState()
    for pt in _love_hold(1, 1, 1, server=1):
        assert s.observe_point(pt) is None


def test_scorestate_emits_at_game_boundary():
    s = _ScoreState()
    for pt in _love_hold(1, 1, 1, server=1):
        s.observe_point(pt)
    snap = s.observe_point(Pt(1, 2, 5, "0-0", 2, 1))
    assert snap is not None
    assert snap["set_number"] == 1
    assert snap["game_number_in_set"] == 1
    assert snap["games_A"] == 1
    assert snap["games_B"] == 0
    assert snap["sets_won_A"] == 0
    assert snap["sets_won_B"] == 0
    assert snap["server_was_a"] is True


def test_scorestate_set_clincher_pre_set_sets_won():
    """A wins 6-3 in set 1: games_A=6 on the set-clincher row but sets_won_A=0."""
    s = _ScoreState()
    # Sequence: A wins games 1,2,3,4,5; B wins games 6,7,8; A wins game 9.
    # Each game is a love hold by the server (the winner serves throughout).
    base = 1
    winners = [1, 1, 1, 1, 1, 2, 2, 2, 1]  # game winner; server is the winner
    for g, w in enumerate(winners, start=1):
        for pt in _love_hold(1, g, base, server=w):
            s.observe_point(pt)
        base += 4
    snap = s.observe_point(Pt(2, 1, base, "0-0", 1, 1))
    assert snap is not None
    assert snap["set_number"] == 1
    assert snap["game_number_in_set"] == 9
    assert snap["games_A"] == 6
    assert snap["games_B"] == 3
    assert snap["sets_won_A"] == 0
    assert snap["sets_won_B"] == 0


def test_scorestate_first_game_of_next_set_increments_sets_won():
    """After A clinches set 1, sets_won_A bumps to 1 only on the next emit (set 2 game 1)."""
    s = _ScoreState()
    base = 1
    winners = [1, 1, 1, 1, 1, 2, 2, 2, 1]
    for g, w in enumerate(winners, start=1):
        for pt in _love_hold(1, g, base, server=w):
            s.observe_point(pt)
        base += 4
    # Set 2 game 1 — B serves (set serve flips), B wins love hold.
    for pt in _love_hold(2, 1, base, server=2):
        s.observe_point(pt)
    base += 4
    # First point of set 2 game 2 triggers the emit of set 2 game 1.
    snap = s.observe_point(Pt(2, 2, base, "0-0", 1, 1))
    assert snap is not None
    assert snap["set_number"] == 2
    assert snap["game_number_in_set"] == 1
    assert snap["games_A"] == 0
    assert snap["games_B"] == 1
    assert snap["sets_won_A"] == 1
    assert snap["sets_won_B"] == 0


def test_scorestate_tiebreak_emits_7_6():
    """A 6-6 set + a 7-3 tiebreak emits the tiebreak row as games_A=7, games_B=6.

    The tiebreak row's ``server_was_a`` must flip from the prior game's
    server (game 12 had Svr=2 → game 13's server_was_a is True).
    """
    s = _ScoreState()
    base = 1
    # Games 1-12: alternating love holds (game k served by 1 if odd else 2).
    for g in range(1, 13):
        server = 1 if g % 2 == 1 else 2
        for pt in _love_hold(1, g, base, server=server):
            s.observe_point(pt)
        base += 4
    # Tiebreak (game 13): 10 points, A wins 7-3.
    tb_winners = [1, 1, 1, 2, 1, 2, 1, 2, 1, 1]  # A: 7, B: 3
    for i, w in enumerate(tb_winners):
        s.observe_point(Pt(1, 13, base + i, f"{i}-0", 1, w, is_tiebreak=True))
    base += len(tb_winners)
    snap = s.observe_point(Pt(2, 1, base, "0-0", 2, 2))
    assert snap is not None
    assert snap["set_number"] == 1
    assert snap["game_number_in_set"] == 13
    assert snap["games_A"] == 7
    assert snap["games_B"] == 6
    assert snap["sets_won_A"] == 0
    assert snap["sets_won_B"] == 0
    assert snap["server_was_a"] is True


def test_scorestate_tiebreak_stuck_server_forced_to_flip():
    """book.points records the same Svr across all tiebreak points. The
    score-state must still flip the server_was_a on the tiebreak row to
    satisfy the §7b convention (and the service's serve-alternation
    invariant). Without the fix this regressed the smoke test on
    matches 16098945 and 16159253 (date 2026-05-11).
    """
    s = _ScoreState()
    base = 1
    # Games 1-12: alternating love holds; game 12 served by 2.
    for g in range(1, 13):
        server = 1 if g % 2 == 1 else 2
        for pt in _love_hold(1, g, base, server=server):
            s.observe_point(pt)
        base += 4
    # Tiebreak (game 13): every point Svr=2 (stuck), matching what
    # book.points produces. A wins 7-3.
    tb_winners = [1, 1, 1, 2, 1, 2, 1, 2, 1, 1]
    for i, w in enumerate(tb_winners):
        s.observe_point(Pt(1, 13, base + i, f"{i}-0", 2, w, is_tiebreak=True))
    base += len(tb_winners)
    snap = s.observe_point(Pt(2, 1, base, "0-0", 2, 2))
    assert snap is not None
    assert snap["game_number_in_set"] == 13
    # Game 12's server_was_a was False; tiebreak row must be True (flipped)
    # even though every tiebreak point reported Svr=2.
    assert snap["server_was_a"] is True


def test_scorestate_finalize_emits_last_game():
    s = _ScoreState()
    for pt in _love_hold(1, 1, 1, server=1):
        s.observe_point(pt)
    snap = s.finalize()
    assert snap is not None
    assert snap["set_number"] == 1
    assert snap["game_number_in_set"] == 1
    assert snap["games_A"] == 1
    assert snap["games_B"] == 0


def test_scorestate_finalize_idempotent():
    s = _ScoreState()
    for pt in _love_hold(1, 1, 1, server=1):
        s.observe_point(pt)
    assert s.finalize() is not None
    assert s.finalize() is None


def test_scorestate_finalize_no_points_returns_none():
    s = _ScoreState()
    assert s.finalize() is None


# ═════════════════ Group B — service construction ═════════════════

def test_init_loads_model_and_metadata(model_files):
    service = _make_minimal_service({"X": 0.6})
    assert service.model.n_features_in_ == 63
    assert len(service.feature_names) == 63
    assert tuple(service.model.classes_) == (0, 1)


def test_init_with_p0_lookup_skips_db(model_files):
    service = _make_minimal_service({"X": 0.6})
    assert service.p0_lookup == {"X": 0.6}


def test_init_loads_p0_from_db(con, model_files):
    service = LivePredictionService(
        model_path=MODEL_PATH,
        metadata_path=METADATA_PATH,
        db_path=DB_PATH,
    )
    assert len(service.p0_lookup) >= 600


# ═════════════════ Group C — start_match behaviour ═════════════════

def test_start_match_resolves_p0_exact_match(model_files):
    service = _make_minimal_service({"Foo": 0.65, "Bar": 0.60})
    service.start_match(1, "Foo", "Bar", "hard", True, player_a_id=1, player_b_id=2)
    assert service.p0_a == 0.65
    assert service.p0_b == 0.60


def test_start_match_resolves_p0_fuzzy_match(model_files):
    service = _make_minimal_service({"Roger Federer": 0.67, "Rafael Nadal": 0.66})
    service.start_match(
        1, "Roger Federerr", "Rafael Nadal", "hard", True,
        player_a_id=1, player_b_id=2,
    )
    assert service.p0_a == 0.67  # fuzzy hit ratio >= 90
    assert service.p0_b == 0.66


def test_start_match_p0_league_average_fallback(model_files):
    service = LivePredictionService(
        model_path=MODEL_PATH,
        metadata_path=METADATA_PATH,
        p0_lookup={"Foo": 0.65},
        league_avg_p0=0.6266,
    )
    service.start_match(
        1, "Totally Unknown Player", "Foo", "hard", True,
        player_a_id=1, player_b_id=2,
    )
    assert service.p0_a == pytest.approx(0.6266)


def test_start_match_p0_overrides(model_files):
    service = _make_minimal_service({"Foo": 0.65, "Bar": 0.60})
    service.start_match(
        1, "Foo", "Bar", "hard", True, p0_a=0.70, p0_b=0.50,
        player_a_id=1, player_b_id=2,
    )
    assert service.p0_a == 0.70
    assert service.p0_b == 0.50


@pytest.mark.parametrize("surface", ["hard", "clay", "grass", "unknown"])
def test_start_match_surface_each_known(model_files, surface):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", surface, True, player_a_id=1, player_b_id=2)
    for cat in ("hard", "clay", "grass", "unknown"):
        expected = 1 if cat == surface else 0
        assert service.surface_dummies[f"surface_{cat}"] == expected


def test_start_match_surface_carpet_to_unknown(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "carpet", True, player_a_id=1, player_b_id=2)
    assert service.surface_dummies == {
        "surface_hard": 0,
        "surface_clay": 0,
        "surface_grass": 0,
        "surface_unknown": 1,
    }


def test_start_match_surface_mixed_case(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "Hard", True, player_a_id=1, player_b_id=2)
    assert service.surface_dummies["surface_hard"] == 1


def test_start_match_clears_markov_cache(model_files):
    # Populate the cache through the real API.
    markov_engine.set_win_prob(0.65, 0.60, 0, 0, True)
    assert len(markov_engine._SET_CACHE) > 0
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    assert len(markov_engine._SET_CACHE) == 0


# ═════════════════ Group D — process_point / finalize contract ═════════════════

def test_process_point_before_start_match_raises(model_files):
    service = _make_minimal_service({"X": 0.6})
    with pytest.raises(RuntimeError):
        service.process_point(Pt(1, 1, 1, "0-0", 1, 1))


def test_process_point_after_finalize_raises(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    service.process_point(Pt(1, 1, 1, "0-0", 1, 1))
    service.finalize()
    with pytest.raises(RuntimeError):
        service.process_point(Pt(1, 1, 2, "15-0", 1, 1))


def test_finalize_idempotent(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    service.process_point(Pt(1, 1, 1, "0-0", 1, 1))
    assert service.finalize() is not None
    assert service.finalize() is None


def test_empty_match_finalize_returns_none(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    assert service.finalize() is None


def test_process_point_no_emit_mid_game(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    for pt in _love_hold(1, 1, 1, server=1):
        assert service.process_point(pt) is None


def test_process_point_emits_at_game_boundary(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(99, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    for pt in _love_hold(1, 1, 1, server=1):
        assert service.process_point(pt) is None
    pred = service.process_point(Pt(1, 2, 5, "0-0", 2, 1))
    assert isinstance(pred, Prediction)
    assert pred.match_id_int == 99
    assert pred.set_number == 1
    assert pred.game_number_in_set == 1
    assert 0.0 <= pred.probability_a <= 1.0
    assert pred.confidence == pytest.approx(
        max(pred.probability_a, 1.0 - pred.probability_a)
    )
    assert len(pred.features) == 63


def test_prediction_features_keys_match_feature_names(model_files):
    service = _make_minimal_service({"X": 0.6})
    service.start_match(99, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    for pt in _love_hold(1, 1, 1, server=1):
        service.process_point(pt)
    pred = service.process_point(Pt(1, 2, 5, "0-0", 2, 1))
    assert set(pred.features) == set(service.feature_names)


# ═════════════════ Group E — invariant tests ═════════════════

def test_serve_alternation_invariant_raises(model_files):
    """Two consecutive games served by Svr=1 must trip the invariant on the second emit."""
    service = _make_minimal_service({"X": 0.6})
    service.start_match(1, "X", "X", "hard", True, player_a_id=1, player_b_id=2)
    # Game 1 — Svr=1, A wins all 4.
    for pt in _love_hold(1, 1, 1, server=1):
        service.process_point(pt)
    # Game 2 — also Svr=1 (illegal). First point triggers emit-of-game-1 (no violation yet).
    for pt in _love_hold(1, 2, 5, server=1):
        service.process_point(pt)
    # First point of game 3 triggers emit-of-game-2 — current server_was_a=True equals
    # the previous emit's server_was_a=True. The invariant check must fire.
    with pytest.raises(RuntimeError) as exc_info:
        service.process_point(Pt(1, 3, 9, "0-0", 2, 1))
    msg = str(exc_info.value)
    assert "Serve-alternation" in msg
    assert "set 1" in msg
    assert "game 2" in msg


# ═════════════════ Group F — end-to-end feature equivalence ═════════════════

def _load_ml_rows(con, mid: int) -> list[dict]:
    """Pull ml_game_level rows in (set, game) order, one dict per row, keyed by column."""
    cols = (
        ["set_number", "game_number_in_set",
         "games_A", "games_B", "sets_won_A", "sets_won_B", "surface"]
        + list(SIGNAL_COLUMNS)
        + ["markov_set_win_prob_A"]
    )
    select = ", ".join(f'"{c}"' for c in cols)
    rows = con.execute(f"""
        SELECT {select}
        FROM core.ml_game_level
        WHERE match_id_int = ?
        ORDER BY set_number, game_number_in_set
    """, [mid]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _load_points(con, mid: int) -> list[Pt]:
    rows = con.execute("""
        SELECT
            (ape.Set1 + ape.Set2 + 1) AS set_number,
            (ape.Gm1 + ape.Gm2 + 1)   AS game_number_in_set,
            ape.Pt,
            ape.score_before,
            CAST(ape.Svr AS INTEGER)      AS Svr,
            CAST(ape.PtWinner AS INTEGER) AS PtWinner,
            ape.is_tiebreak
        FROM core.atp_points_enhanced ape
        JOIN core.match_id_map mim ON mim.match_id_string = ape.match_id
        WHERE mim.match_id_int = ?
          AND ape.match_date BETWEEN DATE '2015-01-01' AND DATE '2025-12-31'
          AND ape.player1_name IS NOT NULL AND ape.player2_name IS NOT NULL
          AND ape.Set1 IS NOT NULL AND ape.Set2 IS NOT NULL
          AND ape.Gm1  IS NOT NULL AND ape.Gm2  IS NOT NULL
          AND ape.Svr  IS NOT NULL AND ape.PtWinner IS NOT NULL
        ORDER BY ape.Pt
    """, [mid]).fetchall()
    return [Pt(
        set_number=int(r[0]),
        game_number_in_set=int(r[1]),
        Pt=int(r[2]),
        score_before=r[3],
        Svr=int(r[4]),
        PtWinner=int(r[5]),
        is_tiebreak=bool(r[6]),
    ) for r in rows]


def _expected_features(ml_row: dict, feature_names: list[str]) -> dict[str, float]:
    surface_norm = (ml_row["surface"] or "unknown").lower()
    if surface_norm not in ("hard", "clay", "grass", "unknown"):
        surface_norm = "unknown"
    surface_dummies = {
        f"surface_{cat}": 1 if surface_norm == cat else 0
        for cat in ("hard", "clay", "grass", "unknown")
    }
    expected = {
        "games_A": float(ml_row["games_A"]),
        "games_B": float(ml_row["games_B"]),
        "set_number": float(ml_row["set_number"]),
        "sets_won_A": float(ml_row["sets_won_A"]),
        "sets_won_B": float(ml_row["sets_won_B"]),
        "game_number_in_set": float(ml_row["game_number_in_set"]),
        **{k: float(v) for k, v in surface_dummies.items()},
        **{col: float(ml_row[col]) for col in SIGNAL_COLUMNS},
        "markov_set_win_prob_A": float(ml_row["markov_set_win_prob_A"]),
    }
    if set(expected) != set(feature_names):
        raise RuntimeError(
            f"expected feature key set differs from service.feature_names"
        )
    return expected


def test_feature_equivalence_with_training_rows(con, model_files):
    """For 3 real matches, every prediction's features must equal the ml_game_level row."""
    sample_ids = [r[0] for r in con.execute("""
        SELECT DISTINCT match_id_int FROM core.ml_game_level
        ORDER BY match_id_int LIMIT 3
    """).fetchall()]
    assert len(sample_ids) == 3, f"expected 3 matches, got {len(sample_ids)}"

    service = LivePredictionService(
        model_path=MODEL_PATH,
        metadata_path=METADATA_PATH,
        db_path=DB_PATH,
    )

    for mid in sample_ids:
        ml_rows = _load_ml_rows(con, mid)
        assert ml_rows, f"no ml_game_level rows for match {mid}"

        meta = con.execute("""
            SELECT player_A, player_B, surface
            FROM core.ml_game_level WHERE match_id_int = ? LIMIT 1
        """, [mid]).fetchone()
        player_a, player_b, surface = meta

        points = _load_points(con, mid)
        assert points, f"no points for match {mid}"
        first_server_is_a = (points[0].Svr == 1)

        service.start_match(
            mid, player_a, player_b, surface, first_server_is_a,
            player_a_id=1, player_b_id=2,
        )
        predictions: list[Prediction] = []
        for pt in points:
            r = service.process_point(pt)
            if r is not None:
                predictions.append(r)
        last = service.finalize()
        if last is not None:
            predictions.append(last)

        if len(predictions) != len(ml_rows):
            pytest.fail(
                f"row count mismatch for match_id_int={mid}: "
                f"predictions={len(predictions)} ml_rows={len(ml_rows)}"
            )

        by_key = {(p.set_number, p.game_number_in_set): p for p in predictions}
        for ml_row in ml_rows:
            key = (int(ml_row["set_number"]), int(ml_row["game_number_in_set"]))
            pred = by_key.get(key)
            if pred is None:
                pytest.fail(
                    f"no prediction for match_id_int={mid} (set, game)={key}"
                )
            expected = _expected_features(ml_row, service.feature_names)
            for name in service.feature_names:
                e = expected[name]
                a = pred.features[name]
                if a != pytest.approx(e, rel=1e-9, abs=1e-9):
                    pytest.fail(
                        f"feature mismatch on match_id_int={mid} "
                        f"(set, game)={key} feature={name!r}: "
                        f"expected={e!r} actual={a!r}"
                    )


# ═════════════════ Group G — PredictionLogger integration ═════════════════

def _service_with_logger(
    mock_logger, *, model_version: str = "v1"
) -> LivePredictionService:
    return LivePredictionService(
        model_path=MODEL_PATH,
        metadata_path=METADATA_PATH,
        p0_lookup={"X": 0.6},
        prediction_logger=mock_logger,
        model_version=model_version,
    )


def _drive_one_prediction(service: LivePredictionService):
    """Feed enough points to trigger exactly one emitted prediction."""
    for pt in _love_hold(1, 1, 1, server=1):
        service.process_point(pt)
    return service.process_point(Pt(1, 2, 5, "0-0", 2, 1))


class TestPredictionLoggerIntegration:
    def test_constructor_with_no_logger_arg_and_singleton_unavailable(
        self, model_files
    ):
        # autouse fixture already patches get_default_logger to return None.
        service = _make_minimal_service({"X": 0.6})
        assert service._prediction_logger is None

    def test_constructor_with_no_logger_arg_auto_resolves_singleton(
        self, monkeypatch, model_files
    ):
        mock_logger = MagicMock()
        monkeypatch.setattr(
            "src.live_prediction_service.get_default_logger",
            lambda: mock_logger,
        )
        service = _make_minimal_service({"X": 0.6})
        assert service._prediction_logger is mock_logger

    def test_constructor_with_explicit_logger_skips_auto_resolution(
        self, monkeypatch, model_files
    ):
        spy = MagicMock(return_value=None)
        monkeypatch.setattr(
            "src.live_prediction_service.get_default_logger", spy
        )
        explicit = MagicMock()
        service = LivePredictionService(
            model_path=MODEL_PATH,
            metadata_path=METADATA_PATH,
            p0_lookup={"X": 0.6},
            prediction_logger=explicit,
        )
        spy.assert_not_called()
        assert service._prediction_logger is explicit

    def test_process_point_calls_logger_with_complete_prediction(
        self, model_files
    ):
        mock_logger = MagicMock()
        service = _service_with_logger(mock_logger)
        service.start_match(
            99, "X", "X", "Clay", True,
            player_a_id=42, player_b_id=77,
        )
        pred = _drive_one_prediction(service)

        assert isinstance(pred, Prediction)
        assert mock_logger.log.call_count == 1
        args, kwargs = mock_logger.log.call_args
        prediction_arg = args[0] if args else kwargs["prediction"]
        assert prediction_arg.player_a_id == 42
        assert prediction_arg.player_b_id == 77
        assert prediction_arg.surface == "Clay"
        assert kwargs.get("model_version") == "v1"

    def test_process_point_no_prediction_no_log_call(self, model_files):
        mock_logger = MagicMock()
        service = _service_with_logger(mock_logger)
        service.start_match(
            1, "X", "X", "hard", True, player_a_id=1, player_b_id=2,
        )
        # One mid-game point — no game boundary, no emit, no log.
        result = service.process_point(Pt(1, 1, 1, "0-0", 1, 1))
        assert result is None
        mock_logger.log.assert_not_called()

    def test_logger_failure_does_not_crash_service(self, model_files):
        mock_logger = MagicMock()
        mock_logger.log.return_value = None  # PredictionLogger's failure contract
        service = _service_with_logger(mock_logger)
        service.start_match(
            1, "X", "X", "hard", True, player_a_id=1, player_b_id=2,
        )

        pred = _drive_one_prediction(service)

        assert isinstance(pred, Prediction)
        assert mock_logger.log.call_count == 1

    def test_explicit_model_version_passed_to_logger(self, model_files):
        mock_logger = MagicMock()
        service = _service_with_logger(mock_logger, model_version="experimental_v2")
        service.start_match(
            1, "X", "X", "hard", True, player_a_id=1, player_b_id=2,
        )

        _drive_one_prediction(service)

        _args, kwargs = mock_logger.log.call_args
        assert kwargs.get("model_version") == "experimental_v2"

    def test_no_logger_no_log_call_no_crash(self, model_files):
        # autouse fixture already makes get_default_logger return None.
        service = _make_minimal_service({"X": 0.6})
        assert service._prediction_logger is None
        service.start_match(
            1, "X", "X", "hard", True, player_a_id=1, player_b_id=2,
        )

        pred = _drive_one_prediction(service)
        assert isinstance(pred, Prediction)
