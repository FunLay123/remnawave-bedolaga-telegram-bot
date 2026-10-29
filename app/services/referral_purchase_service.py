"""Маркер первой оплаченной покупки реферала.

Маркер — строка ledger'а с нулевыми деньгами и нулевыми днями, поэтому ни
статистика, ни вывод реферального баланса её не видят. Наличие строки означает
ровно одно: первая оплаченная покупка этого приглашённого уже учтена. Без неё
первая покупка, совершённая до или после привязки реферала, могла бы быть
посчитана дважды — и наградой днями, и поздним бэкфиллом.

Новых колонок и миграций нет намеренно: ledger уже хранит получателя, вторую
сторону пары, причину и время, чего для «одна покупка — один маркер» хватает.
"""

from __future__ import annotations

import structlog
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ReferralEarning,
    ReferralRewardType,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED


logger = structlog.get_logger(__name__)


def _already_paid_subscription_condition():
    """SQL-условие «эта подписка уже оплачена».

    Одного ``is_trial IS FALSE`` мало: нетриальными бывают и неоплаченные
    черновики оформления (``PENDING``), и СБП-заготовки. Заготовка создаётся
    заранее с нулевым сроком (``end_date == start_date``) и до первого списания
    доступа не даёт, поэтому оплатой не считается. Настоящая истёкшая подписка
    отличается от заготовки непустым сроком: ``end_date > start_date``.

    Свободные купоны, подарки и админские выдачи сюда по-прежнему попадают — они
    нетриальны и с ненулевым сроком. Это осознанно консервативно: лучше не
    выдать повторную награду, чем выдать её дважды.
    """
    return and_(
        Subscription.is_trial.is_(False),
        Subscription.status != SubscriptionStatus.PENDING.value,
        or_(
            Subscription.status != SubscriptionStatus.EXPIRED.value,
            Subscription.end_date.is_(None),
            Subscription.start_date.is_(None),
            Subscription.end_date > Subscription.start_date,
        ),
    )


async def has_first_purchase_marker(db: AsyncSession, referee_id: int) -> bool:
    """Есть ли у приглашённого маркер уже учтённой первой покупки."""
    result = await db.execute(
        select(ReferralEarning.id)
        .where(
            ReferralEarning.user_id == referee_id,
            ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def ensure_marker_for_already_paid_referral(db: AsyncSession, user_id: int) -> bool:
    """Записать маркер рефералу, который уже оплатил подписку до привязки.

    Вызывается после поздней привязки пригласившего: покупка к этому моменту
    могла состояться, и награда за неё уже не придёт, а маркер не даст выдать её
    повторно. Черновик оформления и СБП-заготовка оплатой не считаются — иначе
    настоящая первая покупка осталась бы без награды.

    Возвращает ``True``, только если строка действительно создана.
    """
    user = await db.get(User, user_id)
    if user is None or user.referred_by_id is None:
        return False
    # referred_by_id в БД — целое число. Всё иное (например, неполный объект)
    # привязкой не является и записывать маркер не даёт.
    if not isinstance(user.referred_by_id, int):
        return False
    if await has_first_purchase_marker(db, user_id):
        return False

    paid_exists = await db.execute(
        select(Subscription.id)
        .where(
            Subscription.user_id == user_id,
            _already_paid_subscription_condition(),
        )
        .limit(1)
    )
    if paid_exists.scalar_one_or_none() is None:
        return False

    db.add(
        ReferralEarning(
            user_id=user_id,
            referral_id=user.referred_by_id,
            amount_kopeks=0,
            reason=REASON_FIRST_PURCHASE_REGISTERED,
            reward_type=ReferralRewardType.DAYS.value,
            level=1,
            days_granted=0,
        )
    )
    await db.commit()
    return True


async def backfill_first_purchase_markers(db: AsyncSession) -> int:
    """Проставить маркеры всем рефералам, которые уже покупали подписку.

    Бэкфилл нужен при первом включении схемы: без него покупки, сделанные до
    обновления, выглядели бы как первые и принесли бы награду повторно.
    Черновики оформления и СБП-заготовки с нулевым сроком пропускаются: они
    неоплаченные, и маркер на них лишил бы пользователя настоящей награды.
    Идемпотентен — уже помеченные строки пропускаются. Возвращает число
    созданных маркеров.
    """
    paid_referrals = await db.execute(
        select(User.id, User.referred_by_id).where(
            User.referred_by_id.isnot(None),
            exists(
                select(Subscription.id).where(
                    Subscription.user_id == User.id,
                    _already_paid_subscription_condition(),
                )
            ),
        )
    )

    created = 0
    for user_id, referrer_id in paid_referrals.all():
        if referrer_id is None or referrer_id == user_id:
            continue
        if await has_first_purchase_marker(db, user_id):
            continue
        db.add(
            ReferralEarning(
                user_id=user_id,
                referral_id=referrer_id,
                amount_kopeks=0,
                reason=REASON_FIRST_PURCHASE_REGISTERED,
                reward_type=ReferralRewardType.DAYS.value,
                level=1,
                days_granted=0,
            )
        )
        created += 1

    await db.commit()
    if created:
        logger.info('Referral first-purchase markers backfilled', count=created)
    return created
