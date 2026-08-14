"""Record how each Evidence Span bears on its Claim.

Revision ID: 0003_issue9
Revises: 0002
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_issue9"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidence_spans",
        sa.Column(
            "relation",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE evidence_spans AS evidence
        SET relation = 'supports'
        WHERE EXISTS (
            SELECT 1
            FROM structured_traces AS trace
            WHERE trace.evidence_span_id = evidence.id
              AND trace.operation_key LIKE 'sample-story-v1%'
              AND trace.attributes ->> 'mode' = 'sample'
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM evidence_spans WHERE relation IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Evidence Relation requires explicit review before migration';
            END IF;
        END;
        $$
        """
    )
    op.alter_column("evidence_spans", "relation", nullable=False)
    op.create_check_constraint(
        "ck_evidence_spans_relation",
        "evidence_spans",
        "relation IN ('supports', 'contradicts')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_evidence_spans_relation",
        "evidence_spans",
        type_="check",
    )
    op.drop_column("evidence_spans", "relation")
