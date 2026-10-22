"""«Назад» из экранов лимита устройств ведёт по флоу, а не в меню подписки.

Bug report: «кнопка назад, экран лимита устройств — должна вести на экран
управления устройствами, а не в главное меню. То же касается экранов "+ какое-то
кол-во устройств" после нажатия на них».

Было: «Лимит устройств» возвращал на карточку подписки (``sm:`` /
``menu_subscription``), а «Отмена» на подтверждении изменения — туда же.
Пользователь приходил из списка устройств и терял контекст.

Тесты гоняют настоящие обработчики с замоканными зависимостями и проверяют
callback_data кнопки возврата на каждом экране.
"""

from datetime import UTC, datetime, timedelta
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


def _user():
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.balance_kopeks = 10_000_000
    # Без промогруппы скидка аддонов = 0, иначе MagicMock вернёт мок в расчёт цены.
    user.promo_group = None
    user.get_primary_promo_group = MagicMock(return_value=None)
    return user


def _subscription():
    sub = MagicMock()
    sub.id = SUB_ID
    sub.is_trial = False
    sub.tariff_id = TARIFF_ID
    sub.device_limit = 3
    sub.connected_squads = []
    sub.end_date = datetime.now(UTC) + timedelta(days=30)
    return sub


def _tariff():
    tariff = MagicMock()
    tariff.device_price_kopeks = 5000
    tariff.device_limit = 3
    tariff.max_device_limit = None
    return tariff


def _last_button_callback(callback) -> str | None:
    markup = callback.message.edit_text.await_args.kwargs['reply_markup']
    return markup.inline_keyboard[-1][0].callback_data


async def _run_change_devices(data: str, *, multi_tariff: bool):
    cb = _callback(data)
    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(_subscription(), SUB_ID))),
        patch('app.database.crud.tariff.get_tariff_by_id', new=AsyncMock(return_value=_tariff())),
        patch('app.config.Settings.is_multi_tariff_enabled', lambda self: multi_tariff),
    ):
        await devices_mod.handle_change_devices(cb, _user(), AsyncMock(), None)
    return cb


async def _run_confirm_change_devices(data: str, *, multi_tariff: bool):
    cb = _callback(data)
    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(_subscription(), SUB_ID))),
        patch('app.database.crud.tariff.get_tariff_by_id', new=AsyncMock(return_value=_tariff())),
        patch('app.config.Settings.is_multi_tariff_enabled', lambda self: multi_tariff),
    ):
        await devices_mod.confirm_change_devices(cb, _user(), AsyncMock(), None)
    return cb


@pytest.mark.anyio('asyncio')
async def test_limit_screen_back_goes_to_devices_management():
    """Экран лимита открыт из списка устройств — назад в список."""
    cb = await _run_change_devices('subscription_change_devices', multi_tariff=False)

    assert _last_button_callback(cb) == 'subscription_manage_devices'


@pytest.mark.anyio('asyncio')
async def test_limit_screen_back_goes_to_devices_management_multi_tariff():
    """В мульти-тарифе список открывается по device_management:<sub_id>."""
    cb = await _run_change_devices('subscription_change_devices', multi_tariff=True)

    assert _last_button_callback(cb) == f'device_management:{SUB_ID}'


@pytest.mark.anyio('asyncio')
async def test_limit_screen_back_from_devices_menu_returns_to_menu():
    """Вход из меню «Устройства» мульти-тарифа — назад в это меню, не в список."""
    cb = await _run_change_devices(f'change_devices_menu:{SUB_ID}', multi_tariff=True)

    assert _last_button_callback(cb) == f'sd:{SUB_ID}'


@pytest.mark.anyio('asyncio')
async def test_confirm_cancel_returns_to_limit_screen():
    """«Отмена» на подтверждении +N устройств — назад к выбору количества."""
    cb = await _run_confirm_change_devices('change_devices_5', multi_tariff=False)

    assert _last_button_callback(cb) == 'subscription_change_devices'


@pytest.mark.anyio('asyncio')
async def test_confirm_cancel_returns_to_limit_screen_multi_tariff():
    """В мульти-тарифе возврат сохраняет id подписки в callback_data."""
    cb = await _run_confirm_change_devices('change_devices_5', multi_tariff=True)

    assert _last_button_callback(cb) == f'change_devices_menu:{SUB_ID}'
