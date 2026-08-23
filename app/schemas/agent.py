"""Wire schemas for the dashboard assistant."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AgentTurn(BaseModel):
    role: str
    content: str

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        if value not in ("user", "assistant"):
            raise ValueError("history roles must be user or assistant")
        return value


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    history: list[AgentTurn] = Field(default_factory=list, max_length=100)


class AgentToolTrace(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)
    outcome: dict = Field(default_factory=dict)


class AgentProposal(BaseModel):
    """A typed mutation awaiting the operator's confirm in the dashboard."""

    tool: str
    kind: str
    risk: Literal["reversible", "sensitive"]
    method: Literal["POST", "PUT"]
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
