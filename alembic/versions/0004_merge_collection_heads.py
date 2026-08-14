"""Merge the Issue 9 and Issue 10 migration heads.

Revision ID: 0004
Revises: 0003, 0003_issue9
Create Date: 2026-08-14
"""

from collections.abc import Sequence

revision: str = "0004"
down_revision: str | Sequence[str] | None = ("0003", "0003_issue9")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
