"""The shared read-only action registry behind both agent surfaces."""

import uuid

import pytest

from app.agent_tools import REGISTRY, call_tool
from app.models import Article, InternalLink, Suggestion
from app.services.authorization import Principal


def _admin() -> Principal:
    return Principal(is_admin=True, source="legacy_env")


def _scoped(tenant_id: int) -> Principal:
    return Principal(is_admin=False, source="db", tenant_id=tenant_id)


def test_registry_is_read_only_by_construction():
    """No registry tool name may hint at a mutating action."""
    forbidden = ("approve", "reject", "publish", "crawl", "delete", "create", "update", "trigger")
    assert REGISTRY, "the registry must never ship empty"
    for name in REGISTRY:
        assert not any(word in name for word in forbidden), name


def test_list_sites_returns_fixture(client, db, site):
    result = call_tool(db, _admin(), "list_sites", {})
    names = [entry["name"] for entry in result["sites"]]
    assert site.name in names
    entry = next(e for e in result["sites"] if e["name"] == site.name)
    assert entry["content"]["active_article_count"] == 0
    assert entry["platform"] == "wordpress"


def test_list_sites_exposes_the_dashboard_count_semantics(db, site):
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="source",
        content_text="source",
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/target",
        title="target",
        content_text="target",
    )
    db.add_all([source, target])
    db.flush()
    db.add(
        InternalLink(
            source_article_id=source.id,
            target_article_id=target.id,
        )
    )
    db.add(
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="baseline_cosine",
            score=0.8,
            status="pending",
        )
    )
    db.commit()

    result = call_tool(db, _admin(), "list_sites", {})
    entry = next(item for item in result["sites"] if item["id"] == site.id)

    assert entry["content"]["active_article_count"] == 2
    assert entry["content"]["active_internal_link_count"] == 1
    assert entry["queue"]["active_suggestion_count"] == 1
    # A count and a capacity never sit at the same level: "how many
    # suggestions" has exactly one field that can answer it.
    assert "active_suggestion_count" not in entry
    assert "slots_available" not in entry


def test_a_full_site_keeps_its_count_apart_from_its_capacity(db, site, monkeypatch):
    """The payload a site at capacity publishes must have one readable count.

    A full site reports 1 active suggestion and 0 slots left. Flat, those are
    two bare numbers at the same level and either answers "how many
    suggestions do I have" — the wrong one silently, because nobody
    re-derives a count. Grouping is what makes the question have one answer.
    """
    from app.config import settings

    source = Article(site_id=site.id, url=f"{site.base_url}/a", title="a", content_text="a")
    target = Article(site_id=site.id, url=f"{site.base_url}/b", title="b", content_text="b")
    db.add_all([source, target])
    db.flush()
    db.add(
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="baseline_cosine",
            score=0.8,
            status="pending",
        )
    )
    db.commit()
    # Shrink the ceiling to the one suggestion that already exists.
    monkeypatch.setattr(settings, "hybrid_max_active_suggestions_per_site", 1)

    entry = next(
        item for item in call_tool(db, _admin(), "list_sites", {})["sites"] if item["id"] == site.id
    )

    assert entry["queue"]["active_suggestion_count"] == 1
    assert entry["suggestion_capacity"] == {"slots_available": 0, "at_capacity": True}


def test_unknown_tool_and_bad_args_are_data_not_exceptions(db, site):
    missing = call_tool(db, _admin(), "approve_everything", {})
    assert missing == {"error": "unknown tool 'approve_everything'", "status": 404}

    bad = call_tool(db, _admin(), "search_queue", {"limit": 10_000})
    assert bad["status"] == 422
    assert "error" in bad


def test_evaluation_metrics_requires_admin(db, site):
    scoped = call_tool(db, _scoped(site.tenant_id), "get_evaluation_metrics", {})
    assert scoped == {"error": "admin access required for this tool", "status": 403}

    admin = call_tool(db, _admin(), "get_evaluation_metrics", {})
    assert "editorial" in json_keys(admin) or isinstance(admin, dict)
    assert "error" not in admin


def json_keys(payload):
    return payload.keys()


def test_search_queue_reports_empty_total(db, site):
    result = call_tool(db, _admin(), "search_queue", {"site_id": site.id})
    assert result["total"] == 0
    assert result["suggestions"] == []


def test_find_articles_scopes_to_site(db, client, site):
    result = call_tool(db, _admin(), "find_articles", {"site_id": site.id})
    assert result == {"site_id": site.id, "returned": 0, "articles": []}

    foreign = call_tool(
        db, _scoped((site.tenant_id or 0) + 999), "find_articles", {"site_id": site.id}
    )
    assert foreign["status"] in (403, 404)


def test_openai_specs_match_registry():
    from app.agent_tools import openai_tool_specs

    specs = openai_tool_specs()
    assert {spec["function"]["name"] for spec in specs} == set(REGISTRY)
    for spec in specs:
        schema = spec["function"]["parameters"]
        assert "title" not in schema


class TestSearchQueueFilters:
    """The queue tool reaches past its first page and filters by score band.

    ``list_suggestion_page`` has always supported both; the tool used to pin
    every one of those parameters to ``None``, so an agent could see only the
    top rows of a queue and could not size a threshold before proposing a bulk
    rule.
    """

    @pytest.fixture
    def graded(self, db, site):
        """Four pending rows at distinct, descending scores."""
        articles = [
            Article(
                site_id=site.id,
                url=f"{site.base_url}/graded-{index}-{uuid.uuid4().hex[:8]}",
                title=f"graded article {index}",
                content_text="body " * 40,
            )
            for index in range(2)
        ]
        db.add_all(articles)
        db.flush()
        rows = [
            Suggestion(
                site_id=site.id,
                source_article_id=articles[0].id,
                target_article_id=articles[1].id,
                method="hybrid_bm25",
                score=score,
                status="pending",
            )
            for score in (0.95, 0.80, 0.65, 0.50)
        ]
        db.add_all(rows)
        db.commit()
        yield rows
        for row in rows:
            db.delete(row)
        for article in articles:
            db.delete(article)
        db.commit()

    def test_percent_band_narrows_the_match(self, db, site, graded):
        band = call_tool(
            db,
            _admin(),
            "search_queue",
            {"site_id": site.id, "min_percent": 60, "max_percent": 90},
        )
        # min is inclusive and max exclusive, matching the bulk-rule boundary.
        assert [row["similarity_percent"] for row in band["suggestions"]] == [80, 65]

    def test_cursor_walks_the_whole_queue(self, db, site, graded):
        first = call_tool(db, _admin(), "search_queue", {"site_id": site.id, "limit": 2})
        assert first["total"] == 4, "the first page counts the full match once"
        assert first["returned"] == 2

        second = call_tool(
            db,
            _admin(),
            "search_queue",
            {"site_id": site.id, "limit": 2, "cursor": first["next_cursor"]},
        )
        # A continuation rides the look-ahead row instead of paying for COUNT(*).
        assert "total" not in second
        assert "next_cursor" not in second, "the last page must not invite another call"

        walked = [row["id"] for row in first["suggestions"] + second["suggestions"]]
        assert walked == [row.id for row in graded], "every row, in score order, exactly once"

    def test_malformed_cursor_is_a_422_not_a_crash(self, db, site):
        result = call_tool(db, _admin(), "search_queue", {"cursor": "not-a-cursor"})
        assert result["status"] == 422
        assert "cursor" in result["error"]

    def test_bounds_the_route_declares_with_query_are_enforced(self, db, site):
        # Called directly, the route's Query(...) bounds never run — the args
        # model is the only thing standing between a model and an unbounded scan.
        assert call_tool(db, _admin(), "search_queue", {"min_percent": 500})["status"] == 422
        assert call_tool(db, _admin(), "search_queue", {"limit": 5_000})["status"] == 422


def test_error_of_reads_failures_without_claiming_data():
    from app.agent_tools import error_of

    assert error_of({"error": "nope", "status": 404}) == "nope (status 404)"
    assert error_of({"total": 0, "suggestions": []}) is None
    # A failed job's own `error` string is data, one level down, and must not
    # be mistaken for the tool having failed.
    assert error_of({"failed_jobs": [{"id": 1, "error": "boom"}]}) is None


class TestEvaluationDates:
    """The date forms a model actually sends, and the errors it can act on."""

    def test_a_bare_date_is_read_as_utc(self, db, site):
        from app.agent_tools import _parse_when

        # The route refuses a naive datetime, so this is the difference between
        # "2026-08-01" working and coming back as a 422 about timezones.
        assert _parse_when("date_from", "2026-08-01").isoformat() == "2026-08-01T00:00:00+00:00"

    def test_an_explicit_offset_is_left_alone(self):
        from app.agent_tools import _parse_when

        parsed = _parse_when("date_from", "2026-08-01T12:00:00+02:00")
        assert parsed.utcoffset().total_seconds() == 7_200

    def test_unparseable_input_is_a_422_naming_the_field(self, db, site):
        result = call_tool(db, _admin(), "get_evaluation_metrics", {"date_from": "last week"})
        # Previously a bare ValueError reached call_tool's catch-all and became
        # a 500 — which reads to a model as "retry", not "fix the argument".
        assert result["status"] == 422
        assert "date_from" in result["error"]


class TestSiteStatus:
    """The capacity ceiling, shown as arithmetic rather than as a bare 0."""

    @pytest.fixture
    def stocked(self, db, site):
        """Two articles and one pending suggestion between them."""
        articles = [
            Article(
                site_id=site.id,
                url=f"{site.base_url}/status-{index}-{uuid.uuid4().hex[:8]}",
                title=f"status article {index}",
                content_text="body " * 20,
            )
            for index in range(2)
        ]
        db.add_all(articles)
        db.flush()
        suggestion = Suggestion(
            site_id=site.id,
            source_article_id=articles[0].id,
            target_article_id=articles[1].id,
            method="hybrid_bm25",
            score=0.9,
            status="pending",
        )
        db.add(suggestion)
        db.commit()
        yield articles
        db.delete(suggestion)
        for article in articles:
            db.delete(article)
        db.commit()

    def test_capacity_breakdown_explains_the_slot_count(self, db, site, stocked):
        from app.config import settings

        result = call_tool(db, _admin(), "get_site_status", {"site_id": site.id})
        capacity = result["suggestion_capacity"]
        per_article = capacity["limits"]["per_article"]

        # 2 active articles x the per-article cap, which is well under the
        # per-site ceiling, so the per-article limit is the binding one.
        assert per_article["capacity"] == 2 * settings.hybrid_max_suggestions_per_article
        assert capacity["binding_limit"] == "per_article"
        # The published number stays the route's, so the tool and the Sites
        # page cannot disagree; the breakdown only has to explain it.
        assert capacity["slots_available"] == per_article["capacity"] - 1
        assert capacity["at_capacity"] is False

    def test_a_full_site_reports_what_would_free_a_slot(self, db, site, stocked, monkeypatch):
        from app.config import settings

        # Shrink the ceiling to the one suggestion that already exists.
        monkeypatch.setattr(settings, "hybrid_max_active_suggestions_per_site", 1)

        result = call_tool(db, _admin(), "get_site_status", {"site_id": site.id})
        capacity = result["suggestion_capacity"]

        assert capacity["slots_available"] == 0
        assert capacity["at_capacity"] is True
        assert capacity["binding_limit"] == "per_site"
        # An approved row still occupies capacity, so the two ways out are
        # rejecting a pending row or publishing an approved one.
        assert capacity["slots_freed_by"]["rejecting_all_pending"] == 1
        assert "approved" in capacity["statuses_counting_toward_capacity"]

    def test_queue_counts_ride_along(self, db, site, stocked):
        result = call_tool(db, _admin(), "get_site_status", {"site_id": site.id})
        assert result["queue"]["pending"] == 1
        assert result["content"]["active_article_count"] == 2

    def test_scoped_to_the_callers_tenant(self, db, site):
        foreign = call_tool(
            db, _scoped((site.tenant_id or 0) + 999), "get_site_status", {"site_id": site.id}
        )
        assert foreign["status"] in (403, 404)


class TestDashboardLinks:
    """Results carry a link to the view they describe, when one can be built."""

    def test_absent_without_a_configured_base(self, db, site, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "dashboard_base_url", "")
        result = call_tool(db, _admin(), "search_queue", {"site_id": site.id})
        # Not null — absent. A null link reads to a model as "there is no link
        # for this", which it then says out loud.
        assert "dashboard_url" not in result

    @pytest.mark.parametrize("base", ["not-a-url", "example.com/dash", " "])
    def test_a_non_absolute_base_is_treated_as_unset(self, db, site, monkeypatch, base):
        from app.config import settings

        monkeypatch.setattr(settings, "dashboard_base_url", base)
        result = call_tool(db, _admin(), "search_queue", {"site_id": site.id})
        assert "dashboard_url" not in result

    def test_queue_link_carries_the_filters_that_produced_it(self, db, site, monkeypatch):
        from urllib.parse import parse_qs, urlparse

        from app.config import settings

        monkeypatch.setattr(settings, "dashboard_base_url", "https://dash.example.com/")
        result = call_tool(
            db,
            _admin(),
            "search_queue",
            {"site_id": site.id, "status": "pending", "min_percent": 90, "q": "privacy"},
        )
        parsed = urlparse(result["dashboard_url"])
        # A trailing slash on the base must not become a double slash.
        assert parsed.scheme == "https"
        assert parsed.path == "/queue"
        # Names are the frontend's (useQueueFilters), not this module's.
        assert parse_qs(parsed.query) == {
            "site": [str(site.id)],
            "status": ["pending"],
            "q": ["privacy"],
            "min": ["90"],
        }

    def test_bulk_preview_links_to_the_rule_the_operator_must_confirm(self, db, site, monkeypatch):
        from urllib.parse import parse_qs, urlparse

        from app.config import settings

        monkeypatch.setattr(settings, "dashboard_base_url", "https://dash.example.com")
        result = call_tool(
            db,
            _admin(),
            "preview_bulk_review",
            {"action": "approve", "site_id": site.id, "threshold_percent": 90},
        )
        query = parse_qs(urlparse(result["review_url"]).query)
        # `threshold` is the queue's bulk-rule boundary — the same number the
        # proposal carries, so the link opens on exactly that rule.
        assert query["threshold"] == ["90"]
        assert query["status"] == ["pending"]
        assert result["proposal"]["payload"]["threshold_percent"] == 90


class TestPublicationStatus:
    """What is blocking publication, and what is ready to go."""

    @pytest.fixture
    def article(self, db, site):
        row = Article(
            site_id=site.id,
            url=f"{site.base_url}/pub-{uuid.uuid4().hex[:8]}",
            title="publication source",
            content_text="body " * 20,
        )
        db.add(row)
        db.commit()
        yield row
        db.delete(row)
        db.commit()

    def _plan(self, db, site, article, status):
        from app.models import PublicationPlan

        plan = PublicationPlan(
            site_id=site.id,
            source_article_id=article.id,
            source_url=article.url,
            status=status,
            original_html="<p>before</p>",
            updated_html="<p>after</p>",
            items=[],
            plan_hash=uuid.uuid4().hex,
        )
        db.add(plan)
        db.commit()
        return plan

    def test_nothing_waiting_is_an_answer_not_a_404(self, db, site):
        # pending_publication_site 404s here; this question has a real answer.
        result = call_tool(db, _admin(), "get_publication_status", {"site_id": site.id})
        assert "error" not in result
        assert result["next_action"] == "nothing_waiting"

    def test_a_prepared_plan_outranks_preparing_more(self, db, site, article):
        plan = self._plan(db, site, article, "prepared")
        try:
            result = call_tool(db, _admin(), "get_publication_status", {"site_id": site.id})
            # publication_status counts only approved plans, so without this the
            # site is told to "prepare" work it has already prepared.
            assert result["prepared_plans"] == 1
            assert result["approved_plans"] == 0
            assert result["next_action"] == "approve_plan"
        finally:
            db.delete(plan)
            db.commit()

    def test_an_approved_plan_is_ready_to_queue(self, db, site, article):
        plan = self._plan(db, site, article, "approved")
        try:
            result = call_tool(db, _admin(), "get_publication_status", {"site_id": site.id})
            assert result["approved_plans"] == 1
            assert result["next_action"] == "publish"
        finally:
            db.delete(plan)
            db.commit()

    def test_missing_credentials_block_before_anything_else(self, db, site, article):
        plan = self._plan(db, site, article, "approved")
        original = site.wp_username
        site.wp_username = None
        db.commit()
        try:
            result = call_tool(db, _admin(), "get_publication_status", {"site_id": site.id})
            # Ranked above "publish": preparation would fail on every article,
            # so saying "ready to queue" would send the operator into a 401.
            assert result["can_publish"] is False
            assert result["next_action"] == "blocked"
        finally:
            site.wp_username = original
            db.delete(plan)
            db.commit()

    def test_fleet_view_scopes_to_the_callers_tenant(self, db, site):
        result = call_tool(db, _scoped((site.tenant_id or 0) + 999), "get_publication_status", {})
        assert result["totals"]["sites_with_work"] == 0
        assert result["sites"] == []

    def test_single_site_scoping_is_enforced(self, db, site):
        foreign = call_tool(
            db,
            _scoped((site.tenant_id or 0) + 999),
            "get_publication_status",
            {"site_id": site.id},
        )
        assert foreign["status"] in (403, 404)


@pytest.fixture
def pending_history(db, site):
    """One suggestion and the event its lifecycle trigger records.

    `trg_suggestion_lifecycle_event` writes the `generated` row on insert, so
    the fixture must not write one too — the history is the database's, not the
    test's.
    """
    articles = [
        Article(
            site_id=site.id,
            url=f"{site.base_url}/hist-{index}-{uuid.uuid4().hex[:8]}",
            title=f"history article {index}",
            content_text="body " * 20,
        )
        for index in range(2)
    ]
    db.add_all(articles)
    db.flush()
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=articles[0].id,
        target_article_id=articles[1].id,
        method="hybrid_bm25",
        score=0.91,
        status="pending",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    yield suggestion
    db.delete(suggestion)
    for article in articles:
        db.delete(article)
    db.commit()


@pytest.fixture
def crawl_run(db, site):
    """A finished crawl: two URLs kept, one refused by robots.txt."""
    from app.models import IngestionDiagnostic, IngestionRun

    run = IngestionRun(
        site_id=site.id,
        status="succeeded",
        discovered_urls=3,
        accepted_urls=2,
        skipped_urls=1,
        articles_upserted=2,
        links_found=4,
        diagnostic_summary={"accepted": 2, "blocked_by_robots": 1},
    )
    db.add(run)
    db.flush()
    rows = [
        IngestionDiagnostic(
            site_id=site.id,
            ingestion_run_id=run.id,
            url=f"{site.base_url}/crawl-{index}",
            state=state,
            reason_code=reason,
        )
        for index, (state, reason) in enumerate(
            [("accepted", "accepted"), ("accepted", "accepted"), ("skipped", "blocked_by_robots")]
        )
    ]
    db.add_all(rows)
    db.commit()
    db.refresh(run)
    yield run
    for row in rows:
        db.delete(row)
    db.delete(run)
    db.commit()


@pytest.fixture
def finished_jobs(db, site):
    """Two jobs that have both ended — nothing an active-jobs view would show."""
    from datetime import UTC, datetime

    from app.models import JobRun

    now = datetime.now(UTC)
    runs = [
        JobRun(
            site_id=site.id,
            kind="ingestion",
            status="failed",
            enqueued_at=now,
            started_at=now,
            finished_at=now,
            error="robots.txt refused every discovered URL",
        ),
        JobRun(
            site_id=site.id,
            kind="analysis",
            status="succeeded",
            enqueued_at=now,
            started_at=now,
            finished_at=now,
        ),
    ]
    db.add_all(runs)
    db.commit()
    yield runs
    for run in runs:
        db.delete(run)
    db.commit()


class TestSuggestionHistory:
    """How a suggestion got to its current state, asked either way round."""

    def test_a_numeric_id_resolves_to_its_trace(self, db, site, pending_history):
        result = call_tool(
            db, _admin(), "get_suggestion_history", {"suggestion_id": pending_history.id}
        )
        # The route filters on trace_id; a model normally holds only the number
        # it was shown, so resolving here saves chaining two tools.
        assert result["trace_id"] == pending_history.trace_id
        assert [row["event_type"] for row in result["events"]] == ["generated"]

    def test_the_trace_id_works_directly_too(self, db, site, pending_history):
        result = call_tool(
            db, _admin(), "get_suggestion_history", {"trace_id": pending_history.trace_id}
        )
        assert result["total"] == 1

    def test_unknown_id_is_data_not_an_exception(self, db, site):
        result = call_tool(db, _admin(), "get_suggestion_history", {"suggestion_id": 10_000_000})
        assert result["status"] == 404

    def test_scoped_to_the_callers_tenant(self, db, site, pending_history):
        foreign = call_tool(
            db,
            _scoped((site.tenant_id or 0) + 999),
            "get_suggestion_history",
            {"suggestion_id": pending_history.id},
        )
        assert foreign["status"] in (403, 404)


class TestIngestionDiagnostics:
    """Why a crawl kept what it kept."""

    def test_defaults_to_the_latest_run_and_leads_with_reasons(self, db, site, crawl_run):
        result = call_tool(db, _admin(), "get_ingestion_diagnostics", {"site_id": site.id})
        assert result["run"]["id"] == crawl_run.id
        # The histogram the crawl already computed answers "why only N pages";
        # a list of URLs does not.
        assert result["reasons"] == {"accepted": 2, "blocked_by_robots": 1}
        assert result["counts"]["discovered_urls"] == 3

    def test_examples_can_be_narrowed_to_one_reason(self, db, site, crawl_run):
        result = call_tool(
            db,
            _admin(),
            "get_ingestion_diagnostics",
            {"site_id": site.id, "reason_code": "blocked_by_robots"},
        )
        assert [row["reason_code"] for row in result["examples"]] == ["blocked_by_robots"]

    def test_a_run_from_another_site_is_not_readable(self, db, site, crawl_run):
        result = call_tool(
            db, _admin(), "get_ingestion_diagnostics", {"site_id": site.id, "run_id": 10_000_000}
        )
        assert result["status"] == 404


class TestSiteJobs:
    """Finished jobs, which the active-jobs tool cannot show."""

    def test_reports_a_failed_job_that_is_no_longer_running(self, db, site, finished_jobs):
        active = call_tool(db, _admin(), "list_active_jobs", {})
        assert all(row["site_id"] != site.id for row in active["active_jobs"])

        result = call_tool(db, _admin(), "get_site_jobs", {"site_id": site.id})
        statuses = {row["kind"]: row["status"] for row in result["jobs"]}
        assert statuses == {"ingestion": "failed", "analysis": "succeeded"}
        failure = next(row for row in result["jobs"] if row["status"] == "failed")
        assert "robots" in failure["error"]

    def test_kind_narrows_the_history(self, db, site, finished_jobs):
        result = call_tool(db, _admin(), "get_site_jobs", {"site_id": site.id, "kind": "analysis"})
        assert [row["kind"] for row in result["jobs"]] == ["analysis"]
