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

#: What one approved plan did to the live article.
#:
#: "already_applied" is not a lesser success: it is how a retry after a crash
#: between the WordPress POST and the database commit finishes correctly, by
#: recognising its own earlier write instead of making a second one.
PlannedEditOutcome = Literal["written", "already_applied"]


class StalePlanError(RuntimeError):
    """The live article is neither the approved before nor the approved after.

    Typed, because it must never be treated as a transient failure: RQ retrying
    it would only re-read the same changed article. The approved artifact is
    simply no longer valid, and a human has to prepare and approve a new one
    against the article as it now is.
    """


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
    def apply_planned_edit(
        self, source, *, original_html: str, updated_html: str
    ) -> PlannedEditOutcome:
        """Send one already-approved article edit, and nothing else.

        The only write interface publication has. It receives finished bytes: no
        suggestion, no target, no anchor, no placement — nothing it could use to
        re-decide anything, which is the point. What a human approved is what
        leaves the process.

        Contract, given the live content of `source`:

        - equal to `original_html`  -> POST `updated_html` verbatim, "written"
        - equal to `updated_html`   -> no POST, "already_applied"
        - anything else             -> no POST, raise `StalePlanError`

        Batched per article rather than per suggestion: each link used to cost
        its own GET and POST, so three links into one post meant six requests
        and three WordPress revisions. HTML connector: NotImplementedError (A3).
        """

    def preview_links(self, suggestions: list[Suggestion]) -> LinkPreview:
        """Read and render one article without saving it.

        Preparation only. This is where every rendering decision is made and
        frozen; publication never calls it, because a second rendering could
        differ from the one an operator approved.
        """
        raise NotImplementedError("this connector does not support publication previews")
