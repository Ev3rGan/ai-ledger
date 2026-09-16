"""Persist daily Editorial outcomes and retrieval-index follow-up.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "editorial_day_outcomes",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("publication_date", sa.Date(), nullable=False),
        sa.Column("outcome_kind", sa.String(length=32), nullable=False),
        sa.Column("digest_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_identifier", sa.String(length=255), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome_kind IN ('published', 'no-publication')",
            name="ck_editorial_day_outcomes_kind",
        ),
        sa.CheckConstraint(
            "(outcome_kind = 'published' AND digest_id IS NOT NULL) OR "
            "(outcome_kind = 'no-publication' AND digest_id IS NULL)",
            name="ck_editorial_day_outcomes_digest_shape",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_editorial_day_outcomes_hash_length",
        ),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["digest_plans.id"]),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint(
            "publication_date",
            name="uq_editorial_day_outcomes_publication_date",
        ),
        sa.UniqueConstraint("digest_id", name="uq_editorial_day_outcomes_digest_id"),
    )
    op.create_table(
        "retrieval_index_follow_ups",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("publication_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("index_id", sa.Uuid(), nullable=True),
        sa.Column("fault_code", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_retrieval_index_follow_ups_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_retrieval_index_follow_ups_attempt_count",
        ),
        sa.CheckConstraint(
            "(state = 'queued' AND started_at IS NULL AND completed_at IS NULL "
            "AND claim_id IS NULL AND lease_expires_at IS NULL AND index_id IS NULL "
            "AND fault_code IS NULL AND last_error IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND claim_id IS NOT NULL AND lease_expires_at IS NOT NULL AND index_id IS NULL "
            "AND fault_code IS NULL AND last_error IS NULL) OR "
            "(state = 'succeeded' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND claim_id IS NOT NULL AND lease_expires_at IS NULL AND index_id IS NOT NULL "
            "AND fault_code IS NULL AND last_error IS NULL) OR "
            "(state = 'failed' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND claim_id IS NOT NULL AND lease_expires_at IS NULL AND index_id IS NULL "
            "AND fault_code IS NOT NULL AND last_error IS NOT NULL)",
            name="ck_retrieval_index_follow_ups_state_shape",
        ),
        sa.ForeignKeyConstraint(["index_id"], ["retrieval_indexes.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["editorial_day_outcomes.plan_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
    )
    op.execute(
        """
        INSERT INTO editorial_day_outcomes (
            plan_id,
            publication_date,
            outcome_kind,
            digest_id,
            content_hash,
            actor_identifier,
            completed_at
        )
        SELECT
            approval.plan_id,
            digest.publication_date,
            'published',
            approval.digest_id,
            approval.content_hash,
            approval.actor_identifier,
            approval.approved_at
        FROM digest_plan_approvals AS approval
        JOIN digests AS digest ON digest.id = approval.digest_id
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_immutable_editorial_day_outcome
        BEFORE UPDATE OR DELETE ON editorial_day_outcomes
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Editorial day outcome is immutable'
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_protect_retrieval_index_follow_up_identity()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Retrieval-index follow-up identity is durable';
            END IF;
            IF NEW.plan_id IS DISTINCT FROM OLD.plan_id
               OR NEW.publication_date IS DISTINCT FROM OLD.publication_date THEN
                RAISE EXCEPTION 'Retrieval-index follow-up identity is durable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_retrieval_index_follow_up_identity
        BEFORE UPDATE OR DELETE ON retrieval_index_follow_ups
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_retrieval_index_follow_up_identity()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_validate_retrieval_index_follow_up()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM editorial_day_outcomes AS outcome
                WHERE outcome.plan_id = NEW.plan_id
                  AND outcome.publication_date = NEW.publication_date
                  AND outcome.outcome_kind = 'published'
            ) THEN
                RAISE EXCEPTION
                    'Retrieval-index follow-up requires a published Editorial outcome';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER validate_retrieval_index_follow_up
        BEFORE INSERT ON retrieval_index_follow_ups
        FOR EACH ROW EXECUTE FUNCTION ai_intel_validate_retrieval_index_follow_up()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_validate_editorial_day_outcome()
        RETURNS trigger AS $$
        DECLARE
            plan_content jsonb;
            plan_hash text;
            plan_date date;
            plan_version integer;
            latest_plan_version integer;
            included_count integer;
        BEGIN
            SELECT
                plan.content::jsonb,
                plan.content_hash,
                plan.publication_date,
                plan.version
            INTO plan_content, plan_hash, plan_date, plan_version
            FROM digest_plans AS plan
            WHERE plan.id = NEW.plan_id;

            IF plan_content IS NULL
               OR NEW.content_hash IS DISTINCT FROM plan_hash
               OR NEW.publication_date IS DISTINCT FROM plan_date THEN
                RAISE EXCEPTION 'Editorial outcome differs from its exact Digest Plan';
            END IF;

            SELECT max(plan.version)
            INTO latest_plan_version
            FROM digest_plans AS plan
            WHERE plan.publication_date = NEW.publication_date;
            IF plan_version IS DISTINCT FROM latest_plan_version THEN
                RAISE EXCEPTION 'Only the latest Digest Plan may complete an Editorial day';
            END IF;

            IF NEW.outcome_kind = 'no-publication' THEN
                SELECT count(*)
                INTO included_count
                FROM jsonb_array_elements(plan_content -> 'stories') AS planned(story)
                WHERE planned.story ->> 'inclusion' = 'included';
                IF included_count <> 0 THEN
                    RAISE EXCEPTION 'No-publication requires an exact zero-Story Digest Plan';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM digests AS digest
                    WHERE digest.publication_date = NEW.publication_date
                ) THEN
                    RAISE EXCEPTION 'No-publication cannot coexist with a Digest';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(plan_content -> 'stories') AS planned(story)
                    WHERE planned.story ->> 'inclusion' <> 'held'
                      AND NOT EXISTS (
                          SELECT 1 FROM stories AS decision_story
                          WHERE decision_story.id = (planned.story ->> 'id')::uuid
                            AND decision_story.review_state = 'rejected'
                      )
                ) THEN
                    RAISE EXCEPTION
                        'No-publication Story decisions differ from the exact Digest Plan';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER validate_editorial_day_outcome
        BEFORE INSERT ON editorial_day_outcomes
        FOR EACH ROW EXECUTE FUNCTION ai_intel_validate_editorial_day_outcome()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_validate_editorial_publication_follow_up()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.state = 'published'
               AND NEW.publication_contract = 'm3-editorial-plan'
               AND (
                   TG_OP = 'INSERT'
                   OR OLD.state <> 'published'
                   OR OLD.publication_contract <> 'm3-editorial-plan'
               )
               AND NOT EXISTS (
                   SELECT 1
                   FROM editorial_day_outcomes AS outcome
                   JOIN digest_plan_approvals AS approval
                     ON approval.plan_id = outcome.plan_id
                    AND approval.digest_id = outcome.digest_id
                    AND approval.content_hash = outcome.content_hash
                   JOIN retrieval_index_follow_ups AS follow_up
                     ON follow_up.plan_id = outcome.plan_id
                   WHERE outcome.plan_id = NEW.digest_plan_id
                     AND outcome.publication_date = NEW.publication_date
                     AND outcome.outcome_kind = 'published'
                     AND outcome.digest_id = NEW.id
                     AND follow_up.publication_date = NEW.publication_date
                     AND follow_up.state = 'queued'
               ) THEN
                RAISE EXCEPTION
                    'M3 Editorial Digest requires one durable retrieval-index follow-up';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER validate_z_editorial_publication_follow_up
        BEFORE INSERT OR UPDATE ON digests
        FOR EACH ROW EXECUTE FUNCTION ai_intel_validate_editorial_publication_follow_up()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM retrieval_index_follow_ups)
               OR EXISTS (
                   SELECT 1 FROM editorial_day_outcomes
                   WHERE outcome_kind = 'no-publication'
               ) THEN
                RAISE EXCEPTION
                    '0015 Editorial completion data cannot be represented by 0014; '
                    'this migration cannot be downgraded';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER validate_z_editorial_publication_follow_up ON digests")
    op.execute("DROP FUNCTION ai_intel_validate_editorial_publication_follow_up()")
    op.execute("DROP TRIGGER validate_editorial_day_outcome ON editorial_day_outcomes")
    op.execute("DROP FUNCTION ai_intel_validate_editorial_day_outcome()")
    op.execute(
        "DROP TRIGGER protect_retrieval_index_follow_up_identity "
        "ON retrieval_index_follow_ups"
    )
    op.execute("DROP FUNCTION ai_intel_protect_retrieval_index_follow_up_identity()")
    op.execute(
        "DROP TRIGGER validate_retrieval_index_follow_up ON retrieval_index_follow_ups"
    )
    op.execute("DROP FUNCTION ai_intel_validate_retrieval_index_follow_up()")
    op.execute("DROP TRIGGER protect_immutable_editorial_day_outcome ON editorial_day_outcomes")
    op.drop_table("retrieval_index_follow_ups")
    op.drop_table("editorial_day_outcomes")
