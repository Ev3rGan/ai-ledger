"""Settle temporary Provider reservations against token usage.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_metered_provider_budget_range",
        "metered_provider_budget",
        type_="check",
    )
    op.alter_column(
        "metered_provider_budget",
        "reserved_cents",
        new_column_name="legacy_reserved_cents",
    )
    op.add_column(
        "metered_provider_budget",
        sa.Column("spent_microusd", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "metered_provider_budget",
        sa.Column("held_microusd", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_metered_provider_budget_legacy_range",
        "metered_provider_budget",
        "legacy_reserved_cents >= 0 AND legacy_reserved_cents <= 50000",
    )
    op.create_check_constraint(
        "ck_metered_provider_budget_spent_nonnegative",
        "metered_provider_budget",
        "spent_microusd >= 0",
    )
    op.create_check_constraint(
        "ck_metered_provider_budget_held_nonnegative",
        "metered_provider_budget",
        "held_microusd >= 0",
    )
    op.create_table(
        "metered_provider_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("billing_month", sa.Date(), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("settled_microusd", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reserved_microusd > 0",
            name="ck_metered_provider_reservation_positive",
        ),
        sa.CheckConstraint(
            "settled_microusd IS NULL OR settled_microusd >= 0",
            name="ck_metered_provider_reservation_settled_nonnegative",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'settled', 'released')",
            name="ck_metered_provider_reservation_state",
        ),
        sa.ForeignKeyConstraint(
            ["billing_month"],
            ["metered_provider_budget.billing_month"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM metered_provider_budget
                WHERE GREATEST(
                    legacy_reserved_cents,
                    CEIL((spent_microusd + held_microusd) / 10000.0)::integer,
                    1
                ) > 50000
            ) THEN
                RAISE EXCEPTION
                    '0013 Provider budget data exceeds the 0012 hard cap; '
                    'this migration cannot be downgraded';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        UPDATE metered_provider_budget
        SET legacy_reserved_cents = GREATEST(
            legacy_reserved_cents,
            CEIL((spent_microusd + held_microusd) / 10000.0)::integer,
            1
        )
        """
    )
    op.drop_table("metered_provider_reservations")
    op.drop_constraint(
        "ck_metered_provider_budget_held_nonnegative",
        "metered_provider_budget",
        type_="check",
    )
    op.drop_constraint(
        "ck_metered_provider_budget_spent_nonnegative",
        "metered_provider_budget",
        type_="check",
    )
    op.drop_constraint(
        "ck_metered_provider_budget_legacy_range",
        "metered_provider_budget",
        type_="check",
    )
    op.drop_column("metered_provider_budget", "held_microusd")
    op.drop_column("metered_provider_budget", "spent_microusd")
    op.alter_column(
        "metered_provider_budget",
        "legacy_reserved_cents",
        new_column_name="reserved_cents",
    )
    op.create_check_constraint(
        "ck_metered_provider_budget_range",
        "metered_provider_budget",
        "reserved_cents >= 1 AND reserved_cents <= 50000",
    )
