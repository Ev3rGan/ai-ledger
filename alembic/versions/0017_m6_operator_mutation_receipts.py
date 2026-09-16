"""Persist Operator Console mutation idempotency receipts.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_mutation_receipts",
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'completed')",
            name="ck_operator_mutation_receipts_state",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND response_status IS NULL AND response_body IS NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'completed' AND response_status BETWEEN 200 AND 299 "
            "AND response_body IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_operator_mutation_receipts_shape",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_operator_mutation_receipts_request_hash",
        ),
        sa.PrimaryKeyConstraint("github_user_id", "idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("operator_mutation_receipts")
