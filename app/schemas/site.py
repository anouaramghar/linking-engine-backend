from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.connectors.url_guard import UnsafeURLError, validate_url

MAX_BULK_SITES = 1000


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=2048)
    platform: Literal["wordpress", "html", "pool"]
    crawl_frequency: Literal["manual", "daily"] | None = None
    wp_username: str | None = Field(default=None, max_length=255)
    wp_app_password: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def safe_base_url(self) -> "SiteCreate":
        if bool(self.wp_username) != bool(self.wp_app_password):
            raise ValueError("wp_username and wp_app_password must be provided together")
        if self.platform != "wordpress" and (self.wp_username or self.wp_app_password):
            raise ValueError("WordPress credentials are only valid for WordPress sites")
        if self.platform != "pool" and self.crawl_frequency not in (None, "manual"):
            raise ValueError("daily crawl frequency is reserved for content-pool sources")
        allow = settings.allow_unsafe_crawl_targets
        try:
            validate_url(
                self.base_url,
                allow_private=allow,
                require_https=(
                    bool(self.wp_username or self.wp_app_password) or self.platform == "pool"
                )
                and not allow,
                resolve_dns=False,  # the pinned crawl transport resolves hostnames at connect time
            )
        except UnsafeURLError as e:
            raise ValueError(str(e)) from e
        self.base_url = self.base_url.rstrip("/")
        if self.crawl_frequency is None:
            self.crawl_frequency = "daily" if self.platform == "pool" else "manual"
        return self


class SiteBulkRow(BaseModel):
    """One inbound row of a bulk import.

    Deliberately lenient: every field is optional and untyped beyond `str` so that a
    malformed row is reported against its own row number instead of rejecting the whole
    upload with a 422. Each row is re-validated through `SiteCreate` in the route.
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    base_url: str | None = None
    platform: str | None = None
    wp_username: str | None = None
    wp_app_password: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        # CSV cells arrive as "" rather than absent; treat them as unset.
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("platform", mode="after")
    @classmethod
    def normalize_platform(cls, value: str | None) -> str | None:
        return value.lower() if value else value


class SiteBulkRequest(BaseModel):
    sites: list[SiteBulkRow] = Field(min_length=1, max_length=MAX_BULK_SITES)


class SiteBulkCreated(BaseModel):
    row: int  # 1-based index into the submitted list, not the CSV line number
    id: int
    name: str
    base_url: str


class SiteBulkFailure(BaseModel):
    row: int
    base_url: str | None
    reason: str


class SiteBulkResult(BaseModel):
    created: list[SiteBulkCreated]
    skipped: list[SiteBulkFailure]  # already present, or duplicated within the upload
    rejected: list[SiteBulkFailure]  # failed validation, including the SSRF guard


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    platform: str
    crawl_frequency: str
    pool_source_approved: bool = False
    pool_source_approved_at: datetime | None = None
    pool_source_approved_by: str | None = None
    pool_source_consecutive_failures: int = 0
    pool_source_quarantined: bool = False
    pool_source_quarantined_at: datetime | None = None
    pool_source_quarantine_reason: str | None = None
    pool_source_last_reactivated_at: datetime | None = None
    pool_source_last_reactivated_by: str | None = None
    suggestion_method: Literal["hybrid_bm25"] = "hybrid_bm25"
    suggestion_mode: Literal["standard", "experimental"]
    suggestion_mode_managed: bool = True
    suggestion_comparison_enabled: bool = False
    suggestion_slots_available: int = 0
    created_at: datetime
    last_ingestion_status: str | None = None
    # Last *finished* analysis, so a crawled site reads differently from an
    # analysed one once both jobs have left the active feed.
    last_analysis_status: str | None = None
    last_analysis_at: datetime | None = None
    article_count: int = 0
    internal_link_count: int = 0
    last_crawl_at: datetime | None = None


class PoolSourceApproval(BaseModel):
    approved_by: str = Field(min_length=1, max_length=255)

    @field_validator("approved_by")
    @classmethod
    def normalize_approver(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("approved_by must not be blank")
        return normalized


class PoolSourceReactivation(BaseModel):
    reactivated_by: str = Field(min_length=1, max_length=255)

    @field_validator("reactivated_by")
    @classmethod
    def normalize_reviewer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reactivated_by must not be blank")
        return normalized


class SiteSuggestionModeUpdate(BaseModel):
    suggestion_mode: Literal["standard", "experimental"]


class SiteSuggestionModeState(BaseModel):
    suggestion_mode: Literal["standard", "experimental"]
    suggestion_mode_managed: bool
    suggestion_comparison_enabled: bool


class ArticleBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    url: str


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str | None
    url: str
    title: str
    language: str | None
    published_at: datetime | None


class IngestionRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    status: str
    articles_upserted: int
    links_found: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None
