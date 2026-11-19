"""Админская покупка классической подписки при реферальном триале."""

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.admin import users as admin_users


class _ApiContext:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('scheme', 'base_offset', 'carry_enabled', 'expected_days'),
    [
        # days_only с известной базой: платный срок от now + купленные дни + перенос.
        ('days_only', timedelta(days=1), True, 49),
        # levels без базы: прежняя админская формула — база = будущий end_date
        # (20 дней остатка входит и как база продления, и как bonus_period).
        ('levels', None, True, 70),
        ('levels', None, False, 50),
    ],
)
async def test_admin_classic_purchase_carries_trial_remainder_once(
    monkeypatch, scheme, base_offset, carry_enabled, expected_days
):
    """days_only с базой стартует от now; legacy/levels — от будущего end_date; остаток — один раз."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', scheme)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', carry_enabled)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    # Конверсия триала должна получить дефолт автоплатежа.
    monkeypatch.setattr(type(settings), 'is_autopay_enabled_by_default', lambda self: True)

    before = datetime.now(UTC)
    original_start = before - timedelta(days=1)
    subscription = SimpleNamespace(
        id=10,
        user_id=7,
        status='trial',
        is_trial=True,
        start_date=original_start,
        end_date=before + timedelta(days=20),
        base_trial_end_date=before + base_offset if base_offset is not None else None,
        traffic_limit_gb=10,
        traffic_used_gb=2.0,
        device_limit=1,
        connected_squads=['squad-1'],
        tariff=None,
        remnawave_short_uuid=None,
        updated_at=None,
        autopay_enabled=False,
    )
    target_user = SimpleNamespace(
        id=7,
        telegram_id=777,
        email=None,
        balance_kopeks=200_000,
    )

    monkeypatch.setattr(
        admin_users.UserService,
        'get_user_profile',
        AsyncMock(return_value={'user': target_user, 'subscription': subscription}),
    )
    monkeypatch.setattr(admin_users, 'SubscriptionService', MagicMock())
    monkeypatch.setattr(admin_users, '_calculate_subscription_period_price', AsyncMock(return_value=10_000))
    monkeypatch.setattr(admin_users, 'user_html_link', lambda user: 'user')
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=target_user))
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock())
    monkeypatch.setattr('app.services.grace_access_echo.undo_grace_overlay_echo', AsyncMock(return_value=set()))
    monkeypatch.setattr(
        'app.services.panel_sync.push_subscription',
        AsyncMock(return_value=SimpleNamespace(panel_user=MagicMock())),
    )
    remnawave_service = MagicMock()
    remnawave_service.get_api_client.return_value = _ApiContext()
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', lambda: remnawave_service)

    callback = SimpleNamespace(
        data='admin_buy_sub_execute_7_30_10000',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
        bot=None,
    )
    db = AsyncMock()

    await inspect.unwrap(admin_users.admin_buy_subscription_execute)(callback, MagicMock(), db)
    after = datetime.now(UTC)

    assert subscription.is_trial is False
    expected_end = before + timedelta(days=expected_days)
    tolerance = timedelta(seconds=2)
    assert expected_end - tolerance <= subscription.end_date <= after + timedelta(days=expected_days) + tolerance
    assert subscription.end_date < before + timedelta(days=expected_days + 1)
    if base_offset is not None:
        # days_only с известной базой: платный период стартует от now.
        assert before <= subscription.start_date <= after
    else:
        # legacy/levels: база — будущий end_date, start_date не переписывается.
        assert subscription.start_date == original_start
    # Конверсия триала → платная: автоплатёж по умолчанию.
    assert subscription.autopay_enabled is True
