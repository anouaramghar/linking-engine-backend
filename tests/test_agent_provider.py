"""Which provider the assistant talks to, and what must not follow it there.

The assistant and the placement-context feature both speak OpenAI
chat-completions, and both used to read one pair of settings. That made
"run the assistant on a free development endpoint" impossible to express
without moving placement — a production feature — onto it as well.
"""

import contextlib
import logging

import pytest

from app.config import settings
from app.ml.llm import agent as agent_client

NVIDIA = "https://integrate.api.nvidia.com/v1"


@pytest.fixture
def shared_account(monkeypatch):
    """What every deployment had before the assistant could be separated."""
    monkeypatch.setattr(settings, "openrouter_api_key", "placement-key")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(settings, "agent_base_url", "")
    monkeypatch.setattr(settings, "agent_api_key", "")


class TestProviderResolution:
    def test_defaults_to_the_placement_account(self, shared_account):
        assert agent_client.provider() == ("https://openrouter.ai/api/v1", "placement-key")
        assert agent_client.provider_host() == "openrouter.ai"

    def test_the_assistant_can_have_its_own(self, shared_account, monkeypatch):
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA)
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")

        assert agent_client.provider() == (NVIDIA, "nvapi-test")
        assert agent_client.provider_host() == "integrate.api.nvidia.com"

    def test_placement_does_not_follow_it(self, shared_account, monkeypatch):
        """The reason these are separate settings at all.

        Placement reads the source article on every preview. Moving it onto a
        development endpoint alongside the assistant would take a production
        feature somewhere its provider's terms do not allow.
        """
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA)
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")

        assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"
        assert settings.openrouter_api_key == "placement-key"

    def test_a_trailing_slash_does_not_double_up(self, shared_account, monkeypatch):
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA + "/")
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")
        assert agent_client.provider()[0] == NVIDIA

    def test_whitespace_is_not_a_configured_provider(self, shared_account, monkeypatch):
        monkeypatch.setattr(settings, "agent_base_url", "   ")
        monkeypatch.setattr(settings, "agent_api_key", "  ")
        assert agent_client.provider() == ("https://openrouter.ai/api/v1", "placement-key")


class TestAvailability:
    def test_an_assistant_only_key_still_enables_chat(self, monkeypatch):
        """Asking placement's account whether *chat* is on was the bug.

        A deployment that configured only the assistant reported chat as
        unavailable, because `is_configured` answered for the other account.
        """
        monkeypatch.setattr(settings, "openrouter_api_key", "")
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA)

        assert agent_client.is_configured() is True

    def test_no_key_anywhere_is_off(self, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "")
        monkeypatch.setattr(settings, "agent_api_key", "")
        assert agent_client.is_configured() is False

    def test_status_names_the_host_being_called(self, client, monkeypatch):
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA)
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")

        body = client.get("/api/v1/agent/status").json()
        assert body["configured"] is True
        # An operator debugging "why is chat down" needs the endpoint, not a
        # brand name the engine would have to keep a mapping for.
        assert body["provider"] == "integrate.api.nvidia.com"


class TestTheCallItself:
    @pytest.fixture
    def sent(self, monkeypatch):
        captured: dict = {}

        class Reply:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        def fake(http, method, url, *, max_bytes, json, headers):
            captured.update(url=url, headers=headers, payload=json)
            return Reply()

        monkeypatch.setattr(agent_client, "request_limited_http_response", fake)
        return captured

    def test_it_goes_to_the_assistants_endpoint_with_its_key(self, sent, monkeypatch):
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA)
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")
        monkeypatch.setattr(settings, "agent_model", "nvidia/llama-3.3-nemotron-super-49b-v1.5")

        message = agent_client.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=[]
        )

        assert message["content"] == "ok"
        assert sent["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
        assert sent["headers"]["Authorization"] == "Bearer nvapi-test"
        assert sent["payload"]["model"] == "nvidia/llama-3.3-nemotron-super-49b-v1.5"

    def test_it_falls_back_to_the_shared_account(self, sent, shared_account):
        agent_client.chat_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
        assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
        assert sent["headers"]["Authorization"] == "Bearer placement-key"

    def test_a_failure_names_the_host_that_failed(self, monkeypatch):
        """The message used to say "OpenRouter" whatever had actually answered."""
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA)
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")

        class Rejected:
            status_code = 429
            text = "rate limited"

        monkeypatch.setattr(
            agent_client,
            "request_limited_http_response",
            lambda *a, **k: Rejected(),
        )
        with pytest.raises(agent_client.OpenRouterError) as failure:
            agent_client.chat_with_tools(messages=[], tools=[])
        assert "integrate.api.nvidia.com" in str(failure.value)


class TestDevelopmentOnlyProviders:
    """A development convenience must not become the production path quietly."""

    def test_a_restricted_provider_is_announced_at_startup(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "agent_base_url", NVIDIA)
        monkeypatch.setattr(settings, "agent_api_key", "nvapi-test")

        with caplog.at_level(logging.WARNING):
            agent_client.log_provider_notice()

        assert "development only" in caplog.text
        # The reason, not just the verdict: NVIDIA's own definition of
        # production is what makes this matter for a dashboard with operators.
        assert "real end-users" in caplog.text

    def test_an_unrestricted_provider_says_nothing(self, shared_account, caplog):
        with caplog.at_level(logging.WARNING):
            agent_client.log_provider_notice()
        assert caplog.text == ""


class TestTheStreamedTurn:
    """The same turn, read frame by frame.

    A streamed turn is only useful if it ends where the blocking one does: the
    panel renders the fragments, but the loop still needs one assistant message
    with its tool calls assembled, and a provider sends those in pieces.
    """

    @pytest.fixture(autouse=True)
    def a_provider(self, shared_account):
        return shared_account

    @pytest.fixture
    def transport(self, monkeypatch):
        """Answer one streamed request from a scripted list of SSE lines."""
        captured: dict = {}

        def scripted(lines, *, status_code=200, body=b""):
            class Response:
                def __init__(self):
                    self.status_code = status_code

                @staticmethod
                def read():
                    return body

                @staticmethod
                def iter_lines():
                    yield from lines

            class Client:
                def __init__(self, **kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *exception):
                    return False

                def stream(self, method, url, **kwargs):
                    captured.update(method=method, url=url, **kwargs)
                    return contextlib.nullcontext(Response())

            monkeypatch.setattr(agent_client.httpx, "Client", Client)
            return captured

        return scripted

    @staticmethod
    def _turn(**kwargs):
        """Run a turn to the end: every fragment, then the finished message."""
        fragments = []
        stream = agent_client.stream_chat_with_tools(**kwargs)
        try:
            while True:
                fragments.append(next(stream))
        except StopIteration as finished:
            return fragments, finished.value

    def test_text_arrives_in_pieces_and_ends_as_one_message(self, transport):
        sent = transport(
            [
                ": OPENROUTER PROCESSING",
                'data: {"choices":[{"delta":{"content":"The queue "}}]}',
                "",
                'data: {"choices":[{"delta":{"content":"is empty."}}]}',
                "data: [DONE]",
            ]
        )

        fragments, message = self._turn(messages=[{"role": "user", "content": "hi"}], tools=[])

        # The keep-alive comment and the terminator are the provider's own
        # punctuation, not words the operator was meant to read.
        assert fragments == ["The queue ", "is empty."]
        assert message == {"role": "assistant", "content": "The queue is empty."}
        assert sent["json"]["stream"] is True
        assert sent["headers"]["Accept"] == "text/event-stream"

    def test_a_tool_call_split_across_frames_is_reassembled(self, transport):
        transport(
            [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                '"function":{"name":"search_queue","arguments":"{\\"site_id\\":"}}]}}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"arguments":"7}"}}]}}]}',
                "data: [DONE]",
            ]
        )

        fragments, message = self._turn(messages=[], tools=[{"type": "function"}])

        # Nothing to show an operator: this turn is the model deciding to look
        # something up, and the arguments only parse once the last frame is in.
        assert fragments == []
        assert message["content"] is None
        assert message["tool_calls"] == [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "search_queue", "arguments": '{"site_id":7}'},
            }
        ]

    def test_an_error_frame_inside_a_200_stream_is_a_failure(self, transport):
        """A rate limit reached mid-turn arrives as a frame, not as a status.

        Without this the reply simply stops wherever the error interrupted it,
        and a half-sentence about the queue reads like a finished answer.
        """
        transport(
            [
                'data: {"choices":[{"delta":{"content":"The queue "}}]}',
                'data: {"error":{"message":"rate limit exceeded","code":429}}',
            ]
        )

        with pytest.raises(agent_client.OpenRouterError) as failure:
            self._turn(messages=[], tools=[])
        assert "rate limit exceeded" in str(failure.value)

    def test_a_refused_stream_names_the_host_and_its_reason(self, transport):
        transport([], status_code=429, body=b"slow down")

        with pytest.raises(agent_client.OpenRouterError) as failure:
            self._turn(messages=[], tools=[])
        assert "openrouter.ai" in str(failure.value)
        assert "slow down" in str(failure.value)
