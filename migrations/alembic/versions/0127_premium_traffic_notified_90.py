"""второе предупреждение о премиум-трафике — на 90 %

Revision ID: 0127
Revises: 0126
Create Date: 2026-09-23

Кроме предупреждения на 80 % (notified_80) воркер шлёт второе, на 90 %.
Чтобы не присылать его на каждом проходе, отправку запоминает свой флаг.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0127'
down_revision: Union[str, None] = '0126'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'subscription_premium_traffic'
COLUMN = 'notified_90'


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return COLUMN in [c['name'] for c in inspector.get_columns(TABLE)]


def upgrade() -> None:
    # Свежая установка получает таблицу по модели — уже с этой колонкой.
    if _has_column():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
