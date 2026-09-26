"""add image filename

Revision ID: a2d7f4c9e813
Revises: d6f1b3e8a425
Create Date: 2026-09-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2d7f4c9e813'
down_revision: Union[str, Sequence[str], None] = 'd6f1b3e8a425'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The image's name as uploaded, which downloads are served under. Null
    # for images uploaded before it was kept (or without a name): those are
    # served as before, without one.
    op.add_column('item_images', sa.Column('filename', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('item_images', 'filename')
