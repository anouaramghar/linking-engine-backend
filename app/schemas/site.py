from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class SiteCreate(BaseModel):
    name: str
    base_url: str
    platform: Literal["wordpress", "html"]
    wp_username: str | None = None
    wp_app_password: str | None = None

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")


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
