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


def test_filter_vocabularies_match_the_database():
    """The permitted filter values must equal the columns they filter on.

    The literals in agent_tools are what a model reads out of the JSON Schema
    and what pydantic enforces. They are written by hand because Literal
    members must be literal, so nothing but this test stops them drifting from
    the enums. Drift in either direction is a silent wrong answer: a value
    dropped here becomes "no such filter", and a value added here becomes a
    filter that matches nothing and reports zero.
    """
    from typing import get_args

    from app.agent_tools import JobKind, QueueStatus, SuggestionMethodName
    from app.models.suggestion import SuggestionMethod, SuggestionStatus
    from app.services.job_service import _QUEUES

    assert set(get_args(QueueStatus)) == set(SuggestionStatus.enums)
    assert set(get_args(SuggestionMethodName)) == set(SuggestionMethod.enums)
    assert set(get_args(JobKind)) == set(_QUEUES)


def test_an_unknown_filter_value_is_refused_not_answered_with_zero(db, site):
    """A status the queue does not have must fail, not report an empty queue.

    Unvalidated, this returned {"returned": 0, "total": 0} with no error, which
    reads to a model — and then to an operator — as "there are none of these".
    """
    result = call_tool(db, _admin(), "search_queue", {"status": "nope"})
    assert result["status"] == 422

    for arguments in (
        {"site_id": site.id, "method": "cosine"},
        {"site_id": site.id, "kind": "crawl"},
    ):
        tool = "search_queue" if "method" in arguments else "get_site_jobs"
        assert call_tool(db, _admin(), tool, arguments)["status"] == 422


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
    assert result["match_count"] == 0
    assert result["suggestions"] == []
    # The page size lives one level down, so it cannot be read as the count.
    assert result["page"] == {"returned": 0, "has_more": False}
    assert "returned" not in result


def test_find_articles_scopes_to_site(db, client, site):
    result = call_tool(db, _admin(), "find_articles", {"site_id": site.id})
    assert result == {
        "site_id": site.id,
        "match_count": 0,
        "page": {"returned": 0, "has_more": False},
        "articles": [],
    }

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
        assert first["match_count"] == 4, "the first page counts the full match once"
        assert first["page"]["returned"] == 2
        assert first["page"]["has_more"] is True

        second = call_tool(
            db,
            _admin(),
            "search_queue",
            {"site_id": site.id, "limit": 2, "cursor": first["page"]["next_cursor"]},
        )
        # A continuation rides the look-ahead row instead of paying for COUNT(*).
        assert "match_count" not in second
        assert second["page"]["has_more"] is False
        assert "next_cursor" not in second["page"], "the last page must not invite another call"

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
        assert result["match_count"] == 1
        assert result["page"] == {"returned": 1, "offset": 0, "has_more": False}

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


class TestArgumentErrors:
    """What a rejected call tells the model it got wrong.

    A bare problem count gave a model nothing to act on, so it retried the same
    call until the round cap ended the turn.
    """

    def test_the_rejected_field_is_named(self, db, site):
        result = call_tool(db, _admin(), "search_queue", {"status": "nope"})
        assert result["status"] == 422
        assert "status" in result["error"]

    def test_a_rejected_literal_lists_what_it_accepts(self, db, site):
        error = call_tool(db, _admin(), "search_queue", {"status": "nope"})["error"]
        # Pydantic's own message for a Literal is the permitted set, which is
        # exactly what a model needs to fix the call on its next round.
        assert "pending" in error and "approved" in error

    def test_several_problems_are_all_named(self, db, site):
        error = call_tool(
            db, _admin(), "search_queue", {"status": "nope", "min_percent": 150, "limit": 999}
        )["error"]
        for field in ("status", "min_percent", "limit"):
            assert field in error

    def test_an_internal_failure_reveals_nothing_about_the_query(self, db, site, monkeypatch):
        """A SQLAlchemy error stringifies to its statement and parameters.

        Over /mcp that reaches any external client holding a key, so the caller
        gets a fixed sentence and the detail goes to the log instead.
        """
        from app import agent_tools

        def boom(**kwargs):
            raise RuntimeError("SELECT secret_col FROM suggestions WHERE id = 1")

        monkeypatch.setattr(agent_tools, "list_sites", boom)
        result = call_tool(db, _admin(), "list_sites", {})

        assert result["status"] == 500
        assert result["error"] == "tool 'list_sites' failed unexpectedly"
        assert "SELECT" not in result["error"]


class TestCapsAreVisible:
    """A capped list must never be mistakable for a complete one."""

    @pytest.fixture
    def many_articles(self, db, site):
        from app.models import Article

        rows = [
            Article(
                site_id=site.id,
                url=f"{site.base_url}/a{n}",
                title=f"article {n}",
                content_text="x",
            )
            for n in range(30)
        ]
        db.add_all(rows)
        db.commit()
        yield rows
        for row in rows:
            db.delete(row)
        db.commit()

    def test_find_articles_reports_the_whole_match_not_the_page(self, db, site, many_articles):
        result = call_tool(db, _admin(), "find_articles", {"site_id": site.id, "limit": 5})
        assert result["match_count"] == 30, "the count is the answer to 'how many'"
        assert result["page"] == {"returned": 5, "has_more": True}

    def test_the_count_respects_the_same_filters(self, db, site, many_articles):
        result = call_tool(
            db, _admin(), "find_articles", {"site_id": site.id, "q": "article 1", "limit": 2}
        )
        # "article 1", "article 10".."article 19" — eleven, of which two shown.
        assert result["match_count"] == 11
        assert result["page"]["returned"] == 2

    def test_ops_digest_counts_what_it_only_samples(self, db, site):
        from app.models import Alert

        alerts = [
            Alert(
                site_id=site.id,
                kind="job_failed",
                subject=f"failure {n}",
                payload={"n": n},
                occurrences=1,
            )
            for n in range(20)
        ]
        db.add_all(alerts)
        db.commit()
        try:
            result = call_tool(db, _admin(), "get_ops_digest", {})
            # The list is capped at 15; the count must still be honest.
            assert result["counts"]["alerts"] >= 20
            assert len(result["alerts"]) == 15
        finally:
            for alert in alerts:
                db.delete(alert)
            db.commit()


class TestPublicationJobWindow:
    """Publication history must not vanish behind newer jobs of other kinds."""

    def test_a_publication_job_survives_fifty_newer_crawls(self, db, site):
        from datetime import UTC, datetime, timedelta

        from app.models import JobRun

        base = datetime.now(UTC) - timedelta(days=1)
        published = JobRun(
            site_id=site.id, kind="publication", status="succeeded", enqueued_at=base
        )
        # Sixty ingestion runs, every one of them newer.
        noise = [
            JobRun(
                site_id=site.id,
                kind="ingestion",
                status="succeeded",
                enqueued_at=base + timedelta(minutes=n + 1),
            )
            for n in range(60)
        ]
        db.add_all([published, *noise])
        db.commit()
        try:
            result = call_tool(db, _admin(), "get_publication_status", {"site_id": site.id})
            # Fetching 50 rows of any kind and filtering in Python returned an
            # empty list here, which reads as "publication has never run".
            kinds = [row["kind"] for row in result["recent_publication_jobs"]]
            assert kinds == ["publication"]
        finally:
            for row in [published, *noise]:
                db.delete(row)
            db.commit()


class TestIngestionExampleWindow:
    """A reason filter must search the run, not the first page of it."""

    @pytest.fixture
    def crowded_run(self, db, site):
        from app.models import IngestionDiagnostic, IngestionRun

        run = IngestionRun(
            site_id=site.id,
            status="succeeded",
            discovered_urls=210,
            accepted_urls=0,
            skipped_urls=210,
            diagnostic_summary={"blocked_by_robots": 209, "too_short": 1},
        )
        db.add(run)
        db.flush()
        rows = [
            IngestionDiagnostic(
                site_id=site.id,
                ingestion_run_id=run.id,
                url=f"{site.base_url}/p{n}",
                state="skipped",
                reason_code="blocked_by_robots",
            )
            for n in range(209)
        ]
        # The rare one is last by id, so it sits outside the first 200 rows.
        rows.append(
            IngestionDiagnostic(
                site_id=site.id,
                ingestion_run_id=run.id,
                url=f"{site.base_url}/rare",
                state="skipped",
                reason_code="too_short",
            )
        )
        db.add_all(rows)
        db.commit()
        yield run
        for row in rows:
            db.delete(row)
        db.delete(run)
        db.commit()

    def test_a_reason_beyond_the_first_page_is_still_found(self, db, site, crowded_run):
        result = call_tool(
            db,
            _admin(),
            "get_ingestion_diagnostics",
            {"site_id": site.id, "run_id": crowded_run.id, "reason_code": "too_short"},
        )
        # Filtering the route's first 200 rows in Python reported none of these
        # while the histogram beside it counted one.
        assert result["match_count"] == 1
        assert [row["url"] for row in result["examples"]] == [f"{site.base_url}/rare"]

    def test_the_count_is_the_run_not_the_sample(self, db, site, crowded_run):
        result = call_tool(
            db,
            _admin(),
            "get_ingestion_diagnostics",
            {"site_id": site.id, "run_id": crowded_run.id, "examples": 5},
        )
        assert result["match_count"] == 210
        assert len(result["examples"]) == 5


class TestBulkPreviewRules:
    """The rule's own constraints, published in the schema rather than found by failing."""

    def test_a_scope_must_be_chosen(self, db, site):
        result = call_tool(
            db, _admin(), "preview_bulk_review", {"action": "approve", "threshold_percent": 90}
        )
        assert result["status"] == 422
        assert "all_sites" in result["error"]

    def test_the_two_scopes_cannot_both_be_given(self, db, site):
        result = call_tool(
            db,
            _admin(),
            "preview_bulk_review",
            {
                "action": "approve",
                "threshold_percent": 90,
                "site_id": site.id,
                "all_sites": True,
            },
        )
        # BulkReviewFilter rejects this on submission, so previewing it would
        # promise the operator a rule they could not confirm.
        assert result["status"] == 422

    def test_a_rejection_needs_its_reason(self, db, site):
        result = call_tool(
            db,
            _admin(),
            "preview_bulk_review",
            {"action": "reject", "threshold_percent": 60, "site_id": site.id},
        )
        assert result["status"] == 422
        assert "rejection_reason" in result["error"]

    def test_the_rules_are_visible_in_the_schema(self):
        from app.agent_tools import json_schema

        schema = json_schema(REGISTRY["preview_bulk_review"].args_model)
        # The point of moving them: a model reads them before calling, not after.
        assert "all_sites" in schema["properties"]
        assert "rejection_reason" in schema["properties"]


class TestUncoveredTools:
    """The two tools the audit found with no behavioural test at all."""

    def test_graph_summary_reports_the_sites_structure(self, db, site):
        source = Article(site_id=site.id, url=f"{site.base_url}/s", title="s", content_text="s")
        target = Article(site_id=site.id, url=f"{site.base_url}/t", title="t", content_text="t")
        db.add_all([source, target])
        db.flush()
        db.add(InternalLink(source_article_id=source.id, target_article_id=target.id))
        db.commit()

        result = call_tool(db, _admin(), "get_graph_summary", {"site_id": site.id})
        assert result["article_count"] == 2
        # One link in, so exactly one article has no incoming link.
        assert result["orphan_count"] == 1

    def test_graph_summary_is_scoped_to_the_callers_tenant(self, db, site):
        result = call_tool(
            db, _scoped((site.tenant_id or 0) + 999), "get_graph_summary", {"site_id": site.id}
        )
        assert result["status"] == 403

    def test_ops_digest_reports_a_failed_job(self, db, site):
        from app.models import JobRun

        run = JobRun(site_id=site.id, kind="ingestion", status="failed", error="robots.txt")
        db.add(run)
        db.commit()
        try:
            result = call_tool(db, _admin(), "get_ops_digest", {})
            assert any(row["id"] == run.id for row in result["failed_jobs"])
            assert result["counts"]["failed_jobs"] >= 1
        finally:
            db.delete(run)
            db.commit()

    def test_ops_digest_is_scoped_to_the_callers_tenant(self, db, site):
        from app.models import JobRun

        run = JobRun(site_id=site.id, kind="ingestion", status="failed", error="robots.txt")
        db.add(run)
        db.commit()
        try:
            foreign = call_tool(db, _scoped((site.tenant_id or 0) + 999), "get_ops_digest", {})
            assert foreign["failed_jobs"] == []
            assert foreign["counts"]["failed_jobs"] == 0
        finally:
            db.delete(run)
            db.commit()


class TestDeclaredOutputSchemas:
    """The contract MCP clients read before they call.

    fastmcp publishes `outputSchema` but does not enforce it, so a declared
    shape that drifts from the handler is a promise nothing checks — the exact
    failure this surface has spent its time removing. These tests are the
    enforcement.
    """

    def test_declared_output_schemas_describe_the_real_payload(
        self, db, site, pending_history, crawl_run, finished_jobs
    ):
        from app.agent_tools import output_schema_violation

        calls = {
            "search_queue": {"site_id": site.id},
            "find_articles": {"site_id": site.id},
            "get_site_jobs": {"site_id": site.id},
            "get_suggestion_history": {"trace_id": pending_history.trace_id},
            "get_ingestion_diagnostics": {"site_id": site.id, "run_id": crawl_run.id},
        }
        for name, arguments in calls.items():
            tool = REGISTRY[name]
            assert tool.output_model is not None, f"{name} lost its output model"
            result = call_tool(db, _admin(), name, arguments)
            assert "error" not in result, (name, result)
            assert output_schema_violation(tool, result) is None, name

    def test_a_continuation_page_still_conforms(self, db, site):
        """`match_count` is absent when continuing, so the model must allow it."""
        from app.agent_tools import output_schema_violation

        source = Article(site_id=site.id, url=f"{site.base_url}/c-s", title="s", content_text="s")
        targets = [
            Article(site_id=site.id, url=f"{site.base_url}/c-t{n}", title=f"t{n}", content_text="t")
            for n in range(2)
        ]
        db.add_all([source, *targets])
        db.flush()
        rows = [
            Suggestion(
                site_id=site.id,
                source_article_id=source.id,
                target_article_id=target.id,
                method="hybrid_bm25",
                score=0.9 - index / 100,
                status="pending",
            )
            for index, target in enumerate(targets)
        ]
        db.add_all(rows)
        db.commit()
        try:
            first = call_tool(db, _admin(), "search_queue", {"site_id": site.id, "limit": 1})
            assert first["match_count"] == 2
            cursor = first["page"]["next_cursor"]

            second = call_tool(
                db, _admin(), "search_queue", {"site_id": site.id, "limit": 1, "cursor": cursor}
            )
            # The count is issued once. The shape has to survive its absence.
            assert "match_count" not in second
            assert output_schema_violation(REGISTRY["search_queue"], second) is None
        finally:
            for row in rows:
                db.delete(row)
            for row in [source, *targets]:
                db.delete(row)
            db.commit()

    def test_drift_is_reported_rather_than_assumed_away(self):
        from app.agent_tools import output_schema_violation

        drifted = {"site_id": 1, "page": {"returned": 0, "has_more": False}, "articles": []}
        violation = output_schema_violation(REGISTRY["find_articles"], drifted)
        assert violation is not None
        assert "match_count" in violation

    def test_a_failure_is_not_measured_against_the_success_shape(self):
        from app.agent_tools import output_schema_violation

        failure = {"error": "site 9 not found", "status": 404}
        assert output_schema_violation(REGISTRY["find_articles"], failure) is None

    def test_the_schema_says_which_number_answers_how_many(self):
        from app.agent_tools import json_schema

        schema = json_schema(REGISTRY["search_queue"].output_model)
        # The whole reason for declaring a shape: the distinction that was
        # getting lost is written down, not inferred from key names.
        assert "how many" in schema["properties"]["match_count"]["description"]
        page = schema["$defs"]["Page"]["properties"]
        assert "Never the answer to 'how many'" in page["returned"]["description"]

    def test_only_the_ambiguous_tools_declare_one(self):
        """A contract is a maintenance cost, so it is spent where it buys something."""
        declared = {name for name, tool in REGISTRY.items() if tool.output_model is not None}
        assert declared == {
            "search_queue",
            "find_articles",
            "get_site_jobs",
            "get_suggestion_history",
            "get_ingestion_diagnostics",
        }
        # These answer with unambiguous scalars; a model would add nothing.
        assert REGISTRY["get_queue_counts"].output_model is None
        assert REGISTRY["list_active_jobs"].output_model is None
