#!/usr/bin/env python3
"""
Phase 2 audit-tables migration.

Adds audit.verification_reports and audit.gap_reports plus their
indexes. The migration is purely additive — no existing tables or
columns are modified.

Run scripts/setup/migrate_book_and_audit_v2.py FIRST on any pre-v2
database — this helper creates tables with the post-rename schema.

Runs inside a single transaction. Any failure rolls every operation
back. Re-running after a successful run is a no-op (idempotent).

Usage:
    .venv/bin/python scripts/setup/migrate_phase2_audit_tables.py
"""
from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from src.verification.db_setup import setup_audit_tables  # noqa: E402


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    with closing(psycopg2.connect(db_url)) as conn:
        try:
            result = setup_audit_tables(conn)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print("Migration FAILED — transaction rolled back.", file=sys.stderr)
            print(f"  Error: {exc}", file=sys.stderr)
            print("Migration failed", file=sys.stderr)
            return 2

    print("Migration committed. Operations performed:")
    print(f"  {result['verification_reports']:<8} audit.verification_reports")
    print(f"  {result['gap_reports']:<8} audit.gap_reports")
    print(f"  {result['indexes']:<8} indexes")
    print()
    print("Migration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
