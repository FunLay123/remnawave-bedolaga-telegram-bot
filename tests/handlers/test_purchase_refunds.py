"""Возврат денег, если после списания покупка не была сохранена."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.admin.users as admin_users
import app.handlers.subscription.tariff_purchase as tariff_mod
from app.services.pricing_engine import PricingEngine, pricing_engine


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
        bot=None,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        telegram_id=777,
        email=None,
        language='ru',
        balance_kopeks=200_000,
        restriction_subscription=False,
    )


def _tariff() -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        name='Базовый',
        is_active=True,
        device_limit=1,
        traffic_limit_gb=0,
        allowed_squads=[],
        period_prices={'30': 10_000},
    )


@pytest.mark.asyncio
async def test_tariff_extend_commits_charge_and_extension_atomically(monkeypatch):
    """«Продлить»: списание и продление — одна транзакция; сбой не требует возврата."""
    now = datetime.now(UTC)
    user = _user()
    subscription = SimpleNamespace(
        id=10,
        user_id=user.id,
        tariff_id=42,
        is_trial=False,
        status='active',
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(days=10),
        base_trial_end_date=None,
        device_limit=1,
        traffic_limit_gb=0,
        connected_squads=[],
    )
    refund = AsyncMock(return_value=True)
    subtract = AsyncMock(return_value=True)
    extend = AsyncMock(side_effect=RuntimeError('db down'))

    monkeypatch.setattr(tariff_mod, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    monkeypatch.setattr(
        tariff_mod,
        'get_subscription_by_id_for_user',
        AsyncMock(return_value=subscription),
    )
    monkeypatch.setattr(
        pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=10_000, promo_offer_discount=0)),
    )
    monkeypatch.setattr(tariff_mod, 'subtract_user_balance', subtract)
    monkeypatch.setattr(tariff_mod, 'extend_subscription', extend)
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)

    db = AsyncMock()
    await inspect.unwrap(tariff_mod.confirm_tariff_extend)(
        _callback('tariff_ext_confirm:10:42:30'),
        user,
        db,
        MagicMock(),
    )

    # Обе записи должны попасть в один коммит вызывающего, а не в свои собственные.
    assert subtract.await_args.kwargs.get('commit') is False
    assert extend.await_args.kwargs.get('commit') is False
    db.rollback.assert_awaited()
    # Списание не закоммичено — компенсирующий возврат был бы двойным зачислением.
    refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_classic_purchase_refunds_when_update_fails(monkeypatch):
    """Админское продление: сбой после списания возвращает деньги пользователю."""
    now = datetime.now(UTC)
    subscription = SimpleNamespace(
        id=10,
        user_id=7,
        status='trial',
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=20),
        base_trial_end_date=now + timedelta(days=1),
        traffic_limit_gb=10,
        traffic_used_gb=2.0,
        device_limit=1,
        connected_squads=['squad-1'],
        tariff=None,
        remnawave_short_uuid=None,
        updated_at=None,
        autopay_enabled=False,
    )
    target_user = _user()
    refund = AsyncMock(return_value=True)

    monkeypatch.setattr(
        admin_users.UserService,
        'get_user_profile',
        AsyncMock(return_value={'user': target_user, 'subscription': subscription}),
    )
    monkeypatch.setattr(admin_users, 'SubscriptionService', MagicMock())
    monkeypatch.setattr(
        admin_users,
        '_calculate_subscription_period_price',
        AsyncMock(return_value=10_000),
    )
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=target_user))
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)
    monkeypatch.setattr(
        'app.services.grace_access_echo.undo_grace_overlay_echo',
        AsyncMock(side_effect=RuntimeError('db down')),
    )

    await inspect.unwrap(admin_users.admin_buy_subscription_execute)(
        _callback('admin_buy_sub_execute_7_30_10000'),
        MagicMock(),
        AsyncMock(),
    )

    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_tariff_purchase_refunds_when_extension_fails(monkeypatch):
    """Админская покупка тарифа: сбой после списания возвращает деньги."""
    now = datetime.now(UTC)
    target_user = _user()
    existing = SimpleNamespace(
        id=10,
        user_id=target_user.id,
        tariff_id=42,
        device_limit=1,
        is_trial=False,
        status='active',
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(days=10),
        base_trial_end_date=None,
        traffic_limit_gb=0,
        connected_squads=[],
    )
    refund = AsyncMock(return_value=True)

    monkeypatch.setattr(
        admin_users.UserService,
        'get_user_profile',
        AsyncMock(return_value={'user': target_user, 'subscription': existing}),
    )
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff()))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=target_user))
    monkeypatch.setattr(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=existing))
    monkeypatch.setattr(
        PricingEngine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=10_000)),
    )
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr(
        'app.database.crud.subscription.extend_subscription',
        AsyncMock(side_effect=RuntimeError('db down')),
    )
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)

    await inspect.unwrap(admin_users.admin_buy_tariff_execute)(
        _callback('admin_buy_tariff_execute_7_42_30_10000'),
        MagicMock(),
        AsyncMock(),
    )

    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_classic_refund_survives_failed_callback_answer(monkeypatch):
    """Падение Telegram-ответа не должно пропускать возврат."""
    now = datetime.now(UTC)
    subscription = SimpleNamespace(
        id=10,
        user_id=7,
        status='trial',
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=20),
        base_trial_end_date=now + timedelta(days=1),
        traffic_limit_gb=10,
        traffic_used_gb=2.0,
        device_limit=1,
        connected_squads=['squad-1'],
        tariff=None,
        remnawave_short_uuid=None,
        updated_at=None,
        autopay_enabled=False,
    )
    target_user = _user()
    refund = AsyncMock(return_value=True)

    monkeypatch.setattr(
        admin_users.UserService,
        'get_user_profile',
        AsyncMock(return_value={'user': target_user, 'subscription': subscription}),
    )
    monkeypatch.setattr(admin_users, 'SubscriptionService', MagicMock())
    monkeypatch.setattr(
        admin_users,
        '_calculate_subscription_period_price',
        AsyncMock(return_value=10_000),
    )
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=target_user))
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)
    monkeypatch.setattr(
        'app.services.grace_access_echo.undo_grace_overlay_echo',
        AsyncMock(side_effect=RuntimeError('db down')),
    )

    callback = _callback('admin_buy_sub_execute_7_30_10000')
    callback.answer = AsyncMock(side_effect=RuntimeError('telegram down'))

    await inspect.unwrap(admin_users.admin_buy_subscription_execute)(
        callback,
        MagicMock(),
        AsyncMock(),
    )

    refund.assert_awaited_once()
