"""Raise the internal monthly Provider budget hard cap.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_metered_provider_budget_range",
        "metered_provider_budget",
        type_="check",
    )
    op.create_check_constraint(
        "ck_metered_provider_budget_range",
        "metered_provider_budget",
        "reserved_cents >= 1 AND reserved_cents <= 11500",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM metered_provider_budget
                WHERE reserved_cents > 10000
            ) THEN
                RAISE EXCEPTION
                    '0009 Provider budget data exceeds the 0008 hard cap; '
                    'this migration cannot be downgraded';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "ck_metered_provider_budget_range",
        "metered_provider_budget",
        type_="check",
    )
    op.create_check_constraint(
        "ck_metered_provider_budget_range",
        "metered_provider_budget",
        "reserved_cents >= 1 AND reserved_cents <= 10000",
    )
