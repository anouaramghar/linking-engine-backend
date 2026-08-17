import json
from datetime import UTC, datetime

import pytest

from app.ml.evaluation.temporal_split import (
    FrozenSplitError,
    TemporalEvaluationSplit,
    build_temporal_evaluation_split,
)
from app.models import Article, InternalLink, Site, Suggestion


CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)

# Every link a crawl discovers is stamped with the crawl time, so a real database
# holds one of these for thousands of links. Tests place it after the cutoff: any
# code that dates links by it puts the whole corpus in test and none in train.
CRAWLED_AT = datetime(2026, 8, 3, tzinfo=UTC)


def _article(db, site: Site, slug: str, published_at: datetime | None) -> Article:
    # created_at is deliberately left to its server default (now). It records the
    # database insert, so a split that reads it would call every article new.
    article = Article(
        site_id=site.id,
        external_id=slug,
        url=f"{site.base_url}/{slug}",
        title=slug,
        content_text=f"content for {slug}",
        published_at=published_at,
    )
    db.add(article)
    db.flush()
    return article


def _site(db, suffix: str) -> Site:
    site = Site(
        name=f"Temporal {suffix}",
        base_url=f"https://temporal-{suffix}.example.com",
        platform="html",
    )
    db.add(site)
    db.flush()
    return site


def test_editor_split_is_time_based_deduplicated_and_marks_new_nodes(db):
    site = _site(db, "editor")
    old_source = _article(db, site, "old-source", datetime(2025, 1, 1, tzinfo=UTC))
    old_target = _article(db, site, "old-target", datetime(2025, 2, 1, tzinfo=UTC))
    new_target = _article(db, site, "new-target", datetime(2026, 2, 1, tzinfo=UTC))
    db.add_all(
        [
            Suggestion(
                site_id=site.id,
                source_article_id=old_source.id,
                target_article_id=old_target.id,
                method="baseline_cosine",
                score=0.8,
                status="applied",
                applied_at=datetime(2025, 6, 1, tzinfo=UTC),
            ),
            # Same editorial link from another method: the first application is one event.
            Suggestion(
                site_id=site.id,
                source_article_id=old_source.id,
                target_article_id=old_target.id,
                method="hybrid_bm25",
                score=0.9,
                status="applied",
                applied_at=datetime(2025, 7, 1, tzinfo=UTC),
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=old_source.id,
                target_article_id=new_target.id,
                method="hybrid_bm25",
                score=0.9,
                status="applied",
                applied_at=CUTOFF,
            ),
            Suggestion(
                site_id=site.id,
                source_article_id=old_target.id,
                target_article_id=new_target.id,
                method="hybrid_bm25",
                score=0.7,
                status="rejected",
                reviewed_at=datetime(2026, 3, 1, tzinfo=UTC),
            ),
        ]
    )
    db.commit()

    # Scoped to this test's own site: the builder reads every site by default,
    # so an unscoped call here measures whatever else the suite left in the
    # shared test database rather than the four suggestions above.
    split = build_temporal_evaluation_split(db, cutoff_at=CUTOFF, site_ids=(site.id,))

    assert len(split.train) == 1
    assert split.train[0].target_article_id == old_target.id
    assert split.train[0].event_at < CUTOFF
    assert len(split.test) == 1
    assert split.test[0].target_article_id == new_target.id
    assert split.test[0].event_at == CUTOFF
    assert split.test[0].source_is_new is False
    assert split.test[0].target_is_new is True

    db.delete(site)
    db.commit()


def test_observed_split_dates_links_by_publication_not_by_crawl_time(db):
    site = _site(db, "observed-publication")
    old_source = _article(db, site, "old-source", datetime(2025, 3, 1, tzinfo=UTC))
    old_target = _article(db, site, "old-target", datetime(2025, 1, 1, tzinfo=UTC))
    new_source = _article(db, site, "new-source", datetime(2026, 4, 1, tzinfo=UTC))
    db.add_all(
        [
            InternalLink(
                source_article_id=old_source.id,
                target_article_id=old_target.id,
                first_seen_at=CRAWLED_AT,
            ),
            InternalLink(
                source_article_id=new_source.id,
                target_article_id=old_target.id,
                first_seen_at=CRAWLED_AT,
            ),
        ]
    )
    db.commit()

    split = build_temporal_evaluation_split(
        db,
        cutoff_at=CUTOFF,
        ground_truth="observed",
        site_ids=(site.id,),
    )

    # One crawl timestamp, two sides of the cutoff: publication dates decide.
    assert len(split.train) == 1
    assert split.train[0].source_article_id == old_source.id
    assert split.train[0].event_at == datetime(2025, 3, 1, tzinfo=UTC)
    assert len(split.test) == 1
    assert split.test[0].source_article_id == new_source.id
    assert split.test[0].source_is_new is True
    assert split.test[0].target_is_new is False

    db.delete(site)
    db.commit()


def test_observed_link_cannot_predate_its_target(db):
    site = _site(db, "observed-later-edit")
    source = _article(db, site, "source", datetime(2025, 5, 1, tzinfo=UTC))
    target = _article(db, site, "target", datetime(2026, 6, 1, tzinfo=UTC))
    db.add(
        InternalLink(
            source_article_id=source.id,
            target_article_id=target.id,
            first_seen_at=CRAWLED_AT,
        )
    )
    db.commit()

    split = build_temporal_evaluation_split(
        db,
        cutoff_at=CUTOFF,
        ground_truth="observed",
        site_ids=(site.id,),
    )

    # The source predates the cutoff, but it cannot have linked to an article that
    # did not exist yet. The later edit is the event, so this is a test example.
    assert split.train == ()
    assert len(split.test) == 1
    assert split.test[0].event_at == datetime(2026, 6, 1, tzinfo=UTC)

    db.delete(site)
    db.commit()


def test_links_without_a_publication_date_are_skipped_and_counted(db):
    site = _site(db, "observed-undated")
    dated = _article(db, site, "dated", datetime(2025, 2, 1, tzinfo=UTC))
    undated = _article(db, site, "undated", None)
    db.add_all(
        [
            InternalLink(
                source_article_id=dated.id,
                target_article_id=undated.id,
                first_seen_at=CRAWLED_AT,
            ),
            InternalLink(
                source_article_id=undated.id,
                target_article_id=dated.id,
                first_seen_at=CRAWLED_AT,
            ),
        ]
    )
    db.commit()

    split = build_temporal_evaluation_split(
        db,
        cutoff_at=CUTOFF,
        ground_truth="observed",
        site_ids=(site.id,),
    )

    assert split.train == ()
    assert split.test == ()
    assert split.skipped_without_publication_date == 2

    db.delete(site)
    db.commit()


def test_observed_split_respects_the_site_filter(db):
    included = _site(db, "included")
    excluded = _site(db, "excluded")
    included_source = _article(db, included, "source", datetime(2026, 2, 1, tzinfo=UTC))
    included_target = _article(db, included, "target", datetime(2025, 1, 2, tzinfo=UTC))
    excluded_source = _article(db, excluded, "source", datetime(2026, 2, 1, tzinfo=UTC))
    excluded_target = _article(db, excluded, "target", datetime(2025, 1, 2, tzinfo=UTC))
    db.add_all(
        [
            InternalLink(
                source_article_id=included_source.id,
                target_article_id=included_target.id,
                first_seen_at=CRAWLED_AT,
            ),
            InternalLink(
                source_article_id=excluded_source.id,
                target_article_id=excluded_target.id,
                first_seen_at=CRAWLED_AT,
            ),
        ]
    )
    db.commit()

    split = build_temporal_evaluation_split(
        db,
        cutoff_at=CUTOFF,
        ground_truth="observed",
        site_ids=(included.id,),
    )

    assert split.ground_truth == "observed"
    assert split.train == ()
    assert len(split.test) == 1
    assert split.test[0].site_id == included.id

    db.delete(included)
    db.delete(excluded)
    db.commit()


def test_split_rejects_naive_cutoff(db):
    with pytest.raises(ValueError, match="timezone"):
        build_temporal_evaluation_split(db, cutoff_at=datetime(2026, 1, 1))


def test_serialized_contract_is_ready_for_metrics_handoff(db):
    split = build_temporal_evaluation_split(
        db,
        cutoff_at=CUTOFF,
        site_ids=(2_147_483_647,),
    )

    payload = split.to_dict()
    assert payload == {
        "schema_version": 2,
        "ground_truth": "editor",
        "cutoff_at": "2026-01-01T00:00:00+00:00",
        "train": [],
        "test": [],
        "skipped_without_publication_date": 0,
    }


def test_a_frozen_split_reads_back_as_the_split_that_was_written(db):
    """The frozen file is the fixed target every method is scored against.

    If a round trip lost a row or a timezone, two methods measured a week apart
    would be scored on two different test sets and the difference reported as an
    improvement.
    """
    site = _site(db, "frozen")
    source = _article(db, site, "source", datetime(2026, 2, 1, tzinfo=UTC))
    target = _article(db, site, "target", datetime(2025, 3, 1, tzinfo=UTC))
    db.add(
        Suggestion(
            site_id=site.id,
            source_article_id=source.id,
            target_article_id=target.id,
            method="hybrid_bm25",
            score=0.8,
            status="applied",
            applied_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    db.commit()

    split = build_temporal_evaluation_split(db, cutoff_at=CUTOFF, site_ids=(site.id,))
    reloaded = TemporalEvaluationSplit.from_dict(json.loads(json.dumps(split.to_dict())))

    assert len(split.test) == 1
    assert reloaded == split
    assert reloaded.test[0].event_at.tzinfo is not None
    assert reloaded.test[0].source_is_new is True
    assert reloaded.test[0].target_is_new is False

    db.delete(site)
    db.commit()


def test_a_split_written_by_another_schema_is_refused(db):
    payload = build_temporal_evaluation_split(
        db, cutoff_at=CUTOFF, site_ids=(2_147_483_647,)
    ).to_dict()

    with pytest.raises(FrozenSplitError, match="schema_version"):
        TemporalEvaluationSplit.from_dict({**payload, "schema_version": 1})
    with pytest.raises(FrozenSplitError, match="ground_truth"):
        TemporalEvaluationSplit.from_dict({**payload, "ground_truth": "guessed"})
    # A naive timestamp compares as if it were UTC and would move the cutoff.
    with pytest.raises(FrozenSplitError, match="timezone"):
        TemporalEvaluationSplit.from_dict({**payload, "cutoff_at": "2026-01-01T00:00:00"})
