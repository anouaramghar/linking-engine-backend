from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://linkmesh:linkmesh@localhost:5432/linkmesh"
    redis_url: str = "redis://localhost:6379/0"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    environment: str = "development"

    # Static API key for all non-health endpoints; empty fails closed at the API boundary.
    api_key: str = ""
    # Human operator identities mapped to their individual API keys. These keys
    # may call every protected route and provide trusted approval audit identity.
    operator_api_keys: dict[str, SecretStr] = Field(default_factory=dict)

    # Fernet key used to encrypt WordPress application passwords at rest.
    credential_encryption_key: SecretStr | None = None
    # Comma-separated previous Fernet keys accepted only for decryption during rotation.
    credential_decryption_keys: SecretStr | None = None

    # External search (v3)
    tavily_api_key: str = ""

    # Placement context (v4): an OpenRouter-hosted model reads the source article
    # and picks the passage the link belongs in. Empty key disables the feature —
    # the placement endpoint reports it as unavailable rather than the review
    # queue losing anything it had before.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    placement_model: str = "google/gemma-4-31b-it"
    placement_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    # How much of the source article the model is shown. The model's context
    # window is far larger than this, so the bound is about latency and spend on
    # a per-preview call, not about what it can read.
    placement_max_source_chars: int = Field(default=12_000, ge=500, le=100_000)
    # One passage plus one anchor phrase is a small answer; this only has to be
    # large enough that a valid response is never truncated mid-JSON.
    placement_max_output_tokens: int = Field(default=800, ge=64, le=4_000)

    # Best-effort operations alerting; empty logs alerts locally.
    alert_webhook_url: str = ""

    # Embeddings
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_device: str = "cpu"

    # Global Hybrid contract: BM25-512 final ordering and at most three active
    # suggestions per source article.
    hybrid_max_suggestions_per_article: int = Field(default=3, ge=1, le=3)
    # The cap above bounds open review work, not the article. A reviewed row
    # frees its slot, so once a source's suggestions are applied the next run
    # proposes three fresh targets for it — and the run after that three more,
    # for ever, because the already-suggested filter only blocks a repeated
    # pair. This is the standing bound on how many links LinkMesh may ever add
    # to one article: counted over the rows still on their way to becoming a
    # link plus the ones that already did. A rejection still frees its slot —
    # an editor saying no never put a link on the page.
    hybrid_max_lifetime_links_per_article: int = Field(default=5, ge=1, le=100)
    # Keep each editorial batch reviewable. Later runs continue with sources
    # that still have open suggestion slots.
    hybrid_max_sources_per_run: int = Field(default=50, gt=0)
    # A site-wide queue cap prevents a large crawl from becoming an unreviewable
    # backlog. It is deliberately configurable for larger editorial teams.
    hybrid_max_active_suggestions_per_site: int = Field(default=1500, gt=0)
    # A target this close to the source is the same page, not a link candidate.
    # Applied by the Hybrid ranking path to both halves of its candidate union;
    # the Standard cosine path is unchanged.
    suggestion_duplicate_similarity_threshold: float = Field(default=0.99, ge=0.0, le=1.0)
    # Below this cosine similarity the pair is too weak to ask an editor to
    # review. The local Hybrid corpus currently bottoms out at 0.571, so 0.50
    # removes the weak tail without changing its existing queue.
    suggestion_min_score: float = Field(default=0.50, ge=0.0, le=1.0)
    # Navigational and transactional pages carry no editorial value as link
    # targets. The defaults are English because the pilot sites are; a site in
    # another language needs its own terms, which is why these are settings
    # rather than literals in the ranking SQL. Titles are matched whole and
    # case-insensitively after trimming; slugs are matched as a whole path
    # segment. Emptying either list disables that half of the rule.
    low_value_target_titles: list[str] = Field(
        default=[
            "login", "log in", "sign in", "sign up", "register", "registration",
            "dashboard", "my account", "cart", "shopping cart", "checkout",
            "privacy policy", "terms of service", "terms of use", "cookie policy",
            "support portal",
        ]
    )
    low_value_target_url_slugs: list[str] = Field(
        default=[
            "login", "log-in", "sign-in", "sign-up", "signup", "register",
            "registration", "dashboard", "my-account", "cart", "shopping-cart",
            "checkout", "privacy-policy", "terms-of-service", "terms-of-use",
            "cookie-policy", "support-portal",
        ]
    )

    # How many links publication may place *inside* an article's prose. The
    # per-run suggestion cap above bounds open review work, not the article: an
    # applied suggestion stops counting, so successive runs would keep adding
    # in-text links to the same post. This is the standing limit on the post
    # itself. Suggestions past it still publish, as the appended block — so the
    # setting moves links out of the prose rather than dropping them. 0 turns
    # in-text placement off site-wide and restores the appended-block behaviour.
    publish_max_in_text_links_per_article: int = Field(default=3, ge=0, le=20)
    # Two in-text links a few words apart read as spam even when each is
    # defensible alone. Raw characters, because that is what the splice works
    # in and the bound only has to be roughly right. 0 turns the guard off.
    publish_min_in_text_gap_chars: int = Field(default=300, ge=0, le=10_000)
    # Pause between source articles in a publication run. A run is the densest
    # write traffic LinkMesh ever sends a customer site, and shared hosting
    # answers that with a WAF block rather than a Retry-After we could honour.
    publish_request_delay_seconds: float = Field(default=0.5, ge=0.0, le=60.0)
    # Placements missing at publication time are generated in a preflight pass,
    # because the review queue is worked in bulk and a bulk-approved row never
    # had its drawer opened. Each is a model call of a few seconds, so the pass
    # is capped: past this many the rest publish as the appended block, exactly
    # as they do today. 0 disables preflight and restores lazy-only generation.
    publish_max_placement_calls_per_run: int = Field(default=200, ge=0, le=5_000)
    # How many preflight placement calls run at once. The pass is latency-bound
    # on an external API, not CPU-bound; the ceiling is the provider's rate
    # limit, not ours.
    publish_placement_concurrency: int = Field(default=4, ge=1, le=16)
    # Consecutive failures after which a suggestion stops being retried. Without
    # it a permanently broken row — a post locked by a plugin, a revoked
    # password — fails on every publication run for ever.
    publish_max_suggestion_attempts: int = Field(default=3, ge=1, le=20)

    # Reject suspiciously incomplete crawls before they can replace a healthy snapshot.
    ingestion_min_previous_ratio: float = Field(default=0.5, ge=0.0, le=1.0)

    # Customer-controlled crawl bounds. These apply to HTML and WordPress in
    # addition to the tighter content-pool limits below.
    crawl_max_duration_seconds: int = Field(default=1800, gt=0, le=7200)
    crawl_max_articles: int = Field(default=10_000, ge=1, le=100_000)
    crawl_max_sitemaps: int = Field(default=100, ge=1, le=10_000)
    crawl_max_sitemap_urls: int = Field(default=10_000, ge=1, le=100_000)
    crawl_max_wordpress_pages: int = Field(default=1_000, ge=1, le=10_000)
    crawl_max_response_bytes: int = Field(default=10_000_000, ge=1_024, le=100_000_000)
    crawl_max_article_chars: int = Field(default=100_000, ge=1_000, le=1_000_000)
    crawl_max_links_per_article: int = Field(default=1_000, ge=1, le=100_000)
    crawl_max_total_links: int = Field(default=100_000, ge=1, le=1_000_000)

    # Analysis bounds are checked before embedding or corpus construction.
    analysis_max_articles_per_site: int = Field(default=10_000, ge=1, le=100_000)
    analysis_max_corpus_articles: int = Field(default=20_000, ge=1, le=200_000)

    # External content pool (RSS/Atom and Wikipedia).
    pool_max_articles_per_source: int = Field(default=50, ge=1, le=50)
    pool_source_timeout: float = Field(default=20.0, gt=0.0, le=120.0)
    pool_max_response_bytes: int = Field(default=5_000_000, ge=1_024, le=50_000_000)
    pool_max_article_chars: int = Field(default=100_000, ge=1_000, le=1_000_000)
    pool_max_title_chars: int = Field(default=500, ge=1, le=2_000)
    pool_http_user_agent: str = Field(
        default="LinkMesh/0.1 (contact: linkmesh@example.com)",
        min_length=10,
        max_length=500,
    )
    pool_request_delay_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    pool_allowed_domains: str = "wikipedia.org"
    pool_quarantine_failure_threshold: int = Field(default=3, ge=1, le=20)
    pool_poll_interval_seconds: int = Field(default=86400, ge=60)
    pool_poll_repeat_count: int = Field(default=3650, ge=1)

    # Crawl-target safety (Phase 0, finding #1): block private/loopback/link-local/
    # metadata destinations and require HTTPS when WP credentials are used.
    # True relaxes both for crawling local test sites — development only.
    allow_unsafe_crawl_targets: bool = False

    @model_validator(mode="after")
    def require_api_key_outside_development(self) -> Self:
        if self.environment != "development" and not (self.api_key or self.operator_api_keys):
            raise ValueError("API_KEY or OPERATOR_API_KEYS must be set outside development")
        return self

    @model_validator(mode="after")
    def require_credential_key_outside_development(self) -> Self:
        key = self.credential_encryption_key
        if self.environment != "development" and (key is None or not key.get_secret_value()):
            raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be set outside development")
        return self

    @model_validator(mode="after")
    def forbid_unsafe_crawl_targets_outside_development(self) -> Self:
        if self.environment != "development" and self.allow_unsafe_crawl_targets:
            raise ValueError("ALLOW_UNSAFE_CRAWL_TARGETS is development-only")
        return self


settings = Settings()
