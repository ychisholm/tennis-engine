from __future__ import annotations

import os
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE_URL = "https://tennisapi1.p.rapidapi.com"
_MAX_RETRIES = 3
_RETRY_DELAY = 1.0


class OddsFeed:
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ["RAPIDAPI_KEY"]
        self._headers = {
            "x-rapidapi-host": "tennisapi1.p.rapidapi.com",
            "x-rapidapi-key": self._api_key,
        }

    def _get(self, path: str) -> Any:
        url = f"{_BASE_URL}{path}"
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(_MAX_RETRIES):
            try:
                resp = requests.get(url, headers=self._headers, timeout=10)
                if resp.status_code == 200:
                    return resp.json()
                last_exc = ValueError(
                    f"HTTP {resp.status_code} from {path}: {resp.text[:200]}"
                )
            except requests.RequestException as exc:
                last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAY)
        raise last_exc

    def get_odds(self, match_id: int | str) -> dict | None:
        try:
            data = self._get(f"/api/tennis/event/{match_id}/odds")
        except Exception:
            return None

        if not data:
            return None

        markets = []
        if isinstance(data, dict):
            markets = data.get("markets", [])
        elif isinstance(data, list):
            markets = data

        for market in markets:
            choices = market.get("choices", [])
            home_prob: float | None = None
            away_prob: float | None = None

            for choice in choices:
                decimal = self._parse_decimal(choice)
                if decimal is None or decimal <= 0:
                    continue
                prob = 1.0 / decimal
                name = str(choice.get("name") or "").lower()
                if "1" in name or "home" in name:
                    home_prob = prob
                elif "2" in name or "away" in name:
                    away_prob = prob

            if home_prob is not None and away_prob is not None:
                return {
                    "home_implied_prob": home_prob,
                    "away_implied_prob": away_prob,
                }

        return None

    @staticmethod
    def _parse_decimal(choice: dict) -> float | None:
        decimal = choice.get("decimalValue")
        if decimal is not None:
            try:
                return float(decimal)
            except (TypeError, ValueError):
                pass

        fractional = str(choice.get("fractionalValue", ""))
        if "/" in fractional:
            try:
                num, den = fractional.split("/", 1)
                return float(num) / float(den) + 1.0
            except (ValueError, ZeroDivisionError):
                pass

        return None
