"""Deterministic in-memory BM25 used by the limited-pilot ranking path."""

import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    }
)


def tokenize(text: str) -> list[str]:
    """Return lowercase alphanumeric terms with common English stopwords removed."""
    return [term for term in TOKEN_RE.findall(text.lower()) if term not in STOPWORDS]


class BM25Index:
    """Small Okapi BM25 index with stable ranking and explicit exclusions."""

    def __init__(
        self,
        documents: Mapping[int, Sequence[str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if k1 <= 0:
            raise ValueError("k1 must be positive")
        if not 0 <= b <= 1:
            raise ValueError("b must be between 0 and 1")
        if not documents:
            raise ValueError("documents must not be empty")

        self.k1 = k1
        self.b = b
        self.document_count = len(documents)
        self.document_lengths = {
            document_id: len(terms) for document_id, terms in documents.items()
        }
        self.average_document_length = sum(self.document_lengths.values()) / self.document_count
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for document_id, terms in documents.items():
            for term, frequency in Counter(terms).items():
                self.postings[term].append((document_id, frequency))

    def score_documents(
        self,
        query_terms: Sequence[str],
        *,
        excluded_ids: set[int] | None = None,
    ) -> dict[int, float]:
        """Return the BM25 score of every document sharing a term with the query.

        Exposed separately from `rank` because the pilot needs two things from one
        accumulation pass: the lexical top-N, and the true score of a candidate
        that dense retrieval contributed. Reporting 0.0 for the latter would put
        an untrue number in the stored score components.
        """
        excluded = excluded_ids or set()
        scores: dict[int, float] = defaultdict(float)
        average_length = self.average_document_length or 1.0
        for term, query_frequency in Counter(query_terms).items():
            postings = self.postings.get(term, ())
            if not postings:
                continue
            document_frequency = len(postings)
            inverse_document_frequency = math.log(
                1 + (self.document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            query_weight = min(query_frequency, 3)
            for document_id, term_frequency in postings:
                if document_id in excluded:
                    continue
                length_normalizer = self.k1 * (
                    1 - self.b + self.b * self.document_lengths[document_id] / average_length
                )
                scores[document_id] += (
                    query_weight
                    * inverse_document_frequency
                    * term_frequency
                    * (self.k1 + 1)
                    / (term_frequency + length_normalizer)
                )

        return dict(scores)

    def rank(
        self,
        query_terms: Sequence[str],
        *,
        limit: int,
        excluded_ids: set[int] | None = None,
    ) -> list[tuple[int, float]]:
        """Return the highest BM25 scores for documents sharing query terms."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        scores = self.score_documents(query_terms, excluded_ids=excluded_ids)
        return rank_scores(scores)[:limit]


def rank_scores(scores: Mapping[int, float]) -> list[tuple[int, float]]:
    """Order scored documents highest first, breaking ties on the document id.

    The id tiebreak is what makes the ranking reproducible across runs: dict
    iteration order would otherwise decide which of two equally scored documents
    is suggested.
    """
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
