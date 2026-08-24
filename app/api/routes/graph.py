from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_site_access
from app.api.pagination import MAX_PAGE_SIZE
from app.models import GraphFeature, Site, Suggestion
from app.schemas.graph import (
    GraphCountsOut,
    GraphFeatureOut,
    GraphNeighborhoodEdgeOut,
    GraphNeighborhoodNodeOut,
    GraphNeighborhoodOut,
    GraphNeighborhoodRequest,
    GraphProposedEdgeOut,
    GraphNetworkEdgeOut,
    GraphNetworkOut,
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


@router.get("/{site_id}/graph/network", response_model=GraphNetworkOut)
def get_graph_network(
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> GraphNetworkOut:
    """Return the complete active-site graph and its structural features."""

    snapshot, created = ensure_graph_snapshot(db, site.id)
    if created:
        db.commit()
        db.refresh(snapshot)
    else:
        db.rollback()

    computation = graph_computation_from_snapshot(db, snapshot)
    return GraphNetworkOut(
        site_id=site.id,
        snapshot_id=snapshot.id,
        graph_version=snapshot.graph_version,
        computed_at=snapshot.computed_at,
        article_count=computation.article_count,
        edge_count=computation.edge_count,
        orphan_count=sum(feature.orphan for feature in computation.features),
        underlinked_count=sum(feature.underlinked for feature in computation.features),
        hub_count=sum(feature.hub for feature in computation.features),
        saturated_count=sum(feature.saturated for feature in computation.features),
        nodes=[
            GraphFeatureOut(
                article_id=feature.article_id,
                article_url=feature.article_url,
                article_title=feature.article_title,
                in_degree=feature.in_degree,
                out_degree=feature.out_degree,
                orphan=feature.orphan,
                underlinked=feature.underlinked,
                hub=feature.hub,
                saturated=feature.saturated,
                hub_score=feature.hub_score,
                saturation_score=feature.saturation_score,
            )
            for feature in computation.features
        ],
        edges=[
            GraphNetworkEdgeOut(source_article_id=source_id, target_article_id=target_id)
            for source_id, target_id in computation.edges
        ],
    )


@router.post("/{site_id}/graph/neighborhood", response_model=GraphNeighborhoodOut)
def get_graph_neighborhood(
    payload: GraphNeighborhoodRequest,
    site: Site = Depends(require_site_access),
    db: Session = Depends(get_db),
) -> GraphNeighborhoodOut:
    """Return a bounded, read-only graph view for selected suggestions."""

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

    computation = graph_computation_from_snapshot(db, snapshot)
    features = {feature.article_id: feature for feature in computation.features}
    node_ids = set(features)
    current_edges = set(computation.edges)
    focus_ids: set[int] = set()
    skipped_ids: set[int] = set()
    proposed_edges: list[GraphProposedEdgeOut] = []
    external_count = 0
    inactive_count = 0
    duplicate_count = 0

    for suggestion_id in requested_ids:
        row = by_id[suggestion_id]
        if row.target_article_id is None:
            external_count += 1
            skipped_ids.add(suggestion_id)
            continue
        pair = (row.source_article_id, row.target_article_id)
        if pair[0] not in node_ids or pair[1] not in node_ids:
            inactive_count += 1
            skipped_ids.add(suggestion_id)
            continue

        focus_ids.update(pair)
        status = "already_present" if pair in current_edges else "new"
        if status == "already_present":
            duplicate_count += 1
        proposed_edges.append(
            GraphProposedEdgeOut(
                suggestion_id=suggestion_id,
                source_article_id=pair[0],
                target_article_id=pair[1],
                status=status,
            )
        )

    # The focus set is always kept intact so every selected suggestion can be
    # explained. Only one-hop neighbors are subject to the visual cap.
    neighbor_ids = {
        article_id
        for source_id, target_id in computation.edges
        for article_id in (
            (target_id if source_id in focus_ids else None),
            (source_id if target_id in focus_ids else None),
        )
        if article_id is not None and article_id not in focus_ids
    }
    ordered_neighbors = sorted(
        neighbor_ids,
        key=lambda article_id: (
            -(features[article_id].in_degree + features[article_id].out_degree),
            article_id,
        ),
    )
    neighbor_capacity = max(0, payload.max_nodes - len(focus_ids))
    included_ids = focus_ids | set(ordered_neighbors[:neighbor_capacity])
    truncated = len(focus_ids) > payload.max_nodes or len(ordered_neighbors) > neighbor_capacity

    warnings: list[str] = []
    if external_count:
        warnings.append(
            f"{external_count} selected external suggestion(s) are not represented in the internal graph."
        )
    if inactive_count:
        warnings.append(
            f"{inactive_count} suggestion(s) target inactive or missing articles and were excluded."
        )
    if duplicate_count:
        warnings.append(f"{duplicate_count} selected link(s) already exist in the current graph.")
    if truncated:
        warnings.append(
            f"Showing the selected articles and the {neighbor_capacity} strongest nearby articles; "
            "open a smaller selection to inspect more neighbors."
        )

    return GraphNeighborhoodOut(
        site_id=site.id,
        snapshot_id=snapshot.id,
        graph_version=snapshot.graph_version,
        computed_at=snapshot.computed_at,
        requested_suggestion_ids=requested_ids,
        skipped_suggestion_ids=sorted(skipped_ids),
        nodes=[
            GraphNeighborhoodNodeOut(
                article_id=feature.article_id,
                article_url=feature.article_url,
                article_title=feature.article_title,
                in_degree=feature.in_degree,
                out_degree=feature.out_degree,
                orphan=feature.orphan,
                underlinked=feature.underlinked,
                hub=feature.hub,
                saturated=feature.saturated,
                hub_score=feature.hub_score,
                saturation_score=feature.saturation_score,
                focus=feature.article_id in focus_ids,
            )
            for feature in sorted(
                (features[article_id] for article_id in included_ids),
                key=lambda item: item.article_id,
            )
        ],
        existing_edges=[
            GraphNeighborhoodEdgeOut(source_article_id=source_id, target_article_id=target_id)
            for source_id, target_id in computation.edges
            if source_id in included_ids and target_id in included_ids
        ],
        proposed_edges=proposed_edges,
        truncated=truncated,
        warnings=warnings,
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
