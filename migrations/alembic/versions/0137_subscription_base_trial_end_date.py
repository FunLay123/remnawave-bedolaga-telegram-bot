"""base_trial_end_date on subscriptions

Revision ID: 0137
Revises: 0136
Create Date: 2026-11-15

Дата окончания базового бесплатного триала без реферального бонуса
приглашённого. ``end_date`` остаётся полным сроком (база + бонус + продления);
при первой покупке переносится только неиспользованный реферальный хвост.

Колонка nullable и без backfill/server default: у старых строк истории нет, и
для них сохраняется прежнее поведение ``should_carry_trial_remaining_days()``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0137'
down_revision: Union[str, None] = '0136'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'subscriptions'
COLUMN = 'base_trial_end_date'


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return True  # таблицу создаст модель — уже с этой колонкой
    return COLUMN in [column['name'] for column in inspector.get_columns(TABLE)]


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
