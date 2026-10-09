"""add policy_check analysis type

Adds POLICY_CHECK to the analysis_type enum so policy retrieval results can be
persisted alongside the other deterministic checks.

Postgres cannot remove an enum value, so downgrade() is a deliberate no-op
rather than a broken drop. See the comment there.

Revision ID: 9f2d6b83f4a1
Revises: 4d6b8a1c73f5
Create Date: 2026-10-07 22:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

revision: str = '9f2d6b83f4a1'
down_revision: str | None = '4d6b8a1c73f5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block in some
    # Postgres contexts. autocommit_block() issues it outside one, which is the
    # documented Alembic workaround. The cost is that the migration is not
    # atomic: if it fails partway the enum keeps the old set, which is safe
    # because the application code is deployed separately.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE analysis_type ADD VALUE IF NOT EXISTS 'policy_check'")


def downgrade() -> None:
    # Deliberate no-op. Postgres has no "DROP VALUE" for enum types - the only
    # way to remove one is to recreate the type, which would rewrite the
    # analysis_type column and risk data loss for rows written under the newer
    # schema. Leaving the extra value in place is harmless: the application
    # simply stops reading it. Rolling this back is a manual operation and
    # should be treated as one.
    pass