"""add deterministic graph snapshots

Revision ID: f1b2c3d4e5f6
Revises: f0a2b3c4d5e6
Create Date: 2026-08-14 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "f1b2c3d4e5f6"
down_revision = "f0a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("source_ingestion_run_id", sa.Integer(), nullable=True),
        sa.Column("algorithm_version", sa.String(length=40), nullable=False),
        sa.Column("graph_version", sa.String(length=64), nullable=False),
        sa.Column("article_count", sa.Integer(), nullable=False),
        sa.Column("edge_count", sa.Integer(), nullable=False),
        sa.Column("orphan_count", sa.Integer(), nullable=False),
        sa.Column("underlinked_count", sa.Integer(), nullable=False),
        sa.Column("hub_count", sa.Integer(), nullable=False),
        sa.Column("saturated_count", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_ingestion_run_id"],
            ["ingestion_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "graph_version", name="uq_graph_snapshots_site_version"),
    )
    op.create_index("ix_graph_snapshots_site_id", "graph_snapshots", ["site_id"])
    op.create_index(
        "ix_graph_snapshots_site_computed",
        "graph_snapshots",
        ["site_id", "computed_at", "id"],
    )
    op.create_index(
        "ix_graph_snapshots_source_ingestion_run_id",
        "graph_snapshots",
        ["source_ingestion_run_id"],
    )
    op.create_index("ix_graph_snapshots_computed_at", "graph_snapshots", ["computed_at"])

    op.create_table(
        "graph_features",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("article_url", sa.Text(), nullable=False),
        sa.Column("article_title", sa.Text(), nullable=False),
        sa.Column("in_degree", sa.Integer(), nullable=False),
        sa.Column("out_degree", sa.Integer(), nullable=False),
        sa.Column("orphan_flag", sa.Boolean(), nullable=False),
        sa.Column("underlinked_flag", sa.Boolean(), nullable=False),
        sa.Column("hub_flag", sa.Boolean(), nullable=False),
        sa.Column("saturated_flag", sa.Boolean(), nullable=False),
        sa.Column("hub_score", sa.Float(), nullable=False),
        sa.Column("saturation_score", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["graph_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "article_id", name="uq_graph_features_snapshot_article"),
    )
    op.create_index("ix_graph_features_snapshot_id", "graph_features", ["snapshot_id"])
    op.create_index("ix_graph_features_article_id", "graph_features", ["article_id"])
    op.create_index(
        "ix_graph_features_snapshot_orphan",
        "graph_features",
        ["snapshot_id", "orphan_flag"],
    )
    op.create_index(
        "ix_graph_features_snapshot_underlinked",
        "graph_features",
        ["snapshot_id", "underlinked_flag"],
    )


def downgrade() -> None:
    op.drop_index("ix_graph_features_snapshot_underlinked", table_name="graph_features")
    op.drop_index("ix_graph_features_snapshot_orphan", table_name="graph_features")
    op.drop_index("ix_graph_features_article_id", table_name="graph_features")
    op.drop_index("ix_graph_features_snapshot_id", table_name="graph_features")
    op.drop_table("graph_features")

    op.drop_index("ix_graph_snapshots_computed_at", table_name="graph_snapshots")
    op.drop_index("ix_graph_snapshots_source_ingestion_run_id", table_name="graph_snapshots")
    op.drop_index("ix_graph_snapshots_site_computed", table_name="graph_snapshots")
    op.drop_index("ix_graph_snapshots_site_id", table_name="graph_snapshots")
    op.drop_table("graph_snapshots")
