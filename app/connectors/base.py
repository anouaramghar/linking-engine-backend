"""ContentConnector — the ingestion abstraction (design dossier §3).

The engine only ever sees normalized domain objects (ArticleData, SiteMetadata);
each platform implements this interface. URL is the universal identifier.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime

from pydantic import BaseModel

from app.models.site import Site
from app.models.suggestion import Suggestion


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
    outbound_internal_urls: list[str] = []  # links to other pages of the same site


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
    def apply_link(self, suggestion: Suggestion) -> None:
        """Write an approved link into the source article. HTML connector: NotImplementedError (A3)."""
