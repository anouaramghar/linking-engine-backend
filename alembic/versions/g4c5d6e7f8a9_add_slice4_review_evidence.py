"""add Slice 4 review evidence fields

Revision ID: g4c5d6e7f8a9
Revises: f1b2c3d4e5f6
Create Date: 2026-08-14
"""

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "g4c5d6e7f8a9"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _create_legacy_lifecycle_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION record_suggestion_lifecycle_event()
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


def _create_snapshot_immutability_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_suggestion_ranking_snapshot_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.score_components IS DISTINCT FROM NEW.score_components
               OR OLD.retrieval_version IS DISTINCT FROM NEW.retrieval_version
               OR OLD.ranking_version IS DISTINCT FROM NEW.ranking_version
               OR OLD.final_rank IS DISTINCT FROM NEW.final_rank
               OR OLD.feature_snapshot IS DISTINCT FROM NEW.feature_snapshot THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = 'suggestion ranking snapshot is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def upgrade() -> None:
    op.add_column("suggestions", sa.Column("shown_at", sa.DateTime(timezone=True)))
    op.add_column("suggestions", sa.Column("last_shown_at", sa.DateTime(timezone=True)))
    op.add_column(
        "suggestions",
        sa.Column("exposure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("suggestions", sa.Column("reviewer_id", sa.String(length=255)))
    op.add_column("suggestions", sa.Column("rejection_reason", sa.String(length=40)))
    op.add_column("suggestions", sa.Column("retrieval_version", sa.String(length=80)))
    op.add_column("suggestions", sa.Column("ranking_version", sa.String(length=120)))
    op.add_column("suggestions", sa.Column("final_rank", sa.Integer()))
    op.add_column("suggestions", sa.Column("feature_snapshot", postgresql.JSONB()))
    op.create_index("ix_suggestions_shown_at", "suggestions", ["shown_at"])

    op.execute("DROP TRIGGER trg_suggestion_lifecycle_event ON suggestions")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION record_suggestion_lifecycle_event()
        RETURNS trigger AS $$
        DECLARE
            audit_actor text;
            lifecycle_event text;
            review_context jsonb;
            exposure_context jsonb;
            duration_ms bigint;
        BEGIN
            audit_actor := nullif(current_setting('linkmesh.audit_actor', true), '');
            review_context := coalesce(
                nullif(current_setting('linkmesh.review_context', true), '')::jsonb,
                '{}'::jsonb
            );
            exposure_context := coalesce(
                nullif(current_setting('linkmesh.exposure_context', true), '')::jsonb,
                '{}'::jsonb
            );

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
                        'score_components', NEW.score_components,
                        'retrieval_version', NEW.retrieval_version,
                        'ranking_version', NEW.ranking_version,
                        'final_rank', NEW.final_rank
                    ))
                );
                RETURN NEW;
            END IF;

            IF OLD.shown_at IS DISTINCT FROM NEW.shown_at
               AND NEW.shown_at IS NOT NULL THEN
                INSERT INTO suggestion_events (
                    suggestion_id, event_type, actor, details
                ) VALUES (
                    NEW.id,
                    'exposed',
                    coalesce(audit_actor, 'dashboard'),
                    jsonb_strip_nulls(jsonb_build_object(
                        'surface', coalesce(exposure_context->>'surface', 'queue'),
                        'shown_at', NEW.shown_at,
                        'exposure_count', NEW.exposure_count
                    ))
                );
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
            duration_ms := CASE
                WHEN NEW.status IN ('approved', 'rejected') THEN greatest(
                    0,
                    round(extract(epoch FROM (
                        current_timestamp - coalesce(NEW.shown_at, NEW.created_at)
                    )) * 1000)::bigint
                )
                ELSE NULL
            END;

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
                )) || CASE
                    WHEN NEW.status IN ('approved', 'rejected') THEN review_context
                    ELSE '{}'::jsonb
                END || CASE
                    WHEN NEW.status IN ('approved', 'rejected') THEN
                        jsonb_strip_nulls(jsonb_build_object(
                            'reviewer_id', NEW.reviewer_id,
                            'rejection_reason', NEW.rejection_reason,
                            'review_duration_ms', duration_ms,
                            'exposed', NEW.shown_at IS NOT NULL
                        ))
                    ELSE '{}'::jsonb
                END
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_suggestion_lifecycle_event
        AFTER INSERT OR UPDATE OF status, shown_at ON suggestions
        FOR EACH ROW EXECUTE FUNCTION record_suggestion_lifecycle_event()
        """
    )
    _create_snapshot_immutability_function()
    op.execute(
        """
        CREATE TRIGGER trg_suggestion_ranking_snapshot_immutable
        BEFORE UPDATE OF score_components, retrieval_version, ranking_version,
            final_rank, feature_snapshot ON suggestions
        FOR EACH ROW EXECUTE FUNCTION prevent_suggestion_ranking_snapshot_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_suggestion_ranking_snapshot_immutable ON suggestions")
    op.execute("DROP FUNCTION IF EXISTS prevent_suggestion_ranking_snapshot_mutation()")
    op.execute("DROP TRIGGER trg_suggestion_lifecycle_event ON suggestions")
    _create_legacy_lifecycle_function()
    op.execute(
        """
        CREATE TRIGGER trg_suggestion_lifecycle_event
        AFTER INSERT OR UPDATE OF status ON suggestions
        FOR EACH ROW EXECUTE FUNCTION record_suggestion_lifecycle_event()
        """
    )
    op.drop_index("ix_suggestions_shown_at", table_name="suggestions")
    op.drop_column("suggestions", "feature_snapshot")
    op.drop_column("suggestions", "final_rank")
    op.drop_column("suggestions", "ranking_version")
    op.drop_column("suggestions", "retrieval_version")
    op.drop_column("suggestions", "rejection_reason")
    op.drop_column("suggestions", "reviewer_id")
    op.drop_column("suggestions", "exposure_count")
    op.drop_column("suggestions", "last_shown_at")
    op.drop_column("suggestions", "shown_at")
