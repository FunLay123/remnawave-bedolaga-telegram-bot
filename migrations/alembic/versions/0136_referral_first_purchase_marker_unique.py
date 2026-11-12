"""partial unique on first-purchase marker rows

Маркер ``referral_first_purchase_registered`` — нулевая строка ledger'а,
отмечающая, что первая оплаченная покупка приглашённого уже учтена. Без него
следующая покупка снова считалась бы первой и принесла бы награду повторно.

Маркер пишется из нескольких путей: регистрация (включая повторную обработку
DELETED-restore), админская привязка, диагностика потерянных рефералов,
бэкфилл. SELECT-before-INSERT в приложении сужает окно гонки между сессиями
бота и кабинета, но не закрывает его: обе могут пройти SELECT до чужого
коммита. Этот партиальный UNIQUE-индекс закрывает разрыв структурно:

  1. Схлопываем возможные дубли маркера, оставшиеся от гонок до миграции:
     оставляем строку с минимальным id на пользователя, остальные удаляем.
     ANSI-переносимый подзапрос работает и на PostgreSQL (прод), и на
     SQLite (разработка), а сам DELETE идемпотентен — повторный запуск
     миграции на уже очищенной таблице ничего не меняет.

  2. Создаём партиальный UNIQUE-индекс только по этому reason. Остальные
     причины начислений по-прежнему могут повторяться — каждая выплата
     является отдельной строкой аудита.

PostgreSQL-синтаксис ``CREATE UNIQUE INDEX ... WHERE`` поддерживается и
SQLite ≥ 3.8.0, который требует проект. ``IF NOT EXISTS`` делает создание
индекса безопасным при повторном запуске: на свежей установке индекс уже
создан ``create_all`` из метаданных модели.

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-12
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0136'
down_revision: Union[str, None] = '0135'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Чистка дублей маркера, оставшихся от гонок до появления индекса.
    #    Оставляем MIN(id) на каждого пользователя; чужие reason не трогаем.
    op.execute(
        """
        DELETE FROM referral_earnings
        WHERE reason = 'referral_first_purchase_registered'
          AND id NOT IN (
            SELECT MIN(id) FROM referral_earnings
            WHERE reason = 'referral_first_purchase_registered'
            GROUP BY user_id
          )
        """
    )

    # 2. Партиальный UNIQUE только для маркера; остальные reason не
    #    затрагиваются.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_earnings_first_purchase_registered
        ON referral_earnings (user_id)
        WHERE reason = 'referral_first_purchase_registered'
        """
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS uq_referral_earnings_first_purchase_registered')
