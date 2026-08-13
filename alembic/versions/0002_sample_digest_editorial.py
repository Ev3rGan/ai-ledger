"""Review sample Stories and publish one Digest.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stories",
        sa.Column(
            "review_state",
            sa.String(length=32),
            nullable=False,
            server_default="unreviewed",
        ),
    )
    op.create_check_constraint(
        "ck_stories_review_state",
        "stories",
        "review_state IN ('unreviewed', 'accepted', 'rejected')",
    )
    op.alter_column("stories", "review_state", server_default=None)

    op.create_table(
        "digests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(length=255), nullable=False),
        sa.Column("publication_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('draft', 'published')",
            name="ck_digests_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stable_key"),
    )
    op.create_table(
        "digest_stories",
        sa.Column("digest_id", sa.Uuid(), nullable=False),
        sa.Column("story_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["digest_id"], ["digests.id"]),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"]),
        sa.PrimaryKeyConstraint("digest_id", "story_id"),
        sa.UniqueConstraint("digest_id", "position"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("actor_identifier", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_key"),
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_protect_digest_membership()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_digest_id uuid;
            target_story_id uuid;
        BEGIN
            target_digest_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.digest_id ELSE NEW.digest_id END;
            target_story_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.story_id ELSE NEW.story_id END;

            IF EXISTS (
                SELECT 1 FROM digests
                WHERE id = target_digest_id AND state = 'published'
            ) THEN
                RAISE EXCEPTION 'published Digest membership is immutable';
            END IF;

            IF TG_OP <> 'DELETE' AND NOT EXISTS (
                SELECT 1 FROM stories
                WHERE id = target_story_id AND review_state = 'accepted'
            ) THEN
                RAISE EXCEPTION 'only accepted Stories may enter a Digest';
            END IF;

            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_digest_membership
        BEFORE INSERT OR UPDATE OR DELETE ON digest_stories
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_digest_membership()
        """
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_protect_published_digest()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.state = 'published' THEN
                    RAISE EXCEPTION 'published Digest is immutable';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.state = 'published' THEN
                RAISE EXCEPTION 'published Digest is immutable';
            END IF;

            IF OLD.state = 'draft'
               AND NEW.state = 'published'
               AND OLD.id = NEW.id
               AND OLD.stable_key = NEW.stable_key
               AND OLD.publication_date = NEW.publication_date
               AND OLD.published_at IS NULL
               AND NEW.published_at IS NOT NULL THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION 'invalid Digest lifecycle transition';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_published_digest
        BEFORE UPDATE OR DELETE ON digests
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_published_digest()
        """
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_story_is_published(target_story_id uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM digest_stories AS membership
                JOIN digests AS digest ON digest.id = membership.digest_id
                WHERE membership.story_id = target_story_id
                  AND digest.state = 'published'
            )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_protect_published_story()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF ai_intel_story_is_published(OLD.id) THEN
                RAISE EXCEPTION 'published Story content is immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_published_story
        BEFORE UPDATE OR DELETE ON stories
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_published_story()
        """
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_protect_published_claim()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF (TG_OP = 'INSERT' AND ai_intel_story_is_published(NEW.story_id))
               OR (TG_OP = 'DELETE' AND ai_intel_story_is_published(OLD.story_id))
               OR (TG_OP = 'UPDATE' AND (
                   ai_intel_story_is_published(OLD.story_id)
                   OR ai_intel_story_is_published(NEW.story_id)
               )) THEN
                RAISE EXCEPTION 'published Story content is immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_published_claim
        BEFORE INSERT OR UPDATE OR DELETE ON claims
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_published_claim()
        """
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_protect_published_evidence_span()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_story_id uuid;
            new_story_id uuid;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                SELECT story_id INTO old_story_id FROM claims WHERE id = OLD.claim_id;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                SELECT story_id INTO new_story_id FROM claims WHERE id = NEW.claim_id;
            END IF;

            IF ai_intel_story_is_published(old_story_id)
               OR ai_intel_story_is_published(new_story_id) THEN
                RAISE EXCEPTION 'published Story content is immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_published_evidence_span
        BEFORE INSERT OR UPDATE OR DELETE ON evidence_spans
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_published_evidence_span()
        """
    )
    op.execute(
        """
        CREATE FUNCTION ai_intel_protect_published_document_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM stories AS story
                WHERE story.primary_document_version_id = OLD.id
                  AND ai_intel_story_is_published(story.id)
            ) OR EXISTS (
                SELECT 1
                FROM evidence_spans AS evidence
                JOIN claims AS claim ON claim.id = evidence.claim_id
                WHERE evidence.document_version_id = OLD.id
                  AND ai_intel_story_is_published(claim.story_id)
            ) THEN
                RAISE EXCEPTION 'published Story content is immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_published_document_version
        BEFORE UPDATE OR DELETE ON document_versions
        FOR EACH ROW EXECUTE FUNCTION ai_intel_protect_published_document_version()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER protect_published_document_version ON document_versions")
    op.execute("DROP FUNCTION ai_intel_protect_published_document_version()")
    op.execute("DROP TRIGGER protect_published_evidence_span ON evidence_spans")
    op.execute("DROP FUNCTION ai_intel_protect_published_evidence_span()")
    op.execute("DROP TRIGGER protect_published_claim ON claims")
    op.execute("DROP FUNCTION ai_intel_protect_published_claim()")
    op.execute("DROP TRIGGER protect_published_story ON stories")
    op.execute("DROP FUNCTION ai_intel_protect_published_story()")
    op.execute("DROP FUNCTION ai_intel_story_is_published(uuid)")
    op.execute("DROP TRIGGER protect_published_digest ON digests")
    op.execute("DROP FUNCTION ai_intel_protect_published_digest()")
    op.execute("DROP TRIGGER protect_digest_membership ON digest_stories")
    op.execute("DROP FUNCTION ai_intel_protect_digest_membership()")
    op.drop_table("audit_events")
    op.drop_table("digest_stories")
    op.drop_table("digests")
    op.drop_constraint("ck_stories_review_state", "stories", type_="check")
    op.drop_column("stories", "review_state")
