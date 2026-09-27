"""rate limit counters

Revision ID: a6c2e8f1d394
Revises: f8d2b6e4a197
Create Date: 2026-09-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6c2e8f1d394'
down_revision: Union[str, Sequence[str], None] = 'f8d2b6e4a197'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Rate limit and quota counters shared by every API instance (see
    # `app.rate_limits`): one row per limit window and (hashed) subject.
    op.create_table(
        'rate_limit_counters',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('window_start', sa.BigInteger(), nullable=False),
        sa.Column('count', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    # Pruning counters of periods that have ended.
    op.create_index('ix_rate_limit_counters_expires_at', 'rate_limit_counters', ['expires_at'])
    if op.get_bind().dialect.name == 'postgresql':
        # Rows are updated in place on almost every request that's limited.
        # Free space on each page lets those be HOT updates (no index
        # changes, dead versions reclaimed on the page) as long as the
        # indexed `expires_at` stays the same, i.e. within a period.
        op.execute('ALTER TABLE rate_limit_counters SET (fillfactor = 70)')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_rate_limit_counters_expires_at', table_name='rate_limit_counters')
    op.drop_table('rate_limit_counters')
