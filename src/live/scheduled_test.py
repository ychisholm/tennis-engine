#!/usr/bin/env python3
"""
Scheduled live-feed integration test.

Picks the first available live match, runs the MatchRunner loop for
max_duration_seconds, and writes all output to logs/live_test.log.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging — file only, with timestamps
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "live_test.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(_LOG_FILE)],
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deferred imports (after logging is configured)
# ---------------------------------------------------------------------------

from src.live.match_runner import MatchRunner  # noqa: E402

_MAX_DURATION = 600  # seconds

# Neutral defaults used when real player stats are unavailable
_DEFAULT_PLAYER_TEMPLATE: dict = {
    "p0_hard": 0.63,
    "p0_clay": 0.60,
    "p0_grass": 0.63,
    "archetype": {"sd": 60, "ba": 60, "pe": 60, "tv": 60},
}


def main() -> None:
    log.info("Fetching live matches ...")

    try:
        matches = MatchRunner.list_matches()
    except Exception as exc:
        log.error("Failed to fetch live matches: %s", exc)
        sys.exit(1)

    if not matches:
        log.info("No live matches found")
        return

    match = matches[0]
    match_id = match["id"]
    home = match["home_player"]
    away = match["away_player"]
    log.info("Selected match %s: %s vs %s", match_id, home, away)

    player_a = {**_DEFAULT_PLAYER_TEMPLATE, "name": home}
    player_b = {**_DEFAULT_PLAYER_TEMPLATE, "name": away}

    runner = MatchRunner(
        match_id=match_id,
        player_a=player_a,
        player_b=player_b,
        log_fn=log.info,
    )

    log.info("Starting live loop (max %ds) ...", _MAX_DURATION)
    runner.run(max_duration_seconds=_MAX_DURATION)
    log.info("Scheduled test complete. Log written to %s", _LOG_FILE)


if __name__ == "__main__":
    main()
