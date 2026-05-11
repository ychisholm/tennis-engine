#!/usr/bin/env python3
"""
Daily Phase 2 verification pipeline orchestrator.

Runs four stages in sequence for a single UTC date's finished matches:

  Stage 1 — Backfill fetch:    pull /point-by-point and write
                               live.backfill_points (per-match)
  Stage 2 — Backfill validate: run backfill_adapter → validator → reporter
                               (source='backfill')
  Stage 3 — Live validate:     run live_adapter → validator → reporter
                               (source='live')
  Stage 4 — Promote to book:   promote_match() for each eligible match

One DB connection for the whole run. Per-match try/except keeps a single
bad match from killing the pipeline. Per-match commits in default mode so
partial progress survives a fatal error. --dry-run replaces commits with
savepoint releases and rolls back the entire connection at the end.

Usage:
    .venv/bin/python scripts/daily_pipeline.py
    .venv/bin/python scripts/daily_pipeline.py --date 2026-05-11
    .venv/bin/python scripts/daily_pipeline.py --skip-fetch
    .venv/bin/python scripts/daily_pipeline.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from scripts.backtesting.backfill_today import derive_points  # noqa: E402
from src.book.promoter import promote_match  # noqa: E402
from src.live.tennis_feed import TennisFeed  # noqa: E402
from src.verification.backfill_adapter import (  # noqa: E402
    backfill_points_to_state_rows,
)
from src.verification.live_adapter import (  # noqa: E402
    live_match_states_to_state_rows,
)
from src.verification.reporter import write_report  # noqa: E402
from src.verification.validator import validate_match  # noqa: E402


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Daily Phase 2 verification pipeline."
    )
    p.add_argument(
        "--date",
        default=None,
        help="Process matches finishing on this UTC date (YYYY-MM-DD). "
             "Default: today.",
    )
    p.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip Stage 1 — use existing live.backfill_points rows.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all stages but ROLLBACK at the end instead of committing.",
    )
    return p.parse_args()


def _resolve_target_date(arg: Optional[str]) -> date:
    if arg is None:
        return datetime.now(timezone.utc).date()
    try:
        return datetime.strptime(arg, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--date must be YYYY-MM-DD: {exc}")


# ---------------------------------------------------------------------------
# Commit / savepoint helpers
# ---------------------------------------------------------------------------

_SP_NAME = "sp_match"


def _begin_match(conn, dry_run: bool) -> None:
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {_SP_NAME}")


def _commit_match(conn, dry_run: bool) -> None:
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {_SP_NAME}")
    else:
        conn.commit()


def _rollback_match(conn, dry_run: bool) -> None:
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_SP_NAME}")
            cur.execute(f"RELEASE SAVEPOINT {_SP_NAME}")
    else:
        conn.rollback()


# ---------------------------------------------------------------------------
# Stage 0 — Setup
# ---------------------------------------------------------------------------

def _fetch_finished_matches(cur, target_date: date) -> list[dict]:
    cur.execute(
        """
        SELECT DISTINCT ON (match_id)
               match_id, player_a, player_b,
               MIN(polled_at) OVER (PARTITION BY match_id) AS first_poll
        FROM live.match_polls
        WHERE status = 'finished'
          AND polled_at >= %s::date
          AND polled_at < (%s::date + INTERVAL '1 day')
        ORDER BY match_id
        """,
        (target_date, target_date),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Stage 1 — Backfill fetch
# ---------------------------------------------------------------------------

_INSERT_BACKFILL_POINT = """
INSERT INTO live.backfill_points (
    ts, match_id, player_a, player_b,
    point_num, set_num, game_num,
    home_point, away_point, server, point_winner,
    is_ace, is_double_fault, ingestion_source
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _write_backfill_points_no_commit(
    cur, match_id: int, player_a: str, player_b: str,
    points: list[dict], start_dt: datetime,
) -> int:
    """Inline copy of scripts/backtesting/backfill_today.write_points
    minus the conn.commit() so dry-run mode rolls back cleanly."""
    for i, pt in enumerate(points):
        ts = start_dt + timedelta(seconds=i * 45)
        cur.execute(_INSERT_BACKFILL_POINT, [
            ts, match_id, player_a, player_b,
            i, pt["set_num"], pt["game_num"],
            pt["home_point"], pt["away_point"],
            pt["server"], pt["point_winner"],
            pt["is_ace"], pt["is_double_fault"],
            "daily_pipeline",
        ])
    return len(points)


def _backfill_already_loaded(cur, match_id: int) -> int:
    cur.execute(
        "SELECT COUNT(*) AS n FROM live.backfill_points WHERE match_id = %s",
        (match_id,),
    )
    row = cur.fetchone()
    return row["n"] if isinstance(row, dict) else row[0]


def _stage_fetch(
    conn, feed: TennisFeed, matches: list[dict], dry_run: bool,
) -> tuple[int, int, int]:
    """Returns (ok, failed, skipped_already_present)."""
    ok = failed = skipped = 0
    for m in matches:
        mid = m["match_id"]
        _begin_match(conn, dry_run)
        try:
            with conn.cursor() as cur:
                existing = _backfill_already_loaded(cur, mid)
                if existing > 0:
                    print(f"  [{mid}] skipped — {existing} backfill points already present")
                    skipped += 1
                    _commit_match(conn, dry_run)
                    continue
                raw = feed.get_point_by_point(mid)
                points = derive_points(raw)
                if not points:
                    print(f"  [{mid}] empty pointByPoint payload")
                    failed += 1
                    _rollback_match(conn, dry_run)
                    continue
                start_dt = m["first_poll"] or datetime.now(timezone.utc)
                n = _write_backfill_points_no_commit(
                    cur, mid, m["player_a"], m["player_b"],
                    points, start_dt,
                )
            _commit_match(conn, dry_run)
            print(f"  [{mid}] fetched {n} points")
            ok += 1
            time.sleep(0.3)  # rate-limit cushion
        except Exception as exc:
            _rollback_match(conn, dry_run)
            print(
                f"  [{mid}] FETCH FAIL ({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            failed += 1
    return ok, failed, skipped


# ---------------------------------------------------------------------------
# Stages 2 & 3 — Validation
# ---------------------------------------------------------------------------

def _format_recorded_final_score(poll_row: dict) -> str:
    parts = []
    for n in (1, 2, 3, 4, 5):
        h = poll_row.get(f"home_period{n}")
        a = poll_row.get(f"away_period{n}")
        if h is None or a is None:
            continue
        parts.append(f"{h}-{a}")
    return " ".join(parts)


def _latest_finished_poll(cur, match_id: int) -> Optional[dict]:
    cur.execute(
        """
        SELECT home_period1, away_period1,
               home_period2, away_period2,
               home_period3, away_period3
        FROM live.match_polls
        WHERE match_id = %s AND status = 'finished'
        ORDER BY polled_at DESC LIMIT 1
        """,
        (match_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _stage_validate_backfill(
    conn, matches: list[dict], run_id: uuid.UUID, dry_run: bool,
) -> tuple[Counter, int, int]:
    """Returns (verdict_counter, ok, failed)."""
    verdicts: Counter = Counter()
    ok = failed = 0
    for m in matches:
        mid = m["match_id"]
        _begin_match(conn, dry_run)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT point_num, set_num, game_num,
                           home_point, away_point
                    FROM live.backfill_points
                    WHERE match_id = %s
                    ORDER BY point_num ASC
                    """,
                    (mid,),
                )
                bf_rows = [dict(r) for r in cur.fetchall()]
                poll_row = _latest_finished_poll(cur, mid)
            if not bf_rows:
                print(f"  [{mid}] no backfill_points — skipping validate")
                _rollback_match(conn, dry_run)
                failed += 1
                continue
            recorded = (
                _format_recorded_final_score(poll_row) if poll_row else ""
            )
            state_rows = backfill_points_to_state_rows(bf_rows)
            gaps, summary = validate_match(state_rows, recorded, str(mid))
            write_report(
                conn,
                match_id=str(mid),
                verification_run_id=run_id,
                source="backfill",
                gaps=gaps,
                summary=summary,
            )
            verdicts[summary.verdict] += 1
            _commit_match(conn, dry_run)
            print(
                f"  [{mid}] bf={summary.verdict:14}  "
                f"final={summary.live_final_score!r:25} "
                f"match={summary.final_score_match}"
            )
            ok += 1
        except Exception as exc:
            _rollback_match(conn, dry_run)
            print(
                f"  [{mid}] BACKFILL VALIDATE FAIL "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            failed += 1
    return verdicts, ok, failed


def _stage_validate_live(
    conn, matches: list[dict], run_id: uuid.UUID, dry_run: bool,
) -> tuple[Counter, int, int]:
    verdicts: Counter = Counter()
    ok = failed = 0
    for m in matches:
        mid = m["match_id"]
        _begin_match(conn, dry_run)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT polled_at, status,
                           home_sets_won, away_sets_won,
                           home_set1_games, away_set1_games,
                           home_set2_games, away_set2_games,
                           home_set3_games, away_set3_games,
                           home_current_games, away_current_games,
                           home_current_point, away_current_point
                    FROM live.match_states
                    WHERE match_id = %s
                    ORDER BY polled_at ASC
                    """,
                    (mid,),
                )
                live_rows = [dict(r) for r in cur.fetchall()]
                poll_row = _latest_finished_poll(cur, mid)
            recorded = (
                _format_recorded_final_score(poll_row) if poll_row else ""
            )
            state_rows = live_match_states_to_state_rows(live_rows)
            gaps, summary = validate_match(state_rows, recorded, str(mid))
            write_report(
                conn,
                match_id=str(mid),
                verification_run_id=run_id,
                source="live",
                gaps=gaps,
                summary=summary,
            )
            verdicts[summary.verdict] += 1
            _commit_match(conn, dry_run)
            print(
                f"  [{mid}] live={summary.verdict:14}  "
                f"final={summary.live_final_score!r:25} "
                f"match={summary.final_score_match}"
            )
            ok += 1
        except Exception as exc:
            _rollback_match(conn, dry_run)
            print(
                f"  [{mid}] LIVE VALIDATE FAIL "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            failed += 1
    return verdicts, ok, failed


# ---------------------------------------------------------------------------
# Stage 4 — Promote
# ---------------------------------------------------------------------------

def _stage_promote(
    conn, feed: Optional[TennisFeed], matches: list[dict],
    run_id: uuid.UUID, dry_run: bool,
) -> tuple[int, int, int]:
    promoted = skipped = errored = 0
    for m in matches:
        mid = m["match_id"]
        _begin_match(conn, dry_run)
        try:
            ok = promote_match(
                conn,
                match_id=mid,
                verification_run_id=run_id,
                tennis_feed=feed,
            )
            _commit_match(conn, dry_run)
            if ok:
                promoted += 1
                print(f"  [{mid}] promoted")
            else:
                skipped += 1
                print(f"  [{mid}] skipped (ineligible or already promoted)")
        except Exception as exc:
            _rollback_match(conn, dry_run)
            print(
                f"  [{mid}] PROMOTE FAIL ({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            errored += 1
    return promoted, skipped, errored


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

def _summary_counts(cur, run_id: uuid.UUID) -> dict:
    rid = str(run_id)
    cur.execute(
        "SELECT COUNT(*) AS n FROM audit.verification_reports "
        "WHERE verification_run_id = %s",
        (rid,),
    )
    audit_reports = cur.fetchone()["n"]
    cur.execute(
        "SELECT COUNT(*) AS n FROM audit.gap_reports "
        "WHERE verification_run_id = %s",
        (rid,),
    )
    audit_gaps = cur.fetchone()["n"]
    cur.execute(
        "SELECT COUNT(*) AS n FROM book.matches "
        "WHERE verification_run_id = %s",
        (rid,),
    )
    book_matches = cur.fetchone()["n"]
    cur.execute(
        """
        SELECT COUNT(*) AS n FROM book.points b
        JOIN book.matches m ON b.match_id = m.match_id
        WHERE m.verification_run_id = %s
        """,
        (rid,),
    )
    book_points = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM book.players")
    total_players = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM book.player_career_stats")
    total_stats = cur.fetchone()["n"]
    return {
        "audit_reports": audit_reports,
        "audit_gaps": audit_gaps,
        "book_matches": book_matches,
        "book_points": book_points,
        "total_players": total_players,
        "total_stats_rows": total_stats,
    }


def _print_verdict_counts(label: str, counter: Counter) -> None:
    print(f"  {label}:")
    if not counter:
        print(f"    (none)")
        return
    for v, n in sorted(counter.items()):
        print(f"    {v:18}: {n}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = _parse_args()
    target_date = _resolve_target_date(args.date)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    run_id = uuid.uuid4()

    print("=" * 80)
    print(f"Daily verification pipeline")
    print(f"  target_date          = {target_date}  (UTC)")
    print(f"  verification_run_id  = {run_id}")
    print(f"  skip_fetch           = {args.skip_fetch}")
    print(f"  dry_run              = {args.dry_run}")
    print("=" * 80)

    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    conn.autocommit = False

    feed: Optional[TennisFeed]
    try:
        feed = TennisFeed()
    except Exception as exc:
        if args.skip_fetch:
            feed = None
            print(f"(TennisFeed unavailable — fine, --skip-fetch set: {exc})")
        else:
            print(f"TennisFeed init failed: {exc}", file=sys.stderr)
            return 1

    fetched_ok = fetched_fail = fetched_skip = 0
    bf_verdicts: Counter = Counter()
    bf_ok = bf_fail = 0
    live_verdicts: Counter = Counter()
    live_ok = live_fail = 0
    promoted = skipped_pro = errored_pro = 0

    try:
        with conn.cursor() as cur:
            matches = _fetch_finished_matches(cur, target_date)
        print(f"\nFound {len(matches)} finished matches on {target_date}.\n")
        if not matches:
            print("Nothing to do.")
            return 0

        # ---- Stage 1 ----
        print("-" * 80)
        print("STAGE 1 — Backfill fetch")
        print("-" * 80)
        if args.skip_fetch:
            print("  (skipped via --skip-fetch)")
        else:
            assert feed is not None
            fetched_ok, fetched_fail, fetched_skip = _stage_fetch(
                conn, feed, matches, args.dry_run
            )
            print(
                f"Fetched: {fetched_ok} succeeded, "
                f"{fetched_skip} already-present, {fetched_fail} failed."
            )

        # ---- Stage 2 ----
        print()
        print("-" * 80)
        print("STAGE 2 — Backfill validate")
        print("-" * 80)
        bf_verdicts, bf_ok, bf_fail = _stage_validate_backfill(
            conn, matches, run_id, args.dry_run
        )
        _print_verdict_counts("backfill verdicts", bf_verdicts)
        print(f"  validated: {bf_ok}, failed: {bf_fail}")

        # ---- Stage 3 ----
        print()
        print("-" * 80)
        print("STAGE 3 — Live validate")
        print("-" * 80)
        live_verdicts, live_ok, live_fail = _stage_validate_live(
            conn, matches, run_id, args.dry_run
        )
        _print_verdict_counts("live verdicts", live_verdicts)
        print(f"  validated: {live_ok}, failed: {live_fail}")

        # ---- Stage 4 ----
        print()
        print("-" * 80)
        print("STAGE 4 — Promote to book")
        print("-" * 80)
        promoted, skipped_pro, errored_pro = _stage_promote(
            conn, feed, matches, run_id, args.dry_run
        )
        print(
            f"Promoted: {promoted}, Skipped: {skipped_pro}, "
            f"Errored: {errored_pro}"
        )

        # ---- Final summary (queries run before any final rollback) ----
        print()
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print(f"  verification_run_id  = {run_id}")
        print(f"  target_date          = {target_date}")
        print(f"  total matches        = {len(matches)}")
        print()
        print(f"  Stage 1 (fetch)      : ok={fetched_ok} skipped={fetched_skip} failed={fetched_fail}")
        print(f"  Stage 2 (backfill)   : ok={bf_ok} failed={bf_fail}")
        print(f"  Stage 3 (live)       : ok={live_ok} failed={live_fail}")
        print(f"  Stage 4 (promote)    : promoted={promoted} skipped={skipped_pro} errored={errored_pro}")
        print()
        _print_verdict_counts("backfill verdicts", bf_verdicts)
        _print_verdict_counts("live verdicts", live_verdicts)
        print()
        with conn.cursor() as cur:
            counts = _summary_counts(cur, run_id)
        print(f"  Rows written this run:")
        print(f"    audit.verification_reports : {counts['audit_reports']}")
        print(f"    audit.gap_reports          : {counts['audit_gaps']}")
        print(f"    book.matches               : {counts['book_matches']}")
        print(f"    book.points                : {counts['book_points']}")
        print(f"  DB totals after run:")
        print(f"    book.players               : {counts['total_players']}")
        print(f"    book.player_career_stats   : {counts['total_stats_rows']}")

        if args.dry_run:
            conn.rollback()
            print()
            print("DRY-RUN: rolled back. No data persisted.")
        else:
            # Per-match commits already happened. Nothing left to commit.
            print()
            print("All committed.")
    except Exception:
        conn.rollback()
        print("\nFATAL: rolled back.", file=sys.stderr)
        raise
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
