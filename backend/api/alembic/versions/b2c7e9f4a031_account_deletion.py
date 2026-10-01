"""account deletion

Revision ID: b2c7e9f4a031
Revises: a4e9c7b2d815
Create Date: 2026-10-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c7e9f4a031'
down_revision: Union[str, Sequence[str], None] = 'a4e9c7b2d815'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The tables whose `user_id` references `users` without ON DELETE CASCADE
# (tags, collections and pending uploads already have it). Everything in
# them is the user's, so deleting a user deletes it, in the database: their
# items (and, through the items' own cascades, every row derived from them)
# and their sessions.
_USER_TABLES = [
    'items',
    'access_tokens',
    'refresh_tokens',
]


def _recreate_user_fks(*, ondelete: str | None) -> None:
    for table in _USER_TABLES:
        name = f'{table}_user_id_fkey'
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(name, table, 'users', ['user_id'], ['id'], ondelete=ondelete)


def upgrade() -> None:
    """Upgrade schema."""
    _recreate_user_fks(ondelete='CASCADE')
    # Object deletions still to be carried out (`app.storage.deletions`).
    op.create_table(
        'storage_deletions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('target', sa.String(), nullable=False),
        sa.Column('is_prefix', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('storage_deletions')
    _recreate_user_fks(ondelete=None)
