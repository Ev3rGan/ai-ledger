"""Persist the versioned M2 source universe and evidence policy.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_source_definitions_approved",
        "source_definitions",
        type_="check",
    )
    op.add_column(
        "source_definitions",
        sa.Column(
            "acceptance_group",
            sa.String(length=32),
            nullable=False,
            server_default="legacy",
        ),
    )
    op.add_column(
        "source_definitions",
        sa.Column(
            "contribution_role",
            sa.String(length=64),
            nullable=False,
            server_default="Legacy",
        ),
    )
    op.add_column(
        "source_definitions",
        sa.Column(
            "evidence_eligibility",
            sa.String(length=32),
            nullable=False,
            server_default="body-valid",
        ),
    )
    op.add_column(
        "source_definitions",
        sa.Column(
            "body_eligibility",
            sa.Text(),
            nullable=False,
            server_default="Legacy body-valid policy",
        ),
    )
    op.add_column(
        "source_definitions",
        sa.Column(
            "pause_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "source_definitions",
        sa.Column(
            "expected_contribution",
            sa.Text(),
            nullable=False,
            server_default="Legacy source contribution",
        ),
    )
    op.add_column(
        "source_definitions",
        sa.Column(
            "overlap_rationale",
            sa.Text(),
            nullable=False,
            server_default="Legacy source overlap policy",
        ),
    )
    op.create_check_constraint(
        "ck_source_definitions_activation",
        "source_definitions",
        "activation_conclusion IN ('approved', 'disabled')",
    )
    op.create_check_constraint(
        "ck_source_definitions_evidence_eligibility",
        "source_definitions",
        "evidence_eligibility IN ('body-valid', 'policy-valid-structured', 'never')",
    )
    op.create_check_constraint(
        "ck_source_definitions_pause_state",
        "source_definitions",
        "pause_state IN ('active', 'authorization-required')",
    )

    op.add_column(
        "source_profile_states",
        sa.Column(
            "pause_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_check_constraint(
        "ck_source_profile_states_pause_state",
        "source_profile_states",
        "pause_state IN ('active', 'authorization-required')",
    )

    op.drop_constraint(
        "ck_source_candidate_results_status",
        "source_candidate_results",
        type_="check",
    )
    op.drop_constraint(
        "ck_source_candidate_results_shape",
        "source_candidate_results",
        type_="check",
    )
    op.add_column(
        "source_candidate_results",
        sa.Column(
            "evidence_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "source_candidate_results",
        sa.Column(
            "eligibility_kind",
            sa.String(length=32),
            nullable=False,
            server_default="ineligible",
        ),
    )
    op.execute(
        "DROP TRIGGER protect_source_candidate_collection_result "
        "ON source_candidate_results"
    )
    op.execute(
        """
        UPDATE source_candidate_results
        SET evidence_eligible = true,
            eligibility_kind = 'body-valid'
        WHERE article_status = 'body-valid'
        """
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
    op.create_check_constraint(
        "ck_source_candidate_results_status",
        "source_candidate_results",
        "article_status IN ('body-valid', 'policy-valid-structured', "
        "'metadata-only', 'signal-only', 'invalid-format', 'access-blocked', "
        "'temporary-failure')",
    )
    op.create_check_constraint(
        "ck_source_candidate_results_shape",
        "source_candidate_results",
        "(article_status IN ('body-valid', 'policy-valid-structured') "
        "AND document_version_id IS NOT NULL AND error_code IS NULL "
        "AND error_message IS NULL AND evidence_eligible = true "
        "AND eligibility_kind = article_status) OR "
        "(article_status IN ('metadata-only', 'signal-only') "
        "AND document_version_id IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND evidence_eligible = false "
        "AND eligibility_kind = 'ineligible') OR "
        "(article_status IN ('invalid-format', 'access-blocked', 'temporary-failure') "
        "AND document_version_id IS NULL AND error_code IS NOT NULL "
        "AND error_message IS NOT NULL AND evidence_eligible = false "
        "AND eligibility_kind = 'ineligible')",
    )

    op.create_table(
        "source_specific_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_definition_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("record_kind", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("external_version", sa.String(length=255), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("policy_metadata", sa.JSON(), nullable=False),
        sa.Column("structured_metadata", sa.JSON(), nullable=False),
        sa.Column("evidence_eligible", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(evidence_eligible = true AND document_version_id IS NOT NULL) OR "
            "(evidence_eligible = false AND document_version_id IS NULL)",
            name="ck_source_specific_records_evidence_document",
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definitions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_definition_id",
            "record_kind",
            "external_id",
            "external_version",
            "record_hash",
            name="uq_source_specific_record_identity",
        ),
    )
    op.execute(
        """
        CREATE TRIGGER protect_source_specific_record
        BEFORE UPDATE OR DELETE ON source_specific_records
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Source-specific record is immutable'
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_specific_records)
               OR EXISTS (
                    SELECT 1
                    FROM source_candidate_results
                    WHERE article_status NOT IN (
                        'body-valid', 'invalid-format', 'access-blocked',
                        'temporary-failure'
                    )
               )
               OR EXISTS (
                    SELECT 1
                    FROM source_definitions
                    WHERE activation_conclusion <> 'approved'
               ) THEN
                RAISE EXCEPTION
                    '0008 policy data exists; this forward migration cannot be downgraded';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER protect_source_specific_record ON source_specific_records")
    op.drop_table("source_specific_records")

    op.drop_constraint(
        "ck_source_candidate_results_shape",
        "source_candidate_results",
        type_="check",
    )
    op.drop_constraint(
        "ck_source_candidate_results_status",
        "source_candidate_results",
        type_="check",
    )
    op.drop_column("source_candidate_results", "eligibility_kind")
    op.drop_column("source_candidate_results", "evidence_eligible")
    op.create_check_constraint(
        "ck_source_candidate_results_status",
        "source_candidate_results",
        "article_status IN ('body-valid', 'invalid-format', 'access-blocked', "
        "'temporary-failure')",
    )
    op.create_check_constraint(
        "ck_source_candidate_results_shape",
        "source_candidate_results",
        "(article_status = 'body-valid' AND document_version_id IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL) OR "
        "(article_status <> 'body-valid' AND document_version_id IS NULL "
        "AND error_code IS NOT NULL AND error_message IS NOT NULL)",
    )

    op.drop_constraint(
        "ck_source_profile_states_pause_state",
        "source_profile_states",
        type_="check",
    )
    op.drop_column("source_profile_states", "pause_state")

    op.drop_constraint(
        "ck_source_definitions_pause_state",
        "source_definitions",
        type_="check",
    )
    op.drop_constraint(
        "ck_source_definitions_evidence_eligibility",
        "source_definitions",
        type_="check",
    )
    op.drop_constraint(
        "ck_source_definitions_activation",
        "source_definitions",
        type_="check",
    )
    for column_name in (
        "overlap_rationale",
        "expected_contribution",
        "pause_state",
        "body_eligibility",
        "evidence_eligibility",
        "contribution_role",
        "acceptance_group",
    ):
        op.drop_column("source_definitions", column_name)
    op.create_check_constraint(
        "ck_source_definitions_approved",
        "source_definitions",
        "activation_conclusion = 'approved'",
    )
