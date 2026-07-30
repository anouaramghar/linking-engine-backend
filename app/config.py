from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://linkmesh:linkmesh@localhost:5432/linkmesh"
    redis_url: str = "redis://localhost:6379/0"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    environment: str = "development"

    # Static API key for all non-health endpoints; empty disables the check (local dev only)
    api_key: str = ""

    # External search (v3)
    tavily_api_key: str = ""

    # Best-effort operations alerting; empty logs alerts locally.
    alert_webhook_url: str = ""

    # Embeddings
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_device: str = "cpu"

    # Editorial rules (A4)
    max_suggestions_per_article: int = 5
    # V1 rollout is site-scoped and off by default. Environment values use JSON,
    # for example V1_SHADOW_SITE_IDS='[12, 34]'.
    v1_shadow_site_ids: frozenset[int] = frozenset()
    v1_pilot_site_ids: frozenset[int] = frozenset()
    v1_shadow_max_sources: int = Field(default=100, gt=0)
    # The visible Hybrid path exposes the top three BM25-ranked suggestions.
    # Standard cosine generation and read-only comparisons keep the normal cap.
    v1_pilot_max_suggestions_per_article: int = Field(default=3, gt=0)
    # A target this close to the source is the same page, not a link candidate.
    # Applied by the pilot ranking path to both halves of its candidate union;
    # the Standard cosine path is unchanged.
    suggestion_duplicate_similarity_threshold: float = Field(default=0.99, ge=0.0, le=1.0)

    # Reject suspiciously incomplete crawls before they can replace a healthy snapshot.
    ingestion_min_previous_ratio: float = Field(default=0.5, ge=0.0, le=1.0)

    # Crawl-target safety (Phase 0, finding #1): block private/loopback/link-local/
    # metadata destinations and require HTTPS when WP credentials are used.
    # True relaxes both for crawling local test sites — development only.
    allow_unsafe_crawl_targets: bool = False

    @model_validator(mode="after")
    def require_api_key_outside_development(self) -> Self:
        if self.environment != "development" and not self.api_key:
            raise ValueError("API_KEY must be set outside development")
        return self

    @model_validator(mode="after")
    def forbid_unsafe_crawl_targets_outside_development(self) -> Self:
        if self.environment != "development" and self.allow_unsafe_crawl_targets:
            raise ValueError("ALLOW_UNSAFE_CRAWL_TARGETS is development-only")
        return self

    @model_validator(mode="after")
    def keep_v1_site_scopes_disjoint(self) -> Self:
        overlap = self.v1_shadow_site_ids & self.v1_pilot_site_ids
        if overlap:
            raise ValueError(
                "V1_SHADOW_SITE_IDS and V1_PILOT_SITE_IDS overlap: "
                + ", ".join(str(site_id) for site_id in sorted(overlap))
            )
        return self


settings = Settings()
