"""expire low-value pending suggestions

Revision ID: e3a7c1d5b902
Revises: d8b2f6a4c901
Create Date: 2026-08-04
"""

from alembic import op

revision = "e3a7c1d5b902"
down_revision = "d8b2f6a4c901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE suggestions AS suggestion
        SET status = 'expired'
        FROM articles AS target
        WHERE target.id = suggestion.target_article_id
          AND suggestion.status = 'pending'
          AND (
              lower(btrim(target.title)) IN (
                  'login', 'log in', 'sign in', 'sign up', 'register', 'registration',
                  'dashboard', 'my account', 'cart', 'shopping cart', 'checkout',
                  'privacy policy', 'terms of service', 'terms of use', 'cookie policy',
                  'support portal')
              OR lower(target.url) ~ '(^|/)(login|log-in|sign-in|sign-up|signup|register|registration|dashboard|my-account|cart|shopping-cart|checkout|privacy-policy|terms-of-service|terms-of-use|cookie-policy|support-portal)(/|[?#]|$)'
          )
        """
    )


def downgrade() -> None:
    # Expiration is intentionally irreversible: a later unrelated expiration
    # is indistinguishable from one performed by this cleanup.
    pass
