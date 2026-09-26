"""item search chunks: one embedding per chunk instead of per item

Revision ID: f8d2b6e4a197
Revises: c5a8e2f4d917
Create Date: 2026-09-26 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8d2b6e4a197'
down_revision: Union[str, Sequence[str], None] = 'c5a8e2f4d917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match `stash_shared.embeddings.EMBEDDING_DIMENSIONS`.
_DIMENSIONS = 1536


def upgrade() -> None:
    """Upgrade schema."""
    # Items are searched by their best-matching chunk rather than one
    # embedding of their whole text, so a short query isn't diluted by
    # everything else an image shows. Nothing is carried over: existing
    # items get chunks when their text is embedded again (re-analyze or
    # re-save them), and until then they're found by the lexical searches
    # only.
    op.drop_index('ix_item_embeddings_embedding', table_name='item_embeddings')
    op.drop_table('item_embeddings')

    op.create_table(
        'item_search_chunks',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('item_id', sa.Uuid(), sa.ForeignKey('items.id', ondelete='CASCADE'), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.UniqueConstraint('item_id', 'position', name='uq_item_search_chunks_item_id_position'),
    )
    op.execute(f"ALTER TABLE item_search_chunks ADD COLUMN embedding vector({_DIMENSIONS}) NOT NULL")
    # Same approximate nearest-neighbour index as `item_embeddings` had
    # (cosine, matching search's `<=>`). The best-chunk-per-item query scans
    # a user's chunks exactly for now; this is here for when it's rewritten
    # to take its candidates from the index (see `ItemRepository.search_by_chunks`).
    op.execute(
        "CREATE INDEX ix_item_search_chunks_embedding ON item_search_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_item_search_chunks_embedding', table_name='item_search_chunks')
    op.drop_table('item_search_chunks')
    op.create_table(
        'item_embeddings',
        sa.Column('item_id', sa.Uuid(), sa.ForeignKey('items.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('content_hash', sa.String(), nullable=False),
    )
    op.execute(f"ALTER TABLE item_embeddings ADD COLUMN embedding vector({_DIMENSIONS}) NOT NULL")
    op.execute(
        "CREATE INDEX ix_item_embeddings_embedding ON item_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )
