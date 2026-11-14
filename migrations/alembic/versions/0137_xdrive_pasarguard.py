"""PasarGuard (xdrive) identity, premium baseline and delete intents

Revision ID: 0137
Revises: 0136
Create Date: 2026-11-14

Один пользователь PasarGuard на подписку. Колонки nullable: фича включается
флагом, у существующих подписок идентичности нет. Частичный UNIQUE на
``pasarguard_user_id`` повторяет индекс ``remnawave_id``: непривязанных строк
сколько угодно, две подписки на одного пользователя панели — ошибка.

``pasarguard_delete_intents`` намеренно без внешнего ключа на ``subscriptions``:
интент пишется в транзакции удаления подписки и должен пережить её.

Миграция идемпотентна: свежая установка создаёт колонки и таблицу из моделей,
а повторный запуск на уже обновлённой базе не падает.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0137'
down_revision: Union[str, None] = '0136'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBSCRIPTIONS = 'subscriptions'
PREMIUM_TRAFFIC = 'subscription_premium_traffic'
DELETE_INTENTS = 'pasarguard_delete_intents'

SUBSCRIPTION_COLUMN_NAMES = (
    'pasarguard_user_id',
    'pasarguard_username',
    'pasarguard_created_at',
    'pasarguard_last_revoke_at',
    'pasarguard_revoke_pending_at',
    'pasarguard_synced_at',
    'pasarguard_synced_hash',
)
PREMIUM_COLUMN_NAMES = ('pasarguard_baseline_bytes', 'pasarguard_identity_created_at')


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column['name'] for column in inspector.get_columns(table)}


def _add_subscription_columns() -> None:
    existing = _column_names(SUBSCRIPTIONS)
    with op.batch_alter_table(SUBSCRIPTIONS) as batch:
        if 'pasarguard_user_id' not in existing:
            batch.add_column(sa.Column('pasarguard_user_id', sa.Integer(), nullable=True))
        if 'pasarguard_username' not in existing:
            batch.add_column(sa.Column('pasarguard_username', sa.String(128), nullable=True))
        if 'pasarguard_created_at' not in existing:
            batch.add_column(sa.Column('pasarguard_created_at', sa.DateTime(timezone=True), nullable=True))
        if 'pasarguard_last_revoke_at' not in existing:
            batch.add_column(sa.Column('pasarguard_last_revoke_at', sa.DateTime(timezone=True), nullable=True))
        if 'pasarguard_revoke_pending_at' not in existing:
            batch.add_column(sa.Column('pasarguard_revoke_pending_at', sa.DateTime(timezone=True), nullable=True))
        if 'pasarguard_synced_at' not in existing:
            batch.add_column(sa.Column('pasarguard_synced_at', sa.DateTime(timezone=True), nullable=True))
        if 'pasarguard_synced_hash' not in existing:
            batch.add_column(sa.Column('pasarguard_synced_hash', sa.String(64), nullable=True))


def _add_premium_columns() -> None:
    existing = _column_names(PREMIUM_TRAFFIC)
    with op.batch_alter_table(PREMIUM_TRAFFIC) as batch:
        if 'pasarguard_baseline_bytes' not in existing:
            batch.add_column(sa.Column('pasarguard_baseline_bytes', sa.BigInteger(), nullable=True))
        if 'pasarguard_identity_created_at' not in existing:
            batch.add_column(sa.Column('pasarguard_identity_created_at', sa.DateTime(timezone=True), nullable=True))


def upgrade() -> None:
    tables = _table_names()

    if SUBSCRIPTIONS in tables:
        _add_subscription_columns()
        # IF NOT EXISTS: на свежей установке индекс уже создан моделью.
        op.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_pasarguard_user_id '
            'ON subscriptions (pasarguard_user_id) '
            'WHERE pasarguard_user_id IS NOT NULL'
        )

    if PREMIUM_TRAFFIC in tables:
        _add_premium_columns()

    if DELETE_INTENTS not in tables:
        op.create_table(
            DELETE_INTENTS,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            # Без FK: строка обязана пережить удаление подписки.
            sa.Column('subscription_id', sa.Integer(), nullable=False),
            sa.Column('pasarguard_user_id', sa.Integer(), nullable=True),
            sa.Column('pasarguard_username', sa.String(128), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_pasarguard_delete_intents_id', DELETE_INTENTS, ['id'])


def downgrade() -> None:
    tables = _table_names()

    op.execute('DROP INDEX IF EXISTS uq_subscriptions_pasarguard_user_id')

    if SUBSCRIPTIONS in tables:
        existing = _column_names(SUBSCRIPTIONS)
        with op.batch_alter_table(SUBSCRIPTIONS) as batch:
            for name in SUBSCRIPTION_COLUMN_NAMES:
                if name in existing:
                    batch.drop_column(name)

    if PREMIUM_TRAFFIC in tables:
        existing = _column_names(PREMIUM_TRAFFIC)
        with op.batch_alter_table(PREMIUM_TRAFFIC) as batch:
            for name in PREMIUM_COLUMN_NAMES:
                if name in existing:
                    batch.drop_column(name)

    if DELETE_INTENTS in tables:
        op.execute('DROP INDEX IF EXISTS ix_pasarguard_delete_intents_id')
        op.drop_table(DELETE_INTENTS)
