"""Строка о переносе реферальных дней в меню покупки тарифа."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.subscription.tariff_purchase as purchase_module
from app.config import settings
from app.utils.timezone import format_local_datetime


def _trial(
    now: datetime,
    *,
    base_in_days: int | None = 1,
    end_in_days: int = 20,
    is_trial: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        user_id=1,
        is_trial=is_trial,
        tariff_id=None,
        base_trial_end_date=None if base_in_days is None else now + timedelta(days=base_in_days),
        end_date=now + timedelta(days=end_in_days),
        device_limit=1,
    )


def _enable_days_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')


def test_note_shows_only_full_unused_referral_days(monkeypatch):
    _enable_days_only(monkeypatch)
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    subscription = _trial(now)

    note = purchase_module._format_referral_carry_over_note(subscription, 'ru', now=now)

    assert note == '\n\n<blockquote>Реферальные дни перенесутся в основной тариф — 19 дней</blockquote>'
    assert '🎁' not in note


@pytest.mark.parametrize(
    ('base_in_days', 'end_in_days', 'expected'),
    [
        (1, 2, '1 день'),
        (1, 3, '2 дня'),
        (1, 23, '22 дня'),
        (1, 6, '5 дней'),
    ],
)
def test_note_declines_days(monkeypatch, base_in_days, end_in_days, expected):
    _enable_days_only(monkeypatch)
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

    note = purchase_module._format_referral_carry_over_note(
        _trial(now, base_in_days=base_in_days, end_in_days=end_in_days),
        'ru',
        now=now,
    )

    assert f'— {expected}</blockquote>' in note


@pytest.mark.parametrize(
    'subscription_factory',
    [
        lambda now: _trial(now, base_in_days=None),
        lambda now: _trial(now, is_trial=False),
        lambda now: _trial(now, base_in_days=1, end_in_days=0),
        lambda now: None,
    ],
)
def test_note_is_hidden_without_transferable_referral_days(monkeypatch, subscription_factory):
    _enable_days_only(monkeypatch)
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

    assert purchase_module._format_referral_carry_over_note(subscription_factory(now), 'ru', now=now) == ''


def test_note_is_hidden_for_levels_scheme(monkeypatch):
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    assert purchase_module._format_referral_carry_over_note(_trial(now), 'ru', now=now) == ''


async def test_period_selection_screen_shows_referral_carry_note(monkeypatch):
    _enable_days_only(monkeypatch)
    now = datetime.now(UTC)
    trial = _trial(now, base_in_days=1, end_in_days=20)
    tariff = SimpleNamespace(
        id=42,
        name='Базовый',
        is_active=True,
        is_daily=False,
        can_purchase_custom_days=MagicMock(return_value=False),
        can_purchase_custom_traffic=MagicMock(return_value=False),
    )
    callback = SimpleNamespace(
        data='tariff_select:42',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.update_data = AsyncMock()

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(purchase_module, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(purchase_module, 'get_subscription_by_user_id', AsyncMock(return_value=trial))
    monkeypatch.setattr(purchase_module, 'format_tariff_info_for_user', MagicMock(return_value='INFO'))
    monkeypatch.setattr(purchase_module, 'get_tariff_periods_keyboard', MagicMock(return_value='KB'))

    await purchase_module._proceed_with_selected_tariff(
        callback,
        SimpleNamespace(id=1, language='ru'),
        AsyncMock(),
        state,
        42,
        skip_selection=True,
    )

    rendered = callback.message.edit_text.await_args.args[0]
    assert rendered.startswith('INFO\n\n<blockquote>Реферальные дни перенесутся в основной тариф — ')
    assert '19 дней</blockquote>' in rendered


def _tariff() -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        name='Базовый',
        is_active=True,
        device_limit=1,
        traffic_limit_gb=0,
        period_prices={'30': 10000},
    )


def _pricing_result() -> SimpleNamespace:
    return SimpleNamespace(
        final_total=10_000,
        original_total=10_000,
        promo_group_discount=0,
        promo_offer_discount=0,
    )


def _patch_tariff_pricing(monkeypatch, subscription: SimpleNamespace) -> None:
    from app.services.pricing_engine import pricing_engine

    monkeypatch.setattr(purchase_module, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    monkeypatch.setattr(
        purchase_module,
        'get_subscription_by_id_for_user',
        AsyncMock(return_value=subscription),
    )
    monkeypatch.setattr(
        pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=_pricing_result()),
    )
    monkeypatch.setattr(
        purchase_module,
        'get_tariff_extend_confirm_keyboard',
        MagicMock(return_value='KB'),
    )
    monkeypatch.setattr(
        purchase_module,
        'get_tariff_confirm_keyboard',
        MagicMock(return_value='KB'),
    )


def _assert_preview_date(rendered: str, prefix: str, before: datetime, after: datetime, days: int) -> None:
    expected = {
        format_local_datetime(before + timedelta(days=days), '%d.%m.%Y'),
        format_local_datetime(after + timedelta(days=days), '%d.%m.%Y'),
    }
    assert any(f'{prefix}{date})' in rendered for date in expected), rendered


@pytest.mark.asyncio
async def test_renewal_preview_date_matches_days_only_conversion(monkeypatch):
    """Превью продления: now + 30 + 20 хвоста = now+50, а не end_date+30 = now+52."""
    _enable_days_only(monkeypatch)
    before = datetime.now(UTC)
    subscription = _trial(before, base_in_days=2, end_in_days=22)
    _patch_tariff_pricing(monkeypatch, subscription)

    callback = SimpleNamespace(
        data='tariff_extend:7:42:30',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await purchase_module.select_tariff_extend_period(
        callback,
        SimpleNamespace(id=1, language='ru', balance_kopeks=200_000),
        AsyncMock(),
        MagicMock(),
    )
    after = datetime.now(UTC)

    rendered = callback.message.edit_text.await_args.args[0]
    _assert_preview_date(rendered, '(До ', before, after, 50)


@pytest.mark.asyncio
async def test_purchase_confirm_date_includes_referral_tail(monkeypatch):
    """Подтверждение покупки: now + 30 + 20 хвоста = now+50."""
    _enable_days_only(monkeypatch)
    before = datetime.now(UTC)
    subscription = _trial(before, base_in_days=2, end_in_days=22)
    _patch_tariff_pricing(monkeypatch, subscription)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(
        purchase_module,
        'get_subscription_by_user_id',
        AsyncMock(return_value=subscription),
    )

    callback = SimpleNamespace(
        data='tariff_period:42:30',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    state = MagicMock()
    state.update_data = AsyncMock()
    await purchase_module.select_tariff_period(
        callback,
        SimpleNamespace(id=1, language='ru', balance_kopeks=200_000),
        AsyncMock(),
        state,
    )
    after = datetime.now(UTC)

    rendered = callback.message.edit_text.await_args.args[0]
    _assert_preview_date(rendered, '(До ', before, after, 50)
