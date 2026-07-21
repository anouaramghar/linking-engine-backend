from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.config import settings
from app.connectors.url_guard import UnsafeURLError, validate_url


class SiteCreate(BaseModel):
    name: str
    base_url: str
    platform: Literal["wordpress", "html"]
    wp_username: str | None = None
    wp_app_password: str | None = None

    @model_validator(mode="after")
    def safe_base_url(self) -> "SiteCreate":
        if bool(self.wp_username) != bool(self.wp_app_password):
            raise ValueError("wp_username and wp_app_password must be provided together")
        allow = settings.allow_unsafe_crawl_targets
        try:
            validate_url(
                self.base_url,
                allow_private=allow,
                require_https=bool(self.wp_username or self.wp_app_password) and not allow,
                resolve_dns=False,  # the pinned crawl transport resolves hostnames at connect time
            )
        except UnsafeURLError as e:
            raise ValueError(str(e)) from e
        self.base_url = self.base_url.rstrip("/")
        return self


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    platform: str
    crawl_frequency: str
    created_at: datetime
    last_ingestion_status: str | None = None


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
