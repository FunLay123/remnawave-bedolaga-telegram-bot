"""Discount-wave card: shared renewal period, wave-only preview price, gate opt-out.

The wave preview must charge the wave percent exactly once: the previous personal
offer is replaced (not stacked), while the promo-group discount still applies
first with the engine's sequential integer arithmetic.  The card names the same
period the renewal/autopay path resolves and the same-period checkout uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    PromoGroup,
    ServerSquad,
    Subscription,
    Tariff,
    User,
    UserPromoGroup,
    server_squad_promo_groups,
    tariff_promo_groups,
)
from app.services import monitoring_service as ms
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from app.services.pricing_engine import pricing_engine
from app.utils.notification_emoji import WARNING_EMOJI_ID
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    ServerSquad.__table__,
    server_squad_promo_groups,
    Subscription.__table__,
)

WAVE_ICON_ID = '5303040337059543238'
CLAIM_ICON_ID = '5235695112419303615'
TOPUP_ICON_ID = '5258500422393415126'

FIXED_EXPIRES = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
FIXED_EXPIRES_TEXT = '02.01.2030 03:04'


async def _seed(
    db,
    *,
    personal_percent: int = 0,
    group_percent: int = 0,
    end_date: datetime | None = None,
    autopay_period_days: int = 30,
    period_prices: dict[str, int] | None = None,
) -> None:
    now = datetime.now(UTC)
    expiry = end_date or (now - timedelta(days=2, hours=12))
    db.add(
        User(
            id=1,
            telegram_id=1001,
            first_name='Тест',
            language='ru',
            status='active',
            balance_kopeks=0,
            promo_offer_discount_percent=personal_percent,
            promo_offer_discount_expires_at=(now + timedelta(hours=12) if personal_percent else None),
        )
    )
    if group_percent:
        db.add(PromoGroup(id=5, name='Группа', priority=10, period_discounts={'30': group_percent}))
        db.add(UserPromoGroup(user_id=1, promo_group_id=5, assigned_by='test'))
    db.add(
        Tariff(
            id=3,
            name='Стартовый',
            description='',
            is_active=True,
            traffic_limit_gb=50,
            device_limit=1,
            allowed_squads=[],
            period_prices=period_prices or {'30': 100_000},
            display_order=1,
        )
    )
    await db.flush()
    db.add(
        Subscription(
            id=4,
            remnawave_short_id='s4',
            user_id=1,
            status='expired',
            is_trial=False,
            start_date=expiry - timedelta(days=30),
            end_date=expiry,
            traffic_limit_gb=50,
            traffic_used_gb=0.0,
            device_limit=1,
            tariff_id=3,
            autopay_period_days=autopay_period_days,
            connected_squads=[],
        )
    )
    await db.commit()
    db.expunge_all()


async def _load_subscription(db) -> Subscription:
    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user).options(
                selectinload(User.promo_group),
                selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            ),
            selectinload(Subscription.tariff),
        )
        .where(Subscription.id == 4)
    )
    return result.scalar_one()


def _sender_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    return bot


def _disable_logo(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)


def _buttons(bot) -> list:
    keyboard = bot.send_message.await_args.kwargs['reply_markup']
    return keyboard.inline_keyboard


def _expected_card(percent: int, price: str) -> str:
    return (
        f'<tg-emoji emoji-id="{WAVE_ICON_ID}">⚠️</tg-emoji> '
        f'<b>Скидка {percent}% на продление подписки</b>\n\n'
        f'Активируйте и используйте до <b>{FIXED_EXPIRES_TEXT}</b>\n'
        f'Цена со скидкой за 1 месяц: <b>{price}</b>\n\n'
        'Заберите скидку и продлите доступ.'
    )


# ── Preview pricing ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wave_preview_replaces_previous_personal_offer(monkeypatch):
    """1000 ₽ base + previous 10% personal offer + new 20% wave displays 800 ₽, not 720 ₽."""
    _disable_logo(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, personal_percent=10)
        subscription = await _load_subscription(db)

        sent = await service._send_expired_discount_notification(
            subscription.user,
            subscription,
            20,
            FIXED_EXPIRES,
            99,
            'third',
            db=db,
        )

        assert sent is True
        text = bot.send_message.await_args.kwargs['text']
        assert text == _expected_card(20, '800 ₽')
        assert '720 ₽' not in text

        # The SQLAlchemy user is never mutated for preview arithmetic.
        user = await db.get(User, 1)
        assert user.promo_offer_discount_percent == 10

        # The same 30-day period in checkout (claimed wave = 20% personal
        # offer) charges exactly the card price.
        with patch(
            'app.services.pricing_engine.get_user_active_promo_discount_percent',
            return_value=20,
        ):
            checkout = await pricing_engine.calculate_renewal_price(db, subscription, 30, user=subscription.user)
        assert checkout.final_total == 80_000


@pytest.mark.asyncio
async def test_wave_card_names_the_resolved_autopay_period(monkeypatch):
    """A 60-day autopay period is named on the card and priced for 60 days."""
    _disable_logo(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            db,
            autopay_period_days=60,
            period_prices={'30': 100_000, '60': 180_000},
        )
        subscription = await _load_subscription(db)

        sent = await service._send_expired_discount_notification(
            subscription.user,
            subscription,
            10,
            FIXED_EXPIRES,
            77,
            'second',
            db=db,
        )

    assert sent is True
    text = bot.send_message.await_args.kwargs['text']
    assert 'Цена со скидкой за 2 месяца: <b>1620 ₽</b>' in text


@pytest.mark.asyncio
async def test_wave_preview_stacks_promo_group_then_wave(monkeypatch):
    """Group 10% then wave 20% is 28% effective: 1000 → 900 → 720 ₽."""
    _disable_logo(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, group_percent=10)
        subscription = await _load_subscription(db)

        sent = await service._send_expired_discount_notification(
            subscription.user,
            subscription,
            20,
            FIXED_EXPIRES,
            99,
            'third',
            db=db,
        )

        assert sent is True
        text = bot.send_message.await_args.kwargs['text']
        assert text == _expected_card(20, '720 ₽')
        assert '700 ₽' not in text


@pytest.mark.asyncio
async def test_second_wave_card_uses_same_layout(monkeypatch):
    """Both wave templates use the same layout, own percent, period and price."""
    _disable_logo(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = await _load_subscription(db)

        sent = await service._send_expired_discount_notification(
            subscription.user,
            subscription,
            10,
            FIXED_EXPIRES,
            77,
            'second',
            db=db,
        )

        assert sent is True
        text = bot.send_message.await_args.kwargs['text']
        assert text == _expected_card(10, '900 ₽')


# ── Card layout ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wave_card_buttons_icons_callbacks_and_row_order(monkeypatch):
    """Claim is a primary row with its own icon; topup is the second, non-primary row."""
    _disable_logo(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, personal_percent=10)
        subscription = await _load_subscription(db)

        await service._send_expired_discount_notification(
            subscription.user,
            subscription,
            20,
            FIXED_EXPIRES,
            99,
            'third',
            db=db,
        )

    rows = _buttons(bot)
    assert len(rows) == 2
    assert len(rows[0]) == 1
    assert len(rows[1]) == 1

    claim, topup = rows[0][0], rows[1][0]

    assert claim.text == 'Получить скидку'
    assert claim.callback_data == 'claim_discount_99'
    assert claim.style == 'primary'
    assert claim.icon_custom_emoji_id == CLAIM_ICON_ID

    assert topup.text == 'Пополнить баланс'
    assert topup.callback_data == 'balance_topup'
    assert topup.style != 'primary'
    assert topup.icon_custom_emoji_id == TOPUP_ICON_ID


@pytest.mark.asyncio
async def test_wave_card_preserves_custom_wave_emoji_in_both_send_paths(monkeypatch):
    """The wave gate opt-out keeps the card's own custom emoji (classic send path)."""
    _disable_logo(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = await _load_subscription(db)
        await service._send_expired_discount_notification(
            subscription.user,
            subscription,
            10,
            FIXED_EXPIRES,
            77,
            'second',
            db=db,
        )

    text = bot.send_message.await_args.kwargs['text']
    assert f'<tg-emoji emoji-id="{WAVE_ICON_ID}">⚠️</tg-emoji>' in text
    assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">' not in text


# ── Suppression of a weaker wave ───────────────────────────────────────


@pytest.mark.asyncio
async def test_wave_send_suppressed_when_active_discount_is_better(monkeypatch):
    """An active 50% personal offer suppresses a weaker 10% wave send."""
    _disable_logo(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, personal_percent=50)
        subscription = await _load_subscription(db)

        sent = await service._send_expired_discount_notification(
            subscription.user,
            subscription,
            10,
            FIXED_EXPIRES,
            77,
            'second',
            db=db,
        )

    assert sent is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_followups_do_not_create_wave_when_better_discount_active(monkeypatch):
    """The follow-up loop must not create an offer for a weaker wave at all."""
    monkeypatch.setattr(NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(NotificationSettingsService, 'is_expired_1d_enabled', lambda: False)
    monkeypatch.setattr(NotificationSettingsService, 'is_second_wave_enabled', lambda: True)
    monkeypatch.setattr(NotificationSettingsService, 'is_third_wave_enabled', lambda: False)
    monkeypatch.setattr(NotificationSettingsService, 'get_second_wave_discount_percent', lambda: 10)
    monkeypatch.setattr(NotificationSettingsService, 'get_second_wave_valid_hours', lambda: 24)
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda user: True)

    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, personal_percent=50)
        await service._check_expired_subscription_followups(db)

    upsert.assert_not_awaited()
    bot.send_message.assert_not_awaited()


# ── Shared renewal period ──────────────────────────────────────────────


def test_resolve_renewal_period_prefers_valid_autopay_candidate():
    service = MonitoringService(bot=None)
    tariff = SimpleNamespace(get_available_periods=lambda: [30, 90], get_shortest_period=lambda: 30)
    subscription = SimpleNamespace(autopay_period_days=90, tariff=tariff)

    assert service._resolve_renewal_period_days(subscription) == 90


def test_resolve_renewal_period_rejects_unlisted_autopay_candidate(monkeypatch):
    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 30)
    service = MonitoringService(bot=None)
    tariff = SimpleNamespace(get_available_periods=lambda: [30, 90], get_shortest_period=lambda: 30)
    subscription = SimpleNamespace(autopay_period_days=45, tariff=tariff)

    assert service._resolve_renewal_period_days(subscription) == 30


def test_resolve_renewal_period_falls_back_to_30_without_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 0)
    service = MonitoringService(bot=None)
    subscription = SimpleNamespace(autopay_period_days=None, tariff=None)

    assert service._resolve_renewal_period_days(subscription) == 30


@pytest.mark.asyncio
async def test_renewal_cost_and_wave_preview_use_same_resolved_period(monkeypatch):
    """The normal renewal cost path shares ``_resolve_renewal_period_days``."""
    service = MonitoringService(bot=None)
    tariff = SimpleNamespace(get_available_periods=lambda: [30, 90], get_shortest_period=lambda: 30)
    subscription = SimpleNamespace(autopay_period_days=90, tariff=tariff)

    from app.services import pricing_engine as pricing_engine_module

    calculate = AsyncMock(return_value=SimpleNamespace(final_total=12345))
    monkeypatch.setattr(pricing_engine_module.pricing_engine, 'calculate_renewal_price', calculate)

    price = await service._resolve_renewal_cost_kopeks(object(), subscription, SimpleNamespace())

    assert price == 12345
    assert calculate.await_args.args[2] == 90
