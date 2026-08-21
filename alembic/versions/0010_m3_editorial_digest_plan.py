"""Persist immutable versioned Editorial Digest Plans.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "story_presentations",
        sa.Column(
            "secondary_topics",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.create_table(
        "digest_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("publication_date", sa.Date(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("current_state_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_identifier", sa.String(length=255), nullable=False),
        sa.Column("protocol_version", sa.String(length=255), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_digest_plans_version_positive"),
        sa.CheckConstraint(
            "window_end > window_start",
            name="ck_digest_plans_window_order",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64 AND length(current_state_hash) = 64",
            name="ck_digest_plans_hash_lengths",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_date",
            "version",
            name="uq_digest_plans_publication_version",
        ),
    )
    op.execute(
        """
        CREATE TRIGGER protect_immutable_digest_plan
        BEFORE UPDATE OR DELETE ON digest_plans
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Digest Plan is immutable'
        )
        """
    )
    op.add_column("digests", sa.Column("digest_plan_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_digests_digest_plan_id",
        "digests",
        "digest_plans",
        ["digest_plan_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_digests_digest_plan_id",
        "digests",
        ["digest_plan_id"],
    )
    op.drop_constraint(
        "ck_digests_publication_contract",
        "digests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_digests_publication_contract",
        "digests",
        "publication_contract IN ('legacy-fixture', 'm3-multisource', 'm3-editorial-plan')",
    )
    op.create_check_constraint(
        "ck_digests_editorial_plan_contract",
        "digests",
        "(publication_contract = 'm3-editorial-plan' AND digest_plan_id IS NOT NULL) "
        "OR (publication_contract <> 'm3-editorial-plan' AND digest_plan_id IS NULL)",
    )
    op.create_table(
        "digest_plan_approvals",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("digest_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_identifier", sa.String(length=255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_digest_plan_approvals_hash_length",
        ),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["digest_plans.id"]),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("digest_id", name="uq_digest_plan_approvals_digest_id"),
    )
    op.execute(
        """
        CREATE TRIGGER protect_immutable_digest_plan_approval
        BEFORE UPDATE OR DELETE ON digest_plan_approvals
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Digest Plan approval is immutable'
        )
        """
    )
    op.create_table(
        "digest_withdrawals",
        sa.Column("digest_id", sa.Uuid(), nullable=False),
        sa.Column("actor_identifier", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 20 AND 1000",
            name="ck_digest_withdrawals_reason_length",
        ),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.PrimaryKeyConstraint("digest_id"),
    )
    op.execute(
        """
        CREATE TRIGGER protect_immutable_digest_withdrawal
        BEFORE UPDATE OR DELETE ON digest_withdrawals
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Digest withdrawal is immutable'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_immutable_audit_event
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION ai_intel_reject_immutable_collection_write(
            'Audit event is immutable'
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_validate_m3_digest_publication()
        RETURNS trigger AS $$
        DECLARE
            story_count integer;
            publisher_count integer;
            eligible_story_count integer;
            minimum_position integer;
            maximum_position integer;
            approved_story_count integer;
            approved_content jsonb;
            must_validate boolean := false;
        BEGIN
            IF NEW.state = 'published'
               AND NEW.publication_contract IN (
                   'm3-multisource', 'm3-editorial-plan'
               ) THEN
                IF TG_OP = 'INSERT' THEN
                    must_validate := true;
                ELSIF OLD.state <> 'published'
                      OR OLD.publication_contract NOT IN (
                          'm3-multisource', 'm3-editorial-plan'
                      ) THEN
                    must_validate := true;
                END IF;
            END IF;
            IF must_validate THEN
                IF NEW.publication_contract = 'm3-multisource' THEN
                    RAISE EXCEPTION
                        'Direct M3 publication is retired; exact Digest Plan approval is required';
                END IF;
                IF NEW.introduction IS NULL
                   OR length(btrim(NEW.introduction)) NOT BETWEEN 20 AND 2000 THEN
                    RAISE EXCEPTION 'M3 Digest requires a valid operator introduction';
                END IF;
                SELECT
                    count(*),
                    count(DISTINCT candidate.publisher),
                    count(*) FILTER (
                        WHERE story.review_state = 'accepted'
                          AND presentation.story_id IS NOT NULL
                    ),
                    min(membership.position),
                    max(membership.position)
                INTO
                    story_count,
                    publisher_count,
                    eligible_story_count,
                    minimum_position,
                    maximum_position
                FROM digest_stories AS membership
                JOIN stories AS story ON story.id = membership.story_id
                JOIN document_versions AS document
                  ON document.id = story.primary_document_version_id
                JOIN candidates AS candidate ON candidate.id = document.candidate_id
                LEFT JOIN story_presentations AS presentation
                  ON presentation.story_id = story.id
                WHERE membership.digest_id = NEW.id;
                IF story_count NOT BETWEEN 8 AND 12 THEN
                    RAISE EXCEPTION 'M3 Digest requires between 8 and 12 Stories';
                END IF;
                IF publisher_count < 3 THEN
                    RAISE EXCEPTION 'M3 Digest requires at least three Publishers';
                END IF;
                IF eligible_story_count <> story_count THEN
                    RAISE EXCEPTION 'M3 Digest requires accepted Stories with reader metadata';
                END IF;
                IF minimum_position <> 0 OR maximum_position <> story_count - 1 THEN
                    RAISE EXCEPTION 'M3 Digest requires contiguous explicit Story positions';
                END IF;
                IF NEW.publication_contract = 'm3-editorial-plan' THEN
                    IF NEW.digest_plan_id IS NOT NULL THEN
                        SELECT plan.content::jsonb
                        INTO approved_content
                        FROM digest_plan_approvals AS approval
                        JOIN digest_plans AS plan ON plan.id = approval.plan_id
                        WHERE approval.plan_id = NEW.digest_plan_id
                          AND approval.digest_id = NEW.id
                          AND approval.content_hash = plan.content_hash;
                    END IF;
                    IF approved_content IS NULL THEN
                        RAISE EXCEPTION
                            'M3 Editorial Digest requires an exact Plan approval';
                    END IF;
                    IF NEW.publication_date IS DISTINCT FROM
                           (approved_content ->> 'publication_date')::date
                       OR NEW.introduction IS DISTINCT FROM
                           (approved_content ->> 'digest_summary') THEN
                        RAISE EXCEPTION
                            'M3 Editorial Digest differs from exact approved Plan projection';
                    END IF;
                    SELECT count(*)
                    INTO approved_story_count
                    FROM jsonb_array_elements(approved_content -> 'stories') AS planned(story)
                    WHERE planned.story ->> 'inclusion' = 'included';
                    IF approved_story_count <> story_count THEN
                        RAISE EXCEPTION
                            'M3 Editorial Digest differs from exact approved Plan projection';
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(approved_content -> 'stories') AS planned(story)
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM stories AS decision_story
                            WHERE decision_story.id = (planned.story ->> 'id')::uuid
                              AND (
                                  (
                                      planned.story ->> 'inclusion' = 'included'
                                      AND decision_story.review_state = 'accepted'
                                  ) OR (
                                      planned.story ->> 'inclusion' = 'excluded'
                                      AND decision_story.review_state = 'rejected'
                                  ) OR (
                                      planned.story ->> 'inclusion' = 'held'
                                      AND decision_story.review_state =
                                          planned.story ->> 'review_state'
                                  )
                              )
                        )
                    ) THEN
                        RAISE EXCEPTION
                            'M3 Editorial decisions differ from exact approved Plan projection';
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(approved_content -> 'stories') AS planned(story)
                        WHERE planned.story ->> 'inclusion' = 'included'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM digest_stories AS approved_membership
                              JOIN stories AS approved_story
                                ON approved_story.id = approved_membership.story_id
                              JOIN document_versions AS approved_document
                                ON approved_document.id =
                                   approved_story.primary_document_version_id
                              JOIN candidates AS approved_candidate
                                ON approved_candidate.id = approved_document.candidate_id
                              JOIN story_presentations AS approved_presentation
                                ON approved_presentation.story_id = approved_story.id
                              WHERE approved_membership.digest_id = NEW.id
                                AND approved_membership.story_id =
                                    (planned.story ->> 'id')::uuid
                                AND approved_membership.position =
                                    (planned.story ->> 'order')::integer
                                AND approved_story.stable_key IS NOT DISTINCT FROM
                                    planned.story ->> 'stable_key'
                                AND approved_story.headline IS NOT DISTINCT FROM
                                    planned.story ->> 'headline'
                                AND approved_story.primary_document_version_id IS NOT DISTINCT FROM
                                    (planned.story ->> 'primary_document_version_id')::uuid
                                AND approved_document.content_hash IS NOT DISTINCT FROM
                                    planned.story ->> 'primary_document_content_hash'
                                AND approved_document.published_at IS NOT DISTINCT FROM
                                    (planned.story ->> 'original_published_at')::timestamptz
                                AND approved_candidate.publisher IS NOT DISTINCT FROM
                                    planned.story ->> 'publisher'
                                AND approved_candidate.canonical_url IS NOT DISTINCT FROM
                                    planned.story ->> 'canonical_url'
                                AND approved_presentation.summary IS NOT DISTINCT FROM
                                    planned.story ->> 'summary'
                                AND approved_presentation.why_it_matters IS NOT DISTINCT FROM
                                    planned.story ->> 'why_it_matters'
                                AND approved_presentation.primary_topic IS NOT DISTINCT FROM
                                    planned.story ->> 'primary_topic'
                                AND approved_presentation.secondary_topics::jsonb
                                    IS NOT DISTINCT FROM planned.story -> 'secondary_topics'
                                AND (
                                    SELECT COALESCE(
                                        jsonb_agg(
                                            jsonb_build_object(
                                                'id', approved_claim.id::text,
                                                'text', approved_claim.text,
                                                'evidence_spans', (
                                                    SELECT COALESCE(
                                                        jsonb_agg(
                                                            jsonb_build_object(
                                                                'id', approved_evidence.id::text,
                                                                'document_version_id',
                                                                    approved_evidence.document_version_id::text,
                                                                'exact_text', approved_evidence.exact_text,
                                                                'start_offset',
                                                                    approved_evidence.start_offset,
                                                                'end_offset', approved_evidence.end_offset,
                                                                'text_hash', approved_evidence.text_hash,
                                                                'role', approved_evidence.role,
                                                                'relation', approved_evidence.relation,
                                                                'publisher', evidence_candidate.publisher,
                                                                'canonical_url',
                                                                    evidence_candidate.canonical_url
                                                            ) ORDER BY
                                                                approved_evidence.start_offset,
                                                                approved_evidence.document_version_id,
                                                                approved_evidence.end_offset,
                                                                approved_evidence.id
                                                        ),
                                                        '[]'::jsonb
                                                    )
                                                    FROM evidence_spans AS approved_evidence
                                                    JOIN document_versions AS evidence_document
                                                      ON evidence_document.id =
                                                         approved_evidence.document_version_id
                                                    JOIN candidates AS evidence_candidate
                                                      ON evidence_candidate.id =
                                                         evidence_document.candidate_id
                                                    WHERE approved_evidence.claim_id =
                                                          approved_claim.id
                                                )
                                            ) ORDER BY approved_claim.position
                                        ),
                                        '[]'::jsonb
                                    )
                                    FROM claims AS approved_claim
                                    WHERE approved_claim.story_id = approved_story.id
                                ) IS NOT DISTINCT FROM planned.story -> 'claims'
                          )
                    ) THEN
                        RAISE EXCEPTION
                            'M3 Editorial Digest differs from exact approved Plan projection';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM digest_plan_approvals)
               OR EXISTS (SELECT 1 FROM digest_plans)
               OR EXISTS (SELECT 1 FROM digest_withdrawals)
               OR EXISTS (
                    SELECT 1
                    FROM story_presentations
                    WHERE secondary_topics::jsonb <> '[]'::jsonb
               ) THEN
                RAISE EXCEPTION
                    '0010 Editorial Plan data exists; this migration cannot be downgraded';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER protect_immutable_audit_event ON audit_events")
    op.execute("DROP TRIGGER protect_immutable_digest_plan_approval ON digest_plan_approvals")
    op.execute("DROP TRIGGER protect_immutable_digest_withdrawal ON digest_withdrawals")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ai_intel_validate_m3_digest_publication()
        RETURNS trigger AS $$
        DECLARE
            story_count integer;
            publisher_count integer;
            eligible_story_count integer;
            minimum_position integer;
            maximum_position integer;
            must_validate boolean := false;
        BEGIN
            IF NEW.state = 'published'
               AND NEW.publication_contract = 'm3-multisource' THEN
                IF TG_OP = 'INSERT' THEN
                    must_validate := true;
                ELSIF OLD.state <> 'published'
                      OR OLD.publication_contract <> 'm3-multisource' THEN
                    must_validate := true;
                END IF;
            END IF;
            IF must_validate THEN
                IF NEW.introduction IS NULL
                   OR length(btrim(NEW.introduction)) NOT BETWEEN 20 AND 2000 THEN
                    RAISE EXCEPTION 'M3 Digest requires a valid operator introduction';
                END IF;
                SELECT
                    count(*),
                    count(DISTINCT candidate.publisher),
                    count(*) FILTER (
                        WHERE story.review_state = 'accepted'
                          AND presentation.story_id IS NOT NULL
                    ),
                    min(membership.position),
                    max(membership.position)
                INTO
                    story_count,
                    publisher_count,
                    eligible_story_count,
                    minimum_position,
                    maximum_position
                FROM digest_stories AS membership
                JOIN stories AS story ON story.id = membership.story_id
                JOIN document_versions AS document
                  ON document.id = story.primary_document_version_id
                JOIN candidates AS candidate ON candidate.id = document.candidate_id
                LEFT JOIN story_presentations AS presentation
                  ON presentation.story_id = story.id
                WHERE membership.digest_id = NEW.id;
                IF story_count NOT BETWEEN 8 AND 12 THEN
                    RAISE EXCEPTION 'M3 Digest requires between 8 and 12 Stories';
                END IF;
                IF publisher_count < 3 THEN
                    RAISE EXCEPTION 'M3 Digest requires at least three Publishers';
                END IF;
                IF eligible_story_count <> story_count THEN
                    RAISE EXCEPTION 'M3 Digest requires accepted Stories with reader metadata';
                END IF;
                IF minimum_position <> 0 OR maximum_position <> story_count - 1 THEN
                    RAISE EXCEPTION 'M3 Digest requires contiguous explicit Story positions';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.drop_table("digest_withdrawals")
    op.drop_table("digest_plan_approvals")
    op.drop_constraint(
        "ck_digests_editorial_plan_contract",
        "digests",
        type_="check",
    )
    op.drop_constraint(
        "ck_digests_publication_contract",
        "digests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_digests_publication_contract",
        "digests",
        "publication_contract IN ('legacy-fixture', 'm3-multisource')",
    )
    op.drop_constraint("uq_digests_digest_plan_id", "digests", type_="unique")
    op.drop_constraint("fk_digests_digest_plan_id", "digests", type_="foreignkey")
    op.drop_column("digests", "digest_plan_id")
    op.execute("DROP TRIGGER protect_immutable_digest_plan ON digest_plans")
    op.drop_table("digest_plans")
    op.drop_column("story_presentations", "secondary_topics")
