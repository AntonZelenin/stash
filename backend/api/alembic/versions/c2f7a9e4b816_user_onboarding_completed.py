"""user onboarding completed

Revision ID: c2f7a9e4b816
Revises: b9d3f7a1c528
Create Date: 2026-09-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2f7a9e4b816'
down_revision: Union[str, Sequence[str], None] = 'b9d3f7a1c528'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # When the user dismissed the first-login welcome; NULL: not yet.
    op.add_column('users', sa.Column('onboarding_completed_at', sa.DateTime(timezone=True), nullable=True))
    # Accounts that already exist are past their first login: marked as
    # done now, so only new accounts are welcomed.
    op.execute('UPDATE users SET onboarding_completed_at = now()')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'onboarding_completed_at')
