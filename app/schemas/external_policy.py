from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain_policy import normalized_unique_domains, normalized_unique_tlds


class ExternalLinkPolicyValues(BaseModel):
    external_links_enabled: bool = False
    require_https: bool = True
    min_trust_score: int = Field(default=60, ge=0, le=100)
    min_domain_age_days: int = Field(default=0, ge=0, le=36_500)
    trusted_tlds: list[str] = Field(default_factory=list, max_length=100)
    allowlist_domains: list[str] = Field(default_factory=list, max_length=500)
    blocklist_domains: list[str] = Field(default_factory=list, max_length=500)
    competitor_domains: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("trusted_tlds")
    @classmethod
    def normalize_tlds(cls, value: list[str]) -> list[str]:
        return normalized_unique_tlds(value)

    @field_validator("allowlist_domains", "blocklist_domains", "competitor_domains")
    @classmethod
    def normalize_domains(cls, value: list[str]) -> list[str]:
        return normalized_unique_domains(value)

    @model_validator(mode="after")
    def reject_conflicting_domains(self) -> "ExternalLinkPolicyValues":
        blocked = set(self.blocklist_domains) | set(self.competitor_domains)
        conflicts = sorted(set(self.allowlist_domains) & blocked)
        if conflicts:
            raise ValueError("domains cannot be both allowed and blocked: " + ", ".join(conflicts))
        return self


class ExternalLinkPolicyUpdate(ExternalLinkPolicyValues):
    expected: ExternalLinkPolicyValues | None = Field(
        None,
        description=(
            "Only save if the current effective policy still equals this snapshot. "
            "Agent-staged changes use it as an optimistic-concurrency guard."
        ),
    )
    expected_expiring_suggestion_ids: list[int] | None = Field(
        None,
        max_length=10_000,
        description=(
            "Only save if applying this policy would expire exactly these pending or "
            "approved suggestions. Sensitive agent proposals bind confirmation to this impact."
        ),
    )

    @field_validator("expected_expiring_suggestion_ids")
    @classmethod
    def normalize_expected_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(suggestion_id < 1 for suggestion_id in value):
            raise ValueError("suggestion ids must be positive")
        return sorted(set(value))


class ExternalLinkPolicyOut(ExternalLinkPolicyValues):
    model_config = ConfigDict(from_attributes=True)

    site_id: int
    owned_domain_protection: bool = True
    expired_suggestions: int = 0
    updated_by: str | None = None
    updated_at: datetime | None = None


class ExternalSourceEvaluation(BaseModel):
    site_id: int
    site_name: str
    domain: str
    trust_score: int
    eligible: bool
    eligible_articles: int
    blocked_articles: int
    reasons: list[str]
    checks: dict[str, bool | int | str | None]


class ExternalSourceEvaluationList(BaseModel):
    items: list[ExternalSourceEvaluation]
