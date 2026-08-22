"""The dashboard assistant: status, availability gating, and the tool loop."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
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


def test_count_question_uses_canonical_site_counts(monkeypatch, client, db, site):
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

    def model_must_not_be_used(messages, tools):
        raise AssertionError("count questions must be answered from engine data")

    monkeypatch.setattr(agent_service, "chat_with_tools", model_must_not_be_used)

    response = _chat(client, "how many articles and internal links does the site have?")

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "test-site has 2 active articles and 1 active internal link."
    assert body["tools_used"][0]["name"] == "list_sites"


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


class TestGroundedCountScope:
    """Which count questions the deterministic path may answer.

    It reads two whole-aggregate tools, so it can answer "how many articles do
    I have" exactly and cannot answer "how many above 90%" at all. Matching the
    second kind is the failure worth testing: the operator would get a
    confident reply to a different question with no model in the loop.
    """

    @pytest.mark.parametrize(
        "question",
        [
            "how many suggestions are above 90%?",
            "how many articles are orphans?",
            "how many pending suggestions on site 1?",
            "how many of those are worth approving?",
            "how many suggestions score higher than the rest?",
        ],
    )
    def test_qualified_questions_reach_the_model(self, question):
        assert agent_service._count_tool_for(question) is None

    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("how many sites do I have?", "list_sites"),
            ("how many articles are there?", "list_sites"),
            ("what is the total number of internal links?", "list_sites"),
            ("how many suggestions are in the queue?", "get_queue_counts"),
            ("how many pending suggestions are there?", "get_queue_counts"),
        ],
    )
    def test_plain_counts_stay_deterministic(self, question, expected):
        assert agent_service._count_tool_for(question) == expected

    def test_a_named_status_is_answered_with_that_status(self):
        counts = {"pending": 146, "approved": 1, "rejected": 0, "total": 147}
        # Falling back to the total here reported the whole queue as the
        # rejected count.
        assert "0 rejected suggestions" in agent_service._queue_count_reply(
            counts, "how many rejected suggestions are there?"
        )
        assert "146 pending suggestions" in agent_service._queue_count_reply(
            counts, "how many pending suggestions are there?"
        )
        assert "147 suggestions" in agent_service._queue_count_reply(
            counts, "how many suggestions are there?"
        )
