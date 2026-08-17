"""Give the dashboard one privileged admin group.

Approval used to be the only gate, so every approved account could approve and
revoke every other one. The team lead asked for approve/revoke to belong to a
single admin group instead.

The backfill matters as much as the column: a deployment that upgrades into
"nobody is an admin" can never admit anyone again. Before this migration every
approved account held the power, so exactly one of them keeps it — the
bootstrap account if there is one, otherwise the earliest request. Setting
``DASHBOARD_BOOTSTRAP_ADMIN_ID`` and restarting the bot is the recovery path
either way.

Revision ID: c9a4e17b3d52
Revises: c3e5f7a9b201
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c9a4e17b3d52"
down_revision: str | Sequence[str] | None = "c3e5f7a9b201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dashboard_users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    # `IS NOT DISTINCT FROM` rather than `=`: approved_by is nullable, and a
    # NULL comparison would sort first under DESC in PostgreSQL.
    op.execute(
        sa.text(
            """
            UPDATE dashboard_users
               SET is_admin = true
             WHERE id = (
                   SELECT id
                     FROM dashboard_users
                    WHERE status = 'approved'
                    ORDER BY (approved_by IS NOT DISTINCT FROM 'bootstrap') DESC,
                             requested_at ASC,
                             id ASC
                    LIMIT 1
                   )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("dashboard_users", "is_admin")
