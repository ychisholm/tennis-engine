"""
Tests for src/verification/reporter.py

DB-integration tests gated on DATABASE_URL. Each test uses a unique
verification_run_id (uuid4) and cleans up its own rows via DELETE
on that run_id in teardown. Tests use COMMIT so the rows are real,
which also exercises the SAVEPOINT release path.

Skipped when DATABASE_URL is not set so the suite stays green in
environments without a Postgres instance.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

from src.verification.validator import Gap, ValidationSummary

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
_DATABASE_URL = os.getenv("DATABASE_URL")

if not _DATABASE_URL:
    pytest.skip(
        "DATABASE_URL not set — skipping reporter integration tests",
        allow_module_level=True,
    )

import psycopg2  # noqa: E402

from src.verification.reporter import write_report  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(**overrides) -> ValidationSummary:
    base = {
        "live_point_count": 80,
        "inferred_missing_points": 0,
        "live_final_score": "6-2 6-0",
        "recorded_final_score": "6-2 6-0",
        "final_score_match": True,
        "total_sets": 2,
        "clean_set_count": 2,
        "gapped_set_count": 0,
        "gap_count": 0,
        "severity_max": None,
        "verdict": "clean",
        "set_breakdown": [
            {"set_num": 1, "live_pts": 40, "gap_count": 0, "clean": True},
            {"set_num": 2, "live_pts": 30, "gap_count": 0, "clean": True},
        ],
    }
    base.update(overrides)
    return ValidationSummary(**base)


def _make_gap(**overrides) -> Gap:
    base = {
        "gap_type": "score_jump",
        "severity": "low",
        "description": "Score jumped from 0-0 to 30-0",
        "before_state": {"sets_a": 0, "games_a": 0, "score_a": "0"},
        "after_state": {"sets_a": 0, "games_a": 0, "score_a": "30"},
        "inferred_skipped_points": 1,
        "set_number": 1,
        "game_number": 1,
    }
    base.update(overrides)
    return Gap(**base)


@pytest.fixture
def conn():
    c = psycopg2.connect(_DATABASE_URL)
    c.autocommit = False
    try:
        yield c
    finally:
        c.close()


def _cleanup(conn, run_id: uuid.UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM audit.gap_reports WHERE verification_run_id = %s",
            (str(run_id),),
        )
        cur.execute(
            "DELETE FROM audit.verification_reports WHERE verification_run_id = %s",
            (str(run_id),),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_writes_summary_and_gap_rows_for_live(conn):
    run_id = uuid.uuid4()
    summary = _make_summary(
        verdict="material_gaps",
        gap_count=2,
        gapped_set_count=1,
        clean_set_count=1,
        severity_max="medium",
    )
    gaps = [
        _make_gap(severity="medium", gap_type="score_regression"),
        _make_gap(severity="low", gap_type="score_jump"),
    ]
    try:
        report_id = write_report(
            conn,
            match_id="99999001",
            verification_run_id=run_id,
            source="live",
            gaps=gaps,
            summary=summary,
        )
        conn.commit()
        assert isinstance(report_id, int) and report_id > 0

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, match_id, verdict, gap_count
                FROM audit.verification_reports
                WHERE verification_run_id = %s
                """,
                (str(run_id),),
            )
            rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0] == ("live", "99999001", "material_gaps", 2)

            cur.execute(
                """
                SELECT source, match_id, gap_type, severity
                FROM audit.gap_reports
                WHERE verification_run_id = %s
                ORDER BY id
                """,
                (str(run_id),),
            )
            grows = cur.fetchall()
            assert len(grows) == 2
            assert grows[0] == ("live", "99999001", "score_regression", "medium")
            assert grows[1] == ("live", "99999001", "score_jump", "low")
    finally:
        _cleanup(conn, run_id)


def test_writes_summary_and_gap_rows_for_backfill(conn):
    run_id = uuid.uuid4()
    summary = _make_summary(verdict="minor_gaps", gap_count=1)
    gaps = [_make_gap()]
    try:
        write_report(
            conn,
            match_id="99999002",
            verification_run_id=run_id,
            source="backfill",
            gaps=gaps,
            summary=summary,
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT source FROM audit.verification_reports "
                "WHERE verification_run_id = %s",
                (str(run_id),),
            )
            assert cur.fetchone() == ("backfill",)
            cur.execute(
                "SELECT source FROM audit.gap_reports "
                "WHERE verification_run_id = %s",
                (str(run_id),),
            )
            assert cur.fetchone() == ("backfill",)
    finally:
        _cleanup(conn, run_id)


def test_clean_match_writes_only_summary_row(conn):
    run_id = uuid.uuid4()
    summary = _make_summary()  # default verdict='clean', gap_count=0
    try:
        write_report(
            conn,
            match_id="99999003",
            verification_run_id=run_id,
            source="live",
            gaps=[],
            summary=summary,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM audit.verification_reports "
                "WHERE verification_run_id = %s",
                (str(run_id),),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT COUNT(*) FROM audit.gap_reports "
                "WHERE verification_run_id = %s",
                (str(run_id),),
            )
            assert cur.fetchone()[0] == 0
    finally:
        _cleanup(conn, run_id)


def test_invalid_source_raises_value_error(conn):
    run_id = uuid.uuid4()
    with pytest.raises(ValueError, match="source must be one of"):
        write_report(
            conn,
            match_id="99999004",
            verification_run_id=run_id,
            source="bogus",
            gaps=[],
            summary=_make_summary(),
        )
    # No write should have occurred.
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM audit.verification_reports "
            "WHERE verification_run_id = %s",
            (str(run_id),),
        )
        assert cur.fetchone()[0] == 0


def test_jsonb_set_breakdown_and_gap_states_round_trip(conn):
    """Verify the JSONB columns serialize and come back as the original dicts."""
    run_id = uuid.uuid4()
    set_breakdown = [
        {"set_num": 1, "live_pts": 42, "gap_count": 1, "clean": False},
    ]
    before = {"sets_a": 0, "games_a": 5, "score_a": "40"}
    after = {"game_won_by": "A"}
    summary = _make_summary(set_breakdown=set_breakdown)
    gaps = [
        _make_gap(before_state=before, after_state=after),
    ]
    try:
        write_report(
            conn,
            match_id="99999005",
            verification_run_id=run_id,
            source="live",
            gaps=gaps,
            summary=summary,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_breakdown FROM audit.verification_reports "
                "WHERE verification_run_id = %s",
                (str(run_id),),
            )
            assert cur.fetchone()[0] == set_breakdown
            cur.execute(
                "SELECT before_state, after_state FROM audit.gap_reports "
                "WHERE verification_run_id = %s",
                (str(run_id),),
            )
            row = cur.fetchone()
            assert row[0] == before
            assert row[1] == after
    finally:
        _cleanup(conn, run_id)
