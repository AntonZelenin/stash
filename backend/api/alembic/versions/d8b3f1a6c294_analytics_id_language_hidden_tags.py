"""analytics id, account language, hidden tags

Revision ID: d8b3f1a6c294
Revises: c5d1a8e3f902
Create Date: 2026-10-01 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8b3f1a6c294'
down_revision: Union[str, Sequence[str], None] = 'c5d1a8e3f902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A random analytics id for every user, existing ones included (the
    # default fills them in as the column is added; gen_random_uuid() is
    # built in from Postgres 13). The application sets its own on
    # registration; the default covers any other insert.
    op.add_column(
        'users',
        sa.Column('analytics_id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    )
    op.create_unique_constraint('uq_users_analytics_id', 'users', ['analytics_id'])
    # No language chosen yet: clients keep using the device's.
    op.add_column('users', sa.Column('language', sa.String(length=16), nullable=True))
    # Hidden tags used to be kept per device by the clients; every existing
    # tag starts out visible (clients upload what they had, once).
    op.add_column('tags', sa.Column('is_hidden', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tags', 'is_hidden')
    op.drop_column('users', 'language')
    op.drop_constraint('uq_users_analytics_id', 'users', type_='unique')
    op.drop_column('users', 'analytics_id')
