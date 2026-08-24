from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.connectors.url_guard import UnsafeURLError, validate_url
from app.ml.external.cleaning import normalize_external_url
from app.security.credentials import CredentialEncryptionError, validate_credential_encryption_key

MAX_BULK_SITES = 1000


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=2048)
    platform: Literal["wordpress", "html", "pool"]
    crawl_frequency: Literal["manual", "daily"] | None = None
    wp_username: str | None = Field(default=None, max_length=255)
    wp_app_password: str | None = Field(default=None, max_length=255)
    domain_registered_at: date | None = None

    @model_validator(mode="after")
    def safe_base_url(self) -> "SiteCreate":
        if bool(self.wp_username) != bool(self.wp_app_password):
            raise ValueError("wp_username and wp_app_password must be provided together")
        if self.platform != "wordpress" and (self.wp_username or self.wp_app_password):
            raise ValueError("WordPress credentials are only valid for WordPress sites")
        if self.wp_app_password:
            try:
                # Fail before the request reaches the database. The actual value is
                # encrypted by the model's database type on write.
                validate_credential_encryption_key()
            except CredentialEncryptionError as error:
                raise ValueError(str(error)) from error
        if self.platform != "pool" and self.crawl_frequency not in (None, "manual"):
            raise ValueError("daily crawl frequency is reserved for content-pool sources")
        if self.platform != "pool" and self.domain_registered_at is not None:
            raise ValueError("domain registration date is only valid for content-pool sources")
        if (
            self.domain_registered_at is not None
            and self.domain_registered_at > datetime.now(UTC).date()
        ):
            raise ValueError("domain registration date cannot be in the future")
        if self.platform == "pool":
            # A pool source is external input and may arrive from a copied URL
            # containing a fragment, tracking parameters, or a default port.
            # Store one canonical representation so the unique base_url
            # constraint catches repeated registrations of the same source.
            self.base_url = normalize_external_url(self.base_url)
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


class SiteCreateRequest(SiteCreate):
    """Site creation plus the absence guard carried by staged proposals."""

    expected_absent: Literal[True] | None = Field(
        None,
        description="When true, creation is confirmed only while this tenant has no matching URL.",
    )


class SiteCredentials(BaseModel):
    """A WordPress account for a site that already exists.

    Separate from `SiteCreate` because rotation is not creation: the base URL,
    the platform, and the tenant are settled, and only the account may move. An
    application password that is revoked, or one that was never given, otherwise
    leaves the site unpublishable with no way back but deleting it.
    """

    wp_username: str = Field(min_length=1, max_length=255)
    wp_app_password: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def encryption_configured(self) -> "SiteCredentials":
        try:
            # Fail before the request reaches the database, exactly as creation
            # does: the value is encrypted by the model's database type on write.
            validate_credential_encryption_key()
        except CredentialEncryptionError as error:
            raise ValueError(str(error)) from error
        return self


class PoolSourceExpectedState(BaseModel):
    """The mutable pool-source fields a staged lifecycle action binds."""

    approved: bool
    quarantined: bool
    consecutive_failures: int = Field(ge=0)
    quarantined_at: datetime | None


class PoolSourceActionGuard(BaseModel):
    """Optional race guard for approval, revocation, and reactivation."""

    model_config = ConfigDict(extra="ignore")

    expected: PoolSourceExpectedState | None = None
    expected_expiring_suggestion_ids: list[int] | None = None

    @field_validator("expected_expiring_suggestion_ids")
    @classmethod
    def sorted_unique_positive_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and (any(item < 1 for item in value) or value != sorted(set(value))):
            raise ValueError(
                "expected_expiring_suggestion_ids must be sorted, unique, and positive"
            )
        return value


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
    expected_absent_base_urls: list[str] | None = Field(
        None,
        min_length=1,
        max_length=MAX_BULK_SITES,
        description=(
            "Optional exact normalized URL set for an atomic staged bulk creation. "
            "Ordinary CSV imports omit it and retain partial-success behavior."
        ),
    )

    @field_validator("expected_absent_base_urls")
    @classmethod
    def sorted_unique_expected_urls(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and value != sorted(set(value)):
            raise ValueError("expected_absent_base_urls must be sorted and unique")
        return value


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


class PoolSourceValidationRequest(BaseModel):
    """One pool source to probe without creating or approving it."""

    name: str | None = None
    base_url: str | None = None


class PoolSourceValidationResult(BaseModel):
    base_url: str | None
    valid: bool
    source_type: Literal["wikipedia", "rss_atom"] | None = None
    reason: str | None = None


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
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
    domain_registered_at: date | None = None
    editorial_feedback_enabled: bool = False
    editorial_min_score_percent: int = 0
    editorial_feedback_weight: float = 0.20
    editorial_feedback_min_samples: int = 10
    suggestion_method: Literal["hybrid_bm25"] = "hybrid_bm25"
    suggestion_slots_available: int = 0
    #: Whether an account exists that could edit this site's posts. Read from
    #: the model's property, which tests the username alone — the password is an
    #: encrypted column, and decrypting every row of a list page to learn a
    #: boolean would be work for nothing. The two are always written together.
    has_wordpress_credentials: bool = False
    created_at: datetime
    last_ingestion_status: str | None = None
    #: Why the last crawl failed, in the crawler's own words. Carried on the
    #: list row so a failed site can say what went wrong where it is shown,
    #: instead of sending the operator to the engine logs. Trimmed, because a
    #: stack-trace-length message on every row is payload nobody reads.
    last_ingestion_error: str | None = None
    # Last *finished* analysis, so a crawled site reads differently from an
    # analysed one once both jobs have left the active feed.
    last_analysis_status: str | None = None
    last_analysis_at: datetime | None = None
    last_analysis_error: str | None = None
    article_count: int = 0
    internal_link_count: int = 0
    last_crawl_at: datetime | None = None


class EditorialRankingPolicyValues(BaseModel):
    #: Required, not defaulted: switching feedback reranking on is a deliberate
    #: act while it is unproven, so an omitted field must not turn it on as a
    #: side effect of editing the thresholds beside it.
    enabled: bool
    min_score_percent: int = Field(ge=0, le=100)
    feedback_weight: float = Field(ge=0.0, le=1.0)
    min_samples: int = Field(ge=1, le=10_000)


class EditorialRankingPolicyUpdate(EditorialRankingPolicyValues):
    expected: EditorialRankingPolicyValues | None = Field(
        None,
        description=(
            "Only save if the current policy still equals this snapshot. "
            "Agent-staged changes use it as an optimistic-concurrency guard."
        ),
    )


class EditorialRankingPolicyOut(EditorialRankingPolicyValues):
    site_id: int


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
    discovered_urls: int
    accepted_urls: int
    skipped_urls: int
    diagnostic_summary: dict
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class IngestionDiagnosticOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    site_id: int
    ingestion_run_id: int
    url: str
    state: str
    reason_code: str
    reason_detail: str | None
    discovered_from: str | None
    depth: int
    http_status: int | None
    content_type: str | None
    final_url: str | None
    canonical_url: str | None
    created_at: datetime
