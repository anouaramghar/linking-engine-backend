"""ContentConnector — the ingestion abstraction (design dossier §3).

The engine only ever sees normalized domain objects (ArticleData, SiteMetadata);
each platform implements this interface. URL is the universal identifier.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.site import Site
from app.models.suggestion import Suggestion

#: What publication actually did with one approved suggestion.
#:
#: "applied" alone cannot answer the question the feature exists to answer —
#: how often a link lands in the prose rather than in an appended block — and it
#: reports a link we did not write (one an editor had already added by hand) the
#: same as one we did.
LinkOutcome = Literal["inserted", "block", "already_present"]


@dataclass(frozen=True)
class LinkPreview:
    """The exact WordPress content before and after one batched edit."""

    original_content: str
    updated_content: str
    outcomes: list[LinkOutcome]


class OutboundLink(BaseModel):
    """One link found on a crawled page.

    The anchor travels with the URL because it is the only place it exists: it
    cannot be recovered later from either article's text, and without it no
    report can see forty pages pointing at one target with the same words.
    """

    url: str
    anchor_text: str | None = None


class TaxonomyData(BaseModel):
    kind: str  # category | tag
    name: str
    external_id: str | None = None


class ArticleData(BaseModel):
    url: str
    title: str
    content_text: str
    content_html: str | None = None
    external_id: str | None = None
    language: str | None = None
    published_at: datetime | None = None
    taxonomies: list[TaxonomyData] = []
    outbound_internal_links: list[OutboundLink] = []  # links to other pages of the same site


class SiteMetadata(BaseModel):
    name: str
    base_url: str
    platform: str
    article_count: int | None = None


class ContentConnector(ABC):
    def __init__(self, site: Site):
        self.site = site

    @abstractmethod
    def fetch_articles(self) -> Iterator[ArticleData]: ...

    @abstractmethod
    def fetch_article_by_url(self, url: str) -> ArticleData | None: ...

    @abstractmethod
    def get_site_metadata(self) -> SiteMetadata: ...

    @abstractmethod
    def supports_incremental_sync(self) -> bool: ...

    @abstractmethod
    def apply_links(
        self, suggestions: list[Suggestion], *, dry_run: bool = False
    ) -> list[LinkOutcome]:
        """Write every approved link for ONE source article, in a single edit.

        Batched per article rather than per suggestion: each link used to cost
        its own GET and POST, so three links into one post meant six requests
        and three WordPress revisions. Returns one outcome per suggestion, in
        the order given.

        `dry_run` reads the live post and decides exactly as a real run would,
        then returns without writing — the only way to see what publication will
        do to a customer's article before it does it. HTML connector:
        NotImplementedError (A3).
        """

    def preview_links(self, suggestions: list[Suggestion]) -> LinkPreview:
        """Read and render one article without saving it."""
        raise NotImplementedError("this connector does not support publication previews")
