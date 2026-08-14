from datetime import datetime

from pydantic import BaseModel, Field

MAX_GRAPH_SIMULATION_SUGGESTIONS = 500


class GraphFeatureOut(BaseModel):
    article_id: int
    article_url: str
    article_title: str
    in_degree: int
    out_degree: int
    orphan: bool
    underlinked: bool
    hub: bool
    saturated: bool
    hub_score: float
    saturation_score: float


class GraphSummaryOut(BaseModel):
    site_id: int
    snapshot_id: int
    source_ingestion_run_id: int | None
    algorithm_version: str
    graph_version: str
    computed_at: datetime
    article_count: int
    edge_count: int
    orphan_count: int
    underlinked_count: int
    hub_count: int
    saturated_count: int
    items: list[GraphFeatureOut]
    limit: int
    offset: int


class GraphSimulationRequest(BaseModel):
    suggestion_ids: list[int] = Field(
        min_length=1,
        max_length=MAX_GRAPH_SIMULATION_SUGGESTIONS,
    )


class GraphCountsOut(BaseModel):
    active_articles: int
    active_links: int
    orphan_count: int
    underlinked_count: int
    hub_count: int
    saturated_count: int
    max_in_degree: int
    max_out_degree: int


class GraphSimulationOut(BaseModel):
    site_id: int
    snapshot_id: int
    graph_version: str
    requested_suggestion_ids: list[int]
    applied_suggestion_ids: list[int]
    skipped_suggestion_ids: list[int]
    duplicate_edge_count: int
    before: GraphCountsOut
    after: GraphCountsOut
    orphan_delta: int
    underlinked_delta: int
    newly_connected_article_ids: list[int]
    newly_saturated_article_ids: list[int]
    target_concentration: float
    warnings: list[str]
