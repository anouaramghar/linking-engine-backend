"""Publication accounting: per-suggestion outcome, attempt count, and failure reason.

Three gaps the publication worker could not close without schema:

- `applied` says a suggestion was published but not *how*. Whether a link landed
  in the prose or in an appended "Read also" block is the number that says
  whether paying for placement generation is worth it, and it did not exist.
- A permanently broken suggestion — a post locked by a plugin, a revoked
  application password — rolled back to `approved` and was retried by every
  later publication run, for ever. `publish_attempts` gives it somewhere to
  stop: past the configured limit the row moves to the new `failed` status.
- `internal_links.anchor_text` has always existed and was never written, because
  connectors discarded the anchor at their boundary. That part is a code fix,
  but no backfill is possible: the column stays null for everything crawled
  before this.

`status` is a non-native enum — a plain VARCHAR with no CHECK constraint — so
adding `failed` to the application-side tuple needs no schema change here.
"""

import sqlalchemy as sa
from alembic import op

revision = "b6d1e8f4a237"
down_revision = "a9c4e7f21b58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("suggestions", sa.Column("publish_outcome", sa.String(20), nullable=True))
    op.add_column(
        "suggestions",
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("suggestions", sa.Column("publish_error", sa.Text(), nullable=True))


def downgrade() -> None:
    # 'failed' only exists above this revision; send those rows back to where
    # they came from rather than leaving a status the code below cannot read.
    op.execute("UPDATE suggestions SET status = 'approved' WHERE status = 'failed'")
    op.drop_column("suggestions", "publish_error")
    op.drop_column("suggestions", "publish_attempts")
    op.drop_column("suggestions", "publish_outcome")
