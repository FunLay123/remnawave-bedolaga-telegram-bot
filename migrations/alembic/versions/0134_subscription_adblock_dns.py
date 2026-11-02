"""adblock-DNS flag on subscriptions

Revision ID: 0134
Revises: 0133
Create Date: 2026-11-02

Флаг включает подстановку adblock-DNS в клиентский Xray JSON подписки.
Панель об этом поле не знает; бот меняет его только сам.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0134'
down_revision: Union[str, None] = '0133'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'subscriptions'
COLUMN = 'adblock_dns_enabled'


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return True  # таблицу создаст модель — уже с этой колонкой
    return COLUMN in [column['name'] for column in inspector.get_columns(TABLE)]


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
