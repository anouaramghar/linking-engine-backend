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
    external_provider: str = "tavily"  # tavily | brave
    max_external_per_article: int = 3
    external_trust_threshold: float = 0.5

    # Embeddings
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"

    # Editorial rules (A4)
    max_suggestions_per_article: int = 5

    # Anchor generation (v4) — OpenRouter free models, comma-separated fallback chain
    openrouter_api_key: str = ""
    openrouter_models: str = (
        "meta-llama/llama-3.3-70b-instruct:free,google/gemma-3-27b-it:free"
    )
    anchor_max_words: int = 8

    # GNN (v2)
    gnn_hidden_dim: int = 256
    gnn_out_dim: int = 128
    gnn_epochs: int = 200
    gnn_lr: float = 0.01
    model_dir: str = "models"


settings = Settings()
