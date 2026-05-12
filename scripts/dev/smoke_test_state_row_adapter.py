#!/usr/bin/env python3
"""End-to-end smoke test for Phase 6D wiring against production PostgreSQL.

Pulls a recently-finished match from book.matches, feeds its
live.match_states rows through the full Phase 6D pipeline
(MatchLogger.fetch_state_rows_for_match → StateRowAdapter →
LivePredictionService), and verifies that calibrated predictions land
in live.predictions tagged model_version='6d_smoke_test'.

Differs from the 6C smoke test (scripts/dev/smoke_test_prediction_logger.py)
in source: 6C used book.points (canonical post-match), this uses
live.match_states fed through StateRowAdapter — exactly the data path
production MatchWorker will use once LIVE_PREDICTIONS_ENABLED=1.

All writes are isolated under model_version='6d_smoke_test' so cleanup
is a single DELETE. Default behavior cleans up on exit. Pass
--no-cleanup to leave rows for inspection.

Usage:
    .venv/bin/python scripts/dev/smoke_test_state_row_adapter.py
    .venv/bin/python scripts/dev/smoke_test_state_row_adapter.py --verbose
    .venv/bin/python scripts/dev/smoke_test_state_row_adapter.py --no-cleanup
    .venv/bin/python scripts/dev/smoke_test_state_row_adapter.py --match-id 16160007
    .venv/bin/python scripts/dev/smoke_test_state_row_adapter.py --days 14
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from contextlib import closing
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from src.live.logger import MatchLogger  # noqa: E402
from src.live.state_row_adapter import (  # noqa: E402
    NoLegalPathError,
    StateRowAdapter,
)
from src.live_prediction_service import (  # noqa: E402
    LivePredictionService,
    Prediction,
)
from src.prediction_logger import (  # noqa: E402
    count_predictions,
    get_predictions_for_match,
)


MODEL_VERSION = "6d_smoke_test"
MIN_STATE_ROWS = 20


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set.")
    return psycopg2.connect(url)


# ---------------------------------------------------------------------------
# Step 1 — pick match
# ---------------------------------------------------------------------------

def pick_match(conn, *, days: int, explicit_match_id: int | None) -> dict:
    """Pick a finished match from book.matches.

    Returns dict: match_id, player_a_id, player_b_id, player_a, player_b,
    surface, first_server, tournament, match_date, state_row_count.
    """
    with conn.cursor() as cur:
        if explicit_match_id is not None:
            cur.execute(
                """
                SELECT m.match_id, m.match_date, m.tournament, m.surface,
                       m.player_a_id, m.player_b_id,
                       pa.name AS player_a_name,
                       pb.name AS player_b_name,
                       (
                           SELECT COUNT(*) FROM live.match_states
                           WHERE match_id = m.match_id
                       ) AS state_row_count
                FROM book.matches m
                LEFT JOIN book.players pa ON pa.player_id = m.player_a_id
                LEFT JOIN book.players pb ON pb.player_id = m.player_b_id
                WHERE m.match_id = %s
                """,
                [explicit_match_id],
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(
                    f"match_id={explicit_match_id} not found in book.matches"
                )
            if (row[8] or 0) == 0:
                raise RuntimeError(
                    f"match_id={explicit_match_id} has 0 rows in "
                    f"live.match_states — cannot smoke test"
                )
        else:
            cur.execute(
                """
                SELECT m.match_id, m.match_date, m.tournament, m.surface,
                       m.player_a_id, m.player_b_id,
                       pa.name AS player_a_name,
                       pb.name AS player_b_name,
                       (
                           SELECT COUNT(*) FROM live.match_states
                           WHERE match_id = m.match_id
                       ) AS state_row_count
                FROM book.matches m
                LEFT JOIN book.players pa ON pa.player_id = m.player_a_id
                LEFT JOIN book.players pb ON pb.player_id = m.player_b_id
                WHERE m.match_date >= CURRENT_DATE - %s::int
                ORDER BY m.match_date DESC, m.match_id DESC
                """,
                [days],
            )
            row = None
            for candidate in cur.fetchall():
                if (candidate[8] or 0) >= MIN_STATE_ROWS:
                    row = candidate
                    break
            if row is None:
                raise RuntimeError(
                    f"no eligible match in last {days} days with "
                    f">={MIN_STATE_ROWS} live.match_states rows"
                )

        (match_id, match_date, tournament, surface,
         player_a_id, player_b_id, name_a, name_b, state_row_count) = row

        # Fetch first_server from any non-NULL row for this match.
        cur.execute(
            """
            SELECT first_server FROM live.match_states
            WHERE match_id = %s AND first_server IS NOT NULL
            LIMIT 1
            """,
            [match_id],
        )
        fs_row = cur.fetchone()
        if fs_row is None or fs_row[0] not in ("home", "away"):
            raise RuntimeError(
                f"match {match_id}: first_server unknown — every "
                f"live.match_states row has NULL first_server"
            )
        first_server = fs_row[0]

    if not name_a or not name_b:
        raise RuntimeError(
            f"match {match_id}: missing player names in book.players "
            f"(player_a_id={player_a_id}, player_b_id={player_b_id})"
        )

    return {
        "match_id": int(match_id),
        "match_date": match_date,
        "tournament": tournament,
        "surface": surface,
        "player_a_id": int(player_a_id),
        "player_b_id": int(player_b_id),
        "player_a": name_a,
        "player_b": name_b,
        "first_server": first_server,
        "state_row_count": int(state_row_count or 0),
    }


# ---------------------------------------------------------------------------
# Step 2 — construct pipeline
# ---------------------------------------------------------------------------

def construct_pipeline(match_info: dict) -> tuple[StateRowAdapter, LivePredictionService]:
    """Build the adapter and service for this match.

    Service constructed with model_version='6d_smoke_test'; the singleton
    PredictionLogger is auto-resolved via the constructor's fallback.
    """
    service = LivePredictionService(model_version=MODEL_VERSION)
    if service._prediction_logger is None:
        raise RuntimeError(
            "LivePredictionService._prediction_logger is None — "
            "get_default_logger() returned None. DATABASE_URL may be "
            "unset or the singleton failed to construct."
        )
    service.start_match(
        match_id_int=match_info["match_id"],
        player_a=match_info["player_a"],
        player_b=match_info["player_b"],
        raw_surface=match_info["surface"] or "unknown",
        first_server_is_a=(match_info["first_server"] == "home"),
        player_a_id=match_info["player_a_id"],
        player_b_id=match_info["player_b_id"],
    )
    adapter = StateRowAdapter(first_server=match_info["first_server"])
    return adapter, service


# ---------------------------------------------------------------------------
# Step 3 — fetch state rows
# ---------------------------------------------------------------------------

def fetch_state_rows(match_id: int) -> list[dict]:
    """Use MatchLogger.fetch_state_rows_for_match — the exact method
    MatchWorker uses for replay in production."""
    with closing(MatchLogger()) as ml:
        return ml.fetch_state_rows_for_match(match_id)


# ---------------------------------------------------------------------------
# Step 4 — walk and feed
# ---------------------------------------------------------------------------

def replay_rows(
    adapter: StateRowAdapter,
    service: LivePredictionService,
    rows: list[dict],
    *,
    verbose: bool = False,
) -> dict:
    """Walk (prev, curr) pairs through adapter+service. Returns stats."""
    points_emitted = 0
    predictions_emitted: list[Prediction] = []
    glitch_count = 0
    glitch_samples: list[str] = []
    invariant_violation_msgs: list[str] = []

    prev: dict | None = None
    for curr in rows:
        try:
            points = adapter.transition(prev, curr)
        except NoLegalPathError as exc:
            glitch_count += 1
            if len(glitch_samples) < 3:
                glitch_samples.append(exc.reason)
            prev = curr
            continue

        for pt in points:
            points_emitted += 1
            if verbose:
                print(
                    f"  Pt(set={pt.set_number} game={pt.game_number_in_set} "
                    f"score_before={pt.score_before!r} Svr={pt.Svr} "
                    f"PtWinner={pt.PtWinner} is_tiebreak={pt.is_tiebreak})"
                )
            try:
                pred = service.process_point(pt)
            except RuntimeError as exc:
                # Loudly capture — caller treats as red flag.
                invariant_violation_msgs.append(str(exc))
                # Don't keep feeding after an invariant violation — the
                # service is in a corrupt state.
                return {
                    "rows_processed": rows.index(curr) + 1,
                    "points_emitted": points_emitted,
                    "predictions_emitted": predictions_emitted,
                    "glitches_seen": glitch_count,
                    "glitch_samples": glitch_samples,
                    "invariant_violations_seen": invariant_violation_msgs,
                }
            if pred is not None:
                predictions_emitted.append(pred)
                if verbose:
                    print(
                        f"    → Prediction(set={pred.set_number} "
                        f"game={pred.game_number_in_set} "
                        f"P(A)={pred.probability_a:.4f} "
                        f"conf={pred.confidence:.4f})"
                    )
        prev = curr

    # Flush the final in-progress game.
    final_pred = service.finalize()
    if final_pred is not None:
        predictions_emitted.append(final_pred)
        if verbose:
            print(
                f"  finalize() → Prediction(set={final_pred.set_number} "
                f"game={final_pred.game_number_in_set} "
                f"P(A)={final_pred.probability_a:.4f} "
                f"conf={final_pred.confidence:.4f})"
            )

    return {
        "rows_processed": len(rows),
        "points_emitted": points_emitted,
        "predictions_emitted": predictions_emitted,
        "glitches_seen": glitch_count,
        "glitch_samples": glitch_samples,
        "invariant_violations_seen": invariant_violation_msgs,
    }


# ---------------------------------------------------------------------------
# Step 5 — verify predictions landed
# ---------------------------------------------------------------------------

def verify_predictions_landed(
    match_id: int,
    expected_count: int,
    predictions: list[Prediction],
) -> list:
    """Query live.predictions and confirm the emissions actually wrote."""
    records = get_predictions_for_match(
        match_id_int=match_id, model_version=MODEL_VERSION,
    )
    if len(records) != expected_count:
        pred_keys = {(p.set_number, p.game_number_in_set) for p in predictions}
        db_keys = {(r.set_number, r.game_number_in_set) for r in records}
        only_pred = pred_keys - db_keys
        only_db = db_keys - pred_keys
        msg_lines = [
            f"count mismatch — emitted {expected_count} but "
            f"DB returned {len(records)}"
        ]
        if only_pred:
            msg_lines.append(f"  only in emissions: {sorted(only_pred)[:10]}")
        if only_db:
            msg_lines.append(f"  only in DB: {sorted(only_db)[:10]}")
        raise RuntimeError("\n".join(msg_lines))

    records_sorted = sorted(
        records, key=lambda r: (r.set_number, r.game_number_in_set)
    )
    return records_sorted


def soft_sanity_checks(
    records_sorted: list, match_info: dict,
) -> list[str]:
    """Return human-readable warnings if sanity checks look off.
    These do NOT fail the smoke test — they're informational."""
    warnings: list[str] = []
    if not records_sorted:
        return warnings
    first = records_sorted[0]
    last = records_sorted[-1]
    if first.set_number != 1:
        warnings.append(
            f"first prediction.set_number={first.set_number} (expected 1)"
        )
    if first.game_number_in_set > 3:
        warnings.append(
            f"first prediction.game_number_in_set={first.game_number_in_set} "
            f"(expected 1-3 — service emits after game 1 closes)"
        )
    for r in records_sorted:
        if not (0.0 < r.probability_a < 1.0):
            warnings.append(
                f"probability_a out of (0,1): {r.probability_a} at "
                f"set={r.set_number} game={r.game_number_in_set}"
            )
            break
    for r in records_sorted:
        for key in ("games_A", "games_B", "set_number",
                    "sets_won_A", "sets_won_B", "game_number_in_set"):
            v = r.features.get(key)
            if v is None or v < 0:
                warnings.append(
                    f"feature {key}={v} negative or None at "
                    f"set={r.set_number} game={r.game_number_in_set}"
                )
                break
        else:
            continue
        break
    return warnings


# ---------------------------------------------------------------------------
# Step 6 — idempotency
# ---------------------------------------------------------------------------

def verify_idempotency(
    match_info: dict,
    rows: list[dict],
    record_count_before: int,
) -> int:
    """Re-run the entire pipeline against the same data and confirm no
    new rows landed. Returns the count after the second run."""
    adapter2, service2 = construct_pipeline(match_info)
    replay_rows(adapter2, service2, rows, verbose=False)
    records_after = get_predictions_for_match(
        match_id_int=match_info["match_id"], model_version=MODEL_VERSION,
    )
    if len(records_after) != record_count_before:
        raise RuntimeError(
            f"idempotency violated — DB row count went from "
            f"{record_count_before} to {len(records_after)} after second run"
        )
    return len(records_after)


# ---------------------------------------------------------------------------
# Step 7 — cleanup
# ---------------------------------------------------------------------------

def cleanup(match_id: int) -> int:
    with closing(_connect()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM live.predictions
                WHERE model_version = %s AND match_id_int = %s
                """,
                [MODEL_VERSION, match_id],
            )
            deleted = cur.rowcount
        conn.commit()
    return int(deleted)


def preflight_clear(match_id: int) -> int:
    """Delete any stale 6d_smoke_test rows for this match before run 1."""
    return cleanup(match_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end smoke test for the Phase 6D pipeline.",
    )
    parser.add_argument(
        "--match-id", type=int, default=None,
        help="Specific match_id from book.matches. Default: auto-pick.",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="How many days back to look when auto-picking. Default: 7.",
    )
    parser.add_argument(
        "--no-cleanup", action="store_true",
        help="Leave smoke-test rows in place after run.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print every Point emitted and Prediction logged.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    print("=" * 64)
    print("Phase 6D smoke test: live.match_states → StateRowAdapter")
    print("                     → LivePredictionService → live.predictions")
    print("=" * 64)
    print(f"  match-id:       {args.match_id if args.match_id else '(auto-pick)'}")
    print(f"  days:           {args.days}")
    print(f"  model_version:  {MODEL_VERSION!r}")
    print(f"  cleanup:        {'KEEP rows' if args.no_cleanup else 'DELETE after run'}")
    print(f"  verbose:        {args.verbose}")
    print()

    cleanup_mid: int | None = None
    try:
        # Step 1: pick match.
        with closing(_connect()) as conn:
            match_info = pick_match(
                conn, days=args.days, explicit_match_id=args.match_id,
            )
        cleanup_mid = match_info["match_id"]
        print(
            f"Picked match {match_info['match_id']} "
            f"({match_info['match_date']}, {match_info['tournament']}, "
            f"surface={match_info['surface']!r}):"
        )
        print(
            f"  {match_info['player_a']} "
            f"(id={match_info['player_a_id']}) vs "
            f"{match_info['player_b']} "
            f"(id={match_info['player_b_id']})"
        )
        print(
            f"  first_server={match_info['first_server']!r}, "
            f"live.match_states rows={match_info['state_row_count']}"
        )
        print()

        # Pre-flight: clear any stale 6d_smoke_test rows for this match.
        stale = preflight_clear(match_info["match_id"])
        if stale > 0:
            print(f"Pre-flight: deleted {stale} stale 6d_smoke_test rows.")
            print()

        # Step 2: construct pipeline.
        adapter, service = construct_pipeline(match_info)
        print("Pipeline constructed (adapter + service + auto-resolved logger).")
        print()

        # Step 3: fetch state rows.
        rows = fetch_state_rows(match_info["match_id"])
        print(f"Fetched {len(rows)} state rows via "
              f"MatchLogger.fetch_state_rows_for_match.")
        print()

        # Step 4: replay.
        print("Run 1: replaying through the pipeline …")
        stats = replay_rows(adapter, service, rows, verbose=args.verbose)
        predictions = stats["predictions_emitted"]
        print(f"  rows_processed:               {stats['rows_processed']}")
        print(f"  points_emitted:               {stats['points_emitted']}")
        print(f"  predictions_emitted:          {len(predictions)}")
        print(f"  glitches_seen:                {stats['glitches_seen']}")
        if stats["glitch_samples"]:
            print("  glitch reasons (first 3):")
            for s in stats["glitch_samples"]:
                print(f"    - {s}")
        print(f"  invariant_violations_seen:    {len(stats['invariant_violations_seen'])}")

        if stats["invariant_violations_seen"]:
            print()
            print("RED FLAG: invariant violation during smoke test:", file=sys.stderr)
            for msg in stats["invariant_violations_seen"]:
                print(f"  {msg}", file=sys.stderr)
            return 2
        print()

        # Step 5: verify rows landed.
        records_sorted = verify_predictions_landed(
            match_info["match_id"], len(predictions), predictions,
        )
        print(f"Predictions in DB:              {len(records_sorted)}")
        print("First 3 predictions (set, game, P(A), conf):")
        for r in records_sorted[:3]:
            print(
                f"  set={r.set_number:>2} game={r.game_number_in_set:>2}  "
                f"P(A)={r.probability_a:.4f}  conf={r.confidence:.4f}"
            )
        if len(records_sorted) > 3:
            print(
                f"  … last: set={records_sorted[-1].set_number:>2} "
                f"game={records_sorted[-1].game_number_in_set:>2}  "
                f"P(A)={records_sorted[-1].probability_a:.4f}  "
                f"conf={records_sorted[-1].confidence:.4f}"
            )

        warnings = soft_sanity_checks(records_sorted, match_info)
        if warnings:
            print()
            print("Sanity check warnings (informational, not failures):")
            for w in warnings:
                print(f"  - {w}")
        else:
            print("Sanity checks: all good.")
        print()

        # Step 6: idempotency.
        print("Run 2: re-replaying same data for idempotency …")
        record_count_after = verify_idempotency(
            match_info, rows, len(records_sorted),
        )
        print(
            f"Idempotency: DB count unchanged at {record_count_after} "
            f"(ON CONFLICT DO NOTHING held)"
        )
        print()

        # Step 7: cleanup.
        if args.no_cleanup:
            print(f"--no-cleanup set: leaving {record_count_after} rows in place.")
            print()
            print("Manual cleanup SQL:")
            print(
                f"  DELETE FROM live.predictions "
                f"WHERE model_version='{MODEL_VERSION}' "
                f"AND match_id_int={match_info['match_id']};"
            )
        else:
            deleted = cleanup(match_info["match_id"])
            remaining = count_predictions(model_version=MODEL_VERSION)
            print(
                f"Cleanup: DELETE'd {deleted} rows for "
                f"match {match_info['match_id']}; "
                f"count_predictions(model_version={MODEL_VERSION!r}) = {remaining}"
            )
            if remaining != 0:
                print(
                    f"WARNING: {remaining} rows with "
                    f"model_version={MODEL_VERSION!r} remain (from other "
                    f"matches?)",
                    file=sys.stderr,
                )

        print()
        print("=" * 64)
        print("SMOKE TEST PASSED")
        print("=" * 64)
        return 0

    except Exception:
        traceback.print_exc()
        if not args.no_cleanup and cleanup_mid is not None:
            try:
                deleted = cleanup(cleanup_mid)
                print(
                    f"\nBest-effort cleanup after error: deleted {deleted} "
                    f"rows for match_id={cleanup_mid}, "
                    f"model_version={MODEL_VERSION!r}.",
                    file=sys.stderr,
                )
            except Exception:
                print("\nBest-effort cleanup also failed:", file=sys.stderr)
                traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
