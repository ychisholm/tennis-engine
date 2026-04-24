"""
Seeds live_match_log with the Faria vs Harris data captured in the
April 20 live run (match id 16041415), so the dashboard can be shown
without a live API connection.

Run from project root:
    .venv/bin/python scripts/seed_demo.py
"""

from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.live.logger import MatchLogger

# ---------------------------------------------------------------------------
# Raw data from April 20 terminal run — (score_str, model_p, bookie_p)
# score_str: "sets_A-sets_B games_A-games_B points_A-points_B"
# ---------------------------------------------------------------------------
_ROWS = [
    ("0-0 0-0 1-0",  0.702, 0.769),
    ("0-0 0-0 1-1",  0.601, 0.769),
    ("0-0 0-0 2-1",  0.590, 0.769),
    ("0-0 0-0 3-1",  0.595, 0.769),
    ("0-0 0-0 3-2",  0.582, 0.769),
    ("0-0 0-0 3-3",  0.602, 0.769),
    ("0-0 0-0 4-3",  0.590, 0.769),
    ("0-0 0-1 0-0",  0.571, 0.769),
    ("0-0 0-1 1-0",  0.571, 0.769),
    ("0-0 0-1 2-0",  0.739, 0.769),
    ("0-0 0-1 2-1",  0.789, 0.769),
    ("0-0 0-1 2-2",  0.821, 0.769),
    ("0-0 0-1 3-2",  0.844, 0.769),
    ("0-0 1-1 0-0",  0.862, 0.769),
    ("0-0 1-1 0-1",  0.836, 0.769),
    ("0-0 1-1 0-2",  0.792, 0.769),
    ("0-0 1-1 1-2",  0.750, 0.769),
    ("0-0 1-1 1-3",  0.708, 0.769),
    ("0-0 2-1 0-0",  0.671, 0.769),
    ("0-0 2-1 1-0",  0.745, 0.769),
    ("0-0 2-1 1-1",  0.717, 0.769),
    ("0-0 2-1 1-2",  0.693, 0.769),
    ("0-0 2-1 2-2",  0.583, 0.769),
    ("0-0 2-1 3-2",  0.567, 0.769),
    ("0-0 3-1 0-0",  0.559, 0.769),
    ("0-0 3-1 1-0",  0.714, 0.778),
    ("0-0 3-1 2-0",  0.737, 0.778),
    ("0-0 3-1 3-0",  0.754, 0.778),
    ("0-0 3-2 0-0",  0.768, 0.778),
    ("0-0 3-2 1-0",  0.761, 0.778),
    ("0-0 3-2 2-0",  0.769, 0.778),
    ("0-0 3-2 3-0",  0.776, 0.778),
    ("0-0 3-2 3-1",  0.781, 0.778),
    ("0-0 3-3 0-0",  0.698, 0.778),
    ("0-0 3-3 0-1",  0.673, 0.778),
    ("0-0 3-3 0-2",  0.682, 0.778),
    ("0-0 3-3 0-3",  0.691, 0.778),
    ("0-0 3-4 0-0",  0.695, 0.778),
    ("0-0 3-4 1-0",  0.627, 0.778),
    ("0-0 3-4 1-1",  0.602, 0.778),
    ("0-0 3-4 1-2",  0.583, 0.778),
    ("0-0 3-4 1-3",  0.568, 0.778),
    ("0-0 3-5 0-0",  0.422, 0.778),
    ("0-0 3-5 0-1",  0.372, 0.778),
    ("0-0 3-5 0-2",  0.372, 0.778),
    ("0-0 3-5 0-3",  0.374, 0.778),
    ("0-0 3-5 1-3",  0.370, 0.778),
    ("0-0 3-5 2-3",  0.364, None),
    ("0-0 4-5 0-0",  0.360, None),
    ("0-0 4-5 0-1",  0.565, None),
    ("0-0 4-5 1-1",  0.592, None),
    ("0-0 4-5 2-1",  0.614, None),
    ("0-0 4-5 3-1",  0.634, None),
    ("0-1 0-0 0-0",  0.481, None),
    ("0-1 0-0 0-1",  0.364, None),
    ("0-1 0-0 1-1",  0.319, None),
    ("0-1 0-0 2-1",  0.292, None),
    ("0-1 0-0 2-2",  0.289, None),
    ("0-1 0-0 2-3",  0.292, None),
    ("0-1 0-0 3-3",  0.283, None),
    ("0-1 0-0 3-4",  0.266, None),
    ("0-1 0-1 0-0",  0.248, None),
    ("0-1 0-1 0-1",  0.134, None),
    ("0-1 0-1 0-2",  0.089, None),
    ("0-1 0-1 0-3",  0.065, None),
    ("0-1 0-2 0-0",  0.023, None),
    ("0-1 0-2 0-1",  0.014, None),
    ("0-1 0-2 0-2",  0.012, None),
    ("0-1 0-2 1-2",  0.011, None),
    ("0-1 0-2 2-2",  0.010, None),
    ("0-1 0-2 2-3",  0.009, None),
    ("0-1 0-2 3-3",  0.008, None),
    ("0-1 0-2 4-3",  0.008, None),
    ("0-1 0-2 4-4",  0.007, None),
    ("0-1 0-2 5-4",  0.007, None),
    ("0-1 1-2 0-0",  0.007, None),
    ("0-1 1-2 1-0",  0.023, None),
    ("0-1 1-2 1-1",  0.008, None),
    ("0-1 1-2 1-2",  0.008, None),
    ("0-1 1-2 2-2",  0.007, None),
    ("0-1 1-2 3-2",  0.007, None),
    ("0-1 2-2 0-0",  0.007, None),
    ("0-1 2-2 1-0",  0.033, None),
]


def _parse_score(s: str):
    sets_part, games_part, pts_part = s.split()
    sa, sb = map(int, sets_part.split("-"))
    ga, gb = map(int, games_part.split("-"))
    pa, pb = map(int, pts_part.split("-"))
    set_num  = sa + sb + 1
    game_num = ga + gb + 1
    return set_num, game_num, pa, pb


def main():
    with MatchLogger() as ml:
        # Clear any stale rows for this match
        ml._conn.execute("DELETE FROM live_match_log WHERE match_id = 16041415")

        for i, (score, model_p, bookie_p) in enumerate(_ROWS):
            set_num, game_num, pa, pb = _parse_score(score)
            edge = round(model_p - bookie_p, 6) if bookie_p is not None else None

            # Synthetic but plausible signal values that evolve over the match
            progress = i / len(_ROWS)
            sms = 0.60 + 0.05 * (1 - progress)   # Faria serve fading
            rms = 0.45 + 0.10 * progress           # Harris return improving
            pms = 0.50
            nmi = 0.0
            gps = 0.0

            ml._conn.execute("""
                INSERT INTO live_match_log VALUES (
                    NOW(), 16041415, 'Jaime Faria', 'Lloyd Harris',
                    ?, ?, ?,
                    NULL, NULL, NULL, NULL, false, false,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, NULL
                )
            """, [
                set_num, game_num, i,
                model_p, bookie_p, edge,
                0.55 - 0.3 * progress,          # D_A declining
                0.45 + 0.3 * progress,          # D_B rising
                nmi, nmi,                        # nmi_a, nmi_b
                sms, 1 - sms,                    # sms_a, sms_b
                rms, 1 - rms,                    # rms_a, rms_b
                pms, pms,                        # pms_a, pms_b
                gps, gps,                        # gps_a, gps_b
            ])

        count = ml._conn.execute(
            "SELECT COUNT(*) FROM live_match_log WHERE match_id = 16041415"
        ).fetchone()[0]
        print(f"Seeded {count} points for match 16041415 (Faria vs Harris)")


if __name__ == "__main__":
    main()
