"""Экран партнёрки для схемы «только дни».

В этой схеме реферальная программа не платит деньгами: бонус — дни подписки.
Поэтому экран не показывает уровни, комиссии, QR и вывод средств — только
статистику в днях, ссылку, кнопку «Пригласить» и список рефералов. Человеку
без подписки начислять дни некуда, и вместо бесполезной ссылки он получает
CTA триала (или переход в «Мою подписку», если триал уже недоступен).
"""

from __future__ import annotations

import html
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import inspect as sa_inspect

from app.config import settings
from app.database.models import SubscriptionStatus


HEADER = '<tg-emoji emoji-id="5235695112419303615">🤝</tg-emoji> <b>Партнёрская программа</b>'
INVITED = '<tg-emoji emoji-id="5116368680279606270">👥</tg-emoji>'
PAID = '<tg-emoji emoji-id="5258204546391351475">💳</tg-emoji>'
RECEIVED = '<tg-emoji emoji-id="5258165702707125574">🎁</tg-emoji>'
TERMS = '<tg-emoji emoji-id="5335020345717973061">🎁</tg-emoji>'
SHARE_ICON = '5334552404736104314'
LIST_ICON = '5258274739041883702'
CTA_ICON = '5314310000531766389'
GIFT_ICON = '5359415772844152891'

# EXPIRED тоже считается: дни можно начислить при продлении, и прятать от
# такого пользователя партнёрскую ссылку значило бы врать о доступности программы.
ELIGIBLE_STATUSES = {
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
    SubscriptionStatus.EXPIRED.value,
}


def has_eligible_subscription(user) -> bool:
    """Есть ли подписка, к которой можно применить бонусные дни.

    Требует загруженной связи ``subscriptions``: у ORM-объекта иначе будет
    ленивая подгрузка в async-контексте, поэтому вызывающий код обязан
    убедиться, что она загружена.
    """
    return any(
        (getattr(subscription, 'status', '') or '').lower() in ELIGIBLE_STATUSES
        for subscription in (getattr(user, 'subscriptions', None) or [])
    )


def is_trial_available_for_referral(user) -> bool:
    """Может ли пользователь получить триал прямо с экрана партнёрки."""
    if settings.TRIAL_DURATION_DAYS <= 0:
        return False
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', 'telegram')):
        return False
    if user.is_trial_already_used():
        return False
    return not any(
        (getattr(subscription, 'status', '') or '').lower()
        in {SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value, SubscriptionStatus.LIMITED.value}
        for subscription in (getattr(user, 'subscriptions', None) or [])
    )


async def ensure_subscriptions_loaded(db, user) -> None:
    """Гарантирует загруженную связь ``subscriptions`` до проверки статусов.

    Middleware обычно грузит её через selectinload, но экран не должен на это
    полагаться: незагруженная связь в async-сессии падает при обращении, а при
    неаккуратном чтении выглядит пустой — и действующий подписчик превращается
    в «без подписки».
    """
    try:
        unloaded = sa_inspect(user).unloaded
    except Exception:
        # Не ORM-объект (например, тестовый двойник) — проверять нечего.
        return
    if 'subscriptions' in unloaded:
        await db.refresh(user, ['subscriptions'])


def _days_word(days: int) -> str:
    """Склонение слова «день» для числа в подписи бонуса."""
    days = abs(int(days))
    if days % 10 == 1 and days % 100 != 11:
        return 'день'
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return 'дня'
    return 'дней'


def _share_url(link: str) -> str:
    return (
        'https://t.me/share/url?url='
        + quote(link, safe='')
        + '&text='
        + quote('Присоединяйся по моей ссылке!', safe='')
    )


def build_days_only_referral_screen(
    *,
    user,
    summary: dict,
    bot_referral_link: str,
    trial_available: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает текст и клавиатуру партнёрки для схемы «только дни».

    Без подписки показывается CTA: сначала триал, а если он недоступен —
    «Моя подписка». Ссылку в этом состоянии не показываем: приглашать друзей
    ради дней, которые некуда начислить, бессмысленно.
    """
    back = InlineKeyboardButton(text='Назад', callback_data='back_to_menu')

    if not has_eligible_subscription(user):
        text = (
            f'<blockquote><tg-emoji emoji-id="{GIFT_ICON}">🎁</tg-emoji> '
            'Бонусные дни начисляются только\nпри наличии подписки.</blockquote>'
        )
        if trial_available:
            cta = InlineKeyboardButton(
                text='Начать триал',
                callback_data='referral_start_trial',
                icon_custom_emoji_id=CTA_ICON,
                style='primary',
            )
        else:
            cta = InlineKeyboardButton(
                text='Моя подписка',
                callback_data='menu_subscription',
                icon_custom_emoji_id=CTA_ICON,
                style='primary',
            )
        return text, InlineKeyboardMarkup(inline_keyboard=[[cta], [back]])

    trial_bonus_days = int(settings.REFERRAL_TRIAL_BONUS_DAYS)
    purchase_bonus_days = int(settings.REFERRAL_FIRST_PURCHASE_BONUS_DAYS)
    min_period_days = int(settings.REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS)

    text = (
        f'{HEADER}\n\n'
        f'{INVITED} Приглашено: <b>{int(summary.get("invited_count") or 0)}</b>\n'
        f'{PAID} Оплатили: <b>{int(summary.get("paid_referrals_count") or 0)}</b>\n'
        f'{RECEIVED} Получено бонусом: <b>+{int(summary.get("total_earned_days") or 0)} дн.</b>\n\n'
        f'{TERMS} <b>За каждого друга</b>\n'
        f'• +{trial_bonus_days} {_days_word(trial_bonus_days)} к триалу\n'
        f'• +{purchase_bonus_days} {_days_word(purchase_bonus_days)} вам и другу\n'
        f'  за первую покупку от {min_period_days} {_days_word(min_period_days)}\n\n'
        f'<blockquote><code>{html.escape(bot_referral_link)}</code></blockquote>'
    )

    share = InlineKeyboardButton(
        text='Пригласить',
        url=_share_url(bot_referral_link),
        icon_custom_emoji_id=SHARE_ICON,
        style='primary',
    )
    referrals = InlineKeyboardButton(
        text='Мои рефералы',
        callback_data='referral_list',
        icon_custom_emoji_id=LIST_ICON,
    )
    return text, InlineKeyboardMarkup(inline_keyboard=[[share], [referrals], [back]])
