#!/usr/bin/env python3
"""
Runs the state-machine-driven live tennis pipeline with the FastAPI dashboard.
Usage: python3 scripts/run_collector.py
Ctrl+C to stop.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from src.live.collector import MatchCollector
from src.live.logger import MatchLogger
from src.live.scheduler import MatchScheduler
from src.live.tennis_feed import TennisFeed

_PORT = 8000
_LOG_DIR = _ROOT / "logs"
_LOG_FILE = _LOG_DIR / "collector.log"


def _configure_logging() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Clear any pre-existing handlers (e.g. from basicConfig elsewhere).
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    file_handler = logging.FileHandler(_LOG_FILE)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _start_backend() -> None:
    import uvicorn
    from src.live.backend import app
    uvicorn.run(app, host="0.0.0.0", port=_PORT, log_level="warning")


def main() -> None:
    _configure_logging()
    log = logging.getLogger("run_collector")

    rapidapi_key = os.environ.get("RAPIDAPI_KEY")
    if not rapidapi_key:
        raise ValueError("RAPIDAPI_KEY not set in .env")

    threading.Thread(target=_start_backend, daemon=True, name="backend").start()
    time.sleep(1)
    log.info("Dashboard at http://localhost:%d/dashboard", _PORT)

    feed      = TennisFeed(api_key=rapidapi_key)
    collector = MatchCollector(
        rapidapi_key=rapidapi_key,
    )
    scheduler_logger = MatchLogger()
    scheduler = MatchScheduler(feed=feed, collector=collector, logger=scheduler_logger)

    scheduler.start()
    log.info("MatchScheduler started — current state: %s", scheduler.state)

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutdown requested by user")
    finally:
        scheduler.stop()
        collector.stop()
        scheduler_logger.close()
        log.info("Clean shutdown complete")


if __name__ == "__main__":
    main()
