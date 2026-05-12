"""
Persistent logger for live.predictions rows.

Writes one row per emitted Prediction. Singleton + persistent psycopg2
connection, mirroring src/live/api_logger.py. Failures are swallowed
(rolled back + warned) so a logging hiccup never crashes the
prediction service.

Table DDL: scripts/setup/migrate_create_predictions_table.py.
Column-order contract: see _COLUMN_ORDER below — it is the single
source of truth for both the column list and the parameter tuple.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import psycopg2
from dotenv import load_dotenv

if TYPE_CHECKING:
    from src.live_prediction_service import Prediction

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical column order
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_METADATA_PATH = _ROOT / "data" / "processed" / "model_v1_metadata.json"

with open(_METADATA_PATH) as _f:
    _FEATURE_NAMES: tuple[str, ...] = tuple(json.load(_f)["feature_names"])
if len(_FEATURE_NAMES) != 63:
    raise RuntimeError(
        f"expected 63 feature_names in metadata, got {len(_FEATURE_NAMES)}"
    )

# 7 identity/metadata columns; predicted_at is omitted (table default NOW()).
_IDENTITY_COLUMNS: tuple[str, ...] = (
    "match_id_int",
    "model_version",
    "player_a_id",
    "player_b_id",
    "surface",
    "probability_a",
    "confidence",
)

# Single source of truth — column list and parameter tuple are both
# derived from this, so they cannot drift apart.
_COLUMN_ORDER: tuple[str, ...] = _IDENTITY_COLUMNS + _FEATURE_NAMES
if len(_COLUMN_ORDER) != 70:
    raise RuntimeError(
        f"expected 70 INSERT columns, got {len(_COLUMN_ORDER)}"
    )

_BOOL_FEATURES: frozenset[str] = frozenset({
    "surface_hard", "surface_clay", "surface_grass", "surface_unknown",
})
_INT_FEATURES: frozenset[str] = frozenset({
    "games_A", "games_B", "set_number", "sets_won_A", "sets_won_B",
    "game_number_in_set",
})


def _quote_ident(name: str) -> str:
    """Wrap *name* in double quotes iff it contains an uppercase letter.

    Postgres folds unquoted identifiers to lowercase, so the five
    feature columns whose canonical names contain uppercase (games_A,
    games_B, sets_won_A, sets_won_B, markov_set_win_prob_A) must be
    quoted to match the table schema. Lowercase columns are left
    unquoted to keep ad-hoc SQL ergonomic.
    """
    if any(c.isupper() for c in name):
        return f'"{name}"'
    return name


def _build_insert_sql() -> str:
    cols = ", ".join(_quote_ident(c) for c in _COLUMN_ORDER)
    placeholders = ", ".join(["%s"] * len(_COLUMN_ORDER))
    return (
        f"INSERT INTO live.predictions ({cols}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (match_id_int, set_number, game_number_in_set, "
        f"model_version) DO NOTHING"
    )


_INSERT_SQL = _build_insert_sql()


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class PredictionLogger:
    """Writes one row to live.predictions per Prediction.

    Holds a single persistent psycopg2 connection for the lifetime of
    the instance. Concurrent log() calls serialize through self._lock
    because psycopg2 connections are not thread-safe.
    """

    def __init__(self, db_url: Optional[str] = None) -> None:
        load_dotenv()
        url = db_url or os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL environment variable is not set.")
        self._conn = psycopg2.connect(url)
        self._lock = threading.Lock()

    def log(
        self,
        prediction: "Prediction",
        *,
        model_version: str = "v1",
    ) -> Optional[int]:
        """Insert one row into live.predictions.

        Returns 1 on fresh insert, 0 on duplicate (ON CONFLICT DO
        NOTHING swallowed), None on any failure. Never raises.
        """
        try:
            if prediction.player_a_id is None:
                _log.warning(
                    "PredictionLogger.log: prediction.player_a_id is None; "
                    "skipping row for match %s",
                    getattr(prediction, "match_id_int", "?"),
                )
                return None
            if prediction.player_b_id is None:
                _log.warning(
                    "PredictionLogger.log: prediction.player_b_id is None; "
                    "skipping row for match %s",
                    getattr(prediction, "match_id_int", "?"),
                )
                return None
            if prediction.surface is None:
                _log.warning(
                    "PredictionLogger.log: prediction.surface is None; "
                    "skipping row for match %s",
                    getattr(prediction, "match_id_int", "?"),
                )
                return None

            features = prediction.features
            missing = [k for k in _FEATURE_NAMES if k not in features]
            if missing:
                _log.warning(
                    "PredictionLogger.log: features dict missing %d keys "
                    "(first: %s); skipping row for match %s",
                    len(missing),
                    missing[0],
                    getattr(prediction, "match_id_int", "?"),
                )
                return None

            identity_values: tuple = (
                int(prediction.match_id_int),
                str(model_version),
                int(prediction.player_a_id),
                int(prediction.player_b_id),
                str(prediction.surface),
                float(prediction.probability_a),
                float(prediction.confidence),
            )

            feature_values: list = []
            for name in _FEATURE_NAMES:
                v = features[name]
                if name in _BOOL_FEATURES:
                    feature_values.append(bool(v))
                elif name in _INT_FEATURES:
                    feature_values.append(int(v))
                else:
                    feature_values.append(float(v))

            params = identity_values + tuple(feature_values)

            with self._lock:
                with self._conn.cursor() as cur:
                    cur.execute(_INSERT_SQL, params)
                    affected = cur.rowcount
                self._conn.commit()
                return int(affected)
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            _log.warning("PredictionLogger.log failed: %s", exc)
            return None

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "PredictionLogger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Process-wide singleton accessor
# ---------------------------------------------------------------------------

_default_logger: "PredictionLogger | None" = None
_default_logger_init_attempted: bool = False
_default_logger_lock = threading.Lock()


def get_default_logger() -> Optional[PredictionLogger]:
    """Return the process-wide default PredictionLogger, or None.

    Lazy-initialized on first call. Returns None permanently for the
    life of the process if DATABASE_URL is unset or if construction
    fails. Construction is attempted exactly once; subsequent calls
    return the memoized result. To re-attempt after a transient DB
    outage, restart the process.

    Thread-safe via double-checked locking.
    """
    global _default_logger, _default_logger_init_attempted

    if _default_logger_init_attempted:
        return _default_logger

    with _default_logger_lock:
        if _default_logger_init_attempted:
            return _default_logger

        if not os.getenv("DATABASE_URL"):
            _default_logger_init_attempted = True
            return None

        try:
            _default_logger = PredictionLogger()
        except Exception as exc:
            _log.warning(
                "Default PredictionLogger construction failed (prediction "
                "logging disabled for this process until restart): %s",
                exc,
            )
        _default_logger_init_attempted = True

    return _default_logger


def _reset_default_logger_for_testing() -> None:
    """Reset the singleton state. ONLY for use in tests."""
    global _default_logger, _default_logger_init_attempted
    with _default_logger_lock:
        if _default_logger is not None:
            try:
                _default_logger.close()
            except Exception:
                pass
        _default_logger = None
        _default_logger_init_attempted = False


# ---------------------------------------------------------------------------
# Read path — query helpers
# ---------------------------------------------------------------------------
#
# Read queries open a fresh psycopg2 connection per call (mirroring the
# `with closing(_conn()) as conn:` pattern in src/live/backend.py). They
# deliberately do NOT swallow exceptions — analytics callers should fail
# loudly if a query goes wrong.

_IDENTITY_FIELDS: tuple[str, ...] = (
    "match_id_int",
    "model_version",
    "predicted_at",
    "player_a_id",
    "player_b_id",
    "surface",
    "probability_a",
    "confidence",
)

# 71 SELECT columns total: every column in _COLUMN_ORDER (the INSERT set,
# 70 columns) plus predicted_at, which is omitted from INSERT because the
# table assigns it via NOW().
_SELECT_COLUMNS: tuple[str, ...] = _COLUMN_ORDER + ("predicted_at",)
if len(_SELECT_COLUMNS) != 71:
    raise RuntimeError(
        f"expected 71 SELECT columns, got {len(_SELECT_COLUMNS)}"
    )

_SELECT_SQL_PREFIX: str = (
    "SELECT "
    + ", ".join(_quote_ident(c) for c in _SELECT_COLUMNS)
    + " FROM live.predictions"
)


@dataclass
class PredictionRecord:
    """One row read back from live.predictions.

    `set_number` and `game_number_in_set` appear both as top-level
    attributes (ergonomic access) and as keys inside `features` —
    they're feature columns in the table, so they belong in both.
    """
    match_id_int: int
    set_number: int
    game_number_in_set: int
    model_version: str
    predicted_at: datetime
    player_a_id: int
    player_b_id: int
    surface: str
    probability_a: float
    confidence: float
    features: dict[str, Any]


def _row_to_prediction_record(
    row: tuple, columns: list[str]
) -> PredictionRecord:
    """Map a DB row to a PredictionRecord.

    `columns` is the column names in the same order as `row`. Column
    names must use the ORIGINAL casing (e.g. "games_A", not "games_a")
    so the features dict is keyed consistently with the table schema
    and with _FEATURE_NAMES.
    """
    d = dict(zip(columns, row))
    features: dict[str, Any] = {name: d[name] for name in _FEATURE_NAMES}
    return PredictionRecord(
        match_id_int=d["match_id_int"],
        set_number=d["set_number"],
        game_number_in_set=d["game_number_in_set"],
        model_version=d["model_version"],
        predicted_at=d["predicted_at"],
        player_a_id=d["player_a_id"],
        player_b_id=d["player_b_id"],
        surface=d["surface"],
        probability_a=d["probability_a"],
        confidence=d["confidence"],
        features=features,
    )


def _get_query_conn():
    """Open a fresh psycopg2 connection for a single query call.

    No singleton — these are short-lived. Caller must close (typically
    via `with closing(_get_query_conn()) as conn:`).
    """
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url)


def get_prediction(
    match_id_int: int,
    set_number: int,
    game_number_in_set: int,
    *,
    model_version: str = "v1",
) -> Optional[PredictionRecord]:
    """Single prediction by primary key. Returns None if not found."""
    sql = (
        f"{_SELECT_SQL_PREFIX} "
        "WHERE match_id_int = %s "
        "AND set_number = %s "
        "AND game_number_in_set = %s "
        "AND model_version = %s"
    )
    params: list = [match_id_int, set_number, game_number_in_set, model_version]
    columns = list(_SELECT_COLUMNS)
    with closing(_get_query_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.commit()
    if row is None:
        return None
    return _row_to_prediction_record(row, columns)


def get_predictions_for_match(
    match_id_int: int,
    *,
    model_version: str = "v1",
) -> list[PredictionRecord]:
    """All predictions for one match, ordered chronologically through the match."""
    sql = (
        f"{_SELECT_SQL_PREFIX} "
        "WHERE match_id_int = %s AND model_version = %s "
        "ORDER BY set_number ASC, game_number_in_set ASC"
    )
    params: list = [match_id_int, model_version]
    columns = list(_SELECT_COLUMNS)
    with closing(_get_query_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            conn.commit()
    return [_row_to_prediction_record(row, columns) for row in rows]


def get_predictions_in_range(
    start: datetime,
    end: datetime,
    *,
    model_version: Optional[str] = "v1",
    min_confidence: Optional[float] = None,
    surface: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[PredictionRecord]:
    """Predictions with predicted_at in [start, end).

    Filters apply only when their argument is non-None.
    `model_version=None` matches every model version. Ordered by
    predicted_at ASC.
    """
    where: list[str] = ["predicted_at >= %s", "predicted_at < %s"]
    params: list = [start, end]
    if model_version is not None:
        where.append("model_version = %s")
        params.append(model_version)
    if min_confidence is not None:
        where.append("confidence >= %s")
        params.append(min_confidence)
    if surface is not None:
        where.append("surface = %s")
        params.append(surface)

    sql = (
        f"{_SELECT_SQL_PREFIX} WHERE {' AND '.join(where)} "
        "ORDER BY predicted_at ASC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    columns = list(_SELECT_COLUMNS)
    with closing(_get_query_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            conn.commit()
    return [_row_to_prediction_record(row, columns) for row in rows]


def get_high_confidence_predictions(
    min_confidence: float,
    *,
    model_version: str = "v1",
    since: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[PredictionRecord]:
    """Predictions with confidence >= min_confidence, ordered confidence DESC."""
    where: list[str] = ["confidence >= %s", "model_version = %s"]
    params: list = [min_confidence, model_version]
    if since is not None:
        where.append("predicted_at >= %s")
        params.append(since)

    sql = (
        f"{_SELECT_SQL_PREFIX} WHERE {' AND '.join(where)} "
        "ORDER BY confidence DESC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    columns = list(_SELECT_COLUMNS)
    with closing(_get_query_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            conn.commit()
    return [_row_to_prediction_record(row, columns) for row in rows]


def count_predictions(
    *,
    model_version: Optional[str] = None,
    since: Optional[datetime] = None,
) -> int:
    """Count rows in live.predictions matching optional filters."""
    where: list[str] = []
    params: list = []
    if model_version is not None:
        where.append("model_version = %s")
        params.append(model_version)
    if since is not None:
        where.append("predicted_at >= %s")
        params.append(since)

    sql = "SELECT COUNT(*) FROM live.predictions"
    if where:
        sql += " WHERE " + " AND ".join(where)

    with closing(_get_query_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.commit()
    return int(row[0])
