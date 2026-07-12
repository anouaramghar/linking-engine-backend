from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://linkmesh:linkmesh@localhost:5432/linkmesh"
    redis_url: str = "redis://localhost:6379/0"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    environment: str = "development"

    # External search (v3)
    brave_api_key: str = ""
    tavily_api_key: str = ""

    # Embeddings
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"

    # Editorial rules (A4)
    max_suggestions_per_article: int = 5


settings = Settings()
