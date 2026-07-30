"""add site suggestion mode

Revision ID: 6a7d9e2c4b10
Revises: f3a8b1c2d4e5
Create Date: 2026-07-29 18:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "6a7d9e2c4b10"
down_revision = "f3a8b1c2d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column(
            "suggestion_mode",
            sa.Enum(
                "standard",
                "experimental",
                name="site_suggestion_mode",
                native_enum=False,
                length=20,
            ),
            server_default="standard",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Refuse while any site is still enrolled, before changing anything.

    Dropping the column does not just remove a schema element: it silently
    forgets which sites an operator put on the experimental method, and the
    pre-pilot application has no way to rediscover that. Un-enrolling is a
    decision, so it is made deliberately rather than as a side effect.
    """
    connection = op.get_bind()
    if connection.dialect.has_table(connection, "sites"):
        enrolled = connection.execute(
            sa.text("SELECT id FROM sites WHERE suggestion_mode = 'experimental' ORDER BY id")
        ).scalars().all()
        if enrolled:
            raise RuntimeError(
                "Refusing to downgrade: site(s) "
                + ", ".join(str(site_id) for site_id in enrolled)
                + " are still on the experimental suggestion method.\n"
                "Set each one back to Standard first — in the dashboard, or with\n"
                "    UPDATE sites SET suggestion_mode = 'standard' WHERE id = N;\n"
                "so the rollback cannot quietly discard the rollout state."
            )
    op.drop_column("sites", "suggestion_mode")
