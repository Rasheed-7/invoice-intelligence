"""enable pgvector and convert policy_chunks.embedding to vector

Revision ID: f7b2c4e19a08
Revises: 10e98d41d8b3
Create Date: 2026-10-07 20:40:00.000000

"""
from collections.abc import Sequence

from alembic import op

revision: str = 'f7b2c4e19a08'
down_revision: str | None = '10e98d41d8b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Embedding width. Must match the provider's output dimension.
EMBEDDING_DIM = 384

#: IVFFlat cluster count. pgvector recommends sqrt(rows) per list; 100 is a
#: sane default for small corpora (below ~1M rows).
IVFFLAT_LISTS = 100


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Existing embedding values were JSONB placeholders. The USING NULL is safe
    # because no production data exists and the column is nullable.
    op.execute(
        f"ALTER TABLE policy_chunks ALTER COLUMN embedding "
        f"TYPE vector({EMBEDDING_DIM}) USING NULL"
    )

    # IVFFlat index for approximate nearest-neighbor. lists=100 is a reasonable
    # default for small corpora (< 1M rows). HNSW would be better for larger,
    # but IVFFlat is simpler and correct here.
    op.execute(
        "CREATE INDEX ix_policy_chunks_embedding ON policy_chunks "
        "USING ivfflat (embedding vector_cosine_ops) "
        f"WITH (lists = {IVFFLAT_LISTS})"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_policy_chunks_embedding")

    # Drops any vector values; JSONB cannot represent them. Same reasoning as
    # the upgrade direction: the column is nullable and holds no real data.
    op.execute("ALTER TABLE policy_chunks ALTER COLUMN embedding TYPE jsonb USING NULL")

    op.execute("DROP EXTENSION IF EXISTS vector")