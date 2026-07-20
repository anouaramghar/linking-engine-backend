"""add current ingestion snapshots

Revision ID: 4d3f7b2c9a10
Revises: 689fe839affd
Create Date: 2026-07-16 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "4d3f7b2c9a10"
down_revision = "689fe839affd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("articles", sa.Column("last_seen_run_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_articles_last_seen_run_id_ingestion_runs",
        "articles",
        "ingestion_runs",
        ["last_seen_run_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "internal_links",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("internal_links", sa.Column("last_seen_run_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_internal_links_last_seen_run_id_ingestion_runs",
        "internal_links",
        "ingestion_runs",
        ["last_seen_run_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("article_taxonomies", sa.Column("last_seen_run_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_article_taxonomies_last_seen_run_id_ingestion_runs",
        "article_taxonomies",
        "ingestion_runs",
        ["last_seen_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_article_taxonomies_last_seen_run_id_ingestion_runs",
        "article_taxonomies",
        type_="foreignkey",
    )
    op.drop_column("article_taxonomies", "last_seen_run_id")

    op.drop_constraint(
        "fk_internal_links_last_seen_run_id_ingestion_runs",
        "internal_links",
        type_="foreignkey",
    )
    op.drop_column("internal_links", "last_seen_run_id")
    op.drop_column("internal_links", "is_active")

    op.drop_constraint(
        "fk_articles_last_seen_run_id_ingestion_runs",
        "articles",
        type_="foreignkey",
    )
    op.drop_column("articles", "last_seen_run_id")
    op.drop_column("articles", "is_active")
