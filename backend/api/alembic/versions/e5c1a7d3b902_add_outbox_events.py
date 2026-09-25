"""add outbox events

Revision ID: e5c1a7d3b902
Revises: d4e8b2c6f371
Create Date: 2026-09-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5c1a7d3b902'
down_revision: Union[str, Sequence[str], None] = 'd4e8b2c6f371'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The transactional outbox (see `stash_shared.outbox`).
    op.create_table(
        'outbox_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('queue', sa.String(), nullable=False),
        sa.Column('payload', sa.String(), nullable=False),
        sa.Column('trace_context', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Partial: flushes only ever look for unpublished events, which stay
    # few, however many published ones pile up.
    op.create_index(
        'ix_outbox_events_unpublished',
        'outbox_events',
        ['created_at'],
        postgresql_where=sa.text('published_at IS NULL'),
    )
    # Only the stale-item sweeper, which the outbox replaces, looked items
    # up by it. `items.requeue_count`, the sweeper's other leftover, stays
    # for now: API instances still running the previous release select it
    # while this migration's release rolls out. Drop it in a later one.
    op.drop_index('ix_items_unfinished_status_updated_at', table_name='items')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index(
        'ix_items_unfinished_status_updated_at',
        'items',
        ['status_updated_at'],
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
    op.drop_index('ix_outbox_events_unpublished', table_name='outbox_events')
    op.drop_table('outbox_events')
