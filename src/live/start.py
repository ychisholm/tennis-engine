#!/usr/bin/env python3
"""
Startup script: launches the FastAPI backend, lets you pick a live match,
then runs MatchRunner with database logging attached.

Usage
-----
    python -m src.live.start
    # or
    ~/tennis-engine/.venv/bin/python src/live/start.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# Ensure project root is on sys.path when run directly (python src/live/start.py)
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import uvicorn
from dotenv import load_dotenv

load_dotenv()

from src.live.logger import MatchLogger
from src.live.match_runner import MatchRunner

_PORT = 8000
_DEFAULT_PLAYER: dict = {
    "p0_hard": 0.63,
    "p0_clay": 0.60,
    "p0_grass": 0.63,
    "archetype": {"sd": 60, "ba": 60, "pe": 60, "tv": 60},
}


def _start_backend() -> None:
    from src.live.backend import app
    uvicorn.run(app, host="0.0.0.0", port=_PORT, log_level="warning")


def main() -> None:
    # Initialise DB schema before the backend thread starts so the table exists
    logger = MatchLogger()

    backend = threading.Thread(target=_start_backend, daemon=True)
    backend.start()
    time.sleep(1)  # brief pause so uvicorn binds before we print the URL
    print(f"\nDashboard at http://localhost:{_PORT}/dashboard\n")

    # List live matches
    print("Fetching live matches ...")
    try:
        matches = MatchRunner.list_matches()
    except Exception as exc:
        print(f"Error fetching matches: {exc}")
        logger.close()
        sys.exit(1)

    if not matches:
        print("No live matches found.")
        logger.close()
        return

    print(f"\nFound {len(matches)} live match(es):\n")
    for i, m in enumerate(matches):
        print(f"  [{i:>2}]  {m['id']}  {m['home_player']} vs {m['away_player']}")

    print()
    try:
        idx = int(input("Pick a match number: ").strip())
        match = matches[idx]
    except (ValueError, IndexError):
        print("Invalid selection.")
        logger.close()
        sys.exit(1)

    player_a = {**_DEFAULT_PLAYER, "name": match["home_player"]}
    player_b = {**_DEFAULT_PLAYER, "name": match["away_player"]}

    runner = MatchRunner(
        match_id=match["id"],
        player_a=player_a,
        player_b=player_b,
        logger=logger,
        tournament_id=match.get("tournament_id"),
    )

    print(f"\nTracking: {match['home_player']} vs {match['away_player']}  (id={match['id']})")
    print(f"Dashboard: http://localhost:{_PORT}/dashboard\n")

    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        logger.close()


if __name__ == "__main__":
    main()
