#!/usr/bin/env python3
"""
Step 6C migration — create live.predictions.

Operations (all inside one transaction):
  1. CREATE TABLE IF NOT EXISTS live.predictions with:
       - 8 identity/metadata columns
       - 63 feature columns in canonical order from
         data/processed/model_v1_metadata.json["feature_names"]
       - Composite PK (match_id_int, set_number, game_number_in_set,
         model_version)
  2. CREATE INDEX IF NOT EXISTS idx_predictions_predicted_at
  3. CREATE INDEX IF NOT EXISTS idx_predictions_model_confidence

Schema-design notes:
  - The `live` schema is assumed to exist (created by MatchLogger on
    worker boot; see src/live/logger.py). No CREATE SCHEMA here.
  - Five feature names contain uppercase characters (games_A, games_B,
    sets_won_A, sets_won_B, markov_set_win_prob_A). Postgres folds
    unquoted identifiers to lowercase, so those five are double-quoted
    in the DDL to preserve case fidelity with the canonical
    feature_names list. The PredictionLogger will key inserts by the
    exact same names.
  - No foreign keys: book.* is forward-only, so an FK to book.matches
    would block live inserts for matches not yet promoted.

Re-running after a successful run is a no-op: every CREATE uses IF
NOT EXISTS, and existence probes against information_schema /
pg_indexes report [SKIP] vs [OK] per step.

Usage:
    .venv/bin/python scripts/setup/migrate_create_predictions_table.py
    .venv/bin/python scripts/setup/migrate_create_predictions_table.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

# Feature columns are listed in the canonical order from
# data/processed/model_v1_metadata.json["feature_names"]. Types per
# docs/prediction_logger_recon.md §B2:
#   - games_A, games_B, set_number, sets_won_A, sets_won_B,
#     game_number_in_set      → INT
#   - surface_hard, surface_clay, surface_grass, surface_unknown
#                              → BOOLEAN
#   - 52 signal columns + markov_set_win_prob_A
#                              → DOUBLE PRECISION
# All feature columns are NOT NULL — the streaming engine emits 0.0 as
# a default, never NULL (recon §B3).
_CREATE_PREDICTIONS_DDL = """
CREATE TABLE IF NOT EXISTS live.predictions (
    match_id_int                  BIGINT           NOT NULL,
    model_version                 TEXT             NOT NULL,
    predicted_at                  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    player_a_id                   BIGINT           NOT NULL,
    player_b_id                   BIGINT           NOT NULL,
    surface                       TEXT             NOT NULL,
    probability_a                 DOUBLE PRECISION NOT NULL,
    confidence                    DOUBLE PRECISION NOT NULL,

    "games_A"                     INT              NOT NULL,
    "games_B"                     INT              NOT NULL,
    set_number                    INT              NOT NULL,
    "sets_won_A"                  INT              NOT NULL,
    "sets_won_B"                  INT              NOT NULL,
    game_number_in_set            INT              NOT NULL,
    surface_hard                  BOOLEAN          NOT NULL,
    surface_clay                  BOOLEAN          NOT NULL,
    surface_grass                 BOOLEAN          NOT NULL,
    surface_unknown               BOOLEAN          NOT NULL,
    bpi_bp_rate_ws_a              DOUBLE PRECISION NOT NULL,
    bpi_bp_rate_ws_b              DOUBLE PRECISION NOT NULL,
    bpi_bp_rate_cm_a              DOUBLE PRECISION NOT NULL,
    bpi_bp_rate_cm_b              DOUBLE PRECISION NOT NULL,
    bpi_deep_pressure_rate_ws_a   DOUBLE PRECISION NOT NULL,
    bpi_deep_pressure_rate_ws_b   DOUBLE PRECISION NOT NULL,
    bpi_deep_pressure_rate_cm_a   DOUBLE PRECISION NOT NULL,
    bpi_deep_pressure_rate_cm_b   DOUBLE PRECISION NOT NULL,
    bpi_near_pressure_rate_ws_a   DOUBLE PRECISION NOT NULL,
    bpi_near_pressure_rate_ws_b   DOUBLE PRECISION NOT NULL,
    bpi_near_pressure_rate_cm_a   DOUBLE PRECISION NOT NULL,
    bpi_near_pressure_rate_cm_b   DOUBLE PRECISION NOT NULL,
    sds_serve_win_pct_ws_a        DOUBLE PRECISION NOT NULL,
    sds_serve_win_pct_ws_b        DOUBLE PRECISION NOT NULL,
    sds_serve_win_pct_cm_a        DOUBLE PRECISION NOT NULL,
    sds_serve_win_pct_cm_b        DOUBLE PRECISION NOT NULL,
    sds_hold_rate_ws_a            DOUBLE PRECISION NOT NULL,
    sds_hold_rate_ws_b            DOUBLE PRECISION NOT NULL,
    sds_hold_rate_cm_a            DOUBLE PRECISION NOT NULL,
    sds_hold_rate_cm_b            DOUBLE PRECISION NOT NULL,
    sds_avg_pts_per_game_ws_a     DOUBLE PRECISION NOT NULL,
    sds_avg_pts_per_game_ws_b     DOUBLE PRECISION NOT NULL,
    sds_avg_pts_per_game_cm_a     DOUBLE PRECISION NOT NULL,
    sds_avg_pts_per_game_cm_b     DOUBLE PRECISION NOT NULL,
    res_return_win_pct_ws_a       DOUBLE PRECISION NOT NULL,
    res_return_win_pct_ws_b       DOUBLE PRECISION NOT NULL,
    res_return_win_pct_cm_a       DOUBLE PRECISION NOT NULL,
    res_return_win_pct_cm_b       DOUBLE PRECISION NOT NULL,
    res_bp_conv_rate_ws_a         DOUBLE PRECISION NOT NULL,
    res_bp_conv_rate_ws_b         DOUBLE PRECISION NOT NULL,
    res_bp_conv_rate_cm_a         DOUBLE PRECISION NOT NULL,
    res_bp_conv_rate_cm_b         DOUBLE PRECISION NOT NULL,
    cpi_serve_pressure_pct_ws_a   DOUBLE PRECISION NOT NULL,
    cpi_serve_pressure_pct_ws_b   DOUBLE PRECISION NOT NULL,
    cpi_serve_pressure_pct_cm_a   DOUBLE PRECISION NOT NULL,
    cpi_serve_pressure_pct_cm_b   DOUBLE PRECISION NOT NULL,
    cpi_return_pressure_pct_ws_a  DOUBLE PRECISION NOT NULL,
    cpi_return_pressure_pct_ws_b  DOUBLE PRECISION NOT NULL,
    cpi_return_pressure_pct_cm_a  DOUBLE PRECISION NOT NULL,
    cpi_return_pressure_pct_cm_b  DOUBLE PRECISION NOT NULL,
    mrs_pwr_10_ws_a               DOUBLE PRECISION NOT NULL,
    mrs_pwr_10_ws_b               DOUBLE PRECISION NOT NULL,
    mrs_pwr_10_cm_a               DOUBLE PRECISION NOT NULL,
    mrs_pwr_10_cm_b               DOUBLE PRECISION NOT NULL,
    mrs_pwr_30_ws_a               DOUBLE PRECISION NOT NULL,
    mrs_pwr_30_ws_b               DOUBLE PRECISION NOT NULL,
    mrs_pwr_30_cm_a               DOUBLE PRECISION NOT NULL,
    mrs_pwr_30_cm_b               DOUBLE PRECISION NOT NULL,
    mrs_game_streak_ws_a          DOUBLE PRECISION NOT NULL,
    mrs_game_streak_ws_b          DOUBLE PRECISION NOT NULL,
    mrs_game_streak_cm_a          DOUBLE PRECISION NOT NULL,
    mrs_game_streak_cm_b          DOUBLE PRECISION NOT NULL,
    "markov_set_win_prob_A"       DOUBLE PRECISION NOT NULL,

    PRIMARY KEY (match_id_int, set_number, game_number_in_set, model_version)
)
"""

_CREATE_INDEX_PREDICTED_AT_DDL = """
CREATE INDEX IF NOT EXISTS idx_predictions_predicted_at
    ON live.predictions (predicted_at)
"""

_CREATE_INDEX_MODEL_CONFIDENCE_DDL = """
CREATE INDEX IF NOT EXISTS idx_predictions_model_confidence
    ON live.predictions (model_version, confidence DESC)
"""

# (label_for_progress, ddl) — kept ordered for dry-run output.
_DDL_STEPS: list[tuple[str, str]] = [
    ("live.predictions",                  _CREATE_PREDICTIONS_DDL),
    ("idx_predictions_predicted_at",      _CREATE_INDEX_PREDICTED_AT_DDL),
    ("idx_predictions_model_confidence",  _CREATE_INDEX_MODEL_CONFIDENCE_DDL),
]

_EXPECTED_COLUMN_COUNT = 71   # 8 identity + 63 features
_EXPECTED_INDEX_COUNT = 3     # PK + 2 secondary indexes


# ---------------------------------------------------------------------------
# information_schema helpers (mirror migrate_book_and_audit_v2.py)
# ---------------------------------------------------------------------------

def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
          AND table_type = 'BASE TABLE'
        """,
        (schema, table),
    )
    return cur.fetchone() is not None


def _index_exists(cur, schema: str, index: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
        (schema, index),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _create_predictions_table(cur, counters: dict[str, int]) -> None:
    if _table_exists(cur, "live", "predictions"):
        print("[SKIP] live.predictions already exists")
        counters["skipped"] += 1
        return
    cur.execute(_CREATE_PREDICTIONS_DDL)
    print("[OK]   created live.predictions")
    counters["created"] += 1


def _create_indexes(cur, counters: dict[str, int]) -> None:
    for idx_name, ddl in (
        ("idx_predictions_predicted_at",     _CREATE_INDEX_PREDICTED_AT_DDL),
        ("idx_predictions_model_confidence", _CREATE_INDEX_MODEL_CONFIDENCE_DDL),
    ):
        if _index_exists(cur, "live", idx_name):
            print(f"[SKIP] {idx_name} already exists")
            counters["skipped"] += 1
            continue
        cur.execute(ddl)
        print(f"[OK]   created {idx_name}")
        counters["created"] += 1


# ---------------------------------------------------------------------------
# Post-migration verification
# ---------------------------------------------------------------------------

def _verify(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'live' AND table_name = 'predictions'
            """
        )
        cols = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(*) FROM pg_indexes
            WHERE schemaname = 'live' AND tablename = 'predictions'
            """
        )
        idxs = int(cur.fetchone()[0])

        cur.execute("SELECT COUNT(*) FROM live.predictions")
        rows = int(cur.fetchone()[0])

    print()
    print("--- verification ---")
    print(f"columns: {cols} (expected {_EXPECTED_COLUMN_COUNT})")
    print(f"indexes: {idxs} (expected {_EXPECTED_INDEX_COUNT})")
    print(f"rows:    {rows}")

    failures: list[str] = []
    if cols != _EXPECTED_COLUMN_COUNT:
        failures.append(f"columns={cols} (expected {_EXPECTED_COLUMN_COUNT})")
    if idxs != _EXPECTED_INDEX_COUNT:
        failures.append(f"indexes={idxs} (expected {_EXPECTED_INDEX_COUNT})")

    if failures:
        print(f"VERIFICATION FAILED: {', '.join(failures)}")
        return False
    print("VERIFICATION OK")
    return True


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _dry_run() -> int:
    print("DRY RUN — no DDL will be executed, no transaction opened")
    print("=" * 56)
    for label, ddl in _DDL_STEPS:
        print()
        print(f"--- {label} ---")
        print(ddl.strip())
    print()
    print("DRY RUN complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create live.predictions (Step 6C migration).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the DDL that would be executed, then exit without "
             "connecting to the database.",
    )
    args = parser.parse_args()

    if args.dry_run:
        return _dry_run()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    print("Step 6C migration — create live.predictions")
    print("=" * 56)

    counters = {"created": 0, "skipped": 0, "errors": 0}

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            print()
            print("--- live.predictions table ---")
            _create_predictions_table(cur, counters)

            print()
            print("--- live.predictions indexes ---")
            _create_indexes(cur, counters)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        counters["errors"] += 1
        print(f"[ERR]  {exc}", file=sys.stderr)
        print("Migration FAILED — transaction rolled back.", file=sys.stderr)
        conn.close()
        return 2

    print()
    print(
        f"Done: {counters['created']} created, "
        f"{counters['skipped']} skipped, "
        f"{counters['errors']} errors"
    )

    try:
        ok = _verify(conn)
    finally:
        if not conn.closed:
            conn.close()

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
