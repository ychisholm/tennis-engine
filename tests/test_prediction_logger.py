"""
Tests for src/prediction_logger.py.

Hermetic — no real database connection. psycopg2.connect is mocked
throughout. Mirrors the structure of tests/test_api_logger.py
(MagicMock cursor, double-checked singleton, thread-safety harness).
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import psycopg2
import pytest

import src.prediction_logger as prediction_logger_mod
from src.live_prediction_service import Prediction
from datetime import datetime

from src.prediction_logger import (
    PredictionLogger,
    PredictionRecord,
    _FEATURE_NAMES,
    _SELECT_COLUMNS,
    _quote_ident,
    _reset_default_logger_for_testing,
    _row_to_prediction_record,
    count_predictions,
    get_default_logger,
    get_high_confidence_predictions,
    get_prediction,
    get_predictions_for_match,
    get_predictions_in_range,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_mocked_logger(execute_side_effect=None, rowcount: int = 1):
    """Construct a PredictionLogger backed by a MagicMock psycopg2 connection."""
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.rowcount = rowcount
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_conn.cursor.return_value.__exit__.return_value = False
    if execute_side_effect is not None:
        fake_cursor.execute.side_effect = execute_side_effect

    with patch("src.prediction_logger.psycopg2.connect", return_value=fake_conn):
        logger = PredictionLogger(db_url="postgresql://fake")
    return logger, fake_conn, fake_cursor


def _full_features_dict() -> dict[str, float]:
    """Build a features dict with all 63 expected keys.

    Realistic-ish values: bools for surface_*, ints for score state,
    floats for the rest.
    """
    bools = {
        "surface_hard": 1,
        "surface_clay": 0,
        "surface_grass": 0,
        "surface_unknown": 0,
    }
    ints = {
        "games_A": 3,
        "games_B": 2,
        "set_number": 1,
        "sets_won_A": 0,
        "sets_won_B": 0,
        "game_number_in_set": 5,
    }
    out: dict[str, float] = {}
    for name in _FEATURE_NAMES:
        if name in bools:
            out[name] = bools[name]
        elif name in ints:
            out[name] = ints[name]
        else:
            out[name] = 0.5
    return out


def _build_prediction(**overrides) -> Prediction:
    defaults = dict(
        match_id_int=1000000001,
        set_number=1,
        game_number_in_set=5,
        probability_a=0.62,
        confidence=0.62,
        features=_full_features_dict(),
        player_a_id=42,
        player_b_id=99,
        surface="hard",
    )
    defaults.update(overrides)
    return Prediction(**defaults)


@pytest.fixture(autouse=False)
def _reset_singleton():
    """Each singleton test starts and ends with a clean global state."""
    _reset_default_logger_for_testing()
    yield
    _reset_default_logger_for_testing()


# ---------------------------------------------------------------------------
# PredictionLogger
# ---------------------------------------------------------------------------

class TestPredictionLogger:
    def test_construction_with_explicit_url(self):
        with patch(
            "src.prediction_logger.psycopg2.connect", return_value=MagicMock()
        ) as ctor:
            PredictionLogger(db_url="postgresql://explicit/db")
        ctor.assert_called_once_with("postgresql://explicit/db")

    def test_construction_reads_env_var(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://from-env/db")
        with patch(
            "src.prediction_logger.psycopg2.connect", return_value=MagicMock()
        ) as ctor:
            PredictionLogger()
        ctor.assert_called_once_with("postgresql://from-env/db")

    def test_log_success_inserts_row(self):
        logger, fake_conn, fake_cursor = _build_mocked_logger(rowcount=1)
        prediction = _build_prediction()

        result = logger.log(prediction, model_version="v1")

        assert result == 1
        assert fake_cursor.execute.call_count == 1
        sql_arg, params_arg = fake_cursor.execute.call_args[0]
        assert "INSERT INTO live.predictions" in sql_arg
        assert "ON CONFLICT" in sql_arg
        assert "DO NOTHING" in sql_arg
        assert '"games_A"' in sql_arg
        assert len(params_arg) == 70
        fake_conn.commit.assert_called_once()

    def test_log_duplicate_returns_zero(self):
        logger, fake_conn, fake_cursor = _build_mocked_logger(rowcount=0)
        prediction = _build_prediction()

        result = logger.log(prediction, model_version="v1")

        assert result == 0
        assert fake_cursor.execute.call_count == 1
        fake_conn.commit.assert_called_once()

    def test_log_missing_player_a_id_returns_none(self):
        logger, _fake_conn, fake_cursor = _build_mocked_logger()
        prediction = _build_prediction(player_a_id=None)

        result = logger.log(prediction)

        assert result is None
        fake_cursor.execute.assert_not_called()

    def test_log_missing_player_b_id_returns_none(self):
        logger, _fake_conn, fake_cursor = _build_mocked_logger()
        prediction = _build_prediction(player_b_id=None)

        result = logger.log(prediction)

        assert result is None
        fake_cursor.execute.assert_not_called()

    def test_log_missing_surface_returns_none(self):
        logger, _fake_conn, fake_cursor = _build_mocked_logger()
        prediction = _build_prediction(surface=None)

        result = logger.log(prediction)

        assert result is None
        fake_cursor.execute.assert_not_called()

    def test_log_missing_feature_key_returns_none(self):
        logger, _fake_conn, fake_cursor = _build_mocked_logger()
        broken_features = _full_features_dict()
        del broken_features["bpi_bp_rate_ws_a"]
        prediction = _build_prediction(features=broken_features)

        result = logger.log(prediction)

        assert result is None
        fake_cursor.execute.assert_not_called()

    def test_log_handles_db_exception(self):
        logger, fake_conn, _fake_cursor = _build_mocked_logger(
            execute_side_effect=psycopg2.Error("connection lost"),
        )
        prediction = _build_prediction()

        result = logger.log(prediction)

        assert result is None
        fake_conn.rollback.assert_called_once()

    def test_log_handles_general_exception(self):
        logger, _fake_conn, _fake_cursor = _build_mocked_logger(
            execute_side_effect=RuntimeError("kaboom"),
        )
        prediction = _build_prediction()

        # Must not raise.
        result = logger.log(prediction)

        assert result is None


# ---------------------------------------------------------------------------
# _quote_ident
# ---------------------------------------------------------------------------

class TestQuoteIdent:
    def test_quote_ident_lowercase_unchanged(self):
        assert _quote_ident("games_a") == "games_a"
        assert _quote_ident("bpi_bp_rate_ws_a") == "bpi_bp_rate_ws_a"

    def test_quote_ident_uppercase_quoted(self):
        assert _quote_ident("games_A") == '"games_A"'
        assert _quote_ident("markov_set_win_prob_A") == '"markov_set_win_prob_A"'


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_default_logger_returns_same_instance(
        self, monkeypatch, _reset_singleton
    ):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        fake = MagicMock(spec=PredictionLogger)
        ctor = MagicMock(return_value=fake)
        monkeypatch.setattr(prediction_logger_mod, "PredictionLogger", ctor)

        first = get_default_logger()
        second = get_default_logger()

        assert first is fake
        assert first is second
        assert ctor.call_count == 1

    def test_singleton_thread_safety(self, monkeypatch, _reset_singleton):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        # Slow constructor so threads pile up at the lock and exercise the
        # double-checked path inside the critical section.
        construct_event = threading.Event()

        def slow_ctor(*a, **kw):
            construct_event.wait(timeout=2.0)
            return MagicMock(spec=PredictionLogger)

        ctor = MagicMock(side_effect=slow_ctor)
        monkeypatch.setattr(prediction_logger_mod, "PredictionLogger", ctor)

        N = 10
        barrier = threading.Barrier(N)
        results: list = [None] * N

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = get_default_logger()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()

        # Let the constructor complete after all threads have queued up at
        # the lock. A small sleep is the simplest synchronization here.
        time.sleep(0.1)
        construct_event.set()

        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive()

        assert ctor.call_count == 1
        first = results[0]
        assert first is not None
        for r in results:
            assert r is first

    def test_reset_for_testing_clears_singleton(
        self, monkeypatch, _reset_singleton
    ):
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        instances = [
            MagicMock(spec=PredictionLogger),
            MagicMock(spec=PredictionLogger),
        ]
        ctor = MagicMock(side_effect=instances)
        monkeypatch.setattr(prediction_logger_mod, "PredictionLogger", ctor)

        first = get_default_logger()
        _reset_default_logger_for_testing()
        second = get_default_logger()

        assert first is instances[0]
        assert second is instances[1]
        assert first is not second
        assert ctor.call_count == 2


# ---------------------------------------------------------------------------
# Read-path queries
# ---------------------------------------------------------------------------

_SAMPLE_PREDICTED_AT = datetime(2026, 5, 11, 14, 30, 0)


def _build_fake_row_dict() -> dict:
    """Build a {column_name: value} dict covering every SELECT column."""
    bools = {"surface_hard", "surface_clay", "surface_grass", "surface_unknown"}
    ints = {
        "games_A", "games_B", "set_number", "sets_won_A", "sets_won_B",
        "game_number_in_set",
    }
    values: dict = {
        "match_id_int": 1000000001,
        "model_version": "v1",
        "predicted_at": _SAMPLE_PREDICTED_AT,
        "player_a_id": 42,
        "player_b_id": 99,
        "surface": "hard",
        "probability_a": 0.62,
        "confidence": 0.62,
    }
    for name in _FEATURE_NAMES:
        if name in bools:
            values[name] = (name == "surface_hard")
        elif name in ints:
            # Distinct ints so we can spot ordering bugs in row mapping.
            mapping = {
                "games_A": 3, "games_B": 2, "set_number": 1,
                "sets_won_A": 0, "sets_won_B": 0, "game_number_in_set": 5,
            }
            values[name] = mapping[name]
        else:
            values[name] = 0.5
    return values


def _build_fake_row_tuple() -> tuple:
    """Build a 71-tuple in _SELECT_COLUMNS order, matching the dict above."""
    d = _build_fake_row_dict()
    return tuple(d[c] for c in _SELECT_COLUMNS)


_UNSET = object()


def _patch_query_conn(monkeypatch, fetchone=_UNSET, fetchall=_UNSET):
    """Patch psycopg2.connect inside the query path; return the fake cursor.

    Use the _UNSET sentinel so callers can explicitly set fetchone=None
    (to simulate "no row found") versus leaving fetchone unconfigured.
    """
    fake_cursor = MagicMock()
    if fetchone is not _UNSET:
        fake_cursor.fetchone.return_value = fetchone
    if fetchall is not _UNSET:
        fake_cursor.fetchall.return_value = fetchall

    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    fake_conn.cursor.return_value.__exit__.return_value = False
    # closing() calls conn.close on __exit__; let MagicMock handle it.

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(
        "src.prediction_logger.psycopg2.connect",
        MagicMock(return_value=fake_conn),
    )
    return fake_cursor


class TestPredictionQueries:
    def test_get_prediction_returns_record_when_found(self, monkeypatch):
        row = _build_fake_row_tuple()
        cursor = _patch_query_conn(monkeypatch, fetchone=row)

        record = get_prediction(1000000001, 1, 5, model_version="v1")

        assert record is not None
        assert isinstance(record, PredictionRecord)
        assert record.match_id_int == 1000000001
        assert record.model_version == "v1"
        assert record.predicted_at == _SAMPLE_PREDICTED_AT
        assert record.player_a_id == 42
        assert record.player_b_id == 99
        assert record.surface == "hard"
        assert record.probability_a == 0.62
        assert record.confidence == 0.62
        assert len(record.features) == 63
        assert "games_A" in record.features
        assert "markov_set_win_prob_A" in record.features
        assert isinstance(record.features["games_A"], int)
        cursor.execute.assert_called_once()

    def test_get_prediction_returns_none_when_not_found(self, monkeypatch):
        _patch_query_conn(monkeypatch, fetchone=None)
        assert get_prediction(1, 1, 1) is None

    def test_get_prediction_sql_contains_pk_filter(self, monkeypatch):
        cursor = _patch_query_conn(monkeypatch, fetchone=None)
        get_prediction(1, 1, 1)
        sql = cursor.execute.call_args[0][0]
        assert "WHERE" in sql
        assert "match_id_int = %s" in sql
        assert "set_number = %s" in sql
        assert "game_number_in_set = %s" in sql
        assert "model_version = %s" in sql

    def test_get_predictions_for_match_returns_list(self, monkeypatch):
        row = _build_fake_row_tuple()
        cursor = _patch_query_conn(monkeypatch, fetchall=[row, row, row])

        records = get_predictions_for_match(1000000001)

        assert len(records) == 3
        assert all(isinstance(r, PredictionRecord) for r in records)
        sql = cursor.execute.call_args[0][0]
        assert "ORDER BY" in sql
        assert "set_number" in sql
        assert "game_number_in_set" in sql

    def test_get_predictions_in_range_no_optional_filters(self, monkeypatch):
        cursor = _patch_query_conn(monkeypatch, fetchall=[])
        start = datetime(2026, 5, 1)
        end = datetime(2026, 5, 11)

        get_predictions_in_range(start, end)

        sql, params = cursor.execute.call_args[0]
        assert "predicted_at >= %s" in sql
        assert "predicted_at < %s" in sql
        assert "model_version = %s" in sql  # default "v1" is applied
        assert "confidence >= %s" not in sql
        assert "surface = %s" not in sql
        assert "LIMIT" not in sql
        assert params == [start, end, "v1"]

    def test_get_predictions_in_range_with_all_filters(self, monkeypatch):
        cursor = _patch_query_conn(monkeypatch, fetchall=[])
        start = datetime(2026, 5, 1)
        end = datetime(2026, 5, 11)

        get_predictions_in_range(
            start, end,
            model_version="v1",
            min_confidence=0.7,
            surface="Hard",
            limit=50,
        )

        sql, params = cursor.execute.call_args[0]
        assert "predicted_at >= %s" in sql
        assert "predicted_at < %s" in sql
        assert "model_version = %s" in sql
        assert "confidence >= %s" in sql
        assert "surface = %s" in sql
        assert "LIMIT %s" in sql
        assert params == [start, end, "v1", 0.7, "Hard", 50]

    def test_get_predictions_in_range_model_version_none(self, monkeypatch):
        cursor = _patch_query_conn(monkeypatch, fetchall=[])
        start = datetime(2026, 5, 1)
        end = datetime(2026, 5, 11)

        get_predictions_in_range(start, end, model_version=None)

        sql, params = cursor.execute.call_args[0]
        # The SELECT clause lists `model_version` as one of the returned
        # columns, so the column name will appear in the SQL. What must
        # NOT appear is a `model_version = %s` filter.
        assert "model_version = %s" not in sql
        assert params == [start, end]

    def test_get_high_confidence_predictions_sql_shape(self, monkeypatch):
        cursor = _patch_query_conn(monkeypatch, fetchall=[])
        since = datetime(2026, 5, 1)

        get_high_confidence_predictions(0.8, since=since)

        sql, params = cursor.execute.call_args[0]
        assert "confidence >= %s" in sql
        assert "predicted_at >= %s" in sql
        assert "ORDER BY confidence DESC" in sql
        assert params == [0.8, "v1", since]

    def test_count_predictions_returns_int(self, monkeypatch):
        _patch_query_conn(monkeypatch, fetchone=(42,))
        result = count_predictions()
        assert result == 42
        assert isinstance(result, int)

    def test_count_predictions_with_filters(self, monkeypatch):
        cursor = _patch_query_conn(monkeypatch, fetchone=(7,))
        since = datetime(2026, 5, 1)

        count_predictions(model_version="v1", since=since)

        sql, params = cursor.execute.call_args[0]
        assert "model_version = %s" in sql
        assert "predicted_at >= %s" in sql
        assert params == ["v1", since]

    def test_select_quotes_uppercase_columns(self, monkeypatch):
        cursor = _patch_query_conn(monkeypatch, fetchone=None)
        get_prediction(1, 1, 1)
        sql = cursor.execute.call_args[0][0]
        assert '"games_A"' in sql
        assert '"markov_set_win_prob_A"' in sql

    def test_row_to_prediction_record_uppercase_features_keyed_correctly(self):
        columns = list(_SELECT_COLUMNS)
        row = _build_fake_row_tuple()

        record = _row_to_prediction_record(row, columns)

        assert "games_A" in record.features
        assert "games_a" not in record.features
        assert "markov_set_win_prob_A" in record.features
        assert "markov_set_win_prob_a" not in record.features

    def test_row_to_prediction_record_set_number_in_both_places(self):
        columns = list(_SELECT_COLUMNS)
        row = _build_fake_row_tuple()

        record = _row_to_prediction_record(row, columns)

        assert record.set_number == record.features["set_number"]
        assert record.game_number_in_set == record.features["game_number_in_set"]
