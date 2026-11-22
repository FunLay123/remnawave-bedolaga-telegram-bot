"""ручной безлимит для состояния премиум-трафика."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0139'
down_revision: Union[str, None] = '0138'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'subscription_premium_traffic'
COLUMN = 'unlimited_at'


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return True
    return COLUMN in [column['name'] for column in inspector.get_columns(TABLE)]


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
