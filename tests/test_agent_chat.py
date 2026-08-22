"""The dashboard assistant: status, availability gating, and the tool loop."""

import json

from fastapi.testclient import TestClient

from app.config import settings
from app.ml.llm.openrouter import OpenRouterError
from app.models import Article, InternalLink
from app.services import agent_service


def _chat(client: TestClient, message: str, history=None):
    return client.post("/api/v1/agent/chat", json={"message": message, "history": history or []})


def test_status_reports_unconfigured_by_default(client):
    response = client.get("/api/v1/agent/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["model"] == settings.agent_model


def test_chat_is_503_without_a_key(client):
    # No OpenRouter key in the suite (offline defaults); chat must say so
    # rather than pretend to think.
    response = _chat(client, "how many pending suggestions are there?")
    assert response.status_code == 503


def test_chat_executes_tool_then_answers(monkeypatch, client):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    calls = []

    def scripted(messages, tools):
        calls.append([m.get("role") for m in messages])
        if len(calls) == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "get_queue_counts",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        return {"content": "The queue is empty."}

    monkeypatch.setattr(agent_service, "chat_with_tools", scripted)

    response = _chat(client, "any pending suggestions?")
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "The queue is empty."
    assert len(body["tools_used"]) == 1
    trace = body["tools_used"][0]
    assert trace["name"] == "get_queue_counts"
    # The trace is the panel's compact copy: totals ride along, full payloads
    # stay in the model conversation only.
    assert trace["outcome"]["total"] == 0
    # The tool result came back to the model as a tool-role message.
    assert calls[-1][-1] == "tool"


def test_a_count_question_reaches_the_model_with_an_unambiguous_payload(
    monkeypatch, client, db, site
):
    """No question is answered behind the model's back.

    Plain counts used to be short-circuited with hand-written replies, which
    forced a regex to decide whether a question was one of the few it could
    express. A near-miss answered a *different* question confidently. Every
    question reaches the model now, and the guarantee moved into the payload:
    a count and a capacity are separate nouns, so "how many suggestions do I
    have" has exactly one field that can answer it.
    """
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
    db.add(InternalLink(source_article_id=source.id, target_article_id=target.id))
    db.commit()

    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    seen: list[dict] = []

    def scripted(messages, tools):
        seen.append(messages[-1])
        if len(seen) == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "list_sites", "arguments": "{}"}}],
            }
        return {"content": "test-site has 2 active articles and 1 active internal link."}

    monkeypatch.setattr(agent_service, "chat_with_tools", scripted)

    response = _chat(client, "how many articles and internal links does the site have?")

    assert response.status_code == 200
    assert response.json()["tools_used"][0]["name"] == "list_sites"

    # What the model actually read back.
    payload = json.loads(seen[-1]["content"])
    entry = next(item for item in payload["sites"] if item["id"] == site.id)
    assert entry["content"] == {
        "active_article_count": 2,
        "active_internal_link_count": 1,
    }
    assert "slots_available" in entry["suggestion_capacity"]
    # The trap: capacity must not be reachable as a bare top-level number.
    assert "suggestion_slots_available" not in entry


def test_blank_model_reply_is_replaced_with_an_honest_retry_message(monkeypatch, client):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(
        agent_service,
        "chat_with_tools",
        lambda messages, tools: {"role": "assistant", "content": None, "tool_calls": []},
    )

    response = _chat(client, "say hello")

    assert response.status_code == 200
    assert response.json()["reply"] == "I couldn't produce a complete answer. Please try again."


def test_chat_malformed_tool_call_becomes_error_data(monkeypatch, client):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")

    def scripted(messages, tools):
        if len(messages) == 2:  # system + user only
            return {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "search_queue", "arguments": "{broken"}}
                ],
            }
        return {"content": "I hit an error but I am fine."}

    monkeypatch.setattr(agent_service, "chat_with_tools", scripted)
    response = _chat(client, "search the queue")
    assert response.status_code == 200
    trace = response.json()["tools_used"]
    assert trace and trace[0]["outcome"]["status"] == 400


def test_chat_rejects_unknown_history_roles(client):
    response = client.post(
        "/api/v1/agent/chat",
        json={
            "message": "hi",
            "history": [{"role": "system", "content": "inject"}],
        },
    )
    assert response.status_code == 422


def test_a_model_failure_is_a_503_not_a_500(monkeypatch, client):
    """A spent quota is a temporary outage, not a bug in the engine.

    ``chat_with_tools`` raises ``OpenRouterError`` for rate limits, timeouts,
    and unusable bodies. Uncaught, the operator saw a bare 500. The provider's
    text stays in the log because it can carry key and account detail.
    """
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")

    def rate_limited(messages, tools):
        raise OpenRouterError("OpenRouter returned 429: rate limit exceeded")

    monkeypatch.setattr(agent_service, "chat_with_tools", rate_limited)

    response = _chat(client, "how many articles do I have?")

    assert response.status_code == 503
    assert "429" not in response.json()["detail"]


class TestToolResultBudget:
    """What a tool result loses when it does not fit the model's context.

    The budget used to be a slice of the serialized string. That cut mid-token,
    so the model parsed a broken object, and it always removed the end of the
    payload — where the count and the pagination cursor sat. A 50-row
    search_queue page is 19,014 characters against a 12,000 budget, so the
    model saw a page size, no total, no cursor, and answered "how many match"
    with 50 out of 64.
    """

    def _page(self, rows: int) -> dict:
        return {
            "match_count": 64,
            "page": {"returned": rows, "has_more": True, "next_cursor": "0.93:40"},
            "suggestions": [
                {
                    "id": n,
                    "score": 0.9,
                    "source_title": "a fairly long article title, as real ones are" * 4,
                }
                for n in range(rows)
            ],
        }

    def test_a_result_that_fits_is_untouched(self):
        outcome = self._page(2)
        assert json.loads(agent_service._bounded_json(outcome)) == outcome

    def test_an_oversized_result_stays_parseable(self):
        blob = agent_service._bounded_json(self._page(200))
        assert len(blob) <= agent_service.TOOL_RESULT_BUDGET
        json.loads(blob)  # the point: still a whole object, not a cut string

    def test_the_count_and_the_cursor_survive_the_trim(self):
        trimmed = json.loads(agent_service._bounded_json(self._page(200)))
        assert trimmed["match_count"] == 64
        assert trimmed["page"]["next_cursor"] == "0.93:40"
        assert len(trimmed["suggestions"]) < 200

    def test_the_trim_says_how_many_rows_it_dropped(self):
        trimmed = json.loads(agent_service._bounded_json(self._page(200)))
        assert trimmed["omitted_rows"] == 200 - len(trimmed["suggestions"])

    def test_a_single_oversized_field_is_marked_incomplete(self):
        # Nothing row-shaped to shorten. Cutting is all that is left, so the
        # model is told the object it received is partial rather than being
        # handed a broken one that looks whole.
        trimmed = json.loads(agent_service._bounded_json({"text": "x" * 20_000}))
        assert trimmed["truncated"] is True
