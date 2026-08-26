"""Deterministic baseline for sentences that should carry a source.

The detector is deliberately local and explainable.  It does not decide that a
candidate URL supports a claim, and it never sends article prose to an external
provider.  Its only answer is which source-article sentences contain signals
that normally require editorial evidence.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from app.config import settings

CITATION_DETECTOR_VERSION = "citation_rules_en_v2"

_CLOSING_PUNCTUATION = frozenset("\"'\u2019\u201d)]}")
_ABBREVIATIONS = frozenset(
    {
        "dr",
        "e.g",
        "etc",
        "i.e",
        "mr",
        "mrs",
        "ms",
        "prof",
        "st",
        "vs",
    }
)
_URL_OR_INLINE_CITATION = re.compile(
    r"(?:https?://|www\.)\S+|\[(?:\d{1,3}|citation needed)\]",
    re.IGNORECASE,
)
_WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)

# Every rule above is English lexis, so the detector may only speak about
# English prose. Crawlers that cannot determine a language store ``None``
# (see the WordPress connector), and an unknown language must stay analyzable
# or the feature would silently switch itself off across the managed fleet.
# Only a language positively identified as something else is skipped.
_SUPPORTED_LANGUAGES = frozenset({"en", "und"})


def _language_tags(language: str | None) -> tuple[str, str]:
    """Return the tag to report and the base tag to decide support on.

    The reported tag stays as the article declared it (lowercased) so evidence
    records what was actually seen; only the base subtag drives the gate, so
    ``en-GB`` and ``en_US`` are recognised as the English the rules were written
    for.
    """

    reported = (language or "").strip().lower() or "und"
    return reported, reported.replace("_", "-").split("-", 1)[0] or "und"


@dataclass(frozen=True, slots=True)
class _SignalRule:
    reason: str
    weight: float
    pattern: re.Pattern[str]
    # Primary signals name the thing a source would actually support: an
    # attribution, a number, a health claim. Supporting signals ("now", "more")
    # are ordinary English that happens to co-occur with such claims, so they
    # may only strengthen a primary match, never qualify a sentence alone.
    is_primary: bool = True


_SIGNAL_RULES = (
    _SignalRule(
        "research_or_attribution",
        0.85,
        re.compile(
            r"\b(?:according to|study|studies|research|researchers|report|survey|"
            r"data (?:show|shows|showed|suggest|suggests|indicate|indicates)|"
            r"evidence (?:show|shows|suggest|suggests|indicate|indicates)|"
            r"experts? (?:say|says|recommend|recommends))\b",
            re.IGNORECASE,
        ),
    ),
    _SignalRule(
        "quantitative_claim",
        0.78,
        re.compile(
            r"(?:[$\u00a3\u20ac]\s?\d|\b\d+(?:[.,]\d+)?\s?(?:%|percent|percentage|"
            r"million|billion|trillion|kg|g|mg|km|miles?|hours?|days?|years?|"
            r"degrees?|\u00b0[cf])(?=\W|$)|\b(?:one in|\d+ out of \d+)\b)",
            re.IGNORECASE,
        ),
    ),
    _SignalRule(
        "health_or_safety_claim",
        0.70,
        re.compile(
            r"\b(?:safe|safety|unsafe|dangerous|risk|risks|disease|symptom|symptoms|"
            r"treatment|dose|dosage|toxicity|toxic|mortality|infection|prevent|prevents|"
            r"cure|cures)\b",
            re.IGNORECASE,
        ),
    ),
    _SignalRule(
        "time_sensitive_claim",
        0.62,
        re.compile(
            r"\b(?:(?:19|20)\d{2}|currently|today|now|as of|recently|latest|"
            r"this (?:year|month|quarter)|last (?:year|month|quarter))\b",
            re.IGNORECASE,
        ),
        is_primary=False,
    ),
    _SignalRule(
        "causal_claim",
        0.60,
        re.compile(
            r"\b(?:cause|causes|caused|lead to|leads to|result in|results in|"
            r"increase|increases|decrease|decreases|reduce|reduces|improve|improves|"
            r"associated with|linked to)\b",
            re.IGNORECASE,
        ),
    ),
    _SignalRule(
        "comparative_claim",
        0.55,
        re.compile(
            r"\b(?:more|less|fewer|higher|lower|better|worse|best|worst|largest|"
            r"smallest|fastest|slowest|most|least)\b",
            re.IGNORECASE,
        ),
        is_primary=False,
    ),
)


@dataclass(frozen=True, slots=True)
class CitationNeed:
    """One exact article sentence and the signals that qualified it."""

    sentence: str
    start: int
    end: int
    confidence: float
    reasons: tuple[str, ...]
    detector_version: str = CITATION_DETECTOR_VERSION

    def as_score_component(self) -> dict:
        return {
            "sentence": self.sentence,
            "start": self.start,
            "end": self.end,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "detector_version": self.detector_version,
        }


@dataclass(frozen=True, slots=True)
class CitationNeedAnalysis:
    """Bounded, content-addressed result for one article body."""

    content_fingerprint: str
    detector_version: str
    threshold: float
    language: str
    sentences_analyzed: int
    total_detected: int
    truncated: bool
    needs: tuple[CitationNeed, ...]
    # Distinguishes "analyzed, nothing needs a source" from "never analyzed".
    # Both return no needs, and only this flag keeps an empty result honest.
    language_supported: bool = True

    @property
    def primary(self) -> CitationNeed | None:
        return self.needs[0] if self.needs else None


def _is_abbreviation(text: str, period_index: int) -> bool:
    token_start = period_index - 1
    while token_start >= 0 and (text[token_start].isalpha() or text[token_start] == "."):
        token_start -= 1
    token = text[token_start + 1 : period_index].lower()
    if token in _ABBREVIATIONS:
        return True
    return len(token) == 1 and token.isalpha()


def _sentence_spans(text: str, max_sentences: int) -> tuple[list[tuple[int, int]], bool]:
    """Return trimmed sentence offsets without rewriting the source text."""

    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        boundary_end: int | None = None
        char = text[index]
        if char == "\n" and index + 1 < length and text[index + 1] == "\n":
            boundary_end = index
        elif char in ".!?":
            if char == "." and _is_abbreviation(text, index):
                index += 1
                continue
            candidate_end = index + 1
            while candidate_end < length and text[candidate_end] in _CLOSING_PUNCTUATION:
                candidate_end += 1
            if candidate_end == length or text[candidate_end].isspace():
                boundary_end = candidate_end

        if boundary_end is None:
            index += 1
            continue

        left = start
        right = boundary_end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            spans.append((left, right))
            if len(spans) >= max_sentences:
                return spans, any(not char.isspace() for char in text[boundary_end:])
        start = boundary_end
        while start < length and text[start].isspace():
            start += 1
        index = start

    if start < length:
        left = start
        right = length
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right and len(spans) < max_sentences:
            spans.append((left, right))
        elif left < right:
            return spans, True
    return spans, False


def _score_sentence(sentence: str) -> tuple[float, tuple[str, ...]]:
    sentence_without_closers = sentence.rstrip().rstrip("".join(_CLOSING_PUNCTUATION))
    if sentence_without_closers.endswith("?") or _URL_OR_INLINE_CITATION.search(sentence):
        return 0.0, ()
    words = _WORD.findall(sentence)
    if len(sentence) < 30 or len(words) < 5 or len(sentence) > 1_000:
        return 0.0, ()

    matched = tuple(rule for rule in _SIGNAL_RULES if rule.pattern.search(sentence))
    if not any(rule.is_primary for rule in matched):
        # "now" and "more" are ordinary prose. Without a primary signal beside
        # them there is no claim for a source to support, and flagging the
        # sentence would only cost an editor a read.
        return 0.0, ()
    # Independent-evidence combination: two medium signals strengthen one
    # another without an unbounded sum ever exceeding one.
    confidence = 1.0 - math.prod(1.0 - rule.weight for rule in matched)
    return round(confidence, 4), tuple(rule.reason for rule in matched)


def analyze_citation_needs(
    text: str,
    *,
    language: str | None = None,
    threshold: float | None = None,
    max_results: int | None = None,
    max_article_chars: int | None = None,
    max_sentences: int | None = None,
) -> CitationNeedAnalysis:
    """Detect and rank source-worthy sentences with stable, inspectable rules."""

    effective_threshold = (
        settings.citation_need_threshold if threshold is None else float(threshold)
    )
    effective_results = (
        settings.citation_need_max_results
        if max_results is None
        else min(max_results, settings.citation_need_max_results)
    )
    effective_chars = (
        settings.citation_need_max_article_chars if max_article_chars is None else max_article_chars
    )
    effective_sentences = (
        settings.citation_need_max_sentences if max_sentences is None else max_sentences
    )
    if not 0.0 <= effective_threshold <= 1.0:
        raise ValueError("threshold must be between zero and one")
    if effective_results < 1 or effective_chars < 1 or effective_sentences < 1:
        raise ValueError("citation analysis bounds must be positive")

    reported_language, base_language = _language_tags(language)
    if base_language not in _SUPPORTED_LANGUAGES:
        return CitationNeedAnalysis(
            content_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            detector_version=CITATION_DETECTOR_VERSION,
            threshold=effective_threshold,
            language=reported_language,
            sentences_analyzed=0,
            total_detected=0,
            truncated=False,
            needs=(),
            language_supported=False,
        )

    bounded_text = text[:effective_chars]
    spans, sentence_truncated = _sentence_spans(bounded_text, effective_sentences)
    if len(text) > effective_chars and spans and spans[-1][1] == len(bounded_text):
        # A character bound can land in the middle of a claim. Do not score that
        # fragment as if it were a complete sentence.
        completed_tail = bounded_text.rstrip().rstrip("".join(_CLOSING_PUNCTUATION))
        cut_before_non_whitespace = not text[effective_chars].isspace()
        if cut_before_non_whitespace or not completed_tail.endswith((".", "!", "?")):
            spans.pop()
    needs: list[CitationNeed] = []
    for start, end in spans:
        sentence = bounded_text[start:end]
        confidence, reasons = _score_sentence(sentence)
        if not reasons or confidence < effective_threshold:
            continue
        needs.append(
            CitationNeed(
                sentence=sentence,
                start=start,
                end=end,
                confidence=confidence,
                reasons=reasons,
            )
        )

    needs.sort(key=lambda need: (-need.confidence, need.start, need.end))
    total_detected = len(needs)
    return CitationNeedAnalysis(
        content_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        detector_version=CITATION_DETECTOR_VERSION,
        threshold=effective_threshold,
        language=reported_language,
        sentences_analyzed=len(spans),
        total_detected=total_detected,
        truncated=len(text) > effective_chars or sentence_truncated,
        needs=tuple(needs[:effective_results]),
    )
