"""add suggestion placement context

Adds the columns behind the review drawer's "Placement context" card: the
passage of the source article the link belongs in, and the timestamp recording
that the model was asked.

`anchor_text` and `llm_model` already exist and are still null on every row —
they were added for this feature and are first written by the placement service
this revision supports.

`placement_generated_at` carries the distinction the content columns cannot: a
row where the model ran but found no suitable passage looks exactly like a row
nobody has generated yet, and without the timestamp the first kind would pay for
a fresh completion every time an editor opened it.

Downgrading discards generated placements. That is acceptable here in a way it
was not for `score_components`: no queue row becomes unreadable without it, the
data is regenerable on demand from an article the engine still stores, and
nothing an editor decided is recorded in these columns.

Revision ID: a9c4e7f21b58
Revises: c5f3a91b7d24
Create Date: 2026-08-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a9c4e7f21b58"
down_revision = "c5f3a91b7d24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("suggestions", sa.Column("placement_context", sa.Text(), nullable=True))
    op.add_column(
        "suggestions",
        sa.Column("placement_generated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("suggestions", "placement_generated_at")
    op.drop_column("suggestions", "placement_context")
