"""Экран автоплатежа: статус + один переключатель.

Было: «Включить» и «Выключить» одновременно (одна кнопка всегда бессмысленна),
плюс два подэкрана настройки. Стало: один экран со статусом и одной кнопкой,
которая зависит от текущего состояния подписки.

Логика БД не меняется — тот же ``update_subscription_autopay``, те же три
проверки при включении (триал, тариф, суточный тариф) и то же взаимоисключение
движков продления.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.subscription.autopay as autopay_mod
from app.keyboards.inline import get_autopay_keyboard


def _labels(keyboard) -> list[str]:
    return [b.text for row in keyboard.inline_keyboard for b in row]


def _callbacks(keyboard) -> list[str | None]:
    return [b.callback_data for row in keyboard.inline_keyboard for b in row]


def test_enabled_subscription_shows_disable_toggle():
    """Статус «включён» → кнопка выключает, и наоборот. Тексты не пересекаются."""
    kb = get_autopay_keyboard('ru', sub_id=10, enabled=True)

    labels = _labels(kb)
    assert any('ыключить' in label for label in labels), labels
    assert not any('ключить' in label and 'ыключить' not in label for label in labels), labels


def test_disabled_subscription_shows_enable_toggle():
    kb = get_autopay_keyboard('ru', sub_id=10, enabled=False)

    labels = _labels(kb)
    assert any('ключить' in label and 'ыключить' not in label for label in labels), labels


def test_toggle_is_the_only_action_beside_back():
    """Ровно две кнопки — переключатель и «Назад», обе во всю ширину."""
    kb = get_autopay_keyboard('ru', sub_id=10, enabled=True)

    assert len(kb.inline_keyboard) == 2, kb.inline_keyboard
    for row in kb.inline_keyboard:
        assert len(row) == 1, row
    assert _callbacks(kb)[0] == 'autopay_toggle'


def test_back_returns_to_subscription_menu():
    kb = get_autopay_keyboard('ru', sub_id=10, enabled=True)

    assert _callbacks(kb)[-1] == 'menu_subscription'


@pytest.mark.anyio('asyncio')
async def test_toggle_enables_when_currently_disabled(monkeypatch):
    """callback без «enable/disable»: направление берётся из состояния подписки."""
    cb = SimpleNamespace(data='autopay_toggle', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    user = SimpleNamespace(language='ru', id=1)
    subscription = SimpleNamespace(id=10, user_id=1, is_trial=False, tariff_id=5, tariff=None, autopay_enabled=False)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_update = AsyncMock(return_value=subscription)
    monkeypatch.setattr(autopay_mod, 'update_subscription_autopay', mock_update)
    monkeypatch.setattr(autopay_mod, 'handle_autopay_menu', AsyncMock())

    await autopay_mod.toggle_autopay(cb, user, AsyncMock())

    assert mock_update.await_args.args[2] is True


@pytest.mark.anyio('asyncio')
async def test_toggle_disables_when_currently_enabled(monkeypatch):
    cb = SimpleNamespace(data='autopay_toggle', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    user = SimpleNamespace(language='ru', id=1)
    subscription = SimpleNamespace(id=10, user_id=1, is_trial=False, tariff_id=5, tariff=None, autopay_enabled=True)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_update = AsyncMock(return_value=subscription)
    monkeypatch.setattr(autopay_mod, 'update_subscription_autopay', mock_update)
    monkeypatch.setattr(autopay_mod, 'handle_autopay_menu', AsyncMock())

    await autopay_mod.toggle_autopay(cb, user, AsyncMock())

    assert mock_update.await_args.args[2] is False


@pytest.mark.anyio('asyncio')
async def test_trial_subscription_still_rejected(monkeypatch):
    """Проверки при включении остаются: триал по-прежнему не может включить."""
    cb = SimpleNamespace(data='autopay_toggle', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    user = SimpleNamespace(language='ru', id=1)
    subscription = SimpleNamespace(id=10, user_id=1, is_trial=True, tariff_id=None, tariff=None, autopay_enabled=False)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_update = AsyncMock(return_value=subscription)
    monkeypatch.setattr(autopay_mod, 'update_subscription_autopay', mock_update)

    await autopay_mod.toggle_autopay(cb, user, AsyncMock())

    mock_update.assert_not_awaited()
    cb.answer.assert_awaited()
