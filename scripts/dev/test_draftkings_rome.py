"""Drill into DraftKings ATP Rome → Cobolli match and scrape match-winner odds."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

ROME_URL = "https://sportsbook.draftkings.com/leagues/tennis/atp-rome"


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # ── Load Rome page directly ────────────────────────────────────────
        print(f"\n=== Loading {ROME_URL} ===")
        page.goto(ROME_URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(8)
        print(f"Title: {page.title()!r}")

        # Dismiss DraftKings Social popup if present (it overlays content)
        for sel in ["button:has-text('No Thanks')", "button:has-text('Close')",
                    "[aria-label='Close']", "button[aria-label*='close' i]"]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=2000)
                    print(f"  Dismissed popup via {sel}")
                    time.sleep(1)
                    break
            except Exception:
                pass

        # ── List all match rows on Rome page ───────────────────────────────
        rows = page.evaluate(
            """
            () => {
              const out = [];
              // DK event rows are <a> elements pointing to /event/<id>
              const links = document.querySelectorAll("a[href*='/event/']");
              links.forEach(a => {
                const t = (a.innerText || '').replace(/\\s+/g,' ').trim();
                if (t) out.push({href: a.getAttribute('href'), text: t.slice(0, 200)});
              });
              return out;
            }
            """
        )
        print(f"\nFound {len(rows)} event links on Rome page:")
        for r in rows[:30]:
            print(f"  • {r['text']}  →  {r['href']}")

        # ── Find Cobolli's event link ──────────────────────────────────────
        cobolli_href = None
        for r in rows:
            if re.search(r'cobolli', r['text'], re.IGNORECASE):
                cobolli_href = r['href']
                print(f"\n>>> Cobolli match link: {r['text']}  →  {cobolli_href}")
                break

        if cobolli_href is None:
            print("Cobolli not found among event links — leaving Rome page as-is.")
            page.screenshot(path=str(DATA / "dk_step2_rome_league.png"), full_page=True)
            browser.close()
            return

        # Navigate to the match page
        match_url = cobolli_href if cobolli_href.startswith("http") else (
            "https://sportsbook.draftkings.com" + cobolli_href
        )
        print(f"\n=== Navigating to match: {match_url} ===")
        page.goto(match_url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(8)

        # Dismiss popup again if it reappeared
        for sel in ["button:has-text('No Thanks')", "button:has-text('Close')",
                    "[aria-label='Close']", "button[aria-label*='close' i]"]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=2000)
                    time.sleep(1)
                    break
            except Exception:
                pass

        print(f"Match URL: {page.url}")
        print(f"Match title: {page.title()!r}")
        page.screenshot(path=str(DATA / "dk_step3_cobolli_match.png"), full_page=True)
        print(f"  Screenshot → {DATA / 'dk_step3_cobolli_match.png'}")

        # ── Inspect odds DOM ───────────────────────────────────────────────
        # Find the moneyline section and pull outcome rows.
        inspection = page.evaluate(
            """
            () => {
              // Pull all elements whose class hints at an odds cell or outcome
              const interesting = [];
              const all = document.querySelectorAll('*');
              for (const el of all) {
                const cls = (el.className && typeof el.className === 'string') ? el.className : '';
                if (/outcome|odds|price|line|sportsbook/i.test(cls) && el.children.length < 8) {
                  const t = (el.innerText || '').replace(/\\s+/g,' ').trim();
                  if (t && t.length < 80) {
                    interesting.push({cls: cls.slice(0,120), text: t});
                  }
                }
              }
              return interesting.slice(0, 200);
            }
            """
        )
        print(f"\nFound {len(inspection)} interesting elements (class/text):")
        for it in inspection[:40]:
            print(f"  [{it['cls']}]  {it['text']}")

        # Extract match-winner: typically two price rows under a "Moneyline" header
        moneyline = page.evaluate(
            """
            () => {
              // Find a header containing "Moneyline" then pull next sibling block prices
              const heads = Array.from(document.querySelectorAll('*')).filter(
                e => /^\\s*Moneyline\\s*$/i.test((e.innerText||'')) && e.children.length === 0
              );
              const results = [];
              heads.forEach(h => {
                let node = h.parentElement;
                while (node && node.querySelectorAll && node.querySelectorAll('[class*="sportsbook-odds"], [class*="default-color"]').length < 2) {
                  node = node.parentElement;
                  if (!node || node.tagName === 'BODY') break;
                }
                if (node) {
                  results.push({block_text: (node.innerText||'').replace(/\\s+/g,' ').slice(0, 600)});
                }
              });
              return results;
            }
            """
        )
        print(f"\nMoneyline blocks: {json.dumps(moneyline, indent=2)}")

        body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
        american_tokens = re.findall(r'[+-]\d{2,4}\b', body_text)

        # Save full body text for offline inspection
        (DATA / "dk_cobolli_body.txt").write_text(body_text)
        print(f"\nFull body text saved → {DATA / 'dk_cobolli_body.txt'}")
        print(f"American odds tokens on page ({len(american_tokens)}): {american_tokens[:30]}")

        # Save final structured scrape
        out = {
            "match_url": match_url,
            "match_title": page.title(),
            "interesting_elements": inspection,
            "moneyline_blocks": moneyline,
            "american_odds_tokens": american_tokens,
        }
        (DATA / "dk_cobolli_odds.json").write_text(json.dumps(out, indent=2))
        print(f"Structured scrape → {DATA / 'dk_cobolli_odds.json'}")

        time.sleep(2)
        browser.close()


if __name__ == "__main__":
    main()
