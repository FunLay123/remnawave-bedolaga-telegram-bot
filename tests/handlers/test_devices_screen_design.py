"""Оформление экранов устройств: список, докупка, подтверждение.

Что изменилось:
* сброс устройства возвращает в список с коротким ответом, без перехода
  на экран справки «как подключить заново» (он остался в коде, но в флоу
  больше не вызывается);
* экран списка: заголовок с иконкой, строка «Устройства: X / Y», подсказка
  в blockquote; список текстом не дублируется — устройства и так на кнопках;
* на кнопках устройств кастомная иконка, кнопка лимита — синяя;
* на экране докупки нет кнопки текущего количества, а пояснение про возврат
  средств вынесено в blockquote текста.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.subscription.devices as devices_mod
from app.keyboards.inline import get_confirm_change_devices_keyboard, get_devices_management_keyboard
from app.utils.pagination import paginate_list


DEVICE_ICON = '5258466217273871977'  # крестик (из кнопки автоплатежа)
CONFIRM_ICON = '5334552404736104314'  # монетка (из кнопки «Продлить подписку»)


def _pagination(count: int):
    return paginate_list(
        [{'hwid': f'HW{i}', 'platform': 'iOS', 'deviceModel': f'Device {i}'} for i in range(count)],
        page=1,
        per_page=20,
    )


def _buttons(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def test_device_buttons_carry_the_custom_icon():
    keyboard = get_devices_management_keyboard(_pagination(2).items, _pagination(2), 'ru')

    device_buttons = [b for b in _buttons(keyboard) if b.callback_data.startswith('reset_device_')]
    assert len(device_buttons) == 2
    for button in device_buttons:
        assert button.icon_custom_emoji_id == DEVICE_ICON


def test_limit_button_is_primary_and_named_limit():
    keyboard = get_devices_management_keyboard(
        _pagination(2).items, _pagination(2), 'ru', show_buy_devices=True
    )

    limit_buttons = [b for b in _buttons(keyboard) if b.callback_data == 'subscription_change_devices']
    assert len(limit_buttons) == 1
    assert limit_buttons[0].style == 'primary'
    assert 'Лимит устройств' in limit_buttons[0].text


def test_device_text_has_no_duplicated_list():
    """Список устройств текстом не дублируется — он есть на кнопках."""
    keyboard = get_devices_management_keyboard(_pagination(2).items, _pagination(2), 'ru')

    assert not any('iOS - Device' in b.text for b in _buttons(keyboard) if b.callback_data == 'current_page')


def test_confirm_button_is_primary_with_coin_icon():
    keyboard = get_confirm_change_devices_keyboard(5, 1000, 'ru')

    confirm = _buttons(keyboard)[0]
    assert confirm.style == 'primary'
    assert confirm.icon_custom_emoji_id == CONFIRM_ICON


async def _reset_and_get_render():
    """Прогнать сброс устройства и вернуть (текст, клавиатура) последнего рендера."""
    cb = MagicMock()
    cb.data = 'reset_device_0_1'
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.remnawave_id = 4242
    user.telegram_id = 1001

    subscription = MagicMock()
    subscription.id = 7
    subscription.is_trial = False
    subscription.remnawave_id = 4242
    subscription.tariff_id = None
    subscription.connected_squads = []
    subscription.device_limit = 3

    api = AsyncMock()
    api.get_user_devices = AsyncMock(
        return_value={'total': 2, 'devices': [{'hwid': 'HW0', 'platform': 'iOS', 'deviceModel': 'D0'}]}
    )
    api.remove_device = AsyncMock(return_value=True)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield api

    service = MagicMock()
    service.get_api_client = MagicMock(side_effect=lambda: _cm())

    with (
        patch.object(devices_mod, '_resolve_subscription', new=AsyncMock(return_value=(subscription, 7))),
        patch.object(devices_mod, '_enrich_devices_with_aliases', new=AsyncMock(side_effect=lambda lst, uid: lst)),
        # RemnaWaveService импортируется внутри функции — патчим источник.
        patch('app.services.remnawave_service.RemnaWaveService', return_value=service),
        patch.object(devices_mod, '_can_buy_devices', new=AsyncMock(return_value=False)),
    ):
        await devices_mod.handle_single_device_reset(cb, user, AsyncMock(), None)

    return cb


@pytest.mark.anyio('asyncio')
async def test_reset_shows_list_not_the_help_screen():
    """После сброса — обновлённый список, а не справка «как подключить»."""
    cb = await _reset_and_get_render()

    texts = [c.args[0] for c in cb.message.edit_text.await_args_list]
    assert texts, 'список не отрисован'
    assert not any('подключить' in t.lower() and 'заново' in t.lower() for t in texts), texts


@pytest.mark.anyio('asyncio')
async def test_reset_answers_with_a_short_message():
    """Короткий ответ во всплывашке — без длинных пояснений."""
    cb = await _reset_and_get_render()

    answers = [c.args[0] for c in cb.answer.await_args_list if c.args]
    assert answers, 'всплывашка не показана'
    assert any('сброшено' in a.lower() for a in answers), answers
    assert all(len(a) < 60 for a in answers), answers
