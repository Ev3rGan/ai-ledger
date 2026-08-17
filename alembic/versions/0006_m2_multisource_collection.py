"""Persist M2 Source Profile state and body-gated collection results.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collection_runs",
        sa.Column("operation_key", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_collection_runs_operation_key",
        "collection_runs",
        ["operation_key"],
    )

    op.drop_constraint(
        "ck_source_definition_collection_results_status",
        "source_definition_collection_results",
        type_="check",
    )
    op.drop_constraint(
        "ck_source_definition_collection_results_error_shape",
        "source_definition_collection_results",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_definition_collection_results_status",
        "source_definition_collection_results",
        "status IN ('success', 'empty', 'invalid-format', 'access-blocked', "
        "'temporary-failure', 'succeeded', 'failed')",
    )
    op.create_check_constraint(
        "ck_source_definition_collection_results_error_shape",
        "source_definition_collection_results",
        "(status IN ('success', 'empty', 'succeeded') AND error_code IS NULL "
        "AND error_message IS NULL) OR (status IN ('invalid-format', "
        "'access-blocked', 'temporary-failure', 'failed') AND error_code IS NOT NULL "
        "AND error_message IS NOT NULL)",
    )

    op.create_table(
        "source_profile_states",
        sa.Column("source_definition_id", sa.Uuid(), nullable=False),
        sa.Column("recent_result", sa.String(length=32), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=True),
        sa.Column("health", sa.String(length=32), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_collection_run_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "recent_result IN ('success', 'empty', 'invalid-format', "
            "'access-blocked', 'temporary-failure')",
            name="ck_source_profile_states_recent_result",
        ),
        sa.CheckConstraint(
            "health IN ('healthy', 'degraded', 'blocked')",
            name="ck_source_profile_states_health",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_source_profile_states_consecutive_failures",
        ),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definitions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["last_collection_run_id"],
            ["collection_runs.id"],
        ),
        sa.PrimaryKeyConstraint("source_definition_id"),
    )

    op.create_table(
        "source_candidate_results",
        sa.Column("collection_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_definition_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("article_status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "article_status IN ('body-valid', 'invalid-format', 'access-blocked', "
            "'temporary-failure')",
            name="ck_source_candidate_results_status",
        ),
        sa.CheckConstraint(
            "(article_status = 'body-valid' AND document_version_id IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL) OR "
            "(article_status <> 'body-valid' AND document_version_id IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL)",
            name="ck_source_candidate_results_shape",
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["collection_run_id"], ["collection_runs.id"]),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definitions.id"],
        ),
        sa.PrimaryKeyConstraint(
            "collection_run_id",
            "source_definition_id",
            "candidate_id",
        ),
    )
    op.execute(
        """
        CREATE TRIGGER protect_source_candidate_collection_result
        BEFORE INSERT OR UPDATE OR DELETE ON source_candidate_results
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_collection_run_child(
            'Source candidate collection result is immutable'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_unconditionally_immutable_candidate
        BEFORE UPDATE OR DELETE ON candidates
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Candidate is immutable'
        )
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_protect_collection_run()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               OR OLD.status <> 'running'
               OR NEW.status NOT IN ('complete', 'partial', 'failed')
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.retry_of_run_id IS DISTINCT FROM OLD.retry_of_run_id
               OR NEW.operation_key IS DISTINCT FROM OLD.operation_key
               OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
                RAISE EXCEPTION 'completed Collection Run is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER protect_source_candidate_collection_result "
        "ON source_candidate_results"
    )
    op.execute(
        "DROP TRIGGER protect_unconditionally_immutable_candidate ON candidates"
    )
    op.execute(
        "DROP TRIGGER protect_source_definition_collection_result "
        "ON source_definition_collection_results"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_protect_collection_run()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               OR OLD.status <> 'running'
               OR NEW.status NOT IN ('complete', 'partial', 'failed')
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.retry_of_run_id IS DISTINCT FROM OLD.retry_of_run_id
               OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
                RAISE EXCEPTION 'completed Collection Run is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.drop_table("source_candidate_results")
    op.drop_table("source_profile_states")
    op.execute(
        """
        UPDATE source_definition_collection_results
        SET status = CASE
                WHEN status IN ('success', 'empty') THEN 'succeeded'
                WHEN status IN (
                    'invalid-format', 'access-blocked', 'temporary-failure'
                ) THEN 'failed'
                ELSE status
            END,
            error_code = CASE
                WHEN status IN ('success', 'empty') THEN NULL
                ELSE error_code
            END,
            error_message = CASE
                WHEN status IN ('success', 'empty') THEN NULL
                ELSE error_message
            END
        """
    )
    op.drop_constraint(
        "ck_source_definition_collection_results_error_shape",
        "source_definition_collection_results",
        type_="check",
    )
    op.drop_constraint(
        "ck_source_definition_collection_results_status",
        "source_definition_collection_results",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_definition_collection_results_status",
        "source_definition_collection_results",
        "status IN ('succeeded', 'failed')",
    )
    op.create_check_constraint(
        "ck_source_definition_collection_results_error_shape",
        "source_definition_collection_results",
        "(status = 'succeeded' AND error_code IS NULL AND error_message IS NULL) "
        "OR (status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL)",
    )
    op.drop_constraint(
        "uq_collection_runs_operation_key",
        "collection_runs",
        type_="unique",
    )
    op.drop_column("collection_runs", "operation_key")
    op.execute(
        """
        CREATE TRIGGER protect_source_definition_collection_result
        BEFORE INSERT OR UPDATE OR DELETE ON source_definition_collection_results
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_collection_run_child(
            'Collection Run result is immutable'
        )
        """
    )
