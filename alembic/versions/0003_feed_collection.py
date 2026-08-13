"""Collect approved RSS and Atom Source Definitions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("published_at_raw", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("updated_at_raw", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "source_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("publisher", sa.String(length=255), nullable=False),
        sa.Column("entry_point", sa.String(length=2048), nullable=False),
        sa.Column("audit_version", sa.String(length=255), nullable=False),
        sa.Column("activation_conclusion", sa.String(length=32), nullable=False),
        sa.Column("storage_policy", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "activation_conclusion = 'approved'",
            name="ck_source_definitions_approved",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("audit_version", "entry_point"),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("retry_of_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('complete', 'partial', 'failed')",
            name="ck_collection_runs_status",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at",
            name="ck_collection_runs_time_order",
        ),
        sa.ForeignKeyConstraint(["retry_of_run_id"], ["collection_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "source_definition_collection_results",
        sa.Column("collection_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_definition_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_source_definition_collection_results_status",
        ),
        sa.CheckConstraint(
            "candidate_count >= 0",
            name="ck_source_definition_collection_results_candidate_count",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND error_code IS NULL AND error_message IS NULL) "
            "OR (status = 'failed' AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="ck_source_definition_collection_results_error_shape",
        ),
        sa.ForeignKeyConstraint(["collection_run_id"], ["collection_runs.id"]),
        sa.ForeignKeyConstraint(["source_definition_id"], ["source_definitions.id"]),
        sa.PrimaryKeyConstraint("collection_run_id", "source_definition_id"),
    )
    op.create_table(
        "collection_discoveries",
        sa.Column("collection_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_definition_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["collection_run_id"], ["collection_runs.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.ForeignKeyConstraint(["source_definition_id"], ["source_definitions.id"]),
        sa.PrimaryKeyConstraint(
            "collection_run_id",
            "source_definition_id",
            "document_version_id",
        ),
    )

    op.execute(
        """
        CREATE FUNCTION ai_intel_reject_immutable_collection_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '%', TG_ARGV[0];
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_activated_source_definition
        BEFORE UPDATE OR DELETE ON source_definitions
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'activated Source Definition is immutable'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_completed_collection_run
        BEFORE UPDATE OR DELETE ON collection_runs
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'completed Collection Run is immutable'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_source_definition_collection_result
        BEFORE UPDATE OR DELETE ON source_definition_collection_results
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Collection Run result is immutable'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_collection_discovery
        BEFORE UPDATE OR DELETE ON collection_discoveries
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Collection Run discovery is immutable'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER protect_collection_discovery ON collection_discoveries")
    op.execute(
        "DROP TRIGGER protect_source_definition_collection_result "
        "ON source_definition_collection_results"
    )
    op.execute("DROP TRIGGER protect_completed_collection_run ON collection_runs")
    op.execute(
        "DROP TRIGGER protect_activated_source_definition ON source_definitions"
    )
    op.execute("DROP FUNCTION ai_intel_reject_immutable_collection_write()")
    op.drop_table("collection_discoveries")
    op.drop_table("source_definition_collection_results")
    op.drop_table("collection_runs")
    op.drop_table("source_definitions")
    op.drop_column("document_versions", "updated_at_raw")
    op.drop_column("document_versions", "updated_at")
    op.drop_column("document_versions", "published_at_raw")
    op.drop_column("document_versions", "published_at")
