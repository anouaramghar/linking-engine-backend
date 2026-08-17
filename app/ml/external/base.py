"""Provider-neutral contract for discovering external link candidates."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ExternalSearchError(RuntimeError):
    """An external search provider could not complete a request."""


class ExternalSearchNotConfigured(ExternalSearchError):
    """External search is disabled because its credentials are missing."""


class ExternalSearchRateLimited(ExternalSearchError):
    """The provider temporarily rejected the request because of rate limits."""


class ExternalSearchTransientError(ExternalSearchError):
    """A temporary transport or provider failure exhausted bounded retries."""


class ExternalSearchQuotaExceeded(ExternalSearchError):
    """The provider account cannot search until its quota or plan changes."""


class ExternalSearchResponseError(ExternalSearchError):
    """The provider returned a response that does not satisfy its contract."""


@dataclass(frozen=True, slots=True)
class ExternalSearchResult:
    """One provider result before LinkMesh applies its own ranking and policies.

    ``provider_score`` is deliberately separate from the LinkMesh suggestion
    score. It only expresses the upstream provider's relevance and must not be
    presented as the engine's semantic confidence.
    """

    title: str
    url: str
    snippet: str = ""
    provider_score: float | None = None
    provider_result_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExternalSearchResponse:
    """A bounded set of candidates and provider traceability information."""

    provider: str
    query: str
    results: tuple[ExternalSearchResult, ...]
    request_id: str | None = None
    response_time_seconds: float | None = None
    credits_used: int | None = None
    attempt_count: int = 1


@runtime_checkable
class ExternalSearchProvider(Protocol):
    """Interface implemented by paid or self-hosted search providers."""

    name: str

    def search(
        self,
        query: str,
        *,
        max_results: int,
        exclude_domains: Sequence[str] = (),
    ) -> ExternalSearchResponse:
        """Return no more than ``max_results`` candidates for ``query``."""
