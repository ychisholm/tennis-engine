#!/usr/bin/env python3
"""
Print a chronological audit timeline for one match_id from audit.poll_audit_log.

Usage:
    python scripts/dev/audit_match.py <match_id>
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

_GAP_THRESHOLD_MIN = 10.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit timeline for one match")
    ap.add_argument(
        "match_id",
        help="match_id (string match against the VARCHAR column)",
    )
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, event_type, detail, points_count
                FROM audit.poll_audit_log
                WHERE match_id = %s
                ORDER BY timestamp ASC
                """,
                (str(args.match_id),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"No audit rows found for match_id={args.match_id}.")
        return 0

    print(f"\nTimeline for match {args.match_id}  ({len(rows)} events)")
    print("-" * 72)
    for ts, event_type, detail, points_count in rows:
        ts_s = ts.strftime("%Y-%m-%d %H:%M:%S")
        bits = [ts_s, f"{event_type:<18}"]
        if points_count is not None:
            bits.append(f"points_count={points_count}")
        if detail:
            bits.append(f"detail={detail}")
        print("  ".join(bits))

    first_discovered = next(
        (r for r in rows if r[1] == "MATCH_DISCOVERED"), None
    )
    first_points = next(
        (r for r in rows if r[1] == "POINTS_RECEIVED"), None
    )
    points_rows = [r for r in rows if r[1] == "POINTS_RECEIVED"]
    if points_rows and points_rows[-1][3] is not None:
        total_points = points_rows[-1][3]
    else:
        total_points = len(points_rows)
    errors = [r for r in rows if r[1] == "POLL_ERROR"]

    print()
    print("Summary")
    print("-" * 72)
    if first_discovered:
        print(f"  First MATCH_DISCOVERED : {first_discovered[0]}")
    else:
        print("  First MATCH_DISCOVERED : (none)")
    if first_points:
        print(f"  First POINTS_RECEIVED  : {first_points[0]}")
    else:
        print("  First POINTS_RECEIVED  : (none)")

    if first_discovered and first_points:
        gap_min = (first_points[0] - first_discovered[0]).total_seconds() / 60.0
        print(f"  Gap discovery->points  : {gap_min:.2f} minutes")
        if gap_min > _GAP_THRESHOLD_MIN:
            print(
                f"  WARNING: gap exceeds {_GAP_THRESHOLD_MIN:.0f} minutes — "
                f"investigate why points data was delayed."
            )
    else:
        print("  Gap discovery->points  : (cannot compute)")

    print(f"  Total points received  : {total_points}")
    print(f"  Errors encountered     : {len(errors)}")
    for ts, _et, detail, _pc in errors:
        print(f"    {ts}  {detail}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
