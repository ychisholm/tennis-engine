#!/usr/bin/env python3
"""
Phase 2 schema migration — audit `source` column + book.* tables.

Operations (all inside one transaction):
  1. Add audit.verification_reports.source TEXT NOT NULL (default backfill
     existing rows to 'live' before applying NOT NULL).
  2. Same three-step pattern on audit.live_gap_reports (or audit.gap_reports
     if the rename below has already happened on a previous run).
  3. Rename audit.live_gap_reports → audit.gap_reports.
  4. Rename the four supporting indexes from idx_live_gap_reports_* to
     idx_gap_reports_* so names stay in sync with the renamed table.
  5. Create book.matches, book.players, book.player_career_stats, book.points
     (already-created `book` schema from Phase 1).
  6. Create supporting indexes.

Re-running after a successful run is a no-op: every CREATE uses IF NOT EXISTS,
every ALTER is preceded by an information_schema existence/nullability check,
and the rename short-circuits once the target name is in place.

Usage:
    .venv/bin/python scripts/setup/migrate_book_and_audit_v2.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


# ---------------------------------------------------------------------------
# book.* DDL
# ---------------------------------------------------------------------------

_BOOK_MATCHES_DDL = """
CREATE TABLE IF NOT EXISTS book.matches (
    match_id            BIGINT PRIMARY KEY,
    match_date          DATE NOT NULL,
    tournament          TEXT,
    surface             TEXT,
    player_a_id         BIGINT NOT NULL,
    player_b_id         BIGINT NOT NULL,
    final_score         TEXT NOT NULL,
    winner_id           BIGINT NOT NULL,
    sets_a              INT NOT NULL,
    sets_b              INT NOT NULL,
    verification_run_id UUID NOT NULL,
    promoted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_BOOK_PLAYERS_DDL = """
CREATE TABLE IF NOT EXISTS book.players (
    player_id   BIGINT PRIMARY KEY,
    name        TEXT NOT NULL,
    dob         DATE,
    hand        TEXT,
    country     VARCHAR(3),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_BOOK_PLAYER_CAREER_STATS_DDL = """
CREATE TABLE IF NOT EXISTS book.player_career_stats (
    player_id            BIGINT PRIMARY KEY REFERENCES book.players(player_id),
    serve_points_played  BIGINT NOT NULL DEFAULT 0,
    serve_points_won     BIGINT NOT NULL DEFAULT 0,
    return_points_played BIGINT NOT NULL DEFAULT 0,
    return_points_won    BIGINT NOT NULL DEFAULT 0,
    matches_played       INT NOT NULL DEFAULT 0,
    matches_won          INT NOT NULL DEFAULT 0,
    last_updated         TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_BOOK_POINTS_DDL = """
CREATE TABLE IF NOT EXISTS book.points (
    id               BIGSERIAL PRIMARY KEY,
    match_id         BIGINT NOT NULL REFERENCES book.matches(match_id),
    set_num          INT NOT NULL,
    game_num         INT NOT NULL,
    point_num        INT NOT NULL,
    global_point_num INT NOT NULL,
    server_id        BIGINT NOT NULL,
    point_winner_id  BIGINT NOT NULL,
    score_after      TEXT,
    is_tiebreak      BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (match_id, set_num, game_num, point_num)
)
"""

# (name, qualified_table_name, "CREATE TABLE IF NOT EXISTS ..." DDL)
_BOOK_TABLES: list[tuple[str, str, str]] = [
    ("matches",             "book.matches",             _BOOK_MATCHES_DDL),
    ("players",             "book.players",             _BOOK_PLAYERS_DDL),
    ("player_career_stats", "book.player_career_stats", _BOOK_PLAYER_CAREER_STATS_DDL),
    ("points",              "book.points",              _BOOK_POINTS_DDL),
]


# (schema, index_name, "CREATE INDEX IF NOT EXISTS ..." DDL)
_BOOK_INDEXES: list[tuple[str, str, str]] = [
    ("book", "idx_book_matches_date",
     "CREATE INDEX IF NOT EXISTS idx_book_matches_date "
     "ON book.matches (match_date DESC)"),
    ("book", "idx_book_matches_winner",
     "CREATE INDEX IF NOT EXISTS idx_book_matches_winner "
     "ON book.matches (winner_id)"),
    ("book", "idx_book_points_match_set_game",
     "CREATE INDEX IF NOT EXISTS idx_book_points_match_set_game "
     "ON book.points (match_id, set_num, game_num, point_num)"),
    ("book", "idx_book_points_server",
     "CREATE INDEX IF NOT EXISTS idx_book_points_server "
     "ON book.points (server_id)"),
    ("book", "idx_book_points_winner",
     "CREATE INDEX IF NOT EXISTS idx_book_points_winner "
     "ON book.points (point_winner_id)"),
    ("book", "idx_book_players_name",
     "CREATE INDEX IF NOT EXISTS idx_book_players_name "
     "ON book.players (name)"),
]


# ---------------------------------------------------------------------------
# information_schema helpers
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


def _column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """,
        (schema, table, column),
    )
    return cur.fetchone() is not None


def _column_is_nullable(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """,
        (schema, table, column),
    )
    row = cur.fetchone()
    return row is not None and row[0] == "YES"


def _index_exists(cur, schema: str, index: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
        (schema, index),
    )
    return cur.fetchone() is not None


def _gap_table_name_in_use(cur) -> str | None:
    """The gap-reports table may be named live_gap_reports (pre-rename) or
    gap_reports (post-rename). Return whichever is currently present."""
    if _table_exists(cur, "audit", "live_gap_reports"):
        return "live_gap_reports"
    if _table_exists(cur, "audit", "gap_reports"):
        return "gap_reports"
    return None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _add_source_column(cur, table_name: str, counters: dict[str, int]) -> None:
    """Idempotently add a NOT NULL `source` column to audit.{table_name},
    defaulting pre-existing rows to 'live'. Three steps, each independently
    SKIP-able on re-run."""
    qualified = f"audit.{table_name}"

    # Step A — add column
    if _column_exists(cur, "audit", table_name, "source"):
        print(f"[SKIP] {qualified}.source column already exists")
        counters["skipped"] += 1
    else:
        cur.execute(f"ALTER TABLE {qualified} ADD COLUMN IF NOT EXISTS source TEXT")
        print(f"[OK]   added source column to {qualified}")
        counters["created"] += 1

    # Step B — backfill existing rows
    cur.execute(f"UPDATE {qualified} SET source = 'live' WHERE source IS NULL")
    n = cur.rowcount
    if n > 0:
        print(f"[OK]   backfilled {n} {qualified}.source rows to 'live'")
        counters["created"] += 1
    else:
        print(f"[SKIP] no {qualified}.source NULLs to backfill")
        counters["skipped"] += 1

    # Step C — enforce NOT NULL
    if _column_is_nullable(cur, "audit", table_name, "source"):
        cur.execute(f"ALTER TABLE {qualified} ALTER COLUMN source SET NOT NULL")
        print(f"[OK]   set {qualified}.source NOT NULL")
        counters["created"] += 1
    else:
        print(f"[SKIP] {qualified}.source already NOT NULL")
        counters["skipped"] += 1


def _rename_gap_reports(cur, counters: dict[str, int]) -> None:
    if _table_exists(cur, "audit", "gap_reports"):
        print("[SKIP] audit.gap_reports already exists (rename already applied)")
        counters["skipped"] += 1
        return
    if not _table_exists(cur, "audit", "live_gap_reports"):
        print("[SKIP] audit.live_gap_reports not present; nothing to rename")
        counters["skipped"] += 1
        return
    cur.execute("ALTER TABLE audit.live_gap_reports RENAME TO gap_reports")
    print("[OK]   renamed audit.live_gap_reports -> audit.gap_reports")
    counters["created"] += 1


# (old_index_name, new_index_name) — Postgres preserves index names through
# ALTER TABLE RENAME, so on existing DBs these are still attached to the
# now-renamed audit.gap_reports table under their old names.
_GAP_INDEX_RENAMES: list[tuple[str, str]] = [
    ("idx_live_gap_reports_match_id", "idx_gap_reports_match_id"),
    ("idx_live_gap_reports_run_id",   "idx_gap_reports_run_id"),
    ("idx_live_gap_reports_gap_type", "idx_gap_reports_gap_type"),
    ("idx_live_gap_reports_severity", "idx_gap_reports_severity"),
]


def _rename_gap_indexes(cur, counters: dict[str, int]) -> None:
    for old_name, new_name in _GAP_INDEX_RENAMES:
        if _index_exists(cur, "audit", new_name):
            print(f"[SKIP] {new_name} already exists")
            counters["skipped"] += 1
            continue
        if not _index_exists(cur, "audit", old_name):
            print(f"[SKIP] {old_name} not present; nothing to rename")
            counters["skipped"] += 1
            continue
        cur.execute(
            f"ALTER INDEX IF EXISTS audit.{old_name} RENAME TO {new_name}"
        )
        print(f"[OK]   renamed {old_name} -> {new_name}")
        counters["created"] += 1


def _create_book_tables(cur, counters: dict[str, int]) -> None:
    for table_name, qualified, ddl in _BOOK_TABLES:
        if _table_exists(cur, "book", table_name):
            print(f"[SKIP] {qualified} already exists")
            counters["skipped"] += 1
            continue
        cur.execute(ddl)
        print(f"[OK]   created {qualified}")
        counters["created"] += 1


def _create_book_indexes(cur, counters: dict[str, int]) -> None:
    for schema, index_name, ddl in _BOOK_INDEXES:
        if _index_exists(cur, schema, index_name):
            print(f"[SKIP] {index_name} already exists")
            counters["skipped"] += 1
            continue
        cur.execute(ddl)
        print(f"[OK]   created {index_name}")
        counters["created"] += 1


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    print("Phase 2 schema migration — audit.source + book.* tables")
    print("=" * 56)

    counters = {"created": 0, "skipped": 0, "errors": 0}

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            print()
            print("--- audit.verification_reports.source ---")
            _add_source_column(cur, "verification_reports", counters)

            print()
            print("--- audit.(live_)gap_reports.source ---")
            gap_table = _gap_table_name_in_use(cur)
            if gap_table is None:
                print("[SKIP] no gap-reports table found (expected audit.live_gap_reports or audit.gap_reports)")
                counters["skipped"] += 1
            else:
                _add_source_column(cur, gap_table, counters)

            print()
            print("--- rename audit.live_gap_reports -> audit.gap_reports ---")
            _rename_gap_reports(cur, counters)

            print()
            print("--- rename gap-report indexes ---")
            _rename_gap_indexes(cur, counters)

            print()
            print("--- book.* tables ---")
            _create_book_tables(cur, counters)

            print()
            print("--- book.* indexes ---")
            _create_book_indexes(cur, counters)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        counters["errors"] += 1
        print(f"[ERR]  {exc}", file=sys.stderr)
        print(
            "Migration FAILED — transaction rolled back.",
            file=sys.stderr,
        )
        conn.close()
        return 2
    finally:
        if not conn.closed:
            conn.close()

    print()
    print(
        f"Done: {counters['created']} created, "
        f"{counters['skipped']} skipped, "
        f"{counters['errors']} errors"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
