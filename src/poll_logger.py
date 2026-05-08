"""
Audit-log writer for the live polling pipeline.

Writes one row to audit.poll_audit_log per significant event so post-hoc
diagnostic scripts can reconstruct exactly when each match was discovered,
when polling started, and when point data first arrived.

Failures are silently swallowed: a logging hiccup must never crash the
scheduler or worker that called us.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Optional

import psycopg2
from dotenv import load_dotenv

_log = logging.getLogger(__name__)


class PollLogger:
    def __init__(self, db_url: Optional[str] = None) -> None:
        load_dotenv()
        url = db_url or os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL environment variable is not set.")
        self._conn = psycopg2.connect(url)
        self._lock = threading.Lock()

    def setup(self) -> None:
        ddl = """
        CREATE SCHEMA IF NOT EXISTS audit;
        CREATE TABLE IF NOT EXISTS audit.poll_audit_log (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type VARCHAR(50) NOT NULL,
            match_id VARCHAR(100),
            detail TEXT,
            points_count INTEGER,
            triggering_call_id BIGINT,
            reason TEXT,
            metadata JSONB,
            poll_cycle_id UUID
        );
        CREATE INDEX IF NOT EXISTS idx_poll_audit_match_id
            ON audit.poll_audit_log(match_id);
        CREATE INDEX IF NOT EXISTS idx_poll_audit_timestamp
            ON audit.poll_audit_log(timestamp);
        CREATE INDEX IF NOT EXISTS poll_audit_log_poll_cycle_idx
            ON audit.poll_audit_log(poll_cycle_id);
        """
        try:
            with self._lock:
                with self._conn.cursor() as cur:
                    cur.execute(ddl)
                self._conn.commit()
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            _log.warning("PollLogger.setup failed: %s", exc)

    def log(
        self,
        event_type: str,
        match_id: Optional[object] = None,
        detail: Optional[str] = None,
        points_count: Optional[int] = None,
        poll_cycle_id: Optional[uuid.UUID] = None,
    ) -> None:
        try:
            with self._lock:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO audit.poll_audit_log
                            (event_type, match_id, detail, points_count, poll_cycle_id)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            event_type,
                            str(match_id) if match_id is not None else None,
                            detail,
                            points_count,
                            str(poll_cycle_id) if poll_cycle_id is not None else None,
                        ),
                    )
                self._conn.commit()
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            _log.debug("PollLogger.log swallowed: %s", exc)

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass
