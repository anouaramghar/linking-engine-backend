"""Wire schemas for the dashboard assistant."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentTurn(BaseModel):
    role: str
    content: str

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        if value not in ("user", "assistant"):
            raise ValueError("history roles must be user or assistant")
        return value


#: Bounds on the navigation metadata that decorates a chat turn. They cap what
#: reaches the prompt; they never decide whether the turn is answered.
MAX_CONTEXT_SURFACE = 64
MAX_CONTEXT_PATH = 500
MAX_CONTEXT_SEARCH = 2_000
MAX_CONTEXT_SCOPE = 500
MAX_CONTEXT_FILTERS = 50
MAX_CONTEXT_FILTER_NAME = 80
MAX_CONTEXT_FILTER_VALUE = 500


def _clamped_text(value: object, limit: int) -> str:
    """Coerce one untrusted metadata field to a bounded string."""
    return value[:limit] if isinstance(value, str) else ""


class AgentChatContext(BaseModel):
    """Bounded navigation metadata describing the operator's current dashboard view.

    Every bound here truncates; none of them reject. This metadata rides inside
    the chat body, so a validation error on it would fail the whole request and
    leave the operator's question unanswered — a pasted URL carrying one long
    query parameter would silently disable the assistant on that page. Decorating
    a turn must never be able to cost the turn, so the model normalizes whatever
    it is handed instead of refusing it. Unknown keys are dropped rather than
    forbidden, which is what lets a newer dashboard talk to an older engine.
    """

    model_config = ConfigDict(extra="ignore")

    surface: str = ""
    path: str = ""
    search: str = ""
    scope: str = ""
    filters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def clamp(cls, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        raw_filters = value.get("filters")
        filters: dict[str, str] = {}
        if isinstance(raw_filters, dict):
            for key, item in list(raw_filters.items())[:MAX_CONTEXT_FILTERS]:
                if isinstance(key, str) and isinstance(item, str):
                    filters[key[:MAX_CONTEXT_FILTER_NAME]] = item[:MAX_CONTEXT_FILTER_VALUE]
        return {
            "surface": _clamped_text(value.get("surface"), MAX_CONTEXT_SURFACE),
            "path": _clamped_text(value.get("path"), MAX_CONTEXT_PATH),
            "search": _clamped_text(value.get("search"), MAX_CONTEXT_SEARCH),
            "scope": _clamped_text(value.get("scope"), MAX_CONTEXT_SCOPE),
            "filters": filters,
        }

    def describes_a_view(self) -> bool:
        """Whether anything survived normalization that could resolve a reference."""
        return bool(self.surface or self.path or self.scope or self.filters)


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    history: list[AgentTurn] = Field(default_factory=list, max_length=100)
    context: AgentChatContext | None = None


class AgentToolTrace(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)
    outcome: dict = Field(default_factory=dict)


class AgentProposal(BaseModel):
    """A typed mutation awaiting the operator's confirm in the dashboard."""

    tool: str
    kind: str
    risk: Literal["reversible", "sensitive"]
    method: Literal["POST", "PUT", "DELETE"]
    endpoint: str
    payload: dict
    match_count: int | None = None
    context: dict | None = None
    impact: dict | None = None


class AgentChatResponse(BaseModel):
    reply: str
    tools_used: list[AgentToolTrace] = []
    proposals: list[AgentProposal] = []


class AgentStatusResponse(BaseModel):
    configured: bool
    model: str
    #: The host the assistant calls. Empty when nothing is configured.
    provider: str = ""
