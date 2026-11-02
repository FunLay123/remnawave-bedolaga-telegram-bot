"""Экран «Отключение рекламы»: включение/выключение adblock-DNS у подписки."""

from __future__ import annotations

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import ADBLOCK_ACTION_ICON
from app.localization.texts import get_texts

from .links import _resolve_subscription


logger = structlog.get_logger(__name__)

SCREEN_OFF = """<tg-emoji emoji-id="5359415772844152891">🌾</tg-emoji> <b>Отключение рекламы</b>
<i>Только для INCY и Happ</i>

<blockquote>Эта функция добавляет в ваш VPN-профиль DNS-резолвер, который блокирует рекламу и трекеры. Отдельный профиль не создаётся — меняется только DNS в текущем.

Иногда фильтр может ошибочно задеть нужный сайт. В этом случае блокировку можно выключить этой же кнопкой.

Если основной DNS-сервер с фильтрацией недоступен, запросы автоматически уходят на запасные резолверы — интернет продолжит работать.</blockquote>"""

SCREEN_ON = """<tg-emoji emoji-id="5359415772844152891">🌾</tg-emoji> <b>Отключение рекламы</b>
<i>Только для INCY и Happ</i>

<blockquote>Блокировка рекламы включена: в вашем профиле используется DNS-резолвер с фильтрацией рекламы и трекеров.

После включения обновите подписку в приложении — изменения применятся при следующем обновлении.

Если фильтр задел нужный сайт, выключите блокировку этой же кнопкой.</blockquote>"""


def _feature_available() -> bool:
    return settings.is_adblock_dns_enabled() and settings.is_subscription_proxy_enabled()


def _screen_text(texts, enabled: bool) -> str:
    key = 'SUBSCRIPTION_ADBLOCK_SCREEN_ON' if enabled else 'SUBSCRIPTION_ADBLOCK_SCREEN_OFF'
    return texts.t(key, SCREEN_ON if enabled else SCREEN_OFF)


def _action_callback(action: str, sub_id: int | None) -> str:
    if settings.is_multi_tariff_enabled() and sub_id:
        return f'sub_adblock:{action}:{sub_id}'
    return f'sub_adblock:{action}'


def _back_callback(sub_id: int | None) -> str:
    if settings.is_multi_tariff_enabled() and sub_id:
        return f'subscription_connect:{sub_id}'
    return 'subscription_connect'


def _screen_keyboard(texts, sub_id: int | None, enabled: bool) -> InlineKeyboardMarkup:
    action_text = texts.t(
        'SUBSCRIPTION_ADBLOCK_DISABLE' if enabled else 'SUBSCRIPTION_ADBLOCK_ENABLE',
        'Выключить' if enabled else 'Включить',
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=action_text,
                    callback_data=_action_callback('disable' if enabled else 'enable', sub_id),
                    style='primary',
                    icon_custom_emoji_id=ADBLOCK_ACTION_ICON,
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data=_back_callback(sub_id))],
        ]
    )


async def _refuse_if_unavailable(callback: types.CallbackQuery, texts) -> bool:
    if _feature_available():
        return False
    await callback.answer(
        texts.t('SUBSCRIPTION_ADBLOCK_UNAVAILABLE', 'Функция сейчас недоступна.'),
        show_alert=True,
    )
    return True


async def handle_adblock_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext = None,
):
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    if await _refuse_if_unavailable(callback, texts):
        return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    enabled = bool(getattr(subscription, 'adblock_dns_enabled', False))
    await callback.message.edit_text(
        _screen_text(texts, enabled),
        reply_markup=_screen_keyboard(texts, sub_id, enabled),
        parse_mode='HTML',
    )
    await callback.answer()


async def _set_adblock(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    *,
    enabled: bool,
    state: FSMContext = None,
):
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    if await _refuse_if_unavailable(callback, texts):
        return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    previous = bool(getattr(subscription, 'adblock_dns_enabled', False))
    subscription.adblock_dns_enabled = enabled
    try:
        await db.commit()
    except Exception as error:
        await db.rollback()
        subscription.adblock_dns_enabled = previous
        logger.warning(
            'Не удалось изменить adblock-DNS у подписки',
            subscription_id=getattr(subscription, 'id', None),
            error=str(error),
        )
        await callback.answer(
            texts.t('SUBSCRIPTION_ADBLOCK_ERROR', 'Не удалось изменить настройку. Попробуйте позже.'),
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        _screen_text(texts, enabled),
        reply_markup=_screen_keyboard(texts, sub_id, enabled),
        parse_mode='HTML',
    )
    await callback.answer(
        texts.t(
            'SUBSCRIPTION_ADBLOCK_ENABLED_ALERT' if enabled else 'SUBSCRIPTION_ADBLOCK_DISABLED_ALERT',
            'Готово. Обновите подписку в приложении, чтобы применить изменения.'
            if enabled
            else 'Блокировка рекламы выключена.',
        )
    )


async def handle_adblock_enable(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext = None,
):
    await _set_adblock(callback, db_user, db, enabled=True, state=state)


async def handle_adblock_disable(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext = None,
):
    await _set_adblock(callback, db_user, db, enabled=False, state=state)
