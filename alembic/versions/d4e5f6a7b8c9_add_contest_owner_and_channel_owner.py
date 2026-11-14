"""Fable Randomizer: user-owned channel contests

Adds `contests.owner_id`, `contests.source_channel_id` and `channels.owner_id`
so channel owners can create and manage their own contests. The contest owner
is excluded from the draw (cannot win their own contest).

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("contests", sa.Column("owner_id", sa.BigInteger(), nullable=True))
    op.add_column("contests", sa.Column("source_channel_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_contests_owner_id", "contests", ["owner_id"])
    op.create_index("ix_contests_source_channel_id", "contests", ["source_channel_id"])
    op.create_foreign_key(
        "fk_contests_source_channel_id",
        "contests",
        "channels",
        ["source_channel_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("channels", sa.Column("owner_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_channels_owner_id", "channels", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_channels_owner_id", table_name="channels")
    op.drop_column("channels", "owner_id")

    op.drop_constraint("fk_contests_source_channel_id", "contests", type_="foreignkey")
    op.drop_index("ix_contests_source_channel_id", table_name="contests")
    op.drop_index("ix_contests_owner_id", table_name="contests")
    op.drop_column("contests", "source_channel_id")
    op.drop_column("contests", "owner_id")
