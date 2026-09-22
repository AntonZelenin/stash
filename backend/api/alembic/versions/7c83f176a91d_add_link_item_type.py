"""add link item type

Revision ID: 7c83f176a91d
Revises: fade5255b8e7
Create Date: 2026-09-23 00:41:45.483914

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c83f176a91d'
down_revision: Union[str, Sequence[str], None] = 'fade5255b8e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `ALTER TYPE ... ADD VALUE` can't run inside the transaction block
    # Alembic normally wraps migrations in — Postgres won't let a new enum
    # value be used until the change is committed, so it refuses the
    # combination outright. `autocommit_block()` runs this one statement
    # outside that transaction instead.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE item_type ADD VALUE 'link'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres has no `DROP VALUE` for enums — removing one requires
    # recreating the type (and migrating any rows using it), which isn't
    # meaningful to do generically here. Not reversible.
    raise NotImplementedError("Removing an enum value requires recreating the item_type type")
