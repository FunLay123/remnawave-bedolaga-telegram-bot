from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.discount_offer import (
    get_offer_by_id,
    get_offer_by_id_for_update,
    get_offer_subscription_for_update,
    lock_discount_offer_user_for_update,
    mark_offer_claimed,
)
from app.database.crud.promo_offer_template import get_promo_offer_template_by_id
from app.database.models import User
from app.localization.texts import get_texts
from app.services.promo_offer_service import promo_offer_service
from app.utils.miniapp_buttons import build_main_menu_button, build_miniapp_or_callback_button
from app.utils.payment_button_styles import apply_button_style
from app.utils.pricing_utils import (
    format_period_description,
)
from app.utils.promo_offer import (
    build_promo_offer_hint,
    get_user_active_promo_discount_percent,
)
from app.utils.timezone import format_local_datetime

from .common import _format_text_with_placeholders


async def _get_promo_offer_hint(
    db: AsyncSession,
    db_user: User,
    texts,
    percent: int | None = None,
) -> str | None:
    return await build_promo_offer_hint(db, db_user, texts, percent)


async def _build_promo_group_discount_text(
    db_user: User,
    periods: list[int] | None = None,
    texts=None,
) -> str:
    promo_group = db_user.get_primary_promo_group()

    if not promo_group:
        return ''

    if texts is None:
        texts = get_texts(db_user.language)

    service_lines: list[str] = []

    if promo_group.server_discount_percent > 0:
        service_lines.append(texts.PROMO_GROUP_DISCOUNT_SERVERS.format(percent=promo_group.server_discount_percent))

    if promo_group.traffic_discount_percent > 0:
        service_lines.append(texts.PROMO_GROUP_DISCOUNT_TRAFFIC.format(percent=promo_group.traffic_discount_percent))

    if promo_group.device_discount_percent > 0:
        service_lines.append(texts.PROMO_GROUP_DISCOUNT_DEVICES.format(percent=promo_group.device_discount_percent))

    period_lines: list[str] = []

    period_candidates: set[int] = set(periods or [])

    raw_period_discounts = getattr(promo_group, 'period_discounts', None)
    if isinstance(raw_period_discounts, dict):
        for key in raw_period_discounts.keys():
            try:
                period_candidates.add(int(key))
            except (TypeError, ValueError):
                continue

    for period_days in sorted(period_candidates):
        percent = promo_group.get_discount_percent('period', period_days)

        if percent <= 0:
            continue

        period_display = format_period_description(period_days, db_user.language)
        period_lines.append(
            texts.PROMO_GROUP_PERIOD_DISCOUNT_ITEM.format(
                period=period_display,
                percent=percent,
            )
        )

    if not service_lines and not period_lines:
        return ''

    lines: list[str] = [texts.PROMO_GROUP_DISCOUNTS_HEADER]

    if service_lines:
        lines.extend(service_lines)

    if period_lines:
        if service_lines:
            lines.append('')

        lines.append(texts.PROMO_GROUP_PERIOD_DISCOUNTS_HEADER)
        lines.extend(period_lines)

    return '\n'.join(lines)


WAVE_DISCOUNT_NOTIFICATION_TYPES = frozenset({'expired_discount_wave2', 'expired_discount_wave3'})

WAVE_CLAIM_SUCCESS_FALLBACK = (
    '<tg-emoji emoji-id="5303040337059543238">🎉</tg-emoji> <b>Скидка {percent}% активирована!</b>\n\n'
    'Она суммируется с другими скидками.\n'
    'Примените её при оплате до <b>{expires_at}</b>.'
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat naive legacy datetimes as UTC before comparing them to ``now``."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_lock_not_available(error: DBAPIError) -> bool:
    """Return True for PostgreSQL lock_not_available (SQLSTATE 55P03)."""

    orig = getattr(error, 'orig', None)
    sqlstate = getattr(orig, 'sqlstate', None) or getattr(orig, 'pgcode', None)
    return sqlstate == '55P03'


def _build_wave_claim_success_keyboard(texts) -> InlineKeyboardMarkup:
    purchase_button = apply_button_style(
        build_miniapp_or_callback_button(
            text=texts.get('MENU_BUY_SUBSCRIPTION', '💎 Купить подписку'),
            callback_data='subscription_upgrade',
        ),
        'subscription:upgrade',
    )
    main_menu_button = apply_button_style(
        build_main_menu_button(texts.get('BACK_TO_MENU', '🏠 В главное меню')),
        'success:main_menu',
    )
    return InlineKeyboardMarkup(inline_keyboard=[[purchase_button], [main_menu_button]])


async def _claim_wave_discount_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    offer,
    texts,
) -> None:
    """Claim one of the two expired-discount waves.

    Eligibility is re-checked against locked rows rather than the callback
    message: the subscription may have been renewed, the offer may have been
    deactivated, and a better personal discount may already be active.
    """

    now = datetime.now(UTC)
    expected_subscription_id = offer.subscription_id

    # Lock order: Subscription -> User -> DiscountOffer. Existing device and
    # tariff-switch flows hold the subscription row FOR UPDATE and then lock the
    # user through commit; taking the same order here prevents a real
    # Subscription/User deadlock cycle. The user lock is held until
    # `mark_offer_claimed` commits, making the no-downgrade comparison and the
    # discount write atomic.
    subscription = None
    if expected_subscription_id is not None:
        subscription = await get_offer_subscription_for_update(db, expected_subscription_id)

    try:
        locked_user = await lock_discount_offer_user_for_update(db, db_user.id)
    except DBAPIError as error:
        if not _is_lock_not_available(error):
            raise
        # NOWAIT aborted instead of waiting, so the transaction still holds the
        # subscription lock acquired above. Roll it back to release that lock
        # before showing the generic error; no claim state may be written.
        await db.rollback()
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_ERROR', '❌ Не удалось активировать скидку. Попробуйте позже.'),
            show_alert=True,
        )
        return

    if locked_user is None:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_NOT_FOUND', '❌ Предложение не найдено'),
            show_alert=True,
        )
        return
    db_user = locked_user

    locked_offer = await get_offer_by_id_for_update(db, offer.id)
    if locked_offer is None or locked_offer.user_id != db_user.id:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_NOT_FOUND', '❌ Предложение не найдено'),
            show_alert=True,
        )
        return

    offer = locked_offer

    if offer.subscription_id != expected_subscription_id:
        # The offer was re-pointed after the initial read; the locked
        # subscription belongs to the old binding, so this claim must not write.
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_WAVE_NOT_ELIGIBLE', 'ℹ️ Скидка недоступна: подписка уже не истекла.'),
            show_alert=True,
        )
        return

    if offer.claimed_at is not None:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_ALREADY', 'ℹ️ Скидка уже была активирована'),
            show_alert=True,
        )
        return

    offer_expires_at = _as_utc(offer.expires_at)
    if not offer.is_active or offer_expires_at is None or offer_expires_at <= now:
        offer.is_active = False
        await db.commit()
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_EXPIRED', '⚠️ Время действия предложения истекло'),
            show_alert=True,
        )
        return

    discount_percent = int(offer.discount_percent or 0)
    if discount_percent <= 0:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_ERROR', '❌ Не удалось активировать скидку. Попробуйте позже.'),
            show_alert=True,
        )
        return

    if subscription is None or subscription.user_id != db_user.id or subscription.actual_status != 'expired':
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_WAVE_NOT_ELIGIBLE', 'ℹ️ Скидка недоступна: подписка уже не истекла.'),
            show_alert=True,
        )
        return

    if get_user_active_promo_discount_percent(db_user) >= discount_percent:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_WAVE_ALREADY_BEST', 'ℹ️ У вас уже действует скидка не меньше этой.'),
            show_alert=True,
        )
        return

    db_user.promo_offer_discount_percent = discount_percent
    db_user.promo_offer_discount_source = offer.notification_type
    # The wave discount is usable until the deadline printed on the card; it
    # must not start a fresh `active_discount_hours` window at claim time.
    db_user.promo_offer_discount_expires_at = offer.expires_at
    db_user.updated_at = now

    await mark_offer_claimed(
        db,
        offer,
        details={
            'context': 'discount_claim',
            'discount_percent': discount_percent,
            'discount_expires_at': offer.expires_at.isoformat(),
        },
    )
    await db.refresh(db_user)

    success_template = texts.get('DISCOUNT_CLAIM_WAVE_SUCCESS', WAVE_CLAIM_SUCCESS_FALLBACK)
    expires_text = format_local_datetime(offer.expires_at, '%d.%m.%Y %H:%M')
    success_message = _format_text_with_placeholders(
        success_template,
        {'percent': discount_percent, 'expires_at': expires_text},
    )

    await callback.answer(
        texts.get('DISCOUNT_CLAIM_WAVE_POPUP', 'Скидка активирована!'),
        show_alert=True,
    )
    await callback.message.answer(
        success_message,
        reply_markup=_build_wave_claim_success_keyboard(texts),
    )


async def claim_discount_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    try:
        offer_id = int(callback.data.split('_')[-1])
    except (ValueError, AttributeError):
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_NOT_FOUND', '❌ Предложение не найдено'),
            show_alert=True,
        )
        return

    offer = await get_offer_by_id(db, offer_id)
    if not offer or offer.user_id != db_user.id:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_NOT_FOUND', '❌ Предложение не найдено'),
            show_alert=True,
        )
        return

    if offer.notification_type in WAVE_DISCOUNT_NOTIFICATION_TYPES:
        await _claim_wave_discount_offer(callback, db_user, db, offer, texts)
        return

    now = datetime.now(UTC)
    if offer.claimed_at is not None:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_ALREADY', 'ℹ️ Скидка уже была активирована'),
            show_alert=True,
        )
        return

    if not offer.is_active or offer.expires_at <= now:
        offer.is_active = False
        await db.commit()
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_EXPIRED', '⚠️ Время действия предложения истекло'),
            show_alert=True,
        )
        return

    effect_type = (offer.effect_type or 'percent_discount').lower()
    if effect_type == 'balance_bonus':
        effect_type = 'percent_discount'

    if effect_type == 'test_access':
        success, newly_added, expires_at, error_code = await promo_offer_service.grant_test_access(
            db,
            db_user,
            offer,
        )

        if not success:
            if error_code == 'subscription_missing':
                error_message = texts.get(
                    'TEST_ACCESS_NO_SUBSCRIPTION',
                    '❌ Для активации предложения необходима действующая подписка.',
                )
            elif error_code == 'squads_missing':
                error_message = texts.get(
                    'TEST_ACCESS_NO_SQUADS',
                    '❌ Не удалось определить список серверов для теста. Обратитесь к администратору.',
                )
            elif error_code == 'already_connected':
                error_message = texts.get(
                    'TEST_ACCESS_ALREADY_CONNECTED',
                    'ℹ️ Этот сервер уже подключен к вашей подписке.',
                )
            elif error_code == 'remnawave_sync_failed':
                error_message = texts.get(
                    'TEST_ACCESS_REMNAWAVE_ERROR',
                    '❌ Не удалось подключить серверы. Попробуйте позже или обратитесь в поддержку.',
                )
            else:
                error_message = texts.get(
                    'TEST_ACCESS_UNKNOWN_ERROR',
                    '❌ Не удалось активировать предложение. Попробуйте позже.',
                )
            await callback.answer(error_message, show_alert=True)
            return

        await mark_offer_claimed(
            db,
            offer,
            details={
                'context': 'test_access_claim',
                'new_squads': newly_added,
                'expires_at': expires_at.isoformat() if expires_at else None,
            },
        )

        expires_text = format_local_datetime(expires_at, '%d.%m.%Y %H:%M') if expires_at else ''
        success_message = texts.get(
            'TEST_ACCESS_ACTIVATED_MESSAGE',
            '🎉 Тестовые сервера подключены! Доступ активен до {expires_at}.',
        ).format(expires_at=expires_text)

        popup_text = texts.get('TEST_ACCESS_ACTIVATED_POPUP', '✅ Доступ выдан!')
        await callback.answer(popup_text, show_alert=True)
        back_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.get('BACK_TO_MENU', '🏠 В главное меню'),
                        callback_data='back_to_menu',
                    )
                ]
            ]
        )
        await callback.message.answer(success_message, reply_markup=back_keyboard)
        return

    discount_percent = int(offer.discount_percent or 0)
    if discount_percent <= 0:
        await callback.answer(
            texts.get('DISCOUNT_CLAIM_ERROR', '❌ Не удалось активировать скидку. Попробуйте позже.'),
            show_alert=True,
        )
        return

    db_user.promo_offer_discount_percent = discount_percent
    db_user.promo_offer_discount_source = offer.notification_type
    db_user.updated_at = now

    extra_data = offer.extra_data or {}
    raw_duration = extra_data.get('active_discount_hours')
    template_id = extra_data.get('template_id')

    if raw_duration in (None, '') and template_id:
        try:
            template = await get_promo_offer_template_by_id(db, int(template_id))
        except (ValueError, TypeError):
            template = None
        if template and template.active_discount_hours:
            raw_duration = template.active_discount_hours

    try:
        duration_hours = int(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration_hours = None

    if duration_hours and duration_hours > 0:
        discount_expires_at = now + timedelta(hours=duration_hours)
    else:
        discount_expires_at = None

    db_user.promo_offer_discount_expires_at = discount_expires_at

    await mark_offer_claimed(
        db,
        offer,
        details={
            'context': 'discount_claim',
            'discount_percent': discount_percent,
            'discount_expires_at': discount_expires_at.isoformat() if discount_expires_at else None,
        },
    )
    await db.refresh(db_user)

    success_template = texts.get(
        'DISCOUNT_CLAIM_SUCCESS',
        '🎉 Скидка {percent}% активирована! Она автоматически применится при следующей оплате.',
    )

    expires_text = format_local_datetime(discount_expires_at, '%d.%m.%Y %H:%M') if discount_expires_at else ''

    format_values: dict[str, Any] = {'percent': discount_percent}

    if duration_hours and duration_hours > 0:
        format_values.setdefault('hours', duration_hours)
        format_values.setdefault('duration_hours', duration_hours)

    if discount_expires_at:
        format_values.setdefault('expires_at', expires_text)
        format_values.setdefault('expires_at_iso', discount_expires_at.isoformat())
        try:
            expires_timestamp = int(discount_expires_at.timestamp())
        except (OverflowError, OSError, ValueError):
            expires_timestamp = None
        if expires_timestamp:
            format_values.setdefault('expires_at_ts', expires_timestamp)
        remaining_hours = int((discount_expires_at - now).total_seconds() // 3600)
        if remaining_hours > 0:
            format_values.setdefault('expires_in_hours', remaining_hours)

    amount_text = ''
    if isinstance(extra_data, dict):
        raw_amount_text = (
            extra_data.get('amount_text')
            or extra_data.get('discount_amount_text')
            or extra_data.get('formatted_amount')
        )
        if isinstance(raw_amount_text, str) and raw_amount_text.strip():
            amount_text = raw_amount_text.strip()
        else:
            raw_amount = extra_data.get('amount') or extra_data.get('discount_amount')
            if isinstance(raw_amount, (int, float)):
                amount_text = settings.format_price(int(raw_amount))
            elif isinstance(raw_amount, str) and raw_amount.strip():
                amount_text = raw_amount.strip()

        if not amount_text:
            for key in ('discount_amount_kopeks', 'amount_kopeks', 'bonus_amount_kopeks'):
                maybe_amount = extra_data.get(key)
                try:
                    amount_value = int(maybe_amount)
                except (TypeError, ValueError):
                    continue
                if amount_value > 0:
                    amount_text = settings.format_price(amount_value)
                    break

        for key, value in extra_data.items():
            if (
                isinstance(key, str)
                and key.isidentifier()
                and key not in format_values
                and isinstance(value, (str, int, float))
            ):
                format_values[key] = value

    if not amount_text:
        try:
            bonus_amount = int(getattr(offer, 'bonus_amount_kopeks', 0))
        except (TypeError, ValueError):
            bonus_amount = 0
        if bonus_amount > 0:
            amount_text = settings.format_price(bonus_amount)

    if amount_text:
        format_values.setdefault('amount', amount_text)

    success_message = _format_text_with_placeholders(success_template, format_values)

    await callback.answer('✅ Скидка активирована!', show_alert=True)

    offer_type = None
    if isinstance(extra_data, dict):
        offer_type = extra_data.get('offer_type')

    subscription = getattr(db_user, 'subscription', None)

    if offer_type == 'purchase_discount':
        button_text = texts.get('MENU_BUY_SUBSCRIPTION', '💎 Купить подписку')
        button_callback = 'subscription_upgrade'
    elif offer_type == 'extend_discount':
        button_text = texts.get('SUBSCRIPTION_EXTEND', '💎 Продлить подписку')
        button_callback = 'subscription_extend'
    else:
        has_active_paid_subscription = bool(
            subscription and getattr(subscription, 'is_active', False) and not getattr(subscription, 'is_trial', False)
        )

        if has_active_paid_subscription:
            button_text = texts.get('SUBSCRIPTION_EXTEND', '💎 Продлить подписку')
            button_callback = 'subscription_extend'
        else:
            button_text = texts.get('MENU_BUY_SUBSCRIPTION', '💎 Купить подписку')
            button_callback = 'subscription_upgrade'

    buy_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                build_miniapp_or_callback_button(
                    text=button_text,
                    callback_data=button_callback,
                )
            ]
        ]
    )
    await callback.message.answer(success_message, reply_markup=buy_keyboard)


async def handle_promo_offer_close(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_reply_markup()
        except Exception:
            pass

    await callback.answer()
