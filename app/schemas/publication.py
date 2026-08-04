from pydantic import BaseModel

from app.connectors.base import LinkOutcome


class PendingPublicationSite(BaseModel):
    site_id: int
    awaiting_publication: int


class PlannedLink(BaseModel):
    """One approved suggestion, and what publication would do with it."""

    suggestion_id: int
    source_article_id: int
    source_url: str
    target_url: str
    outcome: LinkOutcome
    #: The phrase the link would be written on, when the outcome is "inserted".
    anchor_text: str | None = None


class PlannedArticle(BaseModel):
    """One exact WordPress edit, shown without saving it."""

    source_article_id: int
    source_url: str
    original_html: str
    updated_html: str
    links: list[PlannedLink]


class PublicationPreviewError(BaseModel):
    source_article_id: int
    source_url: str
    message: str


class PublicationDryRun(BaseModel):
    """What a publication run would write, decided against the live posts.

    Read-only in every sense: the posts are fetched exactly as a real run
    fetches them and the same decisions are made, but nothing is saved and no
    placement is generated. `placements_missing` is the caveat that makes the
    rest honest — those rows show as "block" here and may still become in-text
    links once the run's preflight pass has generated their placements.
    """

    site_id: int
    approved: int
    previewed: int
    placements_missing: int
    inserted: int
    block: int
    already_present: int
    planned: list[PlannedLink]
    articles: list[PlannedArticle]
    errors: list[PublicationPreviewError]
    truncated: bool
