from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain_policy import normalized_unique_domains, normalized_unique_tlds


class ExternalLinkPolicyUpdate(BaseModel):
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
    def reject_conflicting_domains(self) -> "ExternalLinkPolicyUpdate":
        blocked = set(self.blocklist_domains) | set(self.competitor_domains)
        conflicts = sorted(set(self.allowlist_domains) & blocked)
        if conflicts:
            raise ValueError("domains cannot be both allowed and blocked: " + ", ".join(conflicts))
        return self


class ExternalLinkPolicyOut(ExternalLinkPolicyUpdate):
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
