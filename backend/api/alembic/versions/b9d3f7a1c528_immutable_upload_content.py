"""immutable upload content

Revision ID: b9d3f7a1c528
Revises: a6c2e8f1d394
Create Date: 2026-09-27 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9d3f7a1c528'
down_revision: Union[str, Sequence[str], None] = 'a6c2e8f1d394'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Set when an upload is finalized into an item, for its original's
    # canonical, write-once object (see `stash_shared.storage_keys`):
    # - content_etag: that object's ETag, a change-detection token (not a
    #   content hash). Workers read the object only with it (If-Match).
    # - content_sha256: the hex SHA-256 of its content, computed by S3 as
    #   it wrote the copy: the content's identity.
    # Both NULL for items finalized before canonical copies existed, whose
    # objects stay at their old keys: those are "legacy" and read unpinned.
    # Nothing is backfilled: the object's current content isn't known to
    # be the content that was validated.
    for table in ('item_images', 'item_files'):
        op.add_column(table, sa.Column('content_etag', sa.String(), nullable=True))
        op.add_column(table, sa.Column('content_sha256', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for table in ('item_files', 'item_images'):
        op.drop_column(table, 'content_sha256')
        op.drop_column(table, 'content_etag')
