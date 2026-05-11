"""Probe DraftKings pages with Playwright to see what content is reachable."""
from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"


def probe(page, url: str, screenshot_path: Path) -> None:
    print(f"\n--- Navigating to {url} ---")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    time.sleep(3)
    time.sleep(5)

    print(f"Title: {page.title()!r}")

    body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
    print(f"Body text length: {len(body_text)} chars")

    tennis_lines = [
        line.strip()
        for line in body_text.splitlines()
        if re.search(r"tennis", line, re.IGNORECASE)
    ]
    if tennis_lines:
        print(f"Found {len(tennis_lines)} lines containing 'tennis':")
        for line in tennis_lines[:50]:
            print(f"  • {line}")
    else:
        print("No occurrences of 'tennis' found in page text.")

    page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"Screenshot saved → {screenshot_path}")


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        probe(page, "https://www.draftkings.com/lobby#Live", DATA / "draftkings_screenshot.png")
        probe(
            page,
            "https://sportsbook.draftkings.com/leagues/tennis/atp-mens",
            DATA / "draftkings_tennis_screenshot.png",
        )

        browser.close()


if __name__ == "__main__":
    main()
