"""End-to-end proof of the immutable-publication-plan guarantee.

Drives the real HTTP routes and the real worker. Only the WordPress transport is
a mock, because the one site with credentials in this environment is a
third-party production site.
"""

from types import SimpleNamespace

import httpx
import json as jsonlib
import pytest

from app.connectors.wordpress import WordPressConnector
from app.models import Article, PublicationPlan, Suggestion
from app.services import publication_plan_service
from app.tasks import publication
from app.tasks.publication import publish_approved_plans

BEFORE = "<p>Costs of solar panel installations fell last year.</p>"


@pytest.fixture
def wp(db, site):
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/source",
        title="Source",
        content_text="Costs of solar panel installations fell last year.",
        external_id="10",
    )
    target = Article(
        site_id=site.id, url=f"{site.base_url}/target", title="Target", content_text="t"
    )
    db.add_all([source, target])
    db.commit()
    return source, target


def _mock_wordpress(monkeypatch, state):
    """One WordPress post whose stored content is `state["raw"]`. Logs every POST."""

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"content": {"raw": state["raw"]}})
        sent = jsonlib.loads(request.content)["content"]
        state["posts"].append(sent)
        state["raw"] = sent
        return httpx.Response(200, json={"content": {"raw": sent}})

    def get_connector(site):
        connector = WordPressConnector(
            SimpleNamespace(
                name=site.name,
                base_url=site.base_url,
                wp_username=None,
                wp_app_password=None,
            )
        )
        connector._api_candidates = []
        connector._api_base_url = f"{site.base_url}/wp-json/wp/v2/"
        connector.client = httpx.Client(
            base_url=site.base_url, transport=httpx.MockTransport(handler)
        )
        return connector

    monkeypatch.setattr(publication_plan_service, "get_connector", get_connector)
    monkeypatch.setattr(publication, "get_connector", get_connector)


def test_end_to_end_only_the_approved_bytes_are_ever_written(client, db, site, wp, monkeypatch):
    source, target = wp
    state = {"raw": BEFORE, "posts": []}
    _mock_wordpress(monkeypatch, state)

    # 1. Select a suggestion.
    suggestion = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="baseline_cosine",
        score=0.9,
        status="approved",
        anchor_text="solar panel",
    )
    db.add(suggestion)
    db.commit()

    # 2. Prepare a plan and record its hash.
    body = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()
    plan = body["plans"][0]
    first_hash = plan["plan_hash"]
    assert body["selected_suggestions"] == 1
    assert plan["original_html"] == BEFORE
    assert 'data-linkmesh="in-text"' in plan["updated_html"]
    assert state["posts"] == []  # preparation never writes

    # 3. Approve it as a named operator.
    approval = client.post(
        f"/api/v1/publish/{site.id}/plans/approve",
        json={"plans": [{"id": plan["id"], "plan_hash": first_hash}]},
    )
    assert approval.status_code == 200, approval.text
    operator = approval.json()["approved_by"]

    # 4. The article changes before publication: zero writes, plan goes stale.
    state["raw"] = "<p>An editor rewrote this paragraph entirely.</p>"
    result = publish_approved_plans(site.id)

    assert state["posts"] == []
    assert result["applied"] == 0
    db.expire_all()
    assert db.get(PublicationPlan, plan["id"]).status == "stale"
    revived = db.get(Suggestion, suggestion.id)
    assert (revived.status, revived.publication_plan_id) == ("approved", None)

    # 5. Prepare and approve a new plan against the article as it now is.
    second = client.post(f"/api/v1/publish/{site.id}/plans/prepare").json()["plans"][0]
    assert second["id"] != plan["id"]
    assert second["plan_hash"] != first_hash
    assert second["original_html"] == state["raw"]
    approved_html = second["updated_html"]
    assert (
        client.post(
            f"/api/v1/publish/{site.id}/plans/approve",
            json={"plans": [{"id": second["id"], "plan_hash": second["plan_hash"]}]},
        ).status_code
        == 200
    )

    # 6. Publish: the submitted HTML is byte-for-byte the approved HTML.
    published = publish_approved_plans(site.id)

    assert published["applied"] == 1
    assert state["posts"] == [approved_html]

    # 7. Retry: no second POST.
    retry = publish_approved_plans(site.id)

    assert state["posts"] == [approved_html]
    assert retry["applied"] == 0

    # 8. The row records who approved what, and when it landed.
    db.expire_all()
    stored = db.get(PublicationPlan, second["id"])
    assert stored.status == "applied"
    assert stored.approved_by == operator
    assert stored.approved_hash == second["plan_hash"]
    assert stored.applied_at is not None
    assert db.get(Suggestion, suggestion.id).status == "applied"
