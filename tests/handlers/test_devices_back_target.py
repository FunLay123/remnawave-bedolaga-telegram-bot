"""Regression: «Назад» из флоу устройств ведёт в меню подписки, а не в главное.

Bug report: «нажимаю на "устройства", затем нажимаю назад, возвращает в главное
меню хотя должно в меню подписки».

Root cause: дефолт ``get_back_keyboard`` — ``back_to_menu`` (главное меню). В
``devices.py`` часть экранов строила кнопку без явной цели, поэтому возврат уезжал
в главное меню. Правки, менявшие ``back_callback=`` в конструкторах клавиатур,
эти «голые» вызовы не покрывали — самый заметный из них экран «нет устройств».

Тест гоняет настоящие обработчики с замоканным вводом-выводом и проверяет
callback_data кнопки «Назад» на каждом экране флоу.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.subscription.devices as devices_mod


PANEL_USER_ID = 4242
SUB_ID = 7


def _make_callback():
    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _make_user():
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.remnawave_id = PANEL_USER_ID
    return user


def _make_subscription():
    sub = MagicMock()
    sub.id = SUB_ID
    sub.is_trial = False
    sub.remnawave_id = PANEL_USER_ID
    # Лимит устройств показывается в тексте экрана и форматируется как число.
    sub.device_limit = 3
    sub.tariff_id = None
    sub.connected_squads = []
    return sub


def _service(devices, *, remove_ok=True):
    api = AsyncMock()
    api.get_user_devices = AsyncMock(return_value={'total': len(devices), 'devices': list(devices)})
    api.remove_device = AsyncMock(return_value=remove_ok)

    @asynccontextmanager
    async def _cm():
        yield api

    svc = MagicMock()
    svc.get_api_client = MagicMock(side_effect=lambda: _cm())
    svc.api_double = api
    return svc


def _back_callback(callback) -> str | None:
    """callback_data кнопки «Назад» из последнего отрендеренного экрана."""
    for call in reversed(callback.message.edit_text.await_args_list):
        markup = call.kwargs.get('reply_markup')
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            for button in row:
                if 'Назад' in (button.text or ''):
                    return button.callback_data
    return None


@pytest.mark.anyio('asyncio')
async def test_empty_device_list_back_goes_to_subscription_menu():
    """Ровно тот сценарий из баг-репорта: устройств нет → «Назад» в подписку."""
    cb, user = _make_callback(), _make_user()
    service = _service([])

    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(_make_subscription(), SUB_ID))),
        patch('app.services.remnawave_service.RemnaWaveService', return_value=service),
    ):
        await devices_mod.handle_device_management(cb, user, MagicMock(), None)

    assert _back_callback(cb) == 'menu_subscription'


@pytest.mark.anyio('asyncio')
async def test_device_list_back_goes_to_subscription_menu():
    """Список с устройствами вёл в подписку и раньше — держим это поведение."""
    cb, user = _make_callback(), _make_user()
    service = _service([{'hwid': 'AA', 'platform': 'iOS', 'deviceModel': 'X'}])

    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(_make_subscription(), SUB_ID))),
        patch.object(devices_mod, '_enrich_devices_with_aliases', new=AsyncMock(side_effect=lambda lst, uid: lst)),
        # Кнопка докупки не предмет этого теста — её доступность не считаем.
        patch.object(devices_mod, '_can_buy_devices', new=AsyncMock(return_value=False)),
        patch('app.services.remnawave_service.RemnaWaveService', return_value=service),
    ):
        await devices_mod.handle_device_management(cb, user, MagicMock(), None)

    assert _back_callback(cb) == 'menu_subscription'


@pytest.mark.anyio('asyncio')
async def test_multi_tariff_back_goes_to_subscription_card():
    """В мульти-тарифе возврат ведёт на карточку конкретной подписки."""
    cb, user = _make_callback(), _make_user()
    service = _service([])

    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(_make_subscription(), SUB_ID))),
        patch('app.config.Settings.is_multi_tariff_enabled', lambda self: True),
        patch('app.services.remnawave_service.RemnaWaveService', return_value=service),
    ):
        await devices_mod.handle_device_management(cb, user, MagicMock(), None)

    assert _back_callback(cb) == f'sm:{SUB_ID}'
