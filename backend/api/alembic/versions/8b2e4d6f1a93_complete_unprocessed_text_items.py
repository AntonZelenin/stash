"""complete unprocessed text items

Revision ID: 8b2e4d6f1a93
Revises: 3f9a1c2d7e45
Create Date: 2026-09-24 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b2e4d6f1a93'
down_revision: Union[str, Sequence[str], None] = '3f9a1c2d7e45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Only images go through the content analyzer now; text/link items are
    # created `completed`. Existing ones still waiting on a job that will
    # never come are finished the same way.
    op.execute(
        "UPDATE items SET status = 'completed', status_updated_at = now() "
        "WHERE type IN ('text', 'link') AND status IN ('pending', 'processing')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Data-only migration; there's no record of which items it touched.
    pass
