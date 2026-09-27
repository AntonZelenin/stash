"""drop user onboarding completed

Revision ID: d4a8e2f6c193
Revises: c2f7a9e4b816
Create Date: 2026-09-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a8e2f6c193'
down_revision: Union[str, Sequence[str], None] = 'c2f7a9e4b816'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The first-login welcome it tracked was removed.
    op.drop_column('users', 'onboarding_completed_at')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('users', sa.Column('onboarding_completed_at', sa.DateTime(timezone=True), nullable=True))
    op.execute('UPDATE users SET onboarding_completed_at = now()')
