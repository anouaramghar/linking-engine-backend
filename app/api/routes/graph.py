from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_site_access
from app.api.pagination import MAX_PAGE_SIZE
from app.models import GraphFeature, Site, Suggestion
from app.schemas.graph import (
    GraphCountsOut,
    GraphFeatureOut,
    GraphSimulationOut,
    GraphSimulationRequest,
    GraphSummaryOut,
)
from app.services.graph_service import (
    ensure_graph_snapshot,
    graph_computation_from_snapshot,
    simulate_graph,
)

router = APIRouter(prefix="/sites", tags=["graph"])


@router.get("/{site_id}/graph/summary", response_model=GraphSummaryOut)
def get_graph_summary(
    site: Site = Depends(require_site_access),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> GraphSummaryOut:
    """Return the current reproducible graph observation for one site."""

    snapshot, created = ensure_graph_snapshot(db, site.id)
    # GET is allowed to materialize a derived observation, but it never mutates
    # articles, links, suggestions, or publication state. The same graph digest
    # returns the same snapshot on every subsequent read.
    if created:
        db.commit()
        db.refresh(snapshot)
    else:
        db.rollback()

    rows = db.scalars(
        select(GraphFeature)
        .where(GraphFeature.snapshot_id == snapshot.id)
        .order_by(GraphFeature.article_id)
        .limit(limit)
        .offset(offset)
    ).all()
    return GraphSummaryOut(
        site_id=site.id,
        snapshot_id=snapshot.id,
        source_ingestion_run_id=snapshot.source_ingestion_run_id,
        algorithm_version=snapshot.algorithm_version,
        graph_version=snapshot.graph_version,
        computed_at=snapshot.computed_at,
        article_count=snapshot.article_count,
        edge_count=snapshot.edge_count,
        orphan_count=snapshot.orphan_count,
        underlinked_count=snapshot.underlinked_count,
        hub_count=snapshot.hub_count,
        saturated_count=snapshot.saturated_count,
        items=[
            GraphFeatureOut(
                article_id=row.article_id,
                article_url=row.article_url,
                article_title=row.article_title,
                in_degree=row.in_degree,
                out_degree=row.out_degree,
                orphan=row.orphan_flag,
                underlinked=row.underlinked_flag,
                hub=row.hub_flag,
                saturated=row.saturated_flag,
                hub_score=row.hub_score,
                saturation_score=row.saturation_score,
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.post("/{site_id}/graph/simulations", response_model=GraphSimulationOut)
def simulate_graph_application(
    payload: GraphSimulationRequest,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> GraphSimulationOut:
    """Show structural consequences of selected suggestions without publishing."""

    requested_ids = list(dict.fromkeys(payload.suggestion_ids))
    snapshot, created = ensure_graph_snapshot(db, site.id)
    if created:
        db.commit()
        db.refresh(snapshot)
    else:
        db.rollback()

    rows = db.scalars(
        select(Suggestion).where(
            Suggestion.site_id == site.id,
            Suggestion.id.in_(requested_ids),
        )
    ).all()
    by_id = {row.id: row for row in rows}
    missing = [suggestion_id for suggestion_id in requested_ids if suggestion_id not in by_id]
    if missing:
        raise HTTPException(404, f"suggestion(s) not found for site {site.id}: {missing}")
    not_approved = [row.id for row in rows if row.status != "approved"]
    if not_approved:
        raise HTTPException(
            409,
            f"only approved suggestions can be simulated; rejected ids: {not_approved}",
        )

    computation = graph_computation_from_snapshot(db, snapshot)
    node_ids = {feature.article_id for feature in computation.features}
    proposed_pairs: list[tuple[int, int]] = []
    pair_to_suggestion: dict[tuple[int, int], int] = {}
    skipped_ids: set[int] = set()
    for suggestion_id in requested_ids:
        row = by_id[suggestion_id]
        pair = (row.source_article_id, row.target_article_id)
        if row.target_article_id is None:
            skipped_ids.add(suggestion_id)
            continue
        if pair[0] not in node_ids or pair[1] not in node_ids:
            skipped_ids.add(suggestion_id)
            continue
        proposed_pairs.append(pair)
        pair_to_suggestion.setdefault(pair, suggestion_id)

    simulation = simulate_graph(computation, proposed_pairs)
    applied_ids = [pair_to_suggestion[pair] for pair in simulation.applied_edges]
    skipped_ids.update(
        suggestion_id for suggestion_id in requested_ids if suggestion_id not in set(applied_ids)
    )
    warnings = list(simulation.warnings)
    external_count = sum(
        1 for suggestion_id in requested_ids if by_id[suggestion_id].target_article_id is None
    )
    if external_count:
        warnings.append(
            f"{external_count} external suggestion(s) were excluded; this simulation covers internal links only."
        )
    inactive_count = sum(
        1
        for suggestion_id in requested_ids
        if by_id[suggestion_id].target_article_id is not None
        and (
            by_id[suggestion_id].source_article_id not in node_ids
            or by_id[suggestion_id].target_article_id not in node_ids
        )
    )
    if inactive_count:
        warnings.append(
            f"{inactive_count} suggestion(s) target inactive or missing articles and were excluded."
        )

    return GraphSimulationOut(
        site_id=site.id,
        snapshot_id=snapshot.id,
        graph_version=snapshot.graph_version,
        requested_suggestion_ids=requested_ids,
        applied_suggestion_ids=applied_ids,
        skipped_suggestion_ids=sorted(skipped_ids),
        duplicate_edge_count=len(simulation.duplicate_edges),
        before=GraphCountsOut(**simulation.before.__dict__),
        after=GraphCountsOut(**simulation.after.__dict__),
        orphan_delta=simulation.after.orphan_count - simulation.before.orphan_count,
        underlinked_delta=simulation.after.underlinked_count - simulation.before.underlinked_count,
        newly_connected_article_ids=list(simulation.newly_connected_article_ids),
        newly_saturated_article_ids=list(simulation.newly_saturated_article_ids),
        target_concentration=simulation.target_concentration,
        warnings=warnings,
    )
