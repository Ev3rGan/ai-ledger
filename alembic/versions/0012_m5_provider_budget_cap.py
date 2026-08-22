"""Raise the internal monthly Provider budget hard cap to USD 500.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
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
        "reserved_cents >= 1 AND reserved_cents <= 50000",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM metered_provider_budget
                WHERE reserved_cents > 11500
            ) THEN
                RAISE EXCEPTION
                    '0012 Provider budget data exceeds the 0011 hard cap; '
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
        "reserved_cents >= 1 AND reserved_cents <= 11500",
    )
