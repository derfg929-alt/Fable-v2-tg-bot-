"""Add active_coin and rigs_json to player_mining

Revision ID: 533360143200
Revises: 577b0bd5a55b
Create Date: 2026-11-03 17:55:33.020929

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '533360143200'
down_revision: Union[str, Sequence[str], None] = '577b0bd5a55b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


from sqlalchemy.dialects.postgresql import JSONB


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'player_mining',
        sa.Column('active_coin', sa.String(length=16), nullable=False, server_default='BTC')
    )
    op.add_column(
        'player_mining',
        sa.Column('rigs_json', JSONB(), nullable=False, server_default='{}')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('player_mining', 'rigs_json')
    op.drop_column('player_mining', 'active_coin')
