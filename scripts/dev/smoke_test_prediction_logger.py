#!/usr/bin/env python3
"""
End-to-end smoke test for the Step 6C prediction-logger pipeline.

Picks a real match from book.matches, replays its points through
LivePredictionService with the logger auto-resolved via the singleton,
queries the resulting rows back via the live.predictions read helpers,
spot-checks the first prediction's round-trip, and verifies idempotency
by replaying the same match a second time.

Cleanup (DELETE WHERE match_id_int = … AND model_version = …) runs by
default. Pass --keep to leave smoke-test rows behind for manual
inspection. The default model_version tag is 'smoke_test' so cleanup
cannot collide with v1 production rows.

Usage:
    python scripts/dev/smoke_test_prediction_logger.py
    python scripts/dev/smoke_test_prediction_logger.py --match-id 12345 --verbose
    python scripts/dev/smoke_test_prediction_logger.py --keep
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from src.live_prediction_service import LivePredictionService, Prediction  # noqa: E402
from src.prediction_logger import (  # noqa: E402
    PredictionRecord,
    count_predictions,
    get_predictions_for_match,
)


# ---------------------------------------------------------------------------
# Point shape expected by the streaming engine
# (mirror of tests/test_live_prediction_service.py::Pt)
# ---------------------------------------------------------------------------

@dataclass
class Pt:
    set_number: int
    game_number_in_set: int
    Pt: int
    score_before: str
    Svr: int
    PtWinner: int
    is_tiebreak: bool = False


# ---------------------------------------------------------------------------
# Source-data reads (book.*)
# ---------------------------------------------------------------------------

def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    return psycopg2.connect(url)


def _fetch_match(conn, match_id):
    with conn.cursor() as cur:
        if match_id is not None:
            cur.execute("""
                SELECT match_id, match_date, tournament, surface,
                       player_a_id, player_b_id
                FROM book.matches WHERE match_id = %s
            """, [match_id])
        else:
            cur.execute("""
                SELECT match_id, match_date, tournament, surface,
                       player_a_id, player_b_id
                FROM book.matches
                ORDER BY match_date ASC, match_id ASC
                LIMIT 1
            """)
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("match_id", "match_date", "tournament", "surface",
            "player_a_id", "player_b_id")
    return dict(zip(keys, row))


def _fetch_player_name(conn, player_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM book.players WHERE player_id = %s",
            [player_id],
        )
        row = cur.fetchone()
    return row[0] if row else None


def _fetch_points(conn, match_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT set_num, game_num, point_num, server_id,
                   point_winner_id, score_after, is_tiebreak
            FROM book.points
            WHERE match_id = %s
            ORDER BY set_num, game_num, point_num
        """, [match_id])
        rows = cur.fetchall()
    keys = ("set_num", "game_num", "point_num", "server_id",
            "point_winner_id", "score_after", "is_tiebreak")
    return [dict(zip(keys, r)) for r in rows]


# ---------------------------------------------------------------------------
# Conversion: book.points rows → Pt objects the streaming engine expects
# ---------------------------------------------------------------------------

def _convert_points(book_rows, player_a_id: int) -> list[Pt]:
    """Filter 'GAME' sentinels and reshape book.points → Pt.

    score_before for point N in game G is the score_after of point N-1
    in the same game (or "0-0" for the first point of the game).
    """
    filtered = [r for r in book_rows if (r["score_after"] or "") != "GAME"]
    out: list[Pt] = []
    prev_key = None
    prev_score_after = None
    for r in filtered:
        key = (r["set_num"], r["game_num"])
        if key != prev_key:
            score_before = "0-0"
        else:
            score_before = prev_score_after or "0-0"
        out.append(Pt(
            set_number=int(r["set_num"]),
            game_number_in_set=int(r["game_num"]),
            Pt=int(r["point_num"]),
            score_before=score_before,
            Svr=1 if r["server_id"] == player_a_id else 2,
            PtWinner=1 if r["point_winner_id"] == player_a_id else 2,
            is_tiebreak=bool(r["is_tiebreak"]),
        ))
        prev_key = key
        prev_score_after = r["score_after"]
    return out


# ---------------------------------------------------------------------------
# Spot check
# ---------------------------------------------------------------------------

def _feature_close(a, b) -> bool:
    """bool/int exact, float within 1e-9 abs or rel."""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    af, bf = float(a), float(b)
    diff = abs(af - bf)
    return diff <= 1e-9 or diff <= 1e-9 * max(abs(af), abs(bf), 1.0)


def _spot_check(pred: Prediction, rec: PredictionRecord, feature_names) -> None:
    errs: list[str] = []
    if pred.match_id_int != rec.match_id_int:
        errs.append(f"match_id_int: pred={pred.match_id_int} db={rec.match_id_int}")
    if pred.set_number != rec.set_number:
        errs.append(f"set_number: pred={pred.set_number} db={rec.set_number}")
    if pred.game_number_in_set != rec.game_number_in_set:
        errs.append(
            f"game_number_in_set: pred={pred.game_number_in_set} "
            f"db={rec.game_number_in_set}"
        )
    if pred.player_a_id != rec.player_a_id:
        errs.append(f"player_a_id: pred={pred.player_a_id} db={rec.player_a_id}")
    if pred.player_b_id != rec.player_b_id:
        errs.append(f"player_b_id: pred={pred.player_b_id} db={rec.player_b_id}")
    if pred.surface != rec.surface:
        errs.append(f"surface: pred={pred.surface!r} db={rec.surface!r}")
    if abs(pred.probability_a - rec.probability_a) > 1e-9:
        errs.append(
            f"probability_a: pred={pred.probability_a} db={rec.probability_a}"
        )
    if abs(pred.confidence - rec.confidence) > 1e-9:
        errs.append(f"confidence: pred={pred.confidence} db={rec.confidence}")
    for name in feature_names:
        pv = pred.features[name]
        dv = rec.features[name]
        if not _feature_close(pv, dv):
            errs.append(f"feature {name!r}: pred={pv!r} db={dv!r}")
    if errs:
        raise AssertionError(
            "Spot-check mismatch:\n  " + "\n  ".join(errs)
        )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def _replay(
    match, name_a, name_b, first_server_is_a, converted_points, model_version,
) -> list[Prediction]:
    service = LivePredictionService(
        prediction_logger=None,
        model_version=model_version,
    )
    if service._prediction_logger is None:
        raise RuntimeError(
            "get_default_logger() returned None — DATABASE_URL may be "
            "unset, or the singleton failed to construct."
        )
    service.start_match(
        int(match["match_id"]),
        name_a,
        name_b,
        match["surface"] or "unknown",
        first_server_is_a,
        player_a_id=int(match["player_a_id"]),
        player_b_id=int(match["player_b_id"]),
    )
    out: list[Prediction] = []
    for pt in converted_points:
        r = service.process_point(pt)
        if r is not None:
            out.append(r)
    last = service.finalize()
    if last is not None:
        out.append(last)
    return out


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _delete_smoke_rows(match_id: int, model_version: str) -> int:
    with closing(_connect()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM live.predictions
                WHERE match_id_int = %s AND model_version = %s
            """, [match_id, model_version])
            deleted = cur.rowcount
        conn.commit()
    return int(deleted)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end smoke test of the prediction logger.",
    )
    parser.add_argument(
        "--match-id",
        type=int,
        default=None,
        help="Specific match_id from book.matches. Default: oldest.",
    )
    parser.add_argument(
        "--model-version",
        default="smoke_test",
        help="Tag for smoke-test rows. Default: 'smoke_test'.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete smoke-test rows at end.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra per-prediction detail.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    print("Smoke test: prediction logger end-to-end")
    print("=" * 56)
    print(f"  match-id:       {args.match_id if args.match_id else '(default: oldest)'}")
    print(f"  model_version:  {args.model_version!r}")
    print(f"  cleanup:        {'KEEP rows' if args.keep else 'DELETE after run'}")
    print(f"  verbose:        {args.verbose}")
    print()

    cleanup_match_id = None
    try:
        # B) Source match
        with closing(_connect()) as conn:
            match = _fetch_match(conn, args.match_id)
            if match is None:
                print("ERROR: no matching book.matches row found.", file=sys.stderr)
                return 1
            cleanup_match_id = int(match["match_id"])
            print(
                f"Source match: {match['match_id']} on {match['match_date']} "
                f"({match['tournament']}, surface={match['surface']!r})"
            )

            # C) Players
            name_a = _fetch_player_name(conn, match["player_a_id"])
            name_b = _fetch_player_name(conn, match["player_b_id"])
            if not name_a or not name_b:
                print(
                    f"ERROR: missing player rows in book.players "
                    f"(name_a={name_a!r}, name_b={name_b!r}).",
                    file=sys.stderr,
                )
                return 1
            print(
                f"Players: {name_a} (id={match['player_a_id']}) vs "
                f"{name_b} (id={match['player_b_id']})"
            )

            # D) Points
            raw_points = _fetch_points(conn, match["match_id"])
            if not raw_points:
                print("ERROR: no book.points rows for this match.", file=sys.stderr)
                return 1
            filtered_count = sum(
                1 for r in raw_points if (r["score_after"] or "") != "GAME"
            )
            print(
                f"Raw points: {filtered_count} (after filtering 'GAME' "
                f"sentinels from {len(raw_points)} rows)"
            )

            converted = _convert_points(raw_points, int(match["player_a_id"]))

            # E) First server
            first_server_is_a = converted[0].Svr == 1
            print(f"First server: {'Player A' if first_server_is_a else 'Player B'}")

            # F) Pre-flight stale-row cleanup
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM live.predictions
                    WHERE match_id_int = %s AND model_version = %s
                    """,
                    [match["match_id"], args.model_version],
                )
                stale = int(cur.fetchone()[0])
                if stale > 0:
                    print(
                        f"WARNING: {stale} stale rows for "
                        f"(match_id={match['match_id']}, model_version="
                        f"{args.model_version!r}); deleting before run."
                    )
                    cur.execute(
                        """
                        DELETE FROM live.predictions
                        WHERE match_id_int = %s AND model_version = %s
                        """,
                        [match["match_id"], args.model_version],
                    )
                    print(f"Pre-flight: deleted {cur.rowcount} stale rows.")
            conn.commit()

        # G/H/I) Run 1
        print()
        print("Run 1: replaying through LivePredictionService …")
        predictions = _replay(
            match, name_a, name_b, first_server_is_a, converted,
            args.model_version,
        )
        print("Logger auto-resolved via get_default_logger() — OK")
        print(f"Predictions emitted by service: {len(predictions)}")
        if args.verbose:
            for p in predictions[:5]:
                print(
                    f"  set={p.set_number} game={p.game_number_in_set} "
                    f"P(A)={p.probability_a:.4f} conf={p.confidence:.4f}"
                )
            if len(predictions) > 5:
                print(f"  … ({len(predictions) - 5} more)")

        # J) Read-back
        records = get_predictions_for_match(
            match_id_int=int(match["match_id"]),
            model_version=args.model_version,
        )
        print(f"Rows queried back from live.predictions: {len(records)}")
        if len(records) != len(predictions):
            pred_keys = {(p.set_number, p.game_number_in_set) for p in predictions}
            db_keys = {(r.set_number, r.game_number_in_set) for r in records}
            only_pred = pred_keys - db_keys
            only_db = db_keys - pred_keys
            print(
                f"ERROR: count mismatch — emitted {len(predictions)} but "
                f"DB has {len(records)}",
                file=sys.stderr,
            )
            if only_pred:
                print(f"  only in emissions: {sorted(only_pred)[:10]}", file=sys.stderr)
            if only_db:
                print(f"  only in DB: {sorted(only_db)[:10]}", file=sys.stderr)
            return 1

        # K) Spot check first prediction round-trip
        preds_sorted = sorted(
            predictions, key=lambda p: (p.set_number, p.game_number_in_set)
        )
        records_sorted = sorted(
            records, key=lambda r: (r.set_number, r.game_number_in_set)
        )
        # Use service.feature_names from a temp service for the canonical
        # 63-name list — or pull from the prediction itself.
        feature_names = list(preds_sorted[0].features.keys())
        _spot_check(preds_sorted[0], records_sorted[0], feature_names)
        print("Spot check (first prediction round-trip): MATCH")

        # L) Idempotency: replay the same match
        print()
        print("Run 2: replaying same match again …")
        predictions2 = _replay(
            match, name_a, name_b, first_server_is_a, converted,
            args.model_version,
        )
        records_after = get_predictions_for_match(
            match_id_int=int(match["match_id"]),
            model_version=args.model_version,
        )
        if len(records_after) != len(records):
            print(
                f"ERROR: idempotency violated — DB row count went from "
                f"{len(records)} to {len(records_after)} after replay.",
                file=sys.stderr,
            )
            return 1
        print(
            f"Idempotency: 2nd run emitted {len(predictions2)} predictions, "
            f"DB count unchanged at {len(records_after)}"
        )

        # M) Cleanup
        print()
        if args.keep:
            print(f"--keep set: leaving {len(records_after)} rows in place.")
        else:
            deleted = _delete_smoke_rows(
                int(match["match_id"]), args.model_version
            )
            remaining = count_predictions(model_version=args.model_version)
            print(
                f"Cleanup: DELETE'd {deleted} rows; "
                f"count_predictions(model_version={args.model_version!r}) = "
                f"{remaining}"
            )
            if remaining != 0:
                print(
                    f"WARNING: {remaining} rows with model_version="
                    f"{args.model_version!r} remain (from other matches?)",
                    file=sys.stderr,
                )

        print()
        print("SMOKE TEST PASSED")
        return 0

    except Exception:
        traceback.print_exc()
        if not args.keep and cleanup_match_id is not None:
            try:
                deleted = _delete_smoke_rows(cleanup_match_id, args.model_version)
                print(
                    f"\nBest-effort cleanup after error: deleted {deleted} "
                    f"rows for (match_id={cleanup_match_id}, "
                    f"model_version={args.model_version!r}).",
                    file=sys.stderr,
                )
            except Exception:
                print("\nBest-effort cleanup also failed:", file=sys.stderr)
                traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
