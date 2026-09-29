"""filename size indexes

Revision ID: a4e9c7b2d815
Revises: f6c3a8e1d247
Create Date: 2026-09-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a4e9c7b2d815'
down_revision: Union[str, Sequence[str], None] = 'f6c3a8e1d247'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Duplicate checks before an upload look images and files up by
    # filename and size.
    op.create_index('ix_item_images_filename_size_bytes', 'item_images', ['filename', 'size_bytes'])
    op.create_index('ix_item_files_filename_size_bytes', 'item_files', ['filename', 'size_bytes'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_item_files_filename_size_bytes', table_name='item_files')
    op.drop_index('ix_item_images_filename_size_bytes', table_name='item_images')
