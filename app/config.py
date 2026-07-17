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

    # Embeddings
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"

    # Editorial rules (A4)
    max_suggestions_per_article: int = 5

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


settings = Settings()
