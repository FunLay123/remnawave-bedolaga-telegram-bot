"""trial_referral_seconds on subscriptions

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-18

Явный учёт реферального остатка нового бесплатного триала: сколько секунд из
``end_date`` пришло из реферальных дней (бонус приглашённому при активации и
начисления по ``days_only``). ``NULL`` — старая строка без истории, для неё
сохраняется прежнее поведение; ``0`` — новый триал без реферального бонуса.

Колонка nullable, без server default и backfill: восстанавливать историю по
ledger'у нельзя, это изменило бы сроки уже выданных подписок.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0138'
down_revision: Union[str, None] = '0137'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'subscriptions'
COLUMN = 'trial_referral_seconds'


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return True  # таблицу создаст модель — уже с этой колонкой
    return COLUMN in [column['name'] for column in inspector.get_columns(TABLE)]


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
