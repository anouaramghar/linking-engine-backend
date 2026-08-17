from pydantic import AliasChoices, BaseModel, ConfigDict, Field

MAX_ARTICLE_IMPORT_ROWS = 10_000


class ArticleImportLink(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    anchor_text: str | None = None


class ArticleImportRow(BaseModel):
    """Normalized or Screaming Frog-shaped article metadata.

    ``validation_alias`` keeps the API useful for a raw JSON representation of
    a CSV row while the frontend can send the shorter normalized names.
    """

    model_config = ConfigDict(extra="ignore")

    url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("url", "address", "Address", "URL"),
    )
    title: str | None = Field(
        default=None,
        validation_alias=AliasChoices("title", "title_1", "Title 1", "Title"),
    )
    content_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("content_text", "content", "Content"),
    )
    content_html: str | None = Field(
        default=None,
        validation_alias=AliasChoices("content_html", "html", "HTML"),
    )
    canonical_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "canonical_url",
            "canonical",
            "Canonical Link Element 1",
        ),
    )
    status_code: int | None = Field(
        default=None,
        validation_alias=AliasChoices("status_code", "status", "Status Code"),
    )
    indexability: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "indexability",
            "indexability_status",
            "Indexability",
            "Indexability Status",
        ),
    )
    discovered_from: str | None = None
    discovery_depth: int = Field(default=0, ge=0, le=100)
    outbound_internal_links: list[ArticleImportLink] = Field(default_factory=list)


class ArticleImportRequest(BaseModel):
    rows: list[ArticleImportRow] = Field(
        min_length=1,
        max_length=MAX_ARTICLE_IMPORT_ROWS,
    )
    replace_snapshot: bool = False


class ArticleImportFailure(BaseModel):
    row: int
    url: str | None
    reason: str


class ArticleImportResult(BaseModel):
    ingestion_run_id: int
    imported: int
    updated: int
    links_found: int
    skipped: list[ArticleImportFailure]
    rejected: list[ArticleImportFailure]
    diagnostic_summary: dict[str, int]
