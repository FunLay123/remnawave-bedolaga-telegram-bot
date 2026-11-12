"""Единый предикат: можно ли начислить бонусные дни на подписку.

Раньше экран партнёрки и другие места проверяли только статус подписки. Из-за
этого целью считались суточный тариф (ему дни не начисляются) и EXPIRED-пустышка
с нулевой длительностью. Предикат собран в одном модуле, чтобы UI не расходился
с тем, что реально умеет начисление.
"""

from __future__ import annotations

from app.database.models import SubscriptionStatus


ELIGIBLE_REFERRAL_STATUSES = {
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
    SubscriptionStatus.EXPIRED.value,
}


def is_daily_referral_tariff(subscription) -> bool:
    """Суточный тариф: срок можно продлить, но ежедневное списание остаётся.

    Бонусные дни в такой подписке человек всё равно оплатил бы из баланса,
    поэтому цель она не для награды. Отдельный публичный helper нужен, чтобы
    учёт первой покупки мог отличить суточный тариф (marker без дней) от
    прочих неподходящих подписок (placeholder — полный пропуск).
    """
    tariff = getattr(subscription, 'tariff', None)
    return bool(getattr(tariff, 'is_daily', False))


def is_referral_reward_target(subscription) -> bool:
    """Подходит ли подписка как цель для бонусных дней.

    EXPIRED оставлен допустимым статусом: дни можно начислить при продлении.
    Но EXPIRED с нулевой длительностью — это placeholder, а не подписка, и
    обещать за него бонус нельзя. Суточный тариф исключён: бонусные дни в нём
    не работают. ``tariff`` может быть ``None`` — это не повод отказывать.
    """
    status = str(getattr(subscription, 'status', '') or '').strip().lower()
    if status not in ELIGIBLE_REFERRAL_STATUSES:
        return False

    if is_daily_referral_tariff(subscription):
        return False

    if status == SubscriptionStatus.EXPIRED.value and _is_zero_duration_placeholder(subscription):
        return False

    return True


def _is_zero_duration_placeholder(subscription) -> bool:
    """Пустышка с ``end_date <= start_date``.

    Держим в согласии с backend ``_lock_referrer_target``: там пустышкой
    считается только EXPIRED с обеими датами и нулевым сроком. Подписка без
    дат (или с одной датой) не пустышка — сравнивать нечего.
    """
    start_date = getattr(subscription, 'start_date', None)
    end_date = getattr(subscription, 'end_date', None)

    if start_date is None or end_date is None:
        return False

    try:
        return end_date <= start_date
    except TypeError:
        return False
