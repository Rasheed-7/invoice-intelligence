"""add deleted_at to invoices

Revision ID: 781f5be76489
Revises: 9f2d6b83f4a1
Create Date: 2026-10-09 18:44:26.071698

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '781f5be76489'
down_revision: str | None = '9f2d6b83f4a1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_invoices_deleted_at", "invoices", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_invoices_deleted_at", table_name="invoices")
    op.drop_column("invoices", "deleted_at")