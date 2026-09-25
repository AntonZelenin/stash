"""add pending uploads

Revision ID: b4f6d2a8c915
Revises: e5c1a7d3b902
Create Date: 2026-09-25 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4f6d2a8c915'
down_revision: Union[str, Sequence[str], None] = 'e5c1a7d3b902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Direct-to-storage uploads that were started but not finalized (see
    # `app.items.models.PendingUpload`). A row is deleted when its item is
    # created, so what's left are unfinished uploads.
    op.create_table(
        'pending_uploads',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'type',
            postgresql.ENUM('text', 'link', 'image', 'file', name='item_type', create_type=False),
            nullable=False,
        ),
        sa.Column('storage_key', sa.String(), nullable=False),
        sa.Column('content_type', sa.String(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(), nullable=True),
        sa.Column('caption', sa.String(), nullable=True),
        sa.Column('tag_names', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_pending_uploads_user_id', 'pending_uploads', ['user_id'])
    # For a future cleanup of abandoned uploads (oldest expired first).
    op.create_index('ix_pending_uploads_expires_at', 'pending_uploads', ['expires_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_pending_uploads_expires_at', table_name='pending_uploads')
    op.drop_index('ix_pending_uploads_user_id', table_name='pending_uploads')
    op.drop_table('pending_uploads')
