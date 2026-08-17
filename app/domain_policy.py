"""Pure domain parsing helpers shared by policy schemas and services."""

from urllib.parse import urlsplit


def normalize_domain(value: str) -> str:
    """Return one lowercase ASCII domain without a wildcard or trailing dot."""
    candidate = value.strip().lower().removeprefix("*.").rstrip(".")
    if "://" in candidate:
        candidate = urlsplit(candidate).hostname or ""
    if (
        not candidate
        or "/" in candidate
        or ":" in candidate
        or any(character.isspace() for character in candidate)
    ):
        raise ValueError(f"invalid domain: {value!r}")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError(f"invalid domain: {value!r}") from error


def domain_from_url(value: str) -> str:
    host = urlsplit(value).hostname
    if not host:
        raise ValueError(f"URL has no domain: {value!r}")
    return normalize_domain(host)


def domain_matches(domain: str, rule: str) -> bool:
    """Match a domain rule against the domain itself and all its subdomains."""
    normalized_domain = normalize_domain(domain)
    normalized_rule = normalize_domain(rule)
    return normalized_domain == normalized_rule or normalized_domain.endswith(f".{normalized_rule}")


def normalized_unique_domains(values: list[str]) -> list[str]:
    return sorted({normalize_domain(value) for value in values if value.strip()})


def normalize_tld(value: str) -> str:
    candidate = value.strip().lower().lstrip(".")
    if not candidate or "." in candidate or not candidate.replace("-", "").isalnum():
        raise ValueError(f"invalid TLD: {value!r}")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError(f"invalid TLD: {value!r}") from error


def normalized_unique_tlds(values: list[str]) -> list[str]:
    return sorted({normalize_tld(value) for value in values if value.strip()})
