"""Экран подтверждения изменения лимита устройств — минималистичный вид.

Выбранный дизайн: заголовок, одна строка «было → стало», цена доплаты и
подсказка в blockquote. Без «Текущее количество», «Новое количество»,
«Действие: увеличить до N» и «Подтвердить изменение?».
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.subscription.devices as devices_mod


SUB_ID = 7
TARIFF_ID = 5


def _callback(data: str):
    cb = MagicMock()
    cb.data = data
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _user(balance_kopeks: int = 10_000_000):
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.balance_kopeks = balance_kopeks
    user.promo_group = None
    user.get_primary_promo_group = MagicMock(return_value=None)
    return user


def _subscription(device_limit: int = 3, status: str = 'active'):
    sub = MagicMock()
    sub.id = SUB_ID
    sub.is_trial = False
    sub.tariff_id = TARIFF_ID
    sub.device_limit = device_limit
    sub.actual_status = status
    sub.connected_squads = []
    sub.end_date = datetime.now(UTC) + timedelta(days=30)
    return sub


def _tariff():
    return SimpleNamespace(device_price_kopeks=5000, device_limit=3, max_device_limit=20)


def _remnawave_with(connected: int):
    api = AsyncMock()
    api.get_user_devices = AsyncMock(return_value={'total': connected, 'devices': []})

    @asynccontextmanager
    async def _client():
        yield api

    service = MagicMock()
    service.get_api_client = MagicMock(side_effect=lambda: _client())
    return service


def _rendered_text(callback) -> str:
    return callback.message.edit_text.await_args.args[0]


async def _run(
    data: str,
    subscription,
    *,
    connected: int = 0,
    balance_kopeks: int = 10_000_000,
) -> str:
    cb = _callback(data)
    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(subscription, SUB_ID))),
        patch('app.database.crud.tariff.get_tariff_by_id', new=AsyncMock(return_value=_tariff())),
        patch.object(devices_mod, 'RemnaWaveService', return_value=_remnawave_with(connected)),
    ):
        await devices_mod.confirm_change_devices(cb, _user(balance_kopeks), AsyncMock(), None)
    return _rendered_text(cb)


@pytest.mark.anyio('asyncio')
async def test_increase_confirmation_is_compact():
    text = await _run('change_devices_5', _subscription(3))

    assert '3 → 5' in text
    assert 'Доплата спишется с баланса' in text
    assert '→' in text, 'баланс показывается как «было → станет»'
    assert 'за 30 дн.' in text
    # Многословие старого экрана убрано.
    assert 'Текущее количество' not in text
    assert 'Новое количество' not in text
    assert 'Действие' not in text
    assert 'Подтвердить изменение?' not in text
    assert 'скидка' not in text


@pytest.mark.anyio('asyncio')
async def test_decrease_confirmation_warns_about_reset():
    text = await _run('change_devices_5', _subscription(6), connected=8)

    assert '6 → 5' in text
    assert 'Средства не возвращаются' in text
    assert 'Лишние устройства будут сброшены' in text


@pytest.mark.anyio('asyncio')
async def test_decrease_confirmation_without_extra_devices():
    text = await _run('change_devices_5', _subscription(6), connected=2)

    assert '6 → 5' in text
    assert 'Средства не возвращаются' in text
    assert 'Лишние устройства' not in text


@pytest.mark.anyio('asyncio')
async def test_confirmation_rejects_more_than_ten_devices():
    """Даже старый callback с 11 устройствами не должен пройти потолок."""
    cb = _callback('change_devices_11')
    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(_subscription(6), SUB_ID))),
        patch('app.database.crud.tariff.get_tariff_by_id', new=AsyncMock(return_value=_tariff())),
    ):
        await devices_mod.confirm_change_devices(cb, _user(), AsyncMock(), None)

    cb.message.edit_text.assert_not_awaited()
    assert cb.answer.await_args.kwargs['show_alert'] is True
    assert '10' in cb.answer.await_args.args[0]


@pytest.mark.anyio('asyncio')
async def test_inactive_subscription_cannot_buy_more_devices():
    """Истёкшая/отключённая подписка не должна списывать доплату за устройства."""
    cb = _callback('change_devices_5')
    with (
        patch.object(
            devices_mod,
            '_resolve_subscription',
            new=AsyncMock(return_value=(_subscription(3, status='expired'), SUB_ID)),
        ),
        patch('app.database.crud.tariff.get_tariff_by_id', new=AsyncMock(return_value=_tariff())),
    ):
        await devices_mod.confirm_change_devices(cb, _user(), AsyncMock(), None)

    cb.message.edit_text.assert_not_awaited()
    assert cb.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.anyio('asyncio')
async def test_inactive_subscription_can_reduce_devices():
    """Уменьшение/сброс устройств на неактивной подписке разрешено — так чинится 3/1."""
    text = await _run('change_devices_3', _subscription(4, status='expired'), connected=4)
    assert '4 → 3' in text


@pytest.mark.anyio('asyncio')
async def test_execute_change_devices_rejects_inactive_increase():
    """Старое подтверждение не должно списать доплату за мёртвый период."""
    cb = _callback('execute_change_devices_5_1000')
    user = _user()
    with (
        patch.object(devices_mod, 'lock_user_for_pricing', new=AsyncMock(return_value=user)),
        patch.object(
            devices_mod,
            '_resolve_subscription',
            new=AsyncMock(return_value=(_subscription(3, status='expired'), SUB_ID)),
        ),
    ):
        await devices_mod.execute_change_devices(cb, user, AsyncMock(), None)

    cb.answer.assert_awaited()
    assert cb.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.anyio('asyncio')
async def test_confirm_add_devices_rejects_inactive():
    cb = _callback('add_devices_1')
    with patch.object(
        devices_mod,
        '_resolve_subscription',
        new=AsyncMock(return_value=(_subscription(3, status='disabled'), SUB_ID)),
    ):
        await devices_mod.confirm_add_devices(cb, _user(), AsyncMock(), None)

    cb.answer.assert_awaited()
    assert cb.answer.await_args.kwargs.get('show_alert') is True
