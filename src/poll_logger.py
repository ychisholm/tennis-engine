"""
Audit-log writer for the live polling pipeline.

Writes one row to public.poll_audit_log per significant event so post-hoc
diagnostic scripts can reconstruct exactly when each match was discovered,
when polling started, and when point data first arrived.

Failures are silently swallowed: a logging hiccup must never crash the
scheduler or worker that called us.
"""
from __future__ import annotations

import logging
import os
import threading
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
        CREATE TABLE IF NOT EXISTS poll_audit_log (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type VARCHAR(50) NOT NULL,
            match_id VARCHAR(100),
            detail TEXT,
            points_count INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_poll_audit_match_id
            ON poll_audit_log(match_id);
        CREATE INDEX IF NOT EXISTS idx_poll_audit_timestamp
            ON poll_audit_log(timestamp);
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
    ) -> None:
        try:
            with self._lock:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO poll_audit_log
                            (event_type, match_id, detail, points_count)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            event_type,
                            str(match_id) if match_id is not None else None,
                            detail,
                            points_count,
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
