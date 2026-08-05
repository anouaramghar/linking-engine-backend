from datetime import UTC, datetime

import pytest

from app.ml.evaluation.temporal_split import build_temporal_evaluation_split
from app.models import Article, InternalLink, Site, Suggestion


CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


def _article(db, site: Site, slug: str, created_at: datetime) -> Article:
    article = Article(
        site_id=site.id,
        external_id=slug,
        url=f"{site.base_url}/{slug}",
        title=slug,
        content_text=f"content for {slug}",
        created_at=created_at,
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


def test_observed_proxy_uses_first_seen_time_and_respects_site_filter(db):
    included = _site(db, "included")
    excluded = _site(db, "excluded")
    included_source = _article(db, included, "source", datetime(2025, 1, 1, tzinfo=UTC))
    included_target = _article(db, included, "target", datetime(2025, 1, 2, tzinfo=UTC))
    excluded_source = _article(db, excluded, "source", datetime(2025, 1, 1, tzinfo=UTC))
    excluded_target = _article(db, excluded, "target", datetime(2025, 1, 2, tzinfo=UTC))
    db.add_all(
        [
            InternalLink(
                source_article_id=included_source.id,
                target_article_id=included_target.id,
                first_seen_at=datetime(2026, 2, 1, tzinfo=UTC),
            ),
            InternalLink(
                source_article_id=excluded_source.id,
                target_article_id=excluded_target.id,
                first_seen_at=datetime(2025, 2, 1, tzinfo=UTC),
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
        "schema_version": 1,
        "ground_truth": "editor",
        "cutoff_at": "2026-01-01T00:00:00+00:00",
        "train": [],
        "test": [],
    }
