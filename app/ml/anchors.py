"""Anchor text generation via OpenRouter free models (locked v0 decision).

Free models rate-limit aggressively, so we walk a fallback chain; the model that
answered is stored on the suggestion (llm_model) for traceability.
"""

import httpx

from app.config import settings

PROMPT = """You write anchor text for links inside blog articles.
Article: "{source_title}"
Excerpt: {excerpt}
The link points to: "{target_title}"
Reply with ONLY the anchor text — 2 to {max_words} words, natural in-sentence phrasing,
no quotes, no punctuation at the end."""


def _clean_anchor(raw: str, max_words: int) -> str | None:
    anchor = raw.strip().strip("\"'").strip().rstrip(".!:;")
    words = anchor.split()
    if not (2 <= len(words) <= max_words):
        return None
    return anchor


def generate_anchor(source_title: str, excerpt: str, target_title: str) -> tuple[str, str]:
    """-> (anchor_text, model_that_answered). Raises RuntimeError if every model fails."""
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    prompt = PROMPT.format(
        source_title=source_title,
        excerpt=excerpt[:800],
        target_title=target_title,
        max_words=settings.anchor_max_words,
    )
    errors = []
    for model in settings.openrouter_models.split(","):
        model = model.strip()
        try:
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 30,
                    "temperature": 0.3,
                },
                timeout=60,
            )
            resp.raise_for_status()
            anchor = _clean_anchor(
                resp.json()["choices"][0]["message"]["content"], settings.anchor_max_words
            )
            if anchor:
                return anchor, model
            errors.append(f"{model}: unusable output")
        except (httpx.HTTPError, KeyError, IndexError) as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("all anchor models failed: " + "; ".join(errors))
