"""Placement context: the OpenRouter client, the verification rule, and the
lazy endpoint that ties them together.

No test reaches the network. The client is exercised through an httpx mock
transport, and everything above it is given a stub completion — what matters
here is what the engine does with an answer, not that a model produced one.
"""

import json

import httpx
import pytest

from app.config import settings
from app.ml.llm import openrouter
from app.ml.llm.openrouter import OpenRouterError, OpenRouterNotConfigured
from app.models import Article, Suggestion
from app.services import placement_service

SOURCE_TEXT = (
    "Cold brew is steeped for twelve hours or more.\n"
    "The long steep pulls fewer acids out of the grounds, which is why the result "
    "tastes rounder than iced filter coffee.\n"
    "Most cafes serve it diluted."
)

# Quoted the way a model quotes: re-wrapped onto one line.
REAL_PASSAGE = (
    "The long steep pulls fewer acids out of the grounds, which is why the result "
    "tastes rounder than iced filter coffee."
)


@pytest.fixture
def enable_openrouter(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")


@pytest.fixture
def suggestion(db, site):
    """A pending suggestion whose source article has real prose to place into."""
    source = Article(
        site_id=site.id,
        url=f"{site.base_url}/cold-brew",
        title="How cold brew works",
        content_text=SOURCE_TEXT,
    )
    target = Article(
        site_id=site.id,
        url=f"{site.base_url}/acidity",
        title="Acidity in coffee",
        content_text="Acidity is what makes a coffee taste bright.",
    )
    db.add_all([source, target])
    db.flush()
    row = Suggestion(
        site_id=site.id,
        source_article_id=source.id,
        target_article_id=target.id,
        method="baseline_cosine",
        score=0.8,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _stub_completion(monkeypatch, answer: dict | Exception, calls: list | None = None):
    """Replace the model call with a fixed answer, optionally counting calls."""

    def fake(*, system_prompt, user_prompt, client=None):
        if calls is not None:
            calls.append(user_prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(openrouter, "complete_json", fake)


# --- the client ------------------------------------------------------------


def _client_returning(payload, status: int = 200) -> httpx.Client:
    body = payload if isinstance(payload, dict) else {
        "choices": [{"message": {"content": payload}}]
    }
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(status, json=body)))


def _complete(client: httpx.Client) -> dict:
    return openrouter.complete_json(system_prompt="s", user_prompt="u", client=client)


def test_client_parses_a_plain_json_answer(enable_openrouter):
    with _client_returning('{"passage": "a", "anchor": "b"}') as client:
        assert _complete(client) == {"passage": "a", "anchor": "b"}


def test_client_accepts_a_fenced_answer(enable_openrouter):
    """Instruction-tuned models fence their JSON even when told not to."""
    with _client_returning('```json\n{"passage": "a", "anchor": null}\n```') as client:
        assert _complete(client) == {"passage": "a", "anchor": None}


def test_client_sends_the_configured_model(enable_openrouter, monkeypatch):
    monkeypatch.setattr(settings, "placement_model", "google/gemma-4-31b-it")
    seen = {}

    def record(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(record)) as client:
        _complete(client)

    assert seen["model"] == "google/gemma-4-31b-it"
    assert seen["auth"] == "Bearer test-key"
    # Extraction, not composition — a sampled answer paraphrases the article.
    assert seen["temperature"] == 0.0


def test_client_raises_without_a_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    with pytest.raises(OpenRouterNotConfigured):
        openrouter.complete_json(system_prompt="s", user_prompt="u")


def test_client_raises_on_an_error_status(enable_openrouter):
    with _client_returning({"error": {"message": "no credit"}}, status=402) as client:
        with pytest.raises(OpenRouterError, match="402"):
            _complete(client)


def test_client_raises_on_prose_instead_of_json(enable_openrouter):
    with _client_returning("Sure! Here is where the link goes.") as client:
        with pytest.raises(OpenRouterError, match="did not return JSON"):
            _complete(client)


def test_client_raises_on_a_json_array(enable_openrouter):
    with _client_returning('["a", "b"]') as client:
        with pytest.raises(OpenRouterError, match="not a JSON object"):
            _complete(client)


# --- verification ----------------------------------------------------------
#
# The rule the whole feature rests on: a passage and an anchor are accepted only
# if they are genuinely in the article. A model that paraphrases is treated
# exactly like one that refused.


def test_a_verbatim_quote_is_accepted(monkeypatch, enable_openrouter, suggestion):
    _stub_completion(monkeypatch, {"passage": REAL_PASSAGE, "anchor": "fewer acids"})
    placement = placement_service.generate(suggestion)

    assert placement.found
    assert placement.anchor_text == "fewer acids"
    assert placement.placement_context == REAL_PASSAGE
    assert placement.llm_model == settings.placement_model


def test_a_requoted_passage_survives_rewrapping(monkeypatch, enable_openrouter, suggestion):
    """Stored text keeps its crawled line breaks; a quote comes back re-wrapped.

    Whitespace differences must not read as a paraphrase, or nearly every real
    answer would be thrown away.
    """
    rewrapped = REAL_PASSAGE.replace("which is why", "which\n  is    why")
    _stub_completion(monkeypatch, {"passage": rewrapped, "anchor": "fewer acids"})

    assert placement_service.generate(suggestion).placement_context == REAL_PASSAGE


def test_a_paraphrased_passage_is_rejected(monkeypatch, enable_openrouter, suggestion):
    _stub_completion(
        monkeypatch,
        {"passage": "The lengthy steep extracts fewer acids.", "anchor": "fewer acids"},
    )
    placement = placement_service.generate(suggestion)

    assert not placement.found
    assert placement.placement_context is None
    assert placement.anchor_text is None


def test_an_anchor_from_elsewhere_is_rejected(monkeypatch, enable_openrouter, suggestion):
    """The anchor is real, but not inside the passage — it cannot be highlighted
    there, and later cannot be linked there either."""
    _stub_completion(monkeypatch, {"passage": REAL_PASSAGE, "anchor": "Most cafes"})

    assert not placement_service.generate(suggestion).found


def test_a_refusal_is_a_real_answer(monkeypatch, enable_openrouter, suggestion):
    _stub_completion(
        monkeypatch,
        {"passage": None, "anchor": None, "reason": "the target is unrelated"},
    )
    placement = placement_service.generate(suggestion)

    assert not placement.found
    # Still attributed: it is this model that declined.
    assert placement.llm_model == settings.placement_model


def test_a_whole_paragraph_anchor_is_rejected(monkeypatch, enable_openrouter, suggestion):
    """An anchor as long as its passage is not link text."""
    _stub_completion(monkeypatch, {"passage": REAL_PASSAGE, "anchor": REAL_PASSAGE})

    assert not placement_service.generate(suggestion).found


def test_the_model_only_sees_the_configured_slice(monkeypatch, enable_openrouter, suggestion):
    """Verification uses the same slice the model was shown, so it can never
    accept a passage from text it was not given."""
    monkeypatch.setattr(settings, "placement_max_source_chars", 60)
    calls: list[str] = []
    _stub_completion(monkeypatch, {"passage": REAL_PASSAGE, "anchor": "fewer acids"}, calls)

    placement = placement_service.generate(suggestion)

    assert SOURCE_TEXT[:60] in calls[0]
    assert "Most cafes serve it diluted" not in calls[0]
    assert not placement.found  # the passage is past the cut


def test_an_anchor_a_sibling_already_took_is_rejected(
    monkeypatch, enable_openrouter, suggestion
):
    """Two suggestions on one source cannot both link the same words.

    Publication gives the phrase to the first and the loser publishes as the
    appended block — having paid for a placement it can never use. The prompt
    says so, and this is the rule behind the hint, because a model that ignores
    an instruction must not be able to spend the slot anyway.
    """
    _stub_completion(monkeypatch, {"passage": REAL_PASSAGE, "anchor": "fewer acids"})

    assert not placement_service.generate(suggestion, taken_anchors=["Fewer Acids"]).found
    assert placement_service.generate(suggestion, taken_anchors=["long steep"]).found


def test_the_prompt_lists_the_anchors_already_taken(monkeypatch, enable_openrouter, suggestion):
    calls: list[str] = []
    _stub_completion(monkeypatch, {"passage": REAL_PASSAGE, "anchor": "long steep"}, calls)

    placement_service.generate(suggestion, taken_anchors=["fewer acids"])

    assert "ALREADY TAKEN" in calls[0]
    assert "- fewer acids" in calls[0]


# --- the endpoint ----------------------------------------------------------


def test_placement_is_generated_once_and_reused(
    monkeypatch, enable_openrouter, client, db, suggestion
):
    calls: list[str] = []
    _stub_completion(monkeypatch, {"passage": REAL_PASSAGE, "anchor": "fewer acids"}, calls)

    first = client.get(f"/api/v1/suggestions/{suggestion.id}/placement")
    assert first.status_code == 200
    body = first.json()
    assert body["found"] is True
    assert body["anchor_text"] == "fewer acids"
    assert body["placement_context"] == REAL_PASSAGE
    # The client highlights by searching the context, so this has to hold.
    assert body["anchor_text"] in body["placement_context"]

    second = client.get(f"/api/v1/suggestions/{suggestion.id}/placement")
    assert second.status_code == 200
    assert second.json()["placement_context"] == REAL_PASSAGE
    assert len(calls) == 1, "reopening the drawer must not pay for a second completion"

    db.expire_all()
    stored = db.get(Suggestion, suggestion.id)
    assert stored.anchor_text == "fewer acids"
    assert stored.placement_generated_at is not None
    assert stored.status == "pending", "generating a placement is not a review"


def test_nothing_fits_is_also_remembered(monkeypatch, enable_openrouter, client, db, suggestion):
    """Otherwise the most expensive rows — the ones with no answer — would be
    regenerated on every single open."""
    calls: list[str] = []
    _stub_completion(monkeypatch, {"passage": None, "anchor": None}, calls)

    first = client.get(f"/api/v1/suggestions/{suggestion.id}/placement")
    second = client.get(f"/api/v1/suggestions/{suggestion.id}/placement")

    assert first.json()["found"] is False
    assert second.json()["found"] is False
    assert second.json()["placement_context"] is None
    assert len(calls) == 1

    db.expire_all()
    assert db.get(Suggestion, suggestion.id).placement_generated_at is not None


def test_an_upstream_failure_is_not_recorded_as_a_verdict(
    monkeypatch, enable_openrouter, client, db, suggestion
):
    """A 502 has to stay retryable: writing the failure would turn a momentary
    outage into a permanent "no placement" for that row."""
    _stub_completion(monkeypatch, OpenRouterError("connection reset"))

    response = client.get(f"/api/v1/suggestions/{suggestion.id}/placement")

    assert response.status_code == 502
    db.expire_all()
    assert db.get(Suggestion, suggestion.id).placement_generated_at is None


def test_placement_is_unavailable_without_a_key(monkeypatch, client, suggestion):
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    response = client.get(f"/api/v1/suggestions/{suggestion.id}/placement")

    assert response.status_code == 503
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def test_placement_404s_for_an_unknown_suggestion(enable_openrouter, client):
    assert client.get("/api/v1/suggestions/99999999/placement").status_code == 404


def test_the_queue_does_not_carry_placement(monkeypatch, enable_openrouter, client, suggestion):
    """A queue page must not imply a model call per row — the drawer asks."""
    response = client.get("/api/v1/suggestions", params={"site_id": suggestion.site_id})

    assert response.status_code == 200
    assert "placement_context" not in response.json()["items"][0]
