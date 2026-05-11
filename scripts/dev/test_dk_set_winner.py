"""Expand Set Winner & related markets on a live DK tennis match and scrape odds."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
MATCH_URL = (
    "https://sportsbook.draftkings.com/event/"
    "flavio-cobolli-vs-thiago-agustin-tirante/34127760"
)


def parse_outcomes(text: str) -> dict:
    """
    Walk the body text line by line. When we hit a known market header,
    grab the next 4 non-empty lines as (player1, odds1, player2, odds2).
    """
    markets_of_interest = {
        "Moneyline", "Set Winner", "Set Betting", "Total Sets",
        "Player to Win at Least One Set", "Correct Score - Listed Set",
        "Game Winner", "Point Winner",
    }
    lines = [ln.strip() for ln in text.splitlines()]
    odds_re = re.compile(r'^[+-−]\d+(\.\d+)?$|^\d+\.\d+$')

    out: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line in markets_of_interest and line not in out:
            # Collect non-empty lines until we have 2 outcomes (4 tokens)
            picks = []
            j = i + 1
            while j < len(lines) and len(picks) < 8:
                v = lines[j]
                if v and v not in markets_of_interest:
                    picks.append(v)
                elif v in markets_of_interest and picks:
                    break
                j += 1
            # Pair up: [name, odds, name, odds, ...]
            pairs = []
            k = 0
            while k + 1 < len(picks):
                name, odds = picks[k], picks[k + 1]
                if odds_re.match(odds.replace('−', '-')):
                    pairs.append({"name": name, "american": odds.replace('−', '-')})
                    k += 2
                else:
                    k += 1
            if pairs:
                out[line] = pairs
        i += 1
    return out


def american_to_prob(american: str) -> float:
    a = int(american)
    if a > 0:
        return 100.0 / (a + 100.0)
    return -a / (-a + 100.0)


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1400, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        print(f"=== Loading {MATCH_URL} ===")
        page.goto(MATCH_URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(8)

        # Try to expand every collapsed market. DK uses an expandable shell
        # with a header that toggles when clicked.
        for label in [
            "Set Winner", "Set Betting", "Total Sets",
            "Player to Win at Least One Set", "Correct Score - Listed Set",
        ]:
            try:
                # Find the header element, then click it
                hdr = page.locator(f"text=/^{re.escape(label)}$/").first
                if hdr.count() > 0:
                    hdr.scroll_into_view_if_needed(timeout=4000)
                    hdr.click(timeout=4000)
                    print(f"  Expanded: {label}")
                    time.sleep(1.5)
                else:
                    print(f"  Header not found: {label}")
            except Exception as e:
                print(f"  Failed to expand {label}: {e}")

        time.sleep(2)
        page.screenshot(path=str(DATA / "dk_cobolli_expanded.png"), full_page=True)
        print(f"Screenshot → {DATA / 'dk_cobolli_expanded.png'}")

        body_text = page.evaluate("() => document.body.innerText")
        (DATA / "dk_cobolli_body_expanded.txt").write_text(body_text)
        print(f"Body text → {DATA / 'dk_cobolli_body_expanded.txt'}")

        markets = parse_outcomes(body_text)
        print("\n=== Parsed live markets ===")
        for mkt, picks in markets.items():
            print(f"\n  {mkt}:")
            for pk in picks:
                try:
                    prob = american_to_prob(pk["american"])
                    print(f"    {pk['name']:35s} {pk['american']:>6}  (implied {prob:.3f})")
                except Exception:
                    print(f"    {pk['name']:35s} {pk['american']:>6}")

        (DATA / "dk_cobolli_markets.json").write_text(json.dumps(markets, indent=2))
        print(f"\nMarkets JSON → {DATA / 'dk_cobolli_markets.json'}")

        time.sleep(2)
        browser.close()


if __name__ == "__main__":
    main()
