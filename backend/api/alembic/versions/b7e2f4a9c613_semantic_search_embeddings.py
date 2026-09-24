"""semantic search embeddings

Revision ID: b7e2f4a9c613
Revises: a9c3e5d7f102
Create Date: 2026-09-24 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b7e2f4a9c613'
down_revision: Union[str, Sequence[str], None] = 'a9c3e5d7f102'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match `stash_shared.embeddings.EMBEDDING_DIMENSIONS`.
_DIMENSIONS = 1536


def upgrade() -> None:
    """Upgrade schema."""
    # Semantic search replaces full-text search.
    op.drop_index('ix_item_descriptions_search_vector', table_name='item_descriptions')
    op.drop_column('item_descriptions', 'search_vector')

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # `item_embeddings` existed as a placeholder (float array, never
    # written). One row per item: the embedding of its description, plus a
    # hash of the exact text embedded, so stale or missing embeddings can be
    # found and unchanged text isn't re-embedded.
    op.execute("DELETE FROM item_embeddings")
    op.drop_column('item_embeddings', 'vector')
    op.execute(f"ALTER TABLE item_embeddings ADD COLUMN embedding vector({_DIMENSIONS}) NOT NULL")
    op.add_column('item_embeddings', sa.Column('content_hash', sa.String(), nullable=False))

    # Approximate nearest-neighbour index; the operator class must match the
    # distance used by search (cosine: `<=>`).
    op.execute(
        "CREATE INDEX ix_item_embeddings_embedding ON item_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_item_embeddings_embedding', table_name='item_embeddings')
    op.drop_column('item_embeddings', 'content_hash')
    op.drop_column('item_embeddings', 'embedding')
    op.add_column(
        'item_embeddings',
        sa.Column('vector', postgresql.ARRAY(sa.Float()), nullable=False, server_default='{}'),
    )
    op.execute(
        "ALTER TABLE item_descriptions ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS ("
        "to_tsvector('english', text) || "
        "to_tsvector('english', regexp_replace(text, '[[:punct:]]+', ' ', 'g'))"
        ") STORED"
    )
    op.create_index(
        'ix_item_descriptions_search_vector', 'item_descriptions', ['search_vector'], postgresql_using='gin'
    )
