"""News headlines for LLM context (Finnhub). Soft-fails to empty list:
missing news never blocks a cycle, it just means the analyst sees less."""

from __future__ import annotations

import httpx

from ..domain import utcnow

FINNHUB_BASE = "https://finnhub.io/api/v1"


class NewsService:
    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key

    def headlines(self, symbol: str, limit: int = 10) -> list[str]:
        if not self.api_key:
            return []
        try:
            resp = httpx.get(
                f"{FINNHUB_BASE}/news",
                params={"symbol": symbol, "token": self.api_key, "category": "general"},
                timeout=10.0,
            )
            resp.raise_for_status()
            items = resp.json() or []
            return [str(item.get("headline", ""))[:280] for item in items[:limit]]
        except Exception:
            return []


def news_provenance(api_key: str | None) -> str:
    return f"finnhub@{utcnow().isoformat()}" if api_key else "unavailable"
