"""После подтверждения изменения лимита — сразу список устройств.

Экран «Количество устройств увеличено/уменьшено» больше не показывается:
списание и изменение лимита выполняются, а пользователь попадает обратно
в список устройств, где видно новый лимит и подключения.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.subscription.devices as devices_mod


SUB_ID = 7
TARIFF_ID = 5
PANEL_ID = 4242


def _callback(data: str):
    cb = MagicMock()
    cb.data = data
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _user():
    user = MagicMock()
    user.id = 1
    user.telegram_id = 1001
    user.language = 'ru'
    user.balance_kopeks = 10_000_000
    user.remnawave_id = PANEL_ID
    user.promo_group = None
    user.get_primary_promo_group = MagicMock(return_value=None)
    return user


def _subscription(device_limit: int = 6):
    sub = MagicMock()
    sub.id = SUB_ID
    sub.is_trial = False
    sub.tariff_id = TARIFF_ID
    sub.device_limit = device_limit
    sub.remnawave_id = PANEL_ID
    sub.status = 'active'
    sub.connected_squads = []
    sub.end_date = datetime.now(UTC) + timedelta(days=30)
    return sub


def _tariff():
    return SimpleNamespace(device_price_kopeks=5000, device_limit=3, max_device_limit=None)


def _api():
    api = AsyncMock()
    api.get_user_devices = AsyncMock(return_value={'total': 0, 'devices': []})
    api.remove_device = AsyncMock(return_value=True)

    @asynccontextmanager
    async def _client():
        yield api

    service = MagicMock()
    service.get_api_client = MagicMock(side_effect=lambda: _client())
    return service


async def _run(data: str, subscription):
    cb = _callback(data)
    user = _user()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=subscription)))

    charge = AsyncMock(return_value=True)
    devices_list = AsyncMock()

    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(subscription, SUB_ID))),
        patch.object(devices_mod, 'lock_user_for_pricing', new=AsyncMock(return_value=user)),
        patch.object(devices_mod, 'subtract_user_balance', new=charge),
        patch.object(devices_mod, 'create_transaction', new=AsyncMock()),
        patch.object(devices_mod, 'handle_device_management', new=devices_list),
        patch.object(devices_mod, 'RemnaWaveService', return_value=_api()),
        patch.object(
            devices_mod,
            'SubscriptionService',
            return_value=MagicMock(
                update_remnawave_user=AsyncMock(),
                enable_remnawave_user=AsyncMock(),
            ),
        ),
        patch('app.database.crud.tariff.get_tariff_by_id', new=AsyncMock(return_value=_tariff())),
        patch('app.database.crud.subscription.reactivate_subscription', new=AsyncMock()),
        patch(
            'app.services.admin_notification_service.AdminNotificationService',
            return_value=MagicMock(send_subscription_update_notification=AsyncMock()),
        ),
    ):
        await devices_mod.execute_change_devices(cb, user, db, None)

    return cb, charge, devices_list


@pytest.mark.anyio('asyncio')
async def test_increase_charges_and_redirects_to_devices_list():
    subscription = _subscription(6)

    cb, charge, devices_list = await _run('confirm_change_devices_7_0', subscription)

    assert charge.await_count == 1, 'доплата должна списаться'
    assert subscription.device_limit == 7
    devices_list.assert_awaited_once()
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_decrease_redirects_to_devices_list():
    subscription = _subscription(6)

    cb, charge, devices_list = await _run('confirm_change_devices_5_0', subscription)

    assert charge.await_count == 0, 'за уменьшение не списываем'
    assert subscription.device_limit == 5
    devices_list.assert_awaited_once()
    cb.message.edit_text.assert_not_awaited()
