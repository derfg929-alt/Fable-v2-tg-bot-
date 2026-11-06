"""remove_player_level

Revision ID: f7c22c27a6f9
Revises: e061e1882508
Create Date: 2026-11-06 16:41:25.517553

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7c22c27a6f9'
down_revision: Union[str, Sequence[str], None] = 'e061e1882508'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ck_players_level_positive', 'players', type_='check')
    op.drop_column('players', 'level')


def downgrade() -> None:
    op.add_column('players', sa.Column('level', sa.Integer(), nullable=False, server_default='1'))
    op.create_check_constraint('ck_players_level_positive', 'players', 'level >= 1')
