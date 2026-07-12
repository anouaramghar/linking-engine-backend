"""External search abstraction (v3) — same pattern as ContentConnector: the engine
only sees normalized ExternalResult objects, each provider implements search()."""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class ExternalResult(BaseModel):
    url: str
    title: str
    snippet: str = ""
    provider_score: float = 0.5  # provider relevance if given, else neutral


class SearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, k: int = 10) -> list[ExternalResult]: ...
