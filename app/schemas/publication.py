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

    Read-only where it matters: the posts are fetched exactly as a real run
    fetches them and the same decisions are made, but no article is written and
    no suggestion is claimed.

    Placements are the one exception. Missing ones are generated and stored
    before the preview is computed — otherwise every bulk-approved row would
    show as "block" here and then publish in-text, which is a preview that
    predicts the wrong thing. The generation is the same bounded, once-per-row
    pass publication runs, so the publish that follows reuses these choices
    rather than paying again. `placements_missing` counts what that pass could
    not cover: rows past its budget, or whose model call failed. Those show as
    "block" here and may still become in-text links at publication.
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
