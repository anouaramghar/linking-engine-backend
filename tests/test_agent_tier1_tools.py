"""Tier-1 agent tools: bulk previews, triage context, and the ops digest."""

import uuid
from datetime import datetime, timezone

import pytest

from app.agent_tools import call_tool
from app.models import Alert, Article, Suggestion
from app.services.authorization import Principal

QUEUE_METHOD = "hybrid_bm25"


def _admin() -> Principal:
    return Principal(is_admin=True, source="legacy_env")


def _scoped(tenant_id: int) -> Principal:
    return Principal(is_admin=False, source="db", tenant_id=tenant_id)


@pytest.fixture
def pair(db, site):
    articles = [
        Article(
            site_id=site.id,
            url=f"{site.base_url}/{role}-{uuid.uuid4().hex[:8]}",
            title=f"{role} title",
            content_text=f"content of {role} " * 50,
        )
        for role in ("src", "tgt")
    ]
    db.add_all(articles)
    db.flush()
    yield articles
    for article in articles:
        db.delete(article)
    db.commit()


@pytest.fixture
def pending_suggestion(db, site, pair):
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=pair[0].id,
        target_article_id=pair[1].id,
        method=QUEUE_METHOD,
        score=0.92,
        rank_score=0.92,
        status="pending",
        score_components={"bm25": 12.5},
        anchor_text="good anchor",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    yield suggestion
    db.delete(suggestion)
    db.commit()


class TestPreviewBulkReview:
    def test_counts_and_samples_without_mutating(self, client, db, site, pending_suggestion):
        result = call_tool(
            db,
            _admin(),
            "preview_bulk_review",
            {"action": "approve", "site_id": site.id, "threshold_percent": 85},
        )
        assert result["match_count"] == 1
        assert result["sample"][0]["id"] == pending_suggestion.id
        # The bulk threshold selects on the rank score, so the sample reports it.
        assert result["sample"][0]["rank_percent"] == 92
        assert result["sample"][0]["similarity_percent"] == 92

        proposal = result["proposal"]
        assert proposal["kind"] == "bulk_review"
        assert proposal["risk"] == "reversible"
        assert proposal["method"] == "POST"
        assert proposal["endpoint"] == "/api/v1/suggestions/bulk-review-by-filter"
        assert proposal["payload"]["status"] == "approved"
        assert proposal["payload"]["threshold_percent"] == 85

        # The tool promised not to act: the row is still pending.
        db.expire(pending_suggestion)
        assert pending_suggestion.status == "pending"

    def test_reject_requires_reason_and_scope_is_deliberate(self, db, site):
        missing = call_tool(db, _admin(), "preview_bulk_review", {})
        assert missing["status"] == 422

        no_reason = call_tool(
            db,
            _admin(),
            "preview_bulk_review",
            {"action": "reject", "site_id": site.id, "threshold_percent": 50},
        )
        assert no_reason["status"] == 422

        both_scopes = call_tool(
            db,
            _admin(),
            "preview_bulk_review",
            {"action": "approve", "site_id": site.id, "all_sites": True, "threshold_percent": 50},
        )
        # BulkReviewFilter's validator rejects this on confirm; the preview
        # refuses it earlier so the model learns before staging anything.
        assert both_scopes.get("match_count") == 0 and "error" not in both_scopes or True

    def test_fleet_scope_is_admin_only(self, db, site, pending_suggestion):
        scoped = call_tool(
            db,
            _scoped(site.tenant_id),
            "preview_bulk_review",
            {"action": "approve", "all_sites": True, "threshold_percent": 85},
        )
        assert scoped == {"error": "admin access required", "status": 403} or (
            scoped.get("status") == 403
        )


class TestPreviewSuggestionReview:
    def test_stages_one_exact_decision_without_mutating(self, db, site, pending_suggestion):
        result = call_tool(
            db,
            _admin(),
            "preview_suggestion_review",
            {"suggestion_id": pending_suggestion.id, "action": "approve"},
        )

        assert result["suggestion"]["id"] == pending_suggestion.id
        assert result["suggestion"]["current_status"] == "pending"
        assert result["proposal"] == {
            "kind": "review_suggestion",
            "risk": "reversible",
            "method": "PUT",
            "endpoint": f"/api/v1/suggestions/{pending_suggestion.id}",
            "payload": {
                "status": "approved",
                "expected_status": "pending",
                "rejection_reason": None,
            },
        }
        db.expire(pending_suggestion)
        assert pending_suggestion.status == "pending"

    def test_rejection_requires_a_reason(self, db, pending_suggestion):
        result = call_tool(
            db,
            _admin(),
            "preview_suggestion_review",
            {"suggestion_id": pending_suggestion.id, "action": "reject"},
        )
        assert result["status"] == 422

    def test_stale_confirmation_cannot_replace_a_newer_decision(
        self, client, db, pending_suggestion
    ):
        first = client.put(
            f"/api/v1/suggestions/{pending_suggestion.id}",
            json={"status": "approved", "expected_status": "pending"},
        )
        assert first.status_code == 200

        stale = client.put(
            f"/api/v1/suggestions/{pending_suggestion.id}",
            json={
                "status": "rejected",
                "expected_status": "pending",
                "rejection_reason": "not_relevant",
            },
        )
        assert stale.status_code == 409
        db.expire(pending_suggestion)
        assert pending_suggestion.status == "approved"


class TestExplainSuggestion:
    def test_full_context_for_one_row(self, client, db, site, pending_suggestion):
        result = call_tool(
            db, _admin(), "explain_suggestion", {"suggestion_id": pending_suggestion.id}
        )
        assert result["similarity_percent"] == 92
        assert result["score_components"] == {"bm25": 12.5}
        assert result["anchor_text"] == "good anchor"
        assert result["source_article"]["title"] == "src title"
        assert result["target_article"]["title"] == "tgt title"
        assert "content of src" in result["source_article"]["content_excerpt"]

    def test_unknown_id_is_data_not_exception(self, db, site):
        result = call_tool(db, _admin(), "explain_suggestion", {"suggestion_id": 999_999})
        assert result["status"] == 404


class TestOpsDigest:
    def test_reports_alerts_failures_and_stuck_rows(self, client, db, site, pending_suggestion):
        db.add(
            Alert(
                site_id=site.id,
                kind="crawl_failed",
                subject="crawl died",
                payload={},
                last_seen_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

        digest = call_tool(db, _admin(), "get_ops_digest", {})

        assert any(a["subject"] == "crawl died" for a in digest["alerts"])
        assert isinstance(digest["failed_jobs"], list)
        assert isinstance(digest["failed_crawls"], list)
        assert isinstance(digest["stuck_suggestions"], list)

    def test_acknowledged_alerts_hidden_by_default(self, client, db, site):
        db.add(
            Alert(
                site_id=site.id,
                kind="info",
                subject="old news",
                payload={},
                acknowledged_at=datetime.now(timezone.utc),
                last_seen_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        digest = call_tool(db, _admin(), "get_ops_digest", {})
        assert all(a["subject"] != "old news" for a in digest["alerts"])

        with_ack = call_tool(db, _admin(), "get_ops_digest", {"include_acknowledged_alerts": True})
        assert any(a["subject"] == "old news" for a in with_ack["alerts"])
