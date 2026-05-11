"""
Phase 2 audit-tables setup.

Idempotently creates the two tables the daily live-tracking validator
writes to (audit.verification_reports and audit.gap_reports) plus
their indexes. Re-callable from anywhere we need to ensure these tables
exist; the migration script in scripts/setup/migrate_phase2_audit_tables.py
is the canonical caller.
"""
from __future__ import annotations

from typing import Dict


_VERIFICATION_REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS audit.verification_reports (
    id                      BIGSERIAL PRIMARY KEY,
    verification_run_id     UUID NOT NULL,
    run_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    match_id                VARCHAR(100) NOT NULL,
    source                  TEXT NOT NULL,
    live_point_count        INT NOT NULL,
    inferred_missing_points INT NOT NULL DEFAULT 0,
    live_final_score        TEXT,
    recorded_final_score    TEXT,
    final_score_match       BOOLEAN NOT NULL,
    total_sets              INT,
    clean_set_count         INT NOT NULL DEFAULT 0,
    gapped_set_count        INT NOT NULL DEFAULT 0,
    gap_count               INT NOT NULL DEFAULT 0,
    severity_max            TEXT,
    verdict                 TEXT,
    set_breakdown           JSONB,
    notes                   JSONB
)
"""

_GAP_REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS audit.gap_reports (
    id                      BIGSERIAL PRIMARY KEY,
    verification_run_id     UUID NOT NULL,
    match_id                VARCHAR(100) NOT NULL,
    source                  TEXT NOT NULL,
    gap_type                TEXT NOT NULL,
    severity                TEXT,
    description             TEXT,
    before_state            JSONB,
    after_state             JSONB,
    inferred_skipped_points INT,
    set_number              INT,
    game_number             INT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# (index_name, "schema.table (columns)")
_INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_verification_reports_match_id", "audit.verification_reports (match_id)"),
    ("idx_verification_reports_run_id",   "audit.verification_reports (verification_run_id)"),
    ("idx_verification_reports_run_at",   "audit.verification_reports (run_at)"),
    ("idx_verification_reports_verdict",  "audit.verification_reports (verdict)"),
    ("idx_gap_reports_match_id",          "audit.gap_reports (match_id)"),
    ("idx_gap_reports_run_id",            "audit.gap_reports (verification_run_id)"),
    ("idx_gap_reports_gap_type",          "audit.gap_reports (gap_type)"),
    ("idx_gap_reports_severity",          "audit.gap_reports (severity)"),
)


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


def setup_audit_tables(conn) -> Dict[str, str]:
    """
    Idempotently create audit.verification_reports and audit.gap_reports
    plus their indexes.

    Takes an open psycopg2 connection. Caller is responsible for
    commit/rollback. Each DDL operation uses IF NOT EXISTS.

    Returns a dict like
        {"verification_reports": "CREATED" | "SKIP",
         "gap_reports":          "CREATED" | "SKIP",
         "indexes":              "CREATED" | "SKIP"}
    so the caller can print status. CREATED vs SKIP is determined by
    querying information_schema BEFORE running the CREATE statement.
    The "indexes" status is "CREATED" if any of the eight expected
    indexes was missing prior to this call, otherwise "SKIP".
    """
    result: Dict[str, str] = {}
    with conn.cursor() as cur:
        had_verification = _table_exists(cur, "audit", "verification_reports")
        cur.execute(_VERIFICATION_REPORTS_DDL)
        result["verification_reports"] = "SKIP" if had_verification else "CREATED"

        had_gap_reports = _table_exists(cur, "audit", "gap_reports")
        cur.execute(_GAP_REPORTS_DDL)
        result["gap_reports"] = "SKIP" if had_gap_reports else "CREATED"

        any_index_missing = any(
            not _index_exists(cur, "audit", idx_name) for idx_name, _ in _INDEXES
        )
        for idx_name, target in _INDEXES:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {target}")
        result["indexes"] = "CREATED" if any_index_missing else "SKIP"

    return result
