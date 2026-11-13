"""Alternative payment methods offered to a user after a failed top-up.

The module builds a localized fallback screen from the existing amount-first
top-up keyboard and can proactively send it once per provider/payment. A Redis
``SET NX`` claim with a 24-hour TTL deduplicates delivery; when Redis is not
connected a single-process monotonic timestamp map provides the same behavior.
"""

from __future__ import annotations

import html
import time
from typing import Any

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.keyboards.inline import get_payment_methods_keyboard
from app.localization.texts import get_texts
from app.utils.cache import cache


logger = structlog.get_logger(__name__)

FALLBACK_COOLDOWN_SECONDS = 24 * 3600

# Single-process fallback when Redis is unavailable. Maps a cooldown key to the
# monotonic time at which the claim expires.
_MEMORY_CLAIMS: dict[str, float] = {}

_METHOD_LABELS = {
    'yookassa': 'YooKassa',
    'yookassa_sbp': 'YooKassa (СБП)',
    'heleket': 'Heleket',
}

_DEFAULT_TEXT = (
    '❗️ <b>Платёж не прошёл</b>\n\nСпособ оплаты: {method}\nСумма: {amount}\n\nВыберите другой способ пополнения:'
)
_DEFAULT_SUPPORT_BUTTON = '🛠️ Написать в поддержку'


def _language_of(user: Any) -> str:
    return getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE


def _method_label(failed_method: str | None) -> str:
    method = (failed_method or '').strip().lower()
    return _METHOD_LABELS.get(method, failed_method or '—')


def _claim_memory(key: str, ttl_seconds: int, now_monotonic: float) -> bool:
    expires_at = _MEMORY_CLAIMS.get(key)
    if expires_at is not None and expires_at > now_monotonic:
        return False

    _MEMORY_CLAIMS[key] = now_monotonic + ttl_seconds

    if len(_MEMORY_CLAIMS) > 1024:
        for stale_key, stale_expiry in list(_MEMORY_CLAIMS.items()):
            if stale_expiry <= now_monotonic:
                _MEMORY_CLAIMS.pop(stale_key, None)

    return True


async def _try_claim(key: str, ttl_seconds: int, *, use_redis: bool) -> bool:
    if use_redis:
        return await cache.setnx(key, '1', expire=ttl_seconds)
    return _claim_memory(key, ttl_seconds, time.monotonic())


async def _release_claim(key: str, *, use_redis: bool) -> None:
    if use_redis:
        await cache.delete(key)
    else:
        _MEMORY_CLAIMS.pop(key, None)


def build_payment_fallback(
    user: Any,
    amount_kopeks: int,
    failed_method: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the Russian fallback text and a keyboard without ``failed_method``."""

    language = _language_of(user)
    texts = get_texts(language)

    keyboard = get_payment_methods_keyboard(
        amount_kopeks,
        language,
        back_callback='balance_topup',
        exclude_methods={failed_method} if failed_method else None,
    )

    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.inline_keyboard.insert(
            -1,
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_FALLBACK_SUPPORT_BUTTON', _DEFAULT_SUPPORT_BUTTON),
                    url=support_url,
                )
            ],
        )

    text = texts.t('PAYMENT_FALLBACK_TEXT', _DEFAULT_TEXT).format(
        method=html.escape(_method_label(failed_method)),
        amount=settings.format_price(max(0, int(amount_kopeks or 0))),
    )
    return text, keyboard


async def send_payment_fallback_once(
    bot: Any,
    user: Any,
    amount_kopeks: int,
    failed_method: str,
    payment_id: Any,
) -> bool:
    """Send the fallback screen at most once per provider/payment in 24 hours."""

    telegram_id = getattr(user, 'telegram_id', None)
    if bot is None or not telegram_id:
        return False

    method = (failed_method or '').strip()
    key = f'payment_fallback:{method}:{payment_id}'
    use_redis = bool(getattr(cache, '_connected', False))

    try:
        claimed = await _try_claim(key, FALLBACK_COOLDOWN_SECONDS, use_redis=use_redis)
    except Exception as error:  # pragma: no cover - Redis failures are environmental
        logger.warning('Не удалось взять cooldown fallback-платежа', key=key, error=error)
        return False

    if not claimed:
        return False

    try:
        text, keyboard = build_payment_fallback(user, amount_kopeks, method)
        sent = await bot.send_message(
            telegram_id,
            text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    except Exception as error:
        logger.warning('Не удалось отправить fallback-предложение оплаты', key=key, error=error)
        await _release_claim(key, use_redis=use_redis)
        return False

    if sent is False:
        logger.warning('Fallback-предложение оплаты не отправлено, освобождаем cooldown', key=key)
        await _release_claim(key, use_redis=use_redis)
        return False

    logger.info(
        'Предложены альтернативные способы оплаты после неудачи',
        failed_method=method,
        payment_id=payment_id,
    )
    return True
