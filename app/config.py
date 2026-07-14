from typing import Self

from pydantic import model_validator
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

    @model_validator(mode="after")
    def require_api_key_outside_development(self) -> Self:
        if self.environment != "development" and not self.api_key:
            raise ValueError("API_KEY must be set outside development")
        return self


settings = Settings()
