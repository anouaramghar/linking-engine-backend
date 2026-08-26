import hashlib

from app.models import Article, Site
from app.services.citation_need import (
    CITATION_DETECTOR_VERSION,
    analyze_citation_needs,
)


def test_detector_returns_exact_ranked_sentences_with_explainable_signals() -> None:
    text = (
        "Tomatoes are used in many everyday recipes. "
        "A 2024 study found that pressure canning reduces botulism risk by 80%. "
        "How should jars be stored? "
        "Dr. Smith reported that unsafe temperatures cause infections."
    )

    analysis = analyze_citation_needs(text, language="en", threshold=0.65, max_results=10)

    assert analysis.detector_version == CITATION_DETECTOR_VERSION
    assert analysis.content_fingerprint == hashlib.sha256(text.encode()).hexdigest()
    assert analysis.sentences_analyzed == 4
    assert analysis.total_detected == 2
    assert analysis.truncated is False
    assert [need.sentence for need in analysis.needs] == [
        "A 2024 study found that pressure canning reduces botulism risk by 80%.",
        "Dr. Smith reported that unsafe temperatures cause infections.",
    ]
    primary = analysis.primary
    assert primary is not None
    assert text[primary.start : primary.end] == primary.sentence
    assert primary.confidence > 0.99
    assert primary.reasons == (
        "research_or_attribution",
        "quantitative_claim",
        "health_or_safety_claim",
        "time_sensitive_claim",
        "causal_claim",
    )


def test_detector_ignores_questions_short_fragments_and_explicit_inline_citations() -> None:
    text = (
        "2024 results. "
        "“Did revenue increase by 40%?” "
        "Revenue increased by 40% according to https://example.com/report. "
        "This guide explains how teams organize an editorial calendar."
    )

    analysis = analyze_citation_needs(text, threshold=0.65)

    assert analysis.needs == ()


def test_detector_reports_when_character_or_sentence_bounds_truncate_analysis() -> None:
    text = (
        "Research shows that treatment reduces infection risk by 30%. "
        "A 2025 survey found that 70% of teams changed their process."
    )

    character_bounded = analyze_citation_needs(
        text,
        threshold=0.65,
        max_article_chars=65,
    )
    sentence_bounded = analyze_citation_needs(
        text,
        threshold=0.65,
        max_sentences=1,
    )
    decimal = "A 2024 report found that revenue increased by 3.5% across regions."
    decimal_cut = analyze_citation_needs(
        decimal,
        threshold=0.65,
        max_article_chars=decimal.index("3.5") + 2,
    )

    assert character_bounded.truncated is True
    assert character_bounded.sentences_analyzed == 1
    assert character_bounded.total_detected == 1
    assert sentence_bounded.truncated is True
    assert sentence_bounded.sentences_analyzed == 1
    assert len(sentence_bounded.needs) == 1
    assert decimal_cut.truncated is True
    assert decimal_cut.sentences_analyzed == 0
    assert decimal_cut.total_detected == 0


def test_article_citation_need_endpoint_is_site_scoped_and_bounded(client, db, site) -> None:
    article = Article(
        site_id=site.id,
        url=f"{site.base_url}/evidence",
        title="Evidence guide",
        language="en-US",
        content_text=(
            "A 2024 report found that 72% of editors verify quantitative claims. "
            "Research shows that verification reduces publication errors by 30%."
        ),
    )
    db.add(article)
    db.commit()
    db.refresh(article)

    response = client.get(
        f"/api/v1/articles/{article.id}/citation-needs",
        params={"limit": 1},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["article_id"] == article.id
    assert payload["language"] == "en-us"
    assert payload["detector_version"] == CITATION_DETECTOR_VERSION
    assert payload["sentences_analyzed"] == 2
    assert payload["total_detected"] == 2
    assert len(payload["items"]) == 1
    assert payload["items"][0]["sentence"] in article.content_text
    assert payload["items"][0]["start"] < payload["items"][0]["end"]

    pool = Site(
        name="Pool source",
        base_url="https://en.wikipedia.org/wiki/Evidence",
        platform="pool",
    )
    db.add(pool)
    db.flush()
    pool_article = Article(
        site_id=pool.id,
        url="https://en.wikipedia.org/wiki/Evidence",
        title="Evidence",
        content_text="A 2024 report measured a 30% increase.",
    )
    db.add(pool_article)
    db.commit()

    blocked = client.get(f"/api/v1/articles/{pool_article.id}/citation-needs")
    assert blocked.status_code == 409


def test_article_citation_need_endpoint_rejects_unknown_article(client) -> None:
    response = client.get("/api/v1/articles/99999999/citation-needs")
    assert response.status_code == 404


def test_supporting_signals_alone_do_not_flag_ordinary_prose() -> None:
    """ "now" and "more" are everyday English, not a claim needing a source."""

    text = (
        "Our team now offers more flexible scheduling for customers who book online. "
        "The new editor is better than the old one and loads faster today for most users."
    )

    analysis = analyze_citation_needs(text, language="en")

    assert analysis.sentences_analyzed == 2
    assert analysis.total_detected == 0
    assert analysis.needs == ()
    assert analysis.language_supported is True


def test_supporting_signals_still_strengthen_a_primary_signal() -> None:
    supported = "Researchers say the treatment reduces mortality by 34 percent."
    unsupported = "The dashboard now loads more quickly for most of our customers."

    flagged = analyze_citation_needs(supported, language="en")
    ignored = analyze_citation_needs(unsupported, language="en")

    assert flagged.total_detected == 1
    primary = flagged.primary
    assert primary is not None
    assert "research_or_attribution" in primary.reasons
    assert ignored.total_detected == 0


def test_detector_skips_languages_its_rules_were_not_written_for() -> None:
    text = (
        "Selon une etude de 2024, la mise en conserve reduit le risque de botulisme de 80%. "
        "Les chercheurs signalent que des temperatures dangereuses causent des infections."
    )

    analysis = analyze_citation_needs(text, language="fr")

    assert analysis.language_supported is False
    assert analysis.language == "fr"
    assert analysis.total_detected == 0
    assert analysis.sentences_analyzed == 0
    assert analysis.needs == ()
    # Still content-addressed, so a later English re-analysis is comparable.
    assert analysis.content_fingerprint == hashlib.sha256(text.encode()).hexdigest()


def test_english_variants_and_unknown_languages_stay_analyzable() -> None:
    """Crawlers store None when they cannot detect a language (WordPress does)."""

    text = "According to a 2024 study, the treatment reduced mortality by 12 percent."

    for language in ("en", "en-US", "EN_gb", None):
        analysis = analyze_citation_needs(text, language=language)
        assert analysis.language_supported is True, language
        assert analysis.total_detected == 1, language

    assert analyze_citation_needs(text, language=None).language == "und"
