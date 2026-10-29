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

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import add_reward_days
from app.database.models import (
    ReferralEarning,
    ReferralRewardType,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
)


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


# ---------------------------------------------------------------------------
# Награда за первую оплаченную покупку приглашённого (схема 'days_only').
#
# Награда за первую покупку — не уровни и не проценты: обе стороны получают
# фиксированные дни. Факт учёта покупки фиксируется отдельным маркером, и
# маркер пишется на ЛЮБУЮ первую оплаченную покупку, даже если её срок меньше
# бонусного порога. Иначе вторая, длинная покупка считалась бы первой и
# принесла бы награду, которую программа обещает только за самую первую.
# ---------------------------------------------------------------------------

FirstPurchaseStatus = Literal['skipped', 'not_referred', 'already_registered', 'registered', 'rewarded', 'error']


@dataclass(frozen=True, slots=True)
class FirstPurchaseResult:
    """Итог учёта первой покупки. Нули означают «ничего не выдано»."""

    status: FirstPurchaseStatus
    referee_days: int = 0
    referrer_days: int = 0
    referee_id: int | None = None
    referrer_id: int | None = None
    referee_subscription_id: int | None = None
    referrer_subscription_id: int | None = None


async def _lock_user(db: AsyncSession, user_id: int) -> User | None:
    """Прочитать пользователя под блокировкой строки."""
    result = await db.execute(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_subscription(db: AsyncSession, subscription_id: int, user_id: int) -> Subscription | None:
    """Прочитать подписку под блокировкой, только если она принадлежит пользователю."""
    result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_referrer_target(db: AsyncSession, referrer_id: int) -> Subscription | None:
    """Подписка пригласившего, в которую лягут дни.

    Правило выбора то же, что у платной подписки в уровневых наградах: живые
    нетриальные впереди, при равенстве — более поздний срок, затем меньший id.
    Истёкшая подписка тоже годится: ``add_reward_days`` поднимет её обратно.
    ``None`` — подписки, куда можно положить награду, нет вовсе. Это
    единственный разрешённый случай «пригласивший без подписки»: пропускается
    только его сторона, приглашённый свою награду получает.
    """
    ids = (
        (
            await db.execute(
                select(Subscription.id).where(Subscription.user_id == referrer_id).order_by(Subscription.id.asc())
            )
        )
        .scalars()
        .all()
    )

    locked: list[Subscription] = []
    for subscription_id in ids:
        subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == subscription_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if subscription is not None:
            locked.append(subscription)

    allowed = {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
        SubscriptionStatus.LIMITED.value,
        SubscriptionStatus.EXPIRED.value,
    }
    candidates = [sub for sub in locked if (sub.status or '').lower() in allowed]
    if not candidates:
        return None
    candidates.sort(
        key=lambda sub: (
            bool(sub.is_trial),
            -(sub.end_date.timestamp() if sub.end_date else 0),
            sub.id,
        )
    )
    return candidates[0]


async def register_first_purchase(
    db: AsyncSession,
    *,
    referee_id: int,
    subscription_id: int,
    duration_days: int,
    paid_amount_kopeks: int,
    source: str,
) -> FirstPurchaseResult:
    """Учесть первую оплаченную покупку приглашённого и выдать дни.

    Вызывается уже после успешной оплаты. Маркер, награда приглашённому и
    награда пригласившему пишутся в ОДНОЙ транзакции с единственным коммитом:
    половина записи опаснее отказа. Маркер без дней навсегда лишает человека
    награды, дни без маркера позволяют получить её повторно.
    """
    if paid_amount_kopeks <= 0:
        # Бесплатная выдача (промокод, подарок, админская подписка) покупкой не
        # считается и маркер не получает: настоящая первая оплата ещё впереди.
        return FirstPurchaseResult(status='skipped')
    if not settings.is_referral_program_enabled() or not settings.is_referral_days_only_scheme():
        return FirstPurchaseResult(status='skipped')

    referee = await _lock_user(db, referee_id)
    if referee is None or referee.referred_by_id is None:
        return FirstPurchaseResult(status='not_referred')

    referrer_id = referee.referred_by_id
    if referrer_id == referee_id:
        return FirstPurchaseResult(status='not_referred')

    # Обе стороны пары блокируются до проверки маркера: иначе параллельные
    # покупки одного реферала обе увидели бы маркер отсутствующим и выдали бы
    # награду дважды.
    for user_id in sorted({referee_id, referrer_id}):
        if await _lock_user(db, user_id) is None:
            return FirstPurchaseResult(status='skipped')

    if await has_first_purchase_marker(db, referee_id):
        return FirstPurchaseResult(
            status='already_registered',
            referee_id=referee_id,
            referrer_id=referrer_id,
        )

    referee_sub = await _lock_subscription(db, subscription_id, referee_id)
    if referee_sub is None or (referee_sub.status or '').lower() in {
        SubscriptionStatus.PENDING.value,
        SubscriptionStatus.DISABLED.value,
    }:
        # Черновик оформления или отключённая подписка: дни туда класть нельзя,
        # а маркер на такую «покупку» лишил бы человека награды за настоящую.
        return FirstPurchaseResult(status='skipped')

    bonus = settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS
    extended_referee: Subscription | None = None
    extended_referrer: Subscription | None = None
    referrer_days = 0

    try:
        db.add(
            ReferralEarning(
                user_id=referee_id,
                referral_id=referrer_id,
                amount_kopeks=0,
                reason=REASON_FIRST_PURCHASE_REGISTERED,
                reward_type=ReferralRewardType.DAYS.value,
                level=1,
                days_granted=0,
            )
        )

        if duration_days < settings.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS:
            # Короткая покупка получает маркер, но не оплачивается днями.
            # id снимается до коммита: после него объект может быть протухшим,
            # и обращение к полю в async-сессии — MissingGreenlet.
            referee_subscription_id = referee_sub.id
            await db.commit()
            logger.info(
                'Referral first purchase registered below bonus period',
                referee_id=referee_id,
                referrer_id=referrer_id,
                duration_days=duration_days,
                source=source,
            )
            return FirstPurchaseResult(
                status='registered',
                referee_id=referee_id,
                referrer_id=referrer_id,
                referee_subscription_id=referee_subscription_id,
            )

        extended_referee = await add_reward_days(db, referee_sub, bonus)
        if extended_referee is None:
            # Дни не легли никуда — откатываем и маркер: следующая покупка
            # должна иметь возможность принести награду.
            await db.rollback()
            return FirstPurchaseResult(status='skipped')

        db.add(
            ReferralEarning(
                user_id=referee_id,
                referral_id=referrer_id,
                amount_kopeks=0,
                reason=REASON_FIRST_PURCHASE_REFEREE,
                reward_type=ReferralRewardType.DAYS.value,
                level=1,
                days_granted=bonus,
                tariff_id=extended_referee.tariff_id,
            )
        )

        referrer_sub = await _lock_referrer_target(db, referrer_id)
        if referrer_sub is not None:
            extended_referrer = await add_reward_days(db, referrer_sub, bonus)
            if extended_referrer is not None:
                referrer_days = bonus
                db.add(
                    ReferralEarning(
                        user_id=referrer_id,
                        referral_id=referee_id,
                        amount_kopeks=0,
                        reason=REASON_FIRST_PURCHASE,
                        reward_type=ReferralRewardType.DAYS.value,
                        level=1,
                        days_granted=bonus,
                        tariff_id=extended_referrer.tariff_id,
                    )
                )

        # Все id снимаются до коммита, пока объекты ещё не протухли.
        referee_subscription_id = extended_referee.id
        referrer_subscription_id = extended_referrer.id if extended_referrer is not None else None
        await db.commit()
    except SQLAlchemyError as error:
        # ``add_reward_days`` поднимает истёкшую подписку в ACTIVE. В тарифном
        # режиме рядом может оказаться живая подписка того же тарифа, и
        # частичный уникальный индекс отвергнет UPDATE. Откат обязателен: без
        # него в базе остался бы маркер без наград, и первая покупка потеряла бы
        # право на дни навсегда.
        await db.rollback()
        logger.error(
            'Referral first purchase failed, transaction rolled back',
            referee_id=referee_id,
            referrer_id=referrer_id,
            subscription_id=subscription_id,
            duration_days=duration_days,
            source=source,
            error=str(error),
            exc_info=True,
        )
        return FirstPurchaseResult(
            status='error',
            referee_id=referee_id,
            referrer_id=referrer_id,
        )

    logger.info(
        'Referral first purchase rewarded',
        referee_id=referee_id,
        referrer_id=referrer_id,
        duration_days=duration_days,
        referee_days=bonus,
        referrer_days=referrer_days,
        source=source,
    )
    return FirstPurchaseResult(
        status='rewarded',
        referee_days=bonus,
        referrer_days=referrer_days,
        referee_id=referee_id,
        referrer_id=referrer_id,
        referee_subscription_id=referee_subscription_id,
        referrer_subscription_id=referrer_subscription_id,
    )


async def register_first_purchase_after_purchase(
    *,
    referee_id: int,
    subscription_id: int,
    duration_days: int,
    paid_amount_kopeks: int,
    source: str,
    bot=None,
) -> FirstPurchaseResult:
    """Учесть покупку в отдельной сессии и выполнить пост-коммит эффекты.

    Отдельная сессия нужна потому, что вызывающий уже закоммитил покупку в
    своей, и награда не должна ни зависеть от её состояния, ни откатывать её.
    Исключения здесь только логируются: сбой реферальной награды не должен
    превращать успешную оплату в ошибку для пользователя.
    """
    from app.database.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            result = await register_first_purchase(
                db,
                referee_id=referee_id,
                subscription_id=subscription_id,
                duration_days=duration_days,
                paid_amount_kopeks=paid_amount_kopeks,
                source=source,
            )
    except Exception as error:
        logger.error(
            'Failed to process referral first purchase',
            referee_id=referee_id,
            subscription_id=subscription_id,
            source=source,
            error=str(error),
            exc_info=True,
        )
        return FirstPurchaseResult(status='error')

    if result.status == 'rewarded':
        await _post_commit_effects(result, referee_id, bot=bot)
    return result


async def _post_commit_effects(
    result: FirstPurchaseResult,
    referee_id: int,
    *,
    bot=None,
) -> None:
    """Побочные эффекты после коммита награды: панель, Lava, уведомления.

    Вызывается только при ``status == 'rewarded'``. Маркер короткой покупки
    (``registered``) дни не выдавал, поэтому не должен ни синхронизировать
    подписки, ни сдвигать расписание списания Lava — иначе короткая покупка
    молча сдвинула бы пользователю платёж, который никто не продлевал.
    """
    from app.database.database import AsyncSessionLocal
    from app.database.models import Subscription
    from app.services.payment.lava import shift_lava_next_charge_after_manual_extension
    from app.services.remnawave_retry_queue import remnawave_retry_queue
    from app.services.subscription_service import SubscriptionService

    subscription_ids = {
        subscription_id
        for subscription_id in (
            result.referee_subscription_id,
            result.referrer_subscription_id,
        )
        if subscription_id is not None
    }

    for subscription_id in sorted(subscription_ids):
        async with AsyncSessionLocal() as db:
            subscription = await db.get(Subscription, subscription_id)
            if subscription is None:
                continue
            try:
                await SubscriptionService().sync_remnawave_user(db, subscription)
            except Exception as error:
                logger.error(
                    'Failed to sync referral reward subscription',
                    subscription_id=subscription_id,
                    error=str(error),
                )
                remnawave_retry_queue.enqueue(
                    subscription_id=subscription_id,
                    user_id=subscription.user_id,
                    action='update',
                )
            try:
                await shift_lava_next_charge_after_manual_extension(
                    db,
                    subscription_id,
                    settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS,
                )
            except Exception as error:
                logger.warning(
                    'Failed to shift Lava charge after referral reward',
                    subscription_id=subscription_id,
                    error=str(error),
                )

    if bot is None:
        return

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.database.crud.user import get_user_by_id
    from app.localization.texts import get_texts

    async with AsyncSessionLocal() as db:
        referee = await get_user_by_id(db, referee_id)
        if referee is not None and referee.telegram_id:
            texts = get_texts(referee.language)
            if result.referee_days:
                try:
                    await bot.send_message(
                        chat_id=referee.telegram_id,
                        text=(
                            '<tg-emoji emoji-id="5235695112419303615">🎁</tg-emoji> '
                            '<b>Вам +10 дней</b>\n\n'
                            'За первую покупку по приглашению.'
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text=texts.t('MY_SUBSCRIPTION_BUTTON', 'Моя подписка'),
                                        callback_data='menu_subscription',
                                        icon_custom_emoji_id='5314310000531766389',
                                        style='primary',
                                    )
                                ]
                            ]
                        ),
                    )
                except Exception as error:
                    logger.warning('Failed to notify referee about referral days', error=str(error))

        if result.referrer_days and result.referrer_id:
            referrer = await get_user_by_id(db, result.referrer_id)
            if referrer and referrer.telegram_id:
                texts = get_texts(referrer.language)
                try:
                    await bot.send_message(
                        chat_id=referrer.telegram_id,
                        text=(
                            '<tg-emoji emoji-id="5335020345717973061">🎉</tg-emoji> '
                            '<b>Друг купил подписку</b>\n\n'
                            'Вам +10 дней к текущей подписке.'
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text=texts.t('MAIN_MENU_BUTTON', 'Главное меню'),
                                        callback_data='back_to_menu',
                                        icon_custom_emoji_id='5314310000531766389',
                                        style='primary',
                                    )
                                ]
                            ]
                        ),
                    )
                except Exception as error:
                    logger.warning('Failed to notify referrer about referral days', error=str(error))
