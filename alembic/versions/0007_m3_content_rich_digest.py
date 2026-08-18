"""Persist operator-authored M3 reader presentation metadata.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("digests", sa.Column("introduction", sa.Text(), nullable=True))
    op.add_column(
        "digests",
        sa.Column(
            "publication_contract",
            sa.String(length=32),
            nullable=False,
            server_default="legacy-fixture",
        ),
    )
    op.create_check_constraint(
        "ck_digests_publication_contract",
        "digests",
        "publication_contract IN ('legacy-fixture', 'm3-multisource')",
    )
    op.alter_column(
        "digests",
        "publication_contract",
        server_default="m3-multisource",
    )
    op.create_check_constraint(
        "ck_digests_introduction_length",
        "digests",
        "introduction IS NULL OR length(btrim(introduction)) BETWEEN 20 AND 2000",
    )
    op.create_table(
        "story_presentations",
        sa.Column("story_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text(), nullable=False),
        sa.Column("primary_topic", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(btrim(summary)) BETWEEN 20 AND 1000",
            name="ck_story_presentations_summary_length",
        ),
        sa.CheckConstraint(
            "length(btrim(why_it_matters)) BETWEEN 20 AND 1000",
            name="ck_story_presentations_why_length",
        ),
        sa.CheckConstraint(
            "primary_topic IN ('Models', 'Research', 'Products and Tools', "
            "'Industry and Infrastructure', 'Business', 'Applications', "
            "'Policy and Safety', 'Community')",
            name="ck_story_presentations_primary_topic",
        ),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"]),
        sa.PrimaryKeyConstraint("story_id"),
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_protect_published_story_presentation()
        RETURNS trigger AS $$
        DECLARE
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF ai_intel_story_is_published(OLD.story_id) THEN
                    RAISE EXCEPTION 'published Story presentation is immutable';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'UPDATE'
               AND ai_intel_story_is_published(OLD.story_id) THEN
                RAISE EXCEPTION 'published Story presentation is immutable';
            END IF;
            IF ai_intel_story_is_published(NEW.story_id) THEN
                RAISE EXCEPTION 'published Story presentation is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_validate_m3_digest_publication()
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
    op.execute(
        """
        CREATE TRIGGER validate_m3_digest_publication
        BEFORE INSERT OR UPDATE ON digests
        FOR EACH ROW EXECUTE FUNCTION ai_intel_validate_m3_digest_publication()
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_published_story_presentation
        BEFORE INSERT OR UPDATE OR DELETE ON story_presentations
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_published_story_presentation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER validate_m3_digest_publication ON digests")
    op.execute("DROP FUNCTION ai_intel_validate_m3_digest_publication()")
    op.execute(
        "DROP TRIGGER protect_published_story_presentation ON story_presentations"
    )
    op.execute("DROP FUNCTION ai_intel_protect_published_story_presentation()")
    op.drop_table("story_presentations")
    op.drop_constraint(
        "ck_digests_publication_contract", "digests", type_="check"
    )
    op.drop_column("digests", "publication_contract")
    op.drop_constraint("ck_digests_introduction_length", "digests", type_="check")
    op.drop_column("digests", "introduction")
