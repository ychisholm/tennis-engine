"""
Integration test for scripts/setup/migrate_to_v2_schemas.py.

Skipped unless TEST_DATABASE_URL is set. The test seeds a fresh database
with the legacy schema layout (live_raw, live_processed, public.poll_audit_log
populated with sample rows), runs the migration script as a subprocess, and
verifies that:
  - live, audit, and book schemas exist
  - every renamed/moved table landed in its new home with rows preserved
  - dropped tables (live_processed.points, live_processed.dashboard_log) are gone
  - empty live_raw / live_processed schemas are dropped
  - re-running the migration is a no-op

WARNING: this test issues DROP SCHEMA ... CASCADE against TEST_DATABASE_URL.
Point it at a disposable database, NEVER at production.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _TEST_URL,
    reason="TEST_DATABASE_URL not set — skipping migration integration test",
)

# Defer psycopg2 import until after the skip-gate.
import psycopg2  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_SCRIPT = _ROOT / "scripts" / "setup" / "migrate_to_v2_schemas.py"


def _drop_all(conn) -> None:
    with conn.cursor() as cur:
        for s in ("live", "audit", "book", "live_raw", "live_processed"):
            cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
        cur.execute('DROP TABLE IF EXISTS "public"."poll_audit_log"')
    conn.commit()


def _seed(conn) -> None:
    """Recreate the legacy layout with sample rows."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA live_raw")
        cur.execute("CREATE SCHEMA live_processed")

        cur.execute("""
            CREATE TABLE live_raw.match_details (
                match_id INTEGER PRIMARY KEY,
                player_a VARCHAR,
                player_b VARCHAR,
                polled_at TIMESTAMPTZ
            )
        """)
        cur.execute(
            "CREATE INDEX match_details_player_a_idx ON live_raw.match_details(player_a)"
        )
        cur.execute(
            "INSERT INTO live_raw.match_details VALUES (1, 'A', 'B', '2026-01-01')"
        )
        cur.execute(
            "INSERT INTO live_raw.match_details VALUES (2, 'C', 'D', '2026-01-02')"
        )

        cur.execute("""
            CREATE TABLE live_raw.api_call_log (
                id BIGSERIAL PRIMARY KEY,
                endpoint TEXT,
                match_id VARCHAR
            )
        """)
        cur.execute(
            "INSERT INTO live_raw.api_call_log (endpoint, match_id) VALUES ('m', '1')"
        )

        cur.execute("CREATE TABLE live_processed.points (match_id INTEGER, point_num INTEGER)")
        cur.execute("INSERT INTO live_processed.points VALUES (1, 1)")

        cur.execute(
            "CREATE TABLE live_processed.dashboard_log (match_id INTEGER, point_num INTEGER)"
        )
        cur.execute("INSERT INTO live_processed.dashboard_log VALUES (1, 1)")

        cur.execute("""
            CREATE TABLE public.poll_audit_log (
                id SERIAL PRIMARY KEY,
                event_type VARCHAR
            )
        """)
        cur.execute("INSERT INTO public.poll_audit_log (event_type) VALUES ('TICK')")
    conn.commit()


def _run_migration() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = _TEST_URL
    return subprocess.run(
        [sys.executable, str(_MIGRATION_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return cur.fetchone() is not None


def _schema_exists(cur, schema: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,),
    )
    return cur.fetchone() is not None


@pytest.fixture
def conn():
    c = psycopg2.connect(_TEST_URL)
    c.autocommit = False
    yield c
    try:
        _drop_all(c)
    except Exception:
        c.rollback()
    c.close()


def test_migration_full_lifecycle(conn):
    _drop_all(conn)
    _seed(conn)

    result = _run_migration()
    assert result.returncode == 0, (
        f"Migration failed.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    with conn.cursor() as cur:
        for s in ("live", "audit", "book"):
            assert _schema_exists(cur, s), f"schema {s} should exist"

        assert _table_exists(cur, "live", "match_polls")
        assert _table_exists(cur, "audit", "api_call_log")
        assert _table_exists(cur, "audit", "poll_audit_log")

        assert not _table_exists(cur, "live_raw", "match_details")
        assert not _table_exists(cur, "live_raw", "api_call_log")
        assert not _table_exists(cur, "public", "poll_audit_log")

        assert not _table_exists(cur, "live_processed", "points")
        assert not _table_exists(cur, "live_processed", "dashboard_log")

        assert not _schema_exists(cur, "live_raw")
        assert not _schema_exists(cur, "live_processed")

        cur.execute("SELECT COUNT(*) FROM live.match_polls")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT COUNT(*) FROM audit.api_call_log")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM audit.poll_audit_log")
        assert cur.fetchone()[0] == 1

        cur.execute(
            """
            SELECT 1 FROM pg_indexes
            WHERE schemaname = 'live'
              AND tablename = 'match_polls'
              AND indexname = 'match_polls_player_a_idx'
            """
        )
        assert cur.fetchone() is not None, (
            "match_details_player_a_idx should have been renamed to "
            "match_polls_player_a_idx"
        )


def test_migration_is_idempotent(conn):
    _drop_all(conn)
    _seed(conn)

    first = _run_migration()
    assert first.returncode == 0, (
        f"First migration failed.\nSTDOUT:\n{first.stdout}\nSTDERR:\n{first.stderr}"
    )

    second = _run_migration()
    assert second.returncode == 0, (
        f"Second migration failed.\nSTDOUT:\n{second.stdout}\nSTDERR:\n{second.stderr}"
    )

    with conn.cursor() as cur:
        assert _table_exists(cur, "live", "match_polls")
        assert _table_exists(cur, "audit", "api_call_log")
        cur.execute("SELECT COUNT(*) FROM live.match_polls")
        assert cur.fetchone()[0] == 2
