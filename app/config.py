from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    # Connections the engine keeps, and how many more it may open under a burst.
    #
    # These are headroom, not a throughput fix. It is tempting to match the AnyIO
    # thread pool that serves every sync route (forty threads) against the
    # SQLAlchemy defaults (5 + 10) and call the difference a bottleneck. Measured
    # under twenty concurrent queue reads, one API process held about nine
    # connections and four processes held thirty-seven: the ceiling was never
    # what limited it. Python's GIL serializes the request work long before the
    # pool runs out, which is why WEB_CONCURRENCY, not this number, is the dial
    # that changes throughput.
    #
    # What the defaults do buy is a burst of genuinely IO-bound requests not
    # queueing on `pool_timeout`, which is invisible when it happens — it reads
    # as a slow database rather than an exhausted pool.
    #
    # Budget before raising either. Each uvicorn worker builds its own pool, so
    # the API's ceiling is WEB_CONCURRENCY x (pool_size + max_overflow), and the
    # fleet also has the RQ workers and the bot. All of it has to fit inside
    # PostgreSQL's `max_connections`, which is 100 by default.
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    # Seconds a request waits for a connection before failing. The default of 30
    # turns exhaustion into a stall long enough that the caller times out first.
    db_pool_timeout: int = Field(default=10, ge=1, le=120)
    # Recycle before a connection can go stale on the server side. With this set,
    # `pool_pre_ping` is a backstop for abrupt loss rather than the only defence.
    db_pool_recycle: int = Field(default=1800, ge=-1)
    redis_url: str = "redis://localhost:6379/0"

    environment: str = "development"
    # Commit this build was made from, set by the image build. Reported with the
    # evaluation numbers so a figure can be traced back to the code that computed
    # it. Empty means "this deployment does not record one", which is reported as
    # unknown rather than guessed.
    build_commit: str = ""

    # Static API key for all non-health endpoints; empty fails closed at the API boundary.
    # When set, this key is a legacy admin principal (all tenants) with a deprecation warning.
    api_key: str = ""
    # Human operator identities mapped to their individual API keys. These keys
    # may call every protected route as admin and provide trusted approval audit identity.
    operator_api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    # HMAC pepper for database API key hashes. A database leak alone must not let
    # an attacker verify candidate secrets offline. Set a long random value in
    # every non-dev environment before minting tenant keys.
    api_key_pepper: str = ""

    # Dashboard login via Telegram; see docs/design/dashboard-authentication.md.
    # The Login Widget is deliberately not used: it needs a publicly routable
    # registered domain, and this deployment sits behind an IP restriction. The
    # bot deep-link flow needs only outbound access to api.telegram.org.
    # An empty token disables dashboard login; the proxy then has no gate to
    # consult, so it must not be deployed with auth_request enabled.
    telegram_bot_token: SecretStr | None = None
    # Used to build the static t.me deep link the browser shows. No leading '@'.
    telegram_bot_username: str = ""
    # Pre-approved Telegram user ID allowed to approve everyone else. Without it
    # the first login has nobody to admit it and the dashboard is unreachable.
    dashboard_bootstrap_admin_id: int | None = None
    # Sliding: a request within the window extends it. 12 hours covers a working
    # day without forcing a re-login over lunch.
    dashboard_session_ttl_minutes: int = Field(default=720, gt=0, le=43_200)
    # Long enough to switch to Telegram and press Start, short enough that an
    # abandoned one-time code is not a standing invitation. The environment
    # name stays stable for deployment compatibility.
    dashboard_login_nonce_ttl_seconds: int = Field(default=300, gt=0, le=3_600)

    # Fernet key used to encrypt WordPress application passwords at rest.
    credential_encryption_key: SecretStr | None = None
    # Comma-separated previous Fernet keys accepted only for decryption during rotation.
    credential_decryption_keys: SecretStr | None = None

    # External search (v3). Search stays disabled while the API key is empty.
    # Basic depth and a small per-request cap keep paid discovery predictable.
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    tavily_timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    tavily_max_results_per_request: int = Field(default=5, ge=1, le=5)

    # Explainable citation-need baseline. It runs locally and records evidence;
    # it does not send article sentences to Tavily or block publication.
    citation_need_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    citation_need_max_article_chars: int = Field(default=30_000, ge=1_000, le=100_000)
    citation_need_max_sentences: int = Field(default=500, ge=1, le=5_000)
    citation_need_max_results: int = Field(default=10, ge=1, le=10)

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

    # Operator assistant (agent surfaces): the dashboard side panel and the
    # /mcp tool surface share one read-only action registry. Chat reuses the
    # OpenRouter account; an empty key disables only chat — MCP tools keep
    # working because they answer from the database directly. The default model
    # must support tool calling; placement's extraction model is not assumed to.
    # The assistant may run on a different provider from placement above. Both
    # speak OpenAI chat-completions, so repointing these is the whole switch —
    # NVIDIA NIM (https://integrate.api.nvidia.com/v1) is the one this was added
    # for. Empty means "use the placement account", which is what every
    # deployment did before these existed, so nothing changes by default.
    #
    # Separate settings rather than repointing the shared pair, because those
    # also drive placement context — a production feature that must not follow
    # the assistant onto a development provider.
    agent_base_url: str = ""
    agent_api_key: str = ""
    agent_model: str = "anthropic/claude-sonnet-4.5"
    agent_timeout_seconds: float = Field(default=90.0, gt=0.0, le=300.0)
    # One round is one model turn that may carry tool calls. The bound keeps a
    # confused model from circling through the queue for ever; four rounds is
    # comfortably more than any real question needs.
    agent_max_tool_rounds: int = Field(default=4, ge=1, le=8)
    agent_max_output_tokens: int = Field(default=1500, ge=64, le=8000)
    # Transcript bound: the panel sends recent turns, and this is where an
    # abusive client is stopped before tokens are spent.
    agent_max_history_turns: int = Field(default=20, ge=0, le=100)
    # Where the dashboard is served, so agent tools can hand back a link to the
    # view they are describing rather than a bare id. Only the engine knows its
    # own database; nothing tells it where the operator's browser should go, so
    # this is deployment configuration. Empty means links are simply omitted —
    # every tool still answers, and no deployment has to set it.
    dashboard_base_url: str = ""
    # MCP action links carry only signed, compressed preview inputs. The browser
    # must review them promptly, and the opaque receipt it issues is intentionally
    # even shorter-lived so copied credentials do not become standing authority.
    agent_action_envelope_ttl_seconds: int = Field(default=600, ge=60, le=3_600)
    agent_action_receipt_ttl_seconds: int = Field(default=300, ge=30, le=1_800)

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
            "login",
            "log in",
            "sign in",
            "sign up",
            "register",
            "registration",
            "dashboard",
            "my account",
            "cart",
            "shopping cart",
            "checkout",
            "privacy policy",
            "terms of service",
            "terms of use",
            "cookie policy",
            "support portal",
        ]
    )
    low_value_target_url_slugs: list[str] = Field(
        default=[
            "login",
            "log-in",
            "sign-in",
            "sign-up",
            "signup",
            "register",
            "registration",
            "dashboard",
            "my-account",
            "cart",
            "shopping-cart",
            "checkout",
            "privacy-policy",
            "terms-of-service",
            "terms-of-use",
            "cookie-policy",
            "support-portal",
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
    # Placements missing when publication plans are *prepared* are generated in
    # one pass, because the review queue is worked in bulk and a selected row
    # never had its drawer opened. This is the last moment a model may be asked:
    # a placement generated after approval cannot change the approved edit, so
    # the appended block an operator saw stays the block that is published. Each
    # call is a few seconds, so the pass is capped: past this many the rest are
    # prepared as the appended block. 0 disables it and restores lazy-only
    # generation.
    publish_max_placement_calls_per_run: int = Field(default=200, ge=0, le=5_000)
    # How many preparation placement calls run at once. The pass is latency-bound
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
    #: Stop a crawl after this many articles instead of taking the whole site.
    #: `None` -- the default -- means take everything, which is the only correct
    #: behavior for a managed site: a partial snapshot deactivates every article
    #: it did not see. Set it only to sample a corpus far too large to ingest,
    #: such as a public site used as an offline ranking benchmark.
    #: Which score decides the delivered order of a hybrid run.
    #:
    #: "fusion" orders by the weighted-RRF score of the dense and lexical pools.
    #: "bm25" reproduces the previous behavior, where the fusion score was
    #: computed, stored, and then ignored in favour of BM25-512 alone.
    #:
    #: Measured on two real editorial blogs (kinsta 1075 + 528 sources,
    #: speckyboy 417 + 580) against links their own editors wrote, "fusion" with
    #: an equal dense weight recovered 10-26% more known-good targets in the top
    #: five than "bm25" did, and beat either retriever used alone in all four
    #: splits. See docs/research/suggestion-quality-research-2026-08-19.md.
    hybrid_final_order: Literal["fusion", "bm25"] = "fusion"

    #: Weight of the dense pool in the fusion score; the lexical pool is 1.0.
    #: Was 0.25, which made the fusion lexical-heavy enough to be nearly a
    #: no-op. 1.0 was best or within half a percent of best on every split
    #: measured.
    hybrid_dense_rrf_weight: float = Field(default=1.0, ge=0.0, le=10.0)

    crawl_sample_articles: int | None = Field(default=None, ge=1, le=100_000)
    crawl_max_links_per_article: int = Field(default=1_000, ge=1, le=100_000)
    crawl_max_total_links: int = Field(default=100_000, ge=1, le=1_000_000)
    # HTML discovery falls back to a small same-origin frontier when a site has
    # no usable sitemap. The frontier is bounded independently of article and
    # response-size limits so navigation cannot turn into an unbounded crawl.
    crawl_bfs_fallback_enabled: bool = True
    crawl_max_depth: int = Field(default=2, ge=0, le=10)
    crawl_max_discovered_urls: int = Field(default=20_000, ge=1, le=100_000)

    # Analysis bounds are checked before embedding or corpus construction.
    analysis_max_articles_per_site: int = Field(default=10_000, ge=1, le=100_000)
    analysis_max_corpus_articles: int = Field(default=20_000, ge=1, le=200_000)

    # Deterministic graph intelligence. Shadow is the safe default: it records
    # the proposed graph-aware order beside the frozen BM25-512 order until a
    # representative evaluation set proves that promotion is worthwhile.
    graph_algorithm_version: str = Field(default="deterministic_v1", min_length=1, max_length=40)
    graph_underlinked_max_in_degree: int = Field(default=1, ge=0, le=1_000)
    graph_hub_min_out_degree: int = Field(default=10, ge=1, le=100_000)
    graph_saturation_min_in_degree: int = Field(default=10, ge=1, le=100_000)
    graph_reranking_mode: Literal["off", "shadow", "active"] = "shadow"
    # A structural opportunity may only move a candidate inside this bounded
    # relevance margin. It can never compensate for a large topical gap.
    graph_max_relevance_boost: float = Field(default=0.03, ge=0.0, le=0.10)
    graph_simulation_target_share_warning: float = Field(default=0.50, gt=0.0, le=1.0)

    # External content pool (RSS/Atom and Wikipedia).
    #: The default is unchanged at 50: every content pool behaves exactly as it
    #: did. The ceiling is raised only so an offline benchmark corpus can be
    #: built large enough to measure anything -- at 50 articles a Wikipedia
    #: corpus yields about 7 evaluable source articles, and the confidence
    #: interval on every metric swamps the difference between two rankers.
    #: Raising this for a live pool source is a separate decision: cost grows
    #: linearly, because MediaWiki returns one article extract per request.
    pool_max_articles_per_source: int = Field(default=50, ge=1, le=500)
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
    #: Inter-article links captured per pool article. These are the only
    #: human-authored link decisions LinkMesh can read without asking a
    #: reviewer, so they are what an offline ranking benchmark is measured
    #: against. Bounded well under `crawl_max_links_per_article`, which raises
    #: rather than truncates: a heavily linked encyclopedia page would
    #: otherwise fail the whole ingestion run. Set to 0 to capture none.
    pool_max_links_per_article: int = Field(default=500, ge=0, le=5_000)
    pool_allowed_domains: str = "wikipedia.org"
    pool_quarantine_failure_threshold: int = Field(default=3, ge=1, le=20)
    pool_poll_interval_seconds: int = Field(default=86400, ge=60)
    pool_poll_repeat_count: int = Field(default=3650, ge=1)

    # The coordinator is one repeating job for every managed site schedule.
    # Keep its cursor in PostgreSQL so a deployment does not need one Redis
    # repeat job per site.
    site_schedule_poll_interval_seconds: int = Field(default=60, ge=60)
    site_schedule_repeat_count: int = Field(default=5_256_000, ge=1)
    # External target checks are synchronous and run only on candidates that
    # survived static policy/ranking. This is one wall-clock budget shared by
    # DNS, connect, TLS, HEAD/GET fallback and every redirect hop.
    live_url_timeout_seconds: float = Field(default=8.0, gt=0.0, le=30.0)
    live_url_max_redirects: int = Field(default=5, ge=0, le=10)

    # One durable observation per managed site and day. Historical orphan-page
    # counts cannot be reconstructed after a crawl, so the evaluation dashboard
    # records them prospectively instead of inventing a pre-deployment trend.
    evaluation_snapshot_interval_seconds: int = Field(default=86400, ge=60)
    evaluation_snapshot_repeat_count: int = Field(default=3650, ge=1)

    # A ceiling on concurrent work per key scope. With one scope in use this is
    # simply a global cap on active jobs, which is the useful reading today; it
    # is not fair-share scheduling between competing customers.
    max_active_jobs_per_tenant: int = Field(default=100, ge=1, le=10_000)

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
    def require_secure_dashboard_url_outside_development(self) -> Self:
        base_url = self.dashboard_base_url.strip()
        if (
            self.environment != "development"
            and base_url
            and not base_url.lower().startswith("https://")
        ):
            raise ValueError("DASHBOARD_BASE_URL must use HTTPS outside development")
        return self

    @model_validator(mode="after")
    def require_credential_key_outside_development(self) -> Self:
        key = self.credential_encryption_key
        if self.environment != "development" and (key is None or not key.get_secret_value()):
            raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be set outside development")
        return self

    @model_validator(mode="after")
    def require_api_key_pepper_outside_development(self) -> Self:
        if self.environment != "development" and not self.api_key_pepper.strip():
            raise ValueError("API_KEY_PEPPER must be set outside development")
        return self

    @model_validator(mode="after")
    def forbid_unsafe_crawl_targets_outside_development(self) -> Self:
        if self.environment != "development" and self.allow_unsafe_crawl_targets:
            raise ValueError("ALLOW_UNSAFE_CRAWL_TARGETS is development-only")
        return self


settings = Settings()
