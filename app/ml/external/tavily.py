"""Tavily search — primary external provider (locked in v0)."""

import httpx

from app.config import settings
from app.ml.external.base import ExternalResult, SearchProvider


class TavilyProvider(SearchProvider):
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.tavily_api_key
        if not self.api_key:
            raise RuntimeError("TAVILY_API_KEY is not set")

    def search(self, query: str, k: int = 10) -> list[ExternalResult]:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": self.api_key, "query": query, "max_results": k},
            timeout=30,
        )
        resp.raise_for_status()
        return [
            ExternalResult(
                url=r["url"],
                title=r.get("title") or r["url"],
                snippet=r.get("content", ""),
                provider_score=float(r.get("score", 0.5)),
            )
            for r in resp.json().get("results", [])
        ]
