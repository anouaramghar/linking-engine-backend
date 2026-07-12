"""Brave Search — fallback provider when Tavily quota runs out."""

import httpx

from app.config import settings
from app.ml.external.base import ExternalResult, SearchProvider


class BraveProvider(SearchProvider):
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.brave_api_key
        if not self.api_key:
            raise RuntimeError("BRAVE_API_KEY is not set")

    def search(self, query: str, k: int = 10) -> list[ExternalResult]:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": k},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [
            ExternalResult(
                url=r["url"],
                title=r.get("title") or r["url"],
                snippet=r.get("description", ""),
            )
            for r in results
        ]
