"""The dashboard assistant: status, availability gating, and the tool loop."""

import json
import time

import pytest

from fastapi.testclient import TestClient

from app.api.routes import agent as agent_route
from app.config import settings
from app.ml.llm.openrouter import OpenRouterError
from app.models import Article, InternalLink
from app.services import agent_service


def _chat(client: TestClient, message: str, history=None):
    return client.post("/api/v1/agent/chat", json={"message": message, "history": history or []})


def _stream(client: TestClient, message: str, history=None):
    return client.post(
        "/api/v1/agent/chat/stream", json={"message": message, "history": history or []}
    )


def _frames(response) -> list[tuple[str, dict]]:
    """The (event, data) pairs in an SSE body, in order.

    Comment frames are dropped: they are the connection talking, not the run.
    """
    events = []
    for frame in response.text.split("\n\n"):
        lines = [line for line in frame.splitlines() if line]
        name = next((line[len("event:") :].strip() for line in lines if line[:6] == "event:"), None)
        data = next((line[len("data:") :].strip() for line in lines if line[:5] == "data:"), None)
        if name is not None and data is not None:
            events.append((name, json.loads(data)))
    return events


def _scripted_stream(turns):
    """Stand in for the provider: play one scripted turn per call, in order.

    A turn is ``(fragments, message)`` — what the provider streams, and the
    assistant message the client assembles out of it.
    """
    remaining = list(turns)

    def stream(*, messages, tools):
        fragments, message = remaining.pop(0)
        yield from fragments
        return message

    return stream


@pytest.fixture
def unconfigured(monkeypatch):
    """No key on either account.

    Stated rather than assumed: the assistant reads its own settings first and
    falls back to placement's, so "no key" is now two settings, and a developer
    with a real `.env` would otherwise fail these on their machine only.
    """
    monkeypatch.setattr(settings, "agent_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")


def test_status_reports_unconfigured_without_a_key(client, unconfigured):
    response = client.get("/api/v1/agent/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["model"] == settings.agent_model


def test_chat_is_503_without_a_key(client, unconfigured):
    # Chat must say so rather than pretend to think. MCP tools are unaffected:
    # they answer from the database and need no model.
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


def test_a_staged_claim_is_repaired_until_a_structured_proposal_exists(
    monkeypatch, client, site
):
    """Natural-language staging claims cannot bypass the proposal contract."""
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    calls = []
    tool_names_by_call = []
    proposal = {
        "kind": "site_job_start",
        "risk": "sensitive",
        "method": "POST",
        "endpoint": f"/api/v1/suggestions/{site.id}",
        "payload": {"expected_active_job_run_ids": []},
        "impact": {
            "site_count": 1,
            "active_article_count": 2,
            "active_internal_link_count": 1,
            "active_suggestion_count": 0,
        },
    }

    def scripted_tool(_db, _principal, name, arguments):
        if name == "get_site_status":
            return {"site": {"id": site.id, "name": site.name}, "ready": True}
        assert name == "preview_site_job"
        assert arguments == {"site_id": site.id, "kind": "analysis"}
        return {
            "site": {"id": site.id, "name": site.name},
            "kind": "analysis",
            "ready": True,
            "scope": {
                "active_article_count": 2,
                "active_internal_link_count": 1,
                "active_suggestion_count": 0,
            },
            "proposal": proposal,
        }

    def scripted_model(messages, tools):
        calls.append(messages)
        tool_names_by_call.append([spec["function"]["name"] for spec in tools])
        if len(calls) == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "status-1",
                        "function": {
                            "name": "get_site_status",
                            "arguments": json.dumps({"site_id": site.id}),
                        },
                    }
                ],
            }
        if len(calls) == 2:
            return {
                "role": "assistant",
                "content": "The proposal is staged. Confirm in the dashboard to start analysis.",
                "tool_calls": [],
            }
        if len(calls) == 3:
            assert messages[-1]["role"] == "user"
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "preview-1",
                        "function": {
                            "name": "preview_site_job",
                            "arguments": json.dumps({"site_id": site.id, "kind": "analysis"}),
                        },
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": "Analysis is staged for your confirmation.",
            "tool_calls": [],
        }

    monkeypatch.setattr(agent_service, "call_tool", scripted_tool)
    monkeypatch.setattr(agent_service, "chat_with_tools", scripted_model)

    response = _chat(client, f"run analysis for site {site.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Analysis is staged for your confirmation."
    assert [trace["name"] for trace in body["tools_used"]] == [
        "get_site_status",
        "preview_site_job",
    ]
    assert body["proposals"] == [
        {"tool": "preview_site_job", **proposal, "match_count": None, "context": None}
    ]
    assert tool_names_by_call[2] == ["preview_site_job"]


def test_history_limit_counts_complete_user_assistant_turns(monkeypatch, client):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    captured = []

    def scripted(messages, tools):
        captured.append(messages)
        return {"content": "The anchor is LANTERN-42."}

    monkeypatch.setattr(agent_service, "chat_with_tools", scripted)
    history = [
        {"role": role, "content": f"turn-{turn}-{role}"}
        for turn in range(1, 12)
        for role in ("user", "assistant")
    ]

    response = _chat(client, "What is the anchor?", history)

    assert response.status_code == 200
    assert captured[0][1:-1] == history


def test_zero_history_turns_discard_all_history(monkeypatch, client):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_max_history_turns", 0)
    captured = []

    def scripted(messages, tools):
        captured.append(messages)
        return {"content": "A fresh answer."}

    monkeypatch.setattr(agent_service, "chat_with_tools", scripted)

    response = _chat(client, "Start fresh.", [{"role": "user", "content": "old context"}])

    assert response.status_code == 200
    assert captured[0][1:-1] == []


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


class TestStreamedChat:
    """The same run as `/chat`, readable while it happens.

    A turn is several model calls long, and the panel used to have nothing to
    show for any of it but a spinner. What is asserted here is the *order*:
    a tool appears when it returns, text appears as it is written, and the
    finished answer still arrives whole at the end.
    """

    @pytest.fixture(autouse=True)
    def configured(self, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "test-key")

    def test_it_is_refused_before_the_stream_opens_without_a_key(self, client, unconfigured):
        # Once a body has started the status line is already sent, so the one
        # refusal that can be known in advance is made in advance.
        response = _stream(client, "how many pending suggestions are there?")
        assert response.status_code == 503

    def test_a_tool_lands_as_it_runs_and_text_as_it_is_written(self, monkeypatch, client):
        monkeypatch.setattr(
            agent_service,
            "stream_chat_with_tools",
            _scripted_stream(
                [
                    (
                        [],
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {"name": "get_queue_counts", "arguments": "{}"},
                                }
                            ],
                        },
                    ),
                    (["The queue ", "is empty."], {"content": "The queue is empty."}),
                ]
            ),
        )

        events = _frames(_stream(client, "any pending suggestions?"))

        assert [name for name, _ in events] == ["tool", "delta", "delta", "done"]
        assert events[0][1]["name"] == "get_queue_counts"
        assert events[0][1]["outcome"]["total"] == 0
        assert [data["text"] for name, data in events if name == "delta"] == [
            "The queue ",
            "is empty.",
        ]

    def test_the_last_event_is_the_body_the_other_route_returns(self, monkeypatch, client):
        """What makes a dropped fragment survivable, and a silent provider too.

        Not every model streams its text — some send one final chunk — so the
        panel cannot be left assembling the answer itself.
        """
        monkeypatch.setattr(
            agent_service,
            "stream_chat_with_tools",
            _scripted_stream([(["Two sites."], {"content": "Two sites."})]),
        )

        events = _frames(_stream(client, "how many sites?"))

        name, done = events[-1]
        assert name == "done"
        assert done == {"reply": "Two sites.", "tools_used": [], "proposals": []}

    def test_a_provider_keepalive_reaches_the_dashboard_stream(self, monkeypatch, client):
        """Provider warm-up comments keep the browser from timing out mid-turn."""
        monkeypatch.setattr(
            agent_service,
            "stream_chat_with_tools",
            _scripted_stream([([None], {"content": "The queue is ready."})]),
        )

        response = _stream(client, "check the queue")

        assert ": keep-alive\n\n" in response.text
        assert _frames(response)[-1] == (
            "done",
            {"reply": "The queue is ready.", "tools_used": [], "proposals": []},
        )

    def test_the_engine_sends_keepalives_while_a_provider_turn_is_blocked(
        self, monkeypatch, client
    ):
        """A provider that sends no comments must not trip the browser idle timer."""
        monkeypatch.setattr(agent_route, "STREAM_HEARTBEAT_SECONDS", 0.01)

        def delayed(_db, _principal, _message, _history):
            time.sleep(0.03)
            yield agent_service.AssistantReply(reply="The queue is ready.")

        monkeypatch.setattr(agent_route, "stream_answer", delayed)

        response = _stream(client, "check the queue")

        assert ": keep-alive\n\n" in response.text
        assert _frames(response)[-1][0] == "done"

    def test_a_provider_failure_mid_stream_arrives_as_an_error_event(self, monkeypatch, client):
        """503 is gone by then: the first frame already committed a 200."""

        def fails(*, messages, tools):
            yield "The queue "
            raise OpenRouterError("OpenRouter returned 429: rate limit exceeded")

        monkeypatch.setattr(agent_service, "stream_chat_with_tools", fails)

        response = _stream(client, "how busy is the queue?")
        events = _frames(response)

        assert response.status_code == 200
        assert [name for name, _ in events] == ["delta", "error"]
        # The provider's own words stay in the log: they can carry key and
        # account detail.
        assert "429" not in events[-1][1]["detail"]

    def test_a_blank_streamed_reply_still_ends_with_the_honest_retry(self, monkeypatch, client):
        monkeypatch.setattr(
            agent_service,
            "stream_chat_with_tools",
            _scripted_stream([([], {"role": "assistant", "content": None, "tool_calls": []})]),
        )

        events = _frames(_stream(client, "say hello"))

        assert events == [
            (
                "done",
                {
                    "reply": "I couldn't produce a complete answer. Please try again.",
                    "tools_used": [],
                    "proposals": [],
                },
            )
        ]


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
            "page": {"has_more": True, "next_cursor": "0.93:40"},
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


class TestTraceSummary:
    """What the dashboard chip shows beside a reply.

    ``AgentPanel``'s ToolTrace builds its tooltip as `key: String(value)`, so a
    nested value renders as "[object Object]". Every entry here has to be a
    scalar to survive that.
    """

    def _flat(self, summary: dict) -> bool:
        return all(isinstance(v, (str, int, float, bool, type(None))) for v in summary.values())

    def test_a_paged_result_summarizes_to_scalars(self):
        summary = agent_service._summarize(
            {
                "match_count": 64,
                "page": {"has_more": True, "next_cursor": "0.9:1"},
                "suggestions": [{"id": 1}, {"id": 2}],
            }
        )
        assert self._flat(summary), summary
        assert summary["match_count"] == 64
        assert summary["has_more"] is True
        assert summary["suggestions_count"] == 2

    def test_site_counts_read_as_one_line(self):
        summary = agent_service._summarize(
            {
                "sites": [
                    {
                        "id": 1,
                        "name": "hipcollection",
                        "content": {
                            "active_article_count": 49,
                            "active_internal_link_count": 1,
                        },
                        "queue": {"active_suggestion_count": 147},
                        "suggestion_capacity": {"slots_available": 0},
                    }
                ]
            }
        )
        assert self._flat(summary), summary
        # Articles / links / suggestions, and no capacity: the exact string is
        # the assertion, because a fourth number here is the old ambiguity back.
        assert summary["site_counts"] == "hipcollection: 49/1/147"

    def test_a_blocked_preview_keeps_action_state_and_capacity(self):
        summary = agent_service._summarize(
            {
                "site": {"id": 1, "name": "hipcollection"},
                "kind": "analysis",
                "scope": {"active_suggestion_count": 171},
                "suggestion_capacity": {"slots_available": 0, "at_capacity": True},
                "ready": False,
                "blocked_reason": "the site's suggestion capacity is full",
            }
        )
        assert self._flat(summary), summary
        assert summary["site_id"] == 1
        assert summary["site_name"] == "hipcollection"
        assert summary["ready"] is False
        assert summary["suggestion_capacity_slots_available"] == 0
        assert summary["suggestion_capacity_at_capacity"] is True
        assert summary["blocked_reason"] == "the site's suggestion capacity is full"

    def test_a_failure_summarizes_to_its_message(self):
        summary = agent_service._summarize({"error": "nope", "status": 422})
        assert self._flat(summary)
        assert summary == {"error": "nope", "status": 422}
