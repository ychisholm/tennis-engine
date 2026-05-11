"""
Reporter: persist validator output to audit.verification_reports
and audit.gap_reports.

One public function — `write_report` — takes the (gaps, summary) tuple
returned by `validator.validate_match` and writes the summary row plus
one gap row per Gap object, inside a SAVEPOINT so a single match's
write is atomic. The caller controls the outer transaction (i.e. when
to COMMIT / when to ROLLBACK across many matches).

Schema reminder (after migrate_book_and_audit_v2.py):
  * audit.verification_reports.source TEXT NOT NULL
  * audit.gap_reports.source         TEXT NOT NULL
  * the legacy column names live_point_count / live_final_score still
    hold the walked-final values regardless of source ('live' or
    'backfill') — they're misnamed but renaming them is out of scope.
"""
from __future__ import annotations

import uuid
from typing import Iterable

import psycopg2.extensions
from psycopg2.extras import Json

from src.verification.validator import Gap, ValidationSummary


VALID_SOURCES = frozenset({"live", "backfill"})


_INSERT_REPORT = """
INSERT INTO audit.verification_reports (
    source, match_id, verification_run_id,
    live_point_count, inferred_missing_points,
    live_final_score, recorded_final_score, final_score_match,
    total_sets, clean_set_count, gapped_set_count,
    gap_count, severity_max, verdict,
    set_breakdown, notes
) VALUES (
    %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s
)
RETURNING id
"""

_INSERT_GAP = """
INSERT INTO audit.gap_reports (
    verification_run_id, match_id, source,
    gap_type, severity, description,
    before_state, after_state,
    inferred_skipped_points, set_number, game_number
) VALUES (
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s
)
"""


def write_report(
    conn,
    *,
    match_id: str,
    verification_run_id: uuid.UUID,
    source: str,
    gaps: Iterable[Gap],
    summary: ValidationSummary,
) -> int:
    """
    Write one verification_reports row and N gap_reports rows for a single
    match, inside a SAVEPOINT. Returns the inserted
    verification_reports.id.

    Caller responsibilities:
      * own the outer transaction (no commit/rollback inside this fn)
      * pass `source` ∈ {'live', 'backfill'} — anything else raises ValueError
      * pass `match_id` already stringified to match VARCHAR(100) column
      * pass `verification_run_id` as a uuid.UUID (or anything str()-castable
        to a UUID)
    """
    if source not in VALID_SOURCES:
        raise ValueError(
            f"source must be one of {sorted(VALID_SOURCES)!r}, got {source!r}"
        )

    run_id_str = str(verification_run_id)
    gaps_list = list(gaps)

    # Use the default tuple-returning cursor regardless of what factory
    # the caller has configured on the connection. The reporter relies on
    # positional indexing of the RETURNING result, and works the same way
    # whether the caller uses RealDictCursor or anything else.
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("SAVEPOINT sp_write_report")
        try:
            cur.execute(
                _INSERT_REPORT,
                (
                    source,
                    str(match_id),
                    run_id_str,
                    summary.live_point_count,
                    summary.inferred_missing_points,
                    summary.live_final_score,
                    summary.recorded_final_score,
                    summary.final_score_match,
                    summary.total_sets,
                    summary.clean_set_count,
                    summary.gapped_set_count,
                    summary.gap_count,
                    summary.severity_max,
                    summary.verdict,
                    Json(summary.set_breakdown),
                    None,
                ),
            )
            report_id = cur.fetchone()[0]

            for g in gaps_list:
                cur.execute(
                    _INSERT_GAP,
                    (
                        run_id_str,
                        str(match_id),
                        source,
                        g.gap_type,
                        g.severity,
                        g.description,
                        Json(g.before_state),
                        Json(g.after_state),
                        g.inferred_skipped_points,
                        g.set_number,
                        g.game_number,
                    ),
                )
            cur.execute("RELEASE SAVEPOINT sp_write_report")
            return report_id
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_write_report")
            raise
