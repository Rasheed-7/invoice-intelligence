"""use clock_timestamp for created_at and updated_at

Revision ID: 4cd53c27bb01
Revises: 5cac046cdc2c
Create Date: 2026-10-03 12:17:14.944035

"""
from collections.abc import Sequence

from alembic import op

revision: str = '4cd53c27bb01'
down_revision: str | None = '5cac046cdc2c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLES_AND_COLUMNS = [
    ("analysis_results", "created_at"),
    ("audit_events", "created_at"),
    ("documents", "uploaded_at"),
    ("documents", "updated_at"),
    ("invoices", "created_at"),
    ("invoices", "updated_at"),
    ("policy_documents", "uploaded_at"),
    ("policy_documents", "updated_at"),
    ("policy_chunks", "created_at"),
    ("purchase_orders", "created_at"),
    ("purchase_orders", "updated_at"),
    ("review_cases", "created_at"),
    ("review_cases", "updated_at"),
    ("users", "created_at"),
    ("users", "updated_at"),
]


def upgrade() -> None:
    for table, column in TABLES_AND_COLUMNS:
        op.execute(f'ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT clock_timestamp()')


def downgrade() -> None:
    for table, column in TABLES_AND_COLUMNS:
        op.execute(f'ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT now()')
