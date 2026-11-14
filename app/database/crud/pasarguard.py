"""CRUD PasarGuard-идентичности подписки и очереди отложенных удалений.

Идентичность пишется только ботом (PasarGuard — раб): один пользователь панели
на подписку. Очередь удалений нужна, потому что подписку могут удалить, пока
панель недоступна; интент переживает удаление строки подписки и разбирается
проектором на следующем проходе.

Функции не коммитят транзакцию: вызывающий ведёт её сам, а ``flush`` лишь
доводит изменения до БД в рамках его транзакции.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PasarguardDeleteIntent, Subscription


async def set_pasarguard_identity(
    db: AsyncSession,
    subscription: Subscription,
    *,
    user_id: int,
    username: str,
    created_at: datetime,
) -> None:
    """Запомнить личность пользователя PasarGuard, созданного для подписки."""
    subscription.pasarguard_user_id = user_id
    subscription.pasarguard_username = username
    subscription.pasarguard_created_at = created_at
    await db.flush()


async def clear_pasarguard_identity(db: AsyncSession, subscription: Subscription) -> None:
    """Стереть личность вместе с аудитом сверки и отложенным revoke.

    Вызывается, когда пользователь панели удалён или личность признана
    недействительной: старые отметки не должны переживать её.
    """
    subscription.pasarguard_user_id = None
    subscription.pasarguard_username = None
    subscription.pasarguard_created_at = None
    subscription.pasarguard_last_revoke_at = None
    subscription.pasarguard_revoke_pending_at = None
    subscription.pasarguard_synced_at = None
    subscription.pasarguard_synced_hash = None
    await db.flush()


async def add_delete_intent(
    db: AsyncSession,
    *,
    subscription_id: int,
    user_id: int | None,
    username: str | None,
) -> None:
    """Поставить в очередь удаление пользователя PasarGuard для подписки."""
    db.add(
        PasarguardDeleteIntent(
            subscription_id=subscription_id,
            pasarguard_user_id=user_id,
            pasarguard_username=username,
        )
    )
    await db.flush()


async def list_delete_intents(db: AsyncSession, limit: int = 100) -> list[PasarguardDeleteIntent]:
    """Старейшие интенты первыми: очередь разбирается в порядке поступления."""
    result = await db.execute(select(PasarguardDeleteIntent).order_by(PasarguardDeleteIntent.id).limit(limit))
    return list(result.scalars().all())


async def delete_intent(db: AsyncSession, intent: PasarguardDeleteIntent) -> None:
    """Убрать исполненный интент из очереди."""
    await db.delete(intent)
    await db.flush()
