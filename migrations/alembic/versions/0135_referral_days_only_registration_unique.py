"""partial unique on days_only registration anchor rows

Схема «только дни» пишет нулевую строку-якорь
``referral_registration_days_only`` на пару ``(пригласивший, приглашённый)``,
чтобы повторная обработка регистрации (DELETED-restore в панели) не слала
уведомления заново и не дублировала contest-событие.

SELECT-before-INSERT в приложении сужает окно гонки между сессиями бота и
кабинета, но не закрывает его: обе могут пройти SELECT до чужого коммита.
Этот партиальный UNIQUE-индекс закрывает разрыв структурно:

  1. Схлопываем возможные дубли якоря, оставшиеся от гонок до миграции:
     оставляем строку с минимальным id на пару, остальные удаляем.
     ANSI-переносимый подзапрос работает и на PostgreSQL (прод), и на
     SQLite (разработка).

  2. Создаём партиальный UNIQUE-индекс только по этому reason. Остальные
     причины начислений по-прежнему могут повторяться — каждая выплата
     является отдельной строкой аудита.

PostgreSQL-синтаксис ``CREATE UNIQUE INDEX ... WHERE`` поддерживается и
SQLite ≥ 3.8.0, который требует проект.

Revision ID: 0135
Revises: 0134
Create Date: 2026-11-12
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0135'
down_revision: Union[str, None] = '0134'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Чистка дублей, оставшихся от гонок до появления индекса.
    op.execute(
        """
        DELETE FROM referral_earnings
        WHERE reason = 'referral_registration_days_only'
          AND id NOT IN (
            SELECT MIN(id) FROM referral_earnings
            WHERE reason = 'referral_registration_days_only'
            GROUP BY user_id, referral_id
          )
        """
    )

    # 2. Партиальный UNIQUE только для строк-якорей; остальные reason не
    #    затрагиваются.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_earnings_days_only_registration
        ON referral_earnings (user_id, referral_id)
        WHERE reason = 'referral_registration_days_only'
        """
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS uq_referral_earnings_days_only_registration')
