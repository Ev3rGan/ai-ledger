"""Persist M1 usage budgets and Scheduler service status.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "anonymous_research_usage",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("client_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_calls_used", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_calls_used >= 1",
            name="ck_anonymous_research_usage_positive",
        ),
        sa.PrimaryKeyConstraint("usage_date", "client_hash"),
    )
    op.create_table(
        "scheduler_status",
        sa.Column("scheduler_key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('waiting', 'running', 'failed', 'stopped')",
            name="ck_scheduler_status_state",
        ),
        sa.CheckConstraint(
            "last_result IS NULL OR last_result IN ('succeeded', 'failed')",
            name="ck_scheduler_status_last_result",
        ),
        sa.PrimaryKeyConstraint("scheduler_key"),
    )
    op.create_table(
        "metered_provider_budget",
        sa.Column("billing_month", sa.Date(), nullable=False),
        sa.Column("reserved_cents", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reserved_cents >= 1 AND reserved_cents <= 10000",
            name="ck_metered_provider_budget_range",
        ),
        sa.PrimaryKeyConstraint("billing_month"),
    )


def downgrade() -> None:
    op.drop_table("metered_provider_budget")
    op.drop_table("scheduler_status")
    op.drop_table("anonymous_research_usage")
