"""add page_number to policy_chunks

Chunks are page-scoped (a chunk never spans a page boundary), but the column
had no way to record which page a chunk came from. That made it impossible to
cite a retrieval hit back to a page.

Revision ID: 4d6b8a1c73f5
Revises: f7b2c4e19a08
Create Date: 2026-10-07 21:15:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '4d6b8a1c73f5'
down_revision: str | None = 'f7b2c4e19a08'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Added NOT NULL with a temporary server default so the migration works
    # against a table that already holds rows. The default is dropped straight
    # after, so every future insert must supply a real page number.
    op.add_column(
        "policy_chunks",
        sa.Column("page_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("policy_chunks", "page_number", server_default=None)


def downgrade() -> None:
    op.drop_column("policy_chunks", "page_number")