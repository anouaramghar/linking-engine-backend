"""join placement context and hybrid content pool migration chains

Both branches added migrations on top of c5f3a91b7d24, leaving two heads. This
revision joins them and carries no DDL of its own.

A merge revision rather than relinking one chain onto the other, because the
version table records only the head revision: a database already stamped
b6d1e8f4a237 would treat a relinked chain as fully applied and silently skip the
five content-pool migrations. The development and test databases here are in
exactly that state. Merging leaves both ancestries intact, so `upgrade head`
still applies whichever side a given database has not seen.

Revision ID: aac0c72b21a5
Revises: b6d1e8f4a237, e3a7c1d5b902
Create Date: 2026-08-05
"""

revision = "aac0c72b21a5"
down_revision = ("b6d1e8f4a237", "e3a7c1d5b902")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
