"""Add immutable Digest Plan Story-removal lineage.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _publication_guard(
    *,
    minimum_stories: int,
    require_publisher_diversity: bool,
    require_latest_plan: bool,
) -> str:
    publisher_check = (
        """
                IF publisher_count < 3 THEN
                    RAISE EXCEPTION 'M3 Digest requires at least three Publishers';
                END IF;
        """
        if require_publisher_diversity
        else ""
    )
    latest_plan_check = (
        """
                    SELECT max(latest_plan.version)
                    INTO latest_plan_version
                    FROM digest_plans AS latest_plan
                    WHERE latest_plan.publication_date = NEW.publication_date;
                    IF approved_plan_version IS DISTINCT FROM latest_plan_version THEN
                        RAISE EXCEPTION
                            'Only the latest Digest Plan version may be published';
                    END IF;
        """
        if require_latest_plan
        else ""
    )
    return f"""
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
            approved_plan_version integer;
            latest_plan_version integer;
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
                IF story_count NOT BETWEEN {minimum_stories} AND 12 THEN
                    RAISE EXCEPTION
                        'M3 Digest requires between {minimum_stories} and 12 Stories';
                END IF;
                {publisher_check}
                IF eligible_story_count <> story_count THEN
                    RAISE EXCEPTION 'M3 Digest requires accepted Stories with reader metadata';
                END IF;
                IF minimum_position <> 0 OR maximum_position <> story_count - 1 THEN
                    RAISE EXCEPTION 'M3 Digest requires contiguous explicit Story positions';
                END IF;
                IF NEW.publication_contract = 'm3-editorial-plan' THEN
                    IF NEW.digest_plan_id IS NOT NULL THEN
                        SELECT plan.content::jsonb, plan.version
                        INTO approved_content, approved_plan_version
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
                    {latest_plan_check}
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


def upgrade() -> None:
    op.add_column(
        "digest_plans",
        sa.Column("previous_plan_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "digest_plans",
        sa.Column("removed_story_stable_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "digest_plans",
        sa.Column("removal_reason", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_digest_plans_previous_plan_id",
        "digest_plans",
        "digest_plans",
        ["previous_plan_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_digest_plans_previous_plan_id",
        "digest_plans",
        ["previous_plan_id"],
    )
    op.create_check_constraint(
        "ck_digest_plans_derivation_fields",
        "digest_plans",
        "(previous_plan_id IS NULL AND removed_story_stable_key IS NULL "
        "AND removal_reason IS NULL) OR "
        "(previous_plan_id IS NOT NULL AND removed_story_stable_key IS NOT NULL "
        "AND removal_reason IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_digest_plans_removal_reason_length",
        "digest_plans",
        "removal_reason IS NULL OR length(btrim(removal_reason)) BETWEEN 1 AND 1000",
    )
    op.execute(
        _publication_guard(
            minimum_stories=1,
            require_publisher_diversity=False,
            require_latest_plan=True,
        )
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM digest_plans WHERE previous_plan_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1
                FROM digests AS digest
                WHERE digest.publication_contract = 'm3-editorial-plan'
                  AND digest.state = 'published'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM digest_withdrawals AS withdrawal
                      WHERE withdrawal.digest_id = digest.id
                  )
                  AND (
                      (SELECT count(*) FROM digest_stories AS membership
                       WHERE membership.digest_id = digest.id) < 8
                      OR (SELECT count(DISTINCT candidate.publisher)
                          FROM digest_stories AS membership
                          JOIN stories AS story ON story.id = membership.story_id
                          JOIN document_versions AS document
                            ON document.id = story.primary_document_version_id
                          JOIN candidates AS candidate ON candidate.id = document.candidate_id
                          WHERE membership.digest_id = digest.id) < 3
                  )
            ) THEN
                RAISE EXCEPTION
                    '0014 Story-removal data violates the 0013 publication contract; '
                    'this migration cannot be downgraded';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        _publication_guard(
            minimum_stories=8,
            require_publisher_diversity=True,
            require_latest_plan=False,
        )
    )
    op.drop_constraint(
        "ck_digest_plans_removal_reason_length",
        "digest_plans",
        type_="check",
    )
    op.drop_constraint(
        "ck_digest_plans_derivation_fields",
        "digest_plans",
        type_="check",
    )
    op.drop_constraint(
        "uq_digest_plans_previous_plan_id",
        "digest_plans",
        type_="unique",
    )
    op.drop_constraint(
        "fk_digest_plans_previous_plan_id",
        "digest_plans",
        type_="foreignkey",
    )
    op.drop_column("digest_plans", "removal_reason")
    op.drop_column("digest_plans", "removed_story_stable_key")
    op.drop_column("digest_plans", "previous_plan_id")
