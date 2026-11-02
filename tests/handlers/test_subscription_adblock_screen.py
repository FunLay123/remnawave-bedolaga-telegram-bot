"""Экран «Отключение рекламы» и третья кнопка на экране «Подключиться»."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.subscription.adblock as adblock_mod
import app.handlers.subscription.links as links_mod
from app.config import settings
from app.keyboards.inline import get_device_selection_keyboard


OFF_ICON = '5334829606220354437'
ON_ICON = '5335020345717973061'
ACTION_ICON = '5332588449335628124'


@pytest.fixture(autouse=True)
def _feature_on(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_adblock_dns_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_subscription_proxy_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)


def _callback(data: str = 'sub_adblock') -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, language='ru')


def _subscription(*, enabled: bool = False, sub_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=sub_id,
        user_id=1,
        adblock_dns_enabled=enabled,
        subscription_url='https://panel.example/sub/abc123',
    )


def _buttons(callback: SimpleNamespace):
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    return [button for row in keyboard.inline_keyboard for button in row]


def _button(callback: SimpleNamespace, text: str):
    return next(button for button in _buttons(callback) if button.text == text)


def _screen_text(callback: SimpleNamespace) -> str:
    call = callback.message.edit_text.await_args
    if call.kwargs.get('text') is not None:
        return call.kwargs['text']
    return call.args[0]


# ---- кнопка на экране «Подключиться» -------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_connect_link_screen_shows_adblock_off_button(monkeypatch):
    callback = _callback()
    subscription = _subscription(enabled=False)

    monkeypatch.setattr(links_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(links_mod.settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(links_mod, 'get_display_subscription_link', lambda _sub: _sub.subscription_url)
    monkeypatch.setattr(links_mod, 'get_happ_download_button_row', lambda _texts: None)

    await links_mod.handle_connect_subscription(callback, _user(), AsyncMock())

    button = _button(callback, 'Отключить рекламу')
    assert button.style == 'primary'
    assert button.icon_custom_emoji_id == OFF_ICON
    assert button.callback_data == 'sub_adblock'


@pytest.mark.anyio('asyncio')
async def test_connect_link_screen_shows_adblock_on_button(monkeypatch):
    callback = _callback()
    subscription = _subscription(enabled=True)

    monkeypatch.setattr(links_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(links_mod.settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(links_mod, 'get_display_subscription_link', lambda _sub: _sub.subscription_url)
    monkeypatch.setattr(links_mod, 'get_happ_download_button_row', lambda _texts: None)

    await links_mod.handle_connect_subscription(callback, _user(), AsyncMock())

    button = _button(callback, 'Реклама отключена')
    assert button.style == 'primary'
    assert button.icon_custom_emoji_id == ON_ICON
    assert button.callback_data == 'sub_adblock'


@pytest.mark.anyio('asyncio')
async def test_connect_screen_hides_adblock_when_feature_is_off(monkeypatch):
    callback = _callback()
    subscription = _subscription()
    monkeypatch.setattr(type(settings), 'is_adblock_dns_enabled', lambda self: False)

    monkeypatch.setattr(links_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(links_mod.settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(links_mod, 'get_display_subscription_link', lambda _sub: _sub.subscription_url)
    monkeypatch.setattr(links_mod, 'get_happ_download_button_row', lambda _texts: None)

    await links_mod.handle_connect_subscription(callback, _user(), AsyncMock())

    labels = [button.text for button in _buttons(callback)]
    assert 'Отключить рекламу' not in labels
    assert 'Реклама отключена' not in labels


@pytest.mark.anyio('asyncio')
async def test_connect_screen_multi_tariff_callback_carries_sub_id(monkeypatch):
    callback = _callback()
    subscription = _subscription(sub_id=7)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    monkeypatch.setattr(links_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 7)))
    monkeypatch.setattr(links_mod.settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(links_mod, 'get_display_subscription_link', lambda _sub: _sub.subscription_url)
    monkeypatch.setattr(links_mod, 'get_happ_download_button_row', lambda _texts: None)

    await links_mod.handle_connect_subscription(callback, _user(), AsyncMock())

    assert _button(callback, 'Отключить рекламу').callback_data == 'sub_adblock:7'


def test_device_selection_keyboard_includes_adblock_button():
    keyboard = get_device_selection_keyboard(
        'ru',
        platforms=None,
        sub_id=1,
        adblock_visible=True,
        adblock_enabled=False,
    )

    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert 'Отключить рекламу' in labels


# ---- экран «Отключение рекламы» ------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_adblock_menu_off_screen(monkeypatch):
    callback = _callback('sub_adblock')
    subscription = _subscription(enabled=False)
    monkeypatch.setattr(adblock_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))

    await adblock_mod.handle_adblock_menu(callback, _user(), AsyncMock())

    text = _screen_text(callback)
    assert 'Отключение рекламы' in text
    assert 'Только для INCY и Happ' in text
    assert '<blockquote>' in text
    assert 'Эта функция добавляет' in text
    assert 'запасные резолверы' in text

    enable = _button(callback, 'Включить')
    assert enable.style == 'primary'
    assert enable.icon_custom_emoji_id == ACTION_ICON
    assert enable.callback_data == 'sub_adblock:enable'
    assert _button(callback, 'Назад').callback_data == 'subscription_connect'


@pytest.mark.anyio('asyncio')
async def test_adblock_menu_on_screen(monkeypatch):
    callback = _callback('sub_adblock')
    subscription = _subscription(enabled=True)
    monkeypatch.setattr(adblock_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))

    await adblock_mod.handle_adblock_menu(callback, _user(), AsyncMock())

    text = _screen_text(callback)
    assert 'Блокировка рекламы включена' in text
    assert 'обновите подписку в приложении' in text

    disable = _button(callback, 'Выключить')
    assert disable.style == 'primary'
    assert disable.icon_custom_emoji_id == ACTION_ICON
    assert disable.callback_data == 'sub_adblock:disable'


@pytest.mark.anyio('asyncio')
async def test_adblock_enable_sets_flag_and_shows_enabled_screen(monkeypatch):
    callback = _callback('sub_adblock:enable')
    subscription = _subscription(enabled=False)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(adblock_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))

    await adblock_mod.handle_adblock_enable(callback, _user(), db)

    assert subscription.adblock_dns_enabled is True
    db.commit.assert_awaited_once()
    assert 'Блокировка рекламы включена' in _screen_text(callback)
    assert _button(callback, 'Выключить').callback_data == 'sub_adblock:disable'
    assert 'Обновите подписку' in callback.answer.await_args.args[0]


@pytest.mark.anyio('asyncio')
async def test_adblock_disable_sets_flag_and_shows_off_screen(monkeypatch):
    callback = _callback('sub_adblock:disable')
    subscription = _subscription(enabled=True)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(adblock_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))

    await adblock_mod.handle_adblock_disable(callback, _user(), db)

    assert subscription.adblock_dns_enabled is False
    db.commit.assert_awaited_once()
    assert 'Эта функция добавляет' in _screen_text(callback)
    assert _button(callback, 'Включить').callback_data == 'sub_adblock:enable'


@pytest.mark.anyio('asyncio')
async def test_adblock_handler_refuses_when_feature_is_off(monkeypatch):
    callback = _callback('sub_adblock')
    monkeypatch.setattr(type(settings), 'is_adblock_dns_enabled', lambda self: False)

    await adblock_mod.handle_adblock_menu(callback, _user(), AsyncMock())

    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    assert 'недоступна' in callback.answer.await_args.args[0]


@pytest.mark.anyio('asyncio')
async def test_adblock_enable_commit_error_rolls_back(monkeypatch):
    callback = _callback('sub_adblock:enable')
    subscription = _subscription(enabled=False)
    db = SimpleNamespace(commit=AsyncMock(side_effect=RuntimeError('db down')), rollback=AsyncMock())
    monkeypatch.setattr(adblock_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))

    await adblock_mod.handle_adblock_enable(callback, _user(), db)

    db.rollback.assert_awaited_once()
    assert subscription.adblock_dns_enabled is False
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    assert 'Не удалось' in callback.answer.await_args.args[0]


@pytest.mark.anyio('asyncio')
async def test_adblock_multi_tariff_callbacks_carry_sub_id(monkeypatch):
    callback = _callback('sub_adblock:7')
    subscription = _subscription(sub_id=7)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(adblock_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 7)))

    await adblock_mod.handle_adblock_menu(callback, _user(), AsyncMock())

    assert _button(callback, 'Включить').callback_data == 'sub_adblock:enable:7'
    assert _button(callback, 'Назад').callback_data == 'subscription_connect:7'
