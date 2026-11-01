"""Принятие документов на старте: тост вместо сообщения «Условия приняты»."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.start as start_mod


def _make_callback() -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _make_state(data: dict) -> SimpleNamespace:
    return SimpleNamespace(
        get_data=AsyncMock(return_value=dict(data)),
        set_data=AsyncMock(),
        clear=AsyncMock(),
    )


@pytest.mark.anyio('asyncio')
async def test_new_user_gets_toast_instead_of_chat_message(monkeypatch):
    callback = _make_callback()
    state = _make_state({'language': 'ru'})
    continue_registration = AsyncMock()
    monkeypatch.setattr(start_mod, '_continue_registration_after_rules', continue_registration)

    await start_mod.process_legal_accept(callback, state, AsyncMock())

    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once_with('Условия приняты')
    continue_registration.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_existing_user_gate_also_confirms_with_toast(monkeypatch):
    callback = _make_callback()
    state = _make_state({'legal_gate_existing': True})
    db_user = SimpleNamespace(id=1)
    show_main_menu = AsyncMock()
    record_consent = AsyncMock()

    import app.handlers.menu as menu_mod

    monkeypatch.setattr(menu_mod, 'show_main_menu', show_main_menu)
    monkeypatch.setattr(start_mod, '_record_registration_consent', record_consent)

    await start_mod.process_legal_accept(callback, state, AsyncMock(), db_user=db_user)

    record_consent.assert_awaited_once()
    show_main_menu.assert_awaited_once()
    assert show_main_menu.await_args.kwargs['skip_callback_answer'] is True
    callback.answer.assert_awaited_once_with('Условия приняты')
