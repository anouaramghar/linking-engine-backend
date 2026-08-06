"""Where in the source article a suggested link belongs.

The model is asked to *find* a passage, never to write one: both the passage and
the anchor phrase must be copied verbatim out of the article the engine already
stores. Everything it returns is checked against that article before it is
saved, and anything that does not match is discarded as "no placement found".

That rule is what makes the card in the review drawer trustworthy, and it is
also what a later in-text insertion will depend on — an anchor that cannot be
located in the post is an anchor that cannot be linked.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import settings
from app.ml.llm import openrouter
from app.models import Suggestion

logger = logging.getLogger(__name__)

#: How much of the target article is shown as context for what it is about.
#: The model is choosing a position in the *source*, so the target only needs to
#: be recognizable, not reproduced.
TARGET_PREVIEW_CHARS = 600

#: A link needs something to sit on, and a whole paragraph is not an anchor.
MIN_ANCHOR_CHARS = 2
MAX_ANCHOR_CHARS = 120

SYSTEM_PROMPT = """\
You place internal links inside existing articles for an editorial team.

You are given a SOURCE article and a TARGET article. Find the single best place \
in the SOURCE article to link to the TARGET article.

Rules:
1. Choose one passage that ALREADY EXISTS in the SOURCE article. Copy it exactly, \
character for character. Do not rewrite, summarize, translate, or shorten it.
2. Choose an anchor phrase that is a contiguous substring of that passage, again \
copied exactly. It should be 2-8 words and read naturally as link text for the \
TARGET article.
3. The passage should be one or two sentences - enough for an editor to see where \
the link lands, not the whole paragraph.
4. If no passage in the SOURCE article is a genuinely good fit for this link, say \
so instead of forcing one.
5. If the request lists anchor phrases that are already taken, do not reuse one. \
Each link in an article needs its own words.

Answer with a JSON object and nothing else:
{"passage": "<exact text from the source article>", "anchor": "<exact substring \
of passage>", "reason": "<one short sentence>"}

If nothing fits, answer:
{"passage": null, "anchor": null, "reason": "<why not>"}\
"""


@dataclass(frozen=True)
class Placement:
    """A generated placement, or the recorded absence of one."""

    #: Null when the model found nothing suitable, or returned something that
    #: could not be located in the article.
    anchor_text: str | None
    placement_context: str | None
    llm_model: str | None
    generated_at: datetime

    @property
    def found(self) -> bool:
        return self.placement_context is not None


def _normalize(text: str) -> str:
    """Collapse whitespace so that quoting differences are not mismatches.

    Stored article text keeps the line breaks and runs of spaces it was crawled
    with; a model quoting a passage back re-wraps it. Comparing both sides
    whitespace-insensitively is what makes a verbatim quote survive that,
    without loosening the check into a fuzzy match.
    """
    return re.sub(r"\s+", " ", text).strip()


def _locate(needle: str, haystack: str) -> str | None:
    """Return `needle` as it appears in `haystack`, or None if it does not.

    Case-sensitively first, so an exact quote is returned untouched. The
    case-insensitive retry exists because models routinely re-case a leading
    word after a line break; the matched text from the *article* is returned,
    never the model's version of it, so the result is still the article's own.

    That retry matches by span rather than by index arithmetic. Case folding can
    change a string's length — "ß" folds to "ss" — so finding the position in a
    folded haystack and then slicing the original by `len(needle)` returns a
    window shifted by the difference: a passage silently gaining or losing its
    last characters, and an anchor that no longer sits where it says it does.
    """
    if not needle:
        return None
    if needle in haystack:
        return needle
    match = re.search(re.escape(needle), haystack, re.IGNORECASE)
    return match.group(0) if match else None


def _verify(passage: object, anchor: object, source_text: str) -> tuple[str | None, str | None]:
    """Check the model's answer against the article it claims to quote.

    Returns `(context, anchor)` when both are genuinely present, and `(None,
    None)` for every kind of failure — a refusal, a wrong type, a paraphrase, an
    anchor from somewhere else in the article, or one so long it is really the
    passage again. Those are all the same outcome to a caller: no placement.
    """
    if not isinstance(passage, str) or not isinstance(anchor, str):
        return None, None  # includes the model's own "nothing fits" null answer

    found_passage = _locate(_normalize(passage), source_text)
    if found_passage is None:
        logger.info("placement rejected: passage is not present in the source article")
        return None, None

    found_anchor = _locate(_normalize(anchor), found_passage)
    if found_anchor is None:
        logger.info("placement rejected: anchor is not inside the chosen passage")
        return None, None
    if not MIN_ANCHOR_CHARS <= len(found_anchor) <= MAX_ANCHOR_CHARS:
        logger.info("placement rejected: anchor is %d characters", len(found_anchor))
        return None, None
    # An anchor that swallows its own passage is the model declining to choose a
    # phrase. Linking a whole sentence is poor editorial practice at any length,
    # so this is relational rather than another absolute bound.
    if len(found_anchor) == len(found_passage):
        logger.info("placement rejected: anchor is the entire passage")
        return None, None

    return found_passage, found_anchor


def build_user_prompt(
    *,
    source_title: str,
    source_text: str,
    target_title: str,
    target_url: str,
    target_text: str,
    taken_anchors: Sequence[str] = (),
) -> str:
    target_preview = _normalize(target_text)[:TARGET_PREVIEW_CHARS]
    taken = (
        "ALREADY TAKEN anchor phrases in this SOURCE article (pick different words):\n"
        + "\n".join(f"- {anchor}" for anchor in taken_anchors)
        + "\n\n"
        if taken_anchors
        else ""
    )
    return (
        f"TARGET article (the page to link to)\n"
        f"Title: {target_title}\n"
        f"URL: {target_url}\n"
        f"About: {target_preview}\n\n"
        f"{taken}"
        f"SOURCE article (the page to place the link in)\n"
        f"Title: {source_title}\n"
        f"Text:\n{source_text}"
    )


def generate(suggestion: Suggestion, taken_anchors: Sequence[str] = ()) -> Placement:
    """Ask the model where this link belongs. Performs no database work.

    Split from the persistence below so the network call happens outside any
    open transaction: it takes seconds, and an idle transaction held across it
    would pin a connection and block the publication worker's row locks for the
    same length of time.

    `taken_anchors` are phrases other suggestions on this same source article
    have already claimed. Generated one row at a time, the model has no way to
    know that, so two suggestions routinely pick the same phrase — publication
    then gives it to the first and the loser falls back to the appended block,
    having paid for a placement it cannot use. The list is both a hint in the
    prompt and a hard rejection below, because a hint alone is not a rule.

    Raises `OpenRouterError` when the model cannot be reached — that is a
    temporary failure, and recording it as "no placement" would make it
    permanent for the row.
    """
    source = suggestion.source_article
    target = suggestion.target_article
    answer = openrouter.complete_json(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(
            source_title=source.title,
            source_text=source.content_text[: settings.placement_max_source_chars],
            target_title=target.title,
            target_url=target.url,
            target_text=target.content_text,
            taken_anchors=taken_anchors,
        ),
    )
    # Verified against the same slice the model was shown, so a passage can never
    # be accepted from a part of the article it could not have read.
    source_text = _normalize(source.content_text[: settings.placement_max_source_chars])
    context, anchor = _verify(answer.get("passage"), answer.get("anchor"), source_text)
    if anchor is not None and any(anchor.casefold() == taken.casefold() for taken in taken_anchors):
        logger.info("placement rejected: anchor %r is already taken on this article", anchor)
        context, anchor = None, None
    return Placement(
        anchor_text=anchor,
        placement_context=context,
        # Recorded even for a miss: it is the model that declined, and knowing
        # which one did is the point of the column.
        llm_model=settings.placement_model,
        generated_at=datetime.now(timezone.utc),
    )


def store(db: Session, suggestion_id: int, placement: Placement) -> None:
    """Persist a generated placement. The caller commits.

    A bare UPDATE rather than a loaded instance: the row was read before the
    model call and may have been reviewed since. These columns describe the pair,
    not the decision, so writing them must not touch `status` or resurrect a
    stale copy of it.
    """
    db.execute(
        update(Suggestion)
        .where(Suggestion.id == suggestion_id)
        .values(
            anchor_text=placement.anchor_text,
            placement_context=placement.placement_context,
            llm_model=placement.llm_model,
            placement_generated_at=placement.generated_at,
        )
        .execution_options(synchronize_session=False)
    )


def stored(suggestion: Suggestion) -> Placement | None:
    """The placement already on the row, or None if it has never been generated."""
    if suggestion.placement_generated_at is None:
        return None
    return Placement(
        anchor_text=suggestion.anchor_text,
        placement_context=suggestion.placement_context,
        llm_model=suggestion.llm_model,
        generated_at=suggestion.placement_generated_at,
    )
