"""add suggestion lifecycle trace

Revision ID: f9a1c3e5b702
Revises: aac0c72b21a5
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f9a1c3e5b702"
down_revision = "aac0c72b21a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "suggestions",
        sa.Column(
            "trace_id",
            sa.String(length=36),
            server_default=sa.text("gen_random_uuid()::text"),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE suggestions
        SET trace_id = 'legacy-' || lpad(id::text, 29, '0')
        WHERE trace_id IS NULL
        """
    )
    op.alter_column(
        "suggestions",
        "trace_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_index("ix_suggestions_trace_id", "suggestions", ["trace_id"], unique=True)

    op.create_table(
        "suggestion_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "suggestion_id",
            sa.Integer(),
            sa.ForeignKey("suggestions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_suggestion_events_suggestion_id",
        "suggestion_events",
        ["suggestion_id"],
    )
    op.create_index(
        "ix_suggestion_events_suggestion_created",
        "suggestion_events",
        ["suggestion_id", "created_at", "id"],
    )

    # Existing rows start with an honest snapshot rather than a fabricated
    # generation timestamp. Future inserts and every status transition are
    # captured atomically by the trigger in the same transaction as the change.
    op.execute(
        """
        INSERT INTO suggestion_events (suggestion_id, event_type, actor, details)
        SELECT id,
               'imported',
               'migration',
               jsonb_strip_nulls(jsonb_build_object(
                   'status', status,
                   'method', method,
                   'score', score,
                   'score_components', score_components
               ))
        FROM suggestions
        """
    )

    op.execute(
        """
        CREATE FUNCTION record_suggestion_lifecycle_event()
        RETURNS trigger AS $$
        DECLARE
            audit_actor text;
            lifecycle_event text;
        BEGIN
            audit_actor := nullif(current_setting('linkmesh.audit_actor', true), '');

            IF TG_OP = 'INSERT' THEN
                INSERT INTO suggestion_events (
                    suggestion_id, event_type, actor, details
                ) VALUES (
                    NEW.id,
                    'generated',
                    coalesce(audit_actor, 'analysis-engine'),
                    jsonb_strip_nulls(jsonb_build_object(
                        'status', NEW.status,
                        'method', NEW.method,
                        'score', NEW.score,
                        'score_components', NEW.score_components
                    ))
                );
                RETURN NEW;
            END IF;

            IF OLD.status IS NOT DISTINCT FROM NEW.status THEN
                RETURN NEW;
            END IF;

            lifecycle_event := CASE NEW.status
                WHEN 'approved' THEN 'reviewed'
                WHEN 'rejected' THEN 'reviewed'
                WHEN 'pending' THEN 'restored'
                WHEN 'applying' THEN 'publishing'
                WHEN 'applied' THEN 'applied'
                WHEN 'failed' THEN 'failed'
                WHEN 'expired' THEN 'expired'
                ELSE 'status_changed'
            END;
            audit_actor := coalesce(
                audit_actor,
                CASE
                    WHEN NEW.status IN ('applying', 'applied', 'failed')
                        THEN 'publication-worker'
                    WHEN NEW.status = 'expired' THEN 'policy-engine'
                    ELSE 'system'
                END
            );

            INSERT INTO suggestion_events (
                suggestion_id, event_type, actor, details
            ) VALUES (
                NEW.id,
                lifecycle_event,
                audit_actor,
                jsonb_strip_nulls(jsonb_build_object(
                    'from_status', OLD.status,
                    'to_status', NEW.status,
                    'publish_outcome', NEW.publish_outcome,
                    'publish_error', NEW.publish_error
                ))
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_suggestion_lifecycle_event
        AFTER INSERT OR UPDATE OF status ON suggestions
        FOR EACH ROW EXECUTE FUNCTION record_suggestion_lifecycle_event()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_suggestion_lifecycle_event ON suggestions")
    op.execute("DROP FUNCTION record_suggestion_lifecycle_event()")
    op.drop_index(
        "ix_suggestion_events_suggestion_created",
        table_name="suggestion_events",
    )
    op.drop_index("ix_suggestion_events_suggestion_id", table_name="suggestion_events")
    op.drop_table("suggestion_events")
    op.drop_index("ix_suggestions_trace_id", table_name="suggestions")
    op.drop_column("suggestions", "trace_id")
