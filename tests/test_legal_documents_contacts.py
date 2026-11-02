"""Реквизиты оператора в юридических документах и экран «Контакты».

Публичный репозиторий не должен содержать ФИО, адрес, телефон и личный email.
Шаблоны документов хранят только placeholder'ы, а значения подставляются из
настроек окружения на конкретной инсталляции.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.legal_documents as legal_handlers
from app.config import settings
from app.keyboards.inline import get_legal_documents_keyboard, get_legal_gate_keyboard
from app.legal_documents import contacts_text, document_pages


@pytest.fixture(autouse=True)
def _operator_details(monkeypatch):
    monkeypatch.setattr(settings, 'LEGAL_OPERATOR_NAME', 'Иван Иванов', raising=False)
    monkeypatch.setattr(settings, 'LEGAL_OPERATOR_INN', '123456789012', raising=False)
    monkeypatch.setattr(settings, 'LEGAL_OPERATOR_ADDRESS', 'г. Москва, ул. Тестовая, 1', raising=False)
    monkeypatch.setattr(settings, 'SUPPORT_EMAIL', 'support@example.com', raising=False)
    monkeypatch.setattr(settings, 'SUPPORT_PHONE', '+7 900 000-00-00', raising=False)
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@example_support', raising=False)


def _all_pages(key: str) -> str:
    return '\n'.join(document_pages(key))


def test_offer_renders_operator_details_from_settings():
    text = _all_pages('offer')

    assert 'Иван Иванов' in text
    assert '123456789012' in text
    assert 'г. Москва, ул. Тестовая, 1' in text
    assert 'support@example.com' in text
    assert '+7 900 000-00-00' in text
    assert '{{' not in text


def test_privacy_and_refund_use_the_same_contacts():
    for key in ('privacy', 'refund', 'rules'):
        text = _all_pages(key)
        assert 'support@example.com' in text
        assert '{{' not in text


def test_unconfigured_operator_gets_neutral_fallbacks(monkeypatch):
    monkeypatch.setattr(settings, 'LEGAL_OPERATOR_NAME', '', raising=False)
    monkeypatch.setattr(settings, 'LEGAL_OPERATOR_INN', '', raising=False)
    monkeypatch.setattr(settings, 'LEGAL_OPERATOR_ADDRESS', '', raising=False)
    monkeypatch.setattr(settings, 'SUPPORT_EMAIL', '', raising=False)
    monkeypatch.setattr(settings, 'SUPPORT_PHONE', '', raising=False)

    text = _all_pages('offer')

    assert 'Оператор сервиса' in text
    assert 'не указан' in text
    assert '{{' not in text


def test_contacts_text_contains_details_and_escapes_html(monkeypatch):
    monkeypatch.setattr(settings, 'LEGAL_OPERATOR_NAME', '<b>Иван</b>', raising=False)

    text = contacts_text()

    assert '&lt;b&gt;Иван&lt;/b&gt;' in text
    assert 'support@example.com' in text
    assert '+7 900 000-00-00' in text
    assert '@example_support' in text


def test_whitelist_term_is_defined_as_an_option_not_a_bypass_tool():
    offer = _all_pages('offer')
    rules = _all_pages('rules')

    assert '«Белые списки»' in offer
    assert 'опции/группы серверов' in offer
    assert 'не предназначена для обхода' in offer
    assert '«Белые списки»' in rules
    assert 'обхода блокировок или ограничений' in rules


def test_legal_keyboards_expose_contacts_button():
    gate_buttons = [button for row in get_legal_gate_keyboard().inline_keyboard for button in row]
    docs_buttons = [button for row in get_legal_documents_keyboard('ru').inline_keyboard for button in row]

    assert any(button.callback_data == 'legal_contacts:gate' for button in gate_buttons)
    assert any(button.callback_data == 'legal_contacts:hub' for button in docs_buttons)


@pytest.mark.anyio('asyncio')
async def test_show_contacts_renders_contacts_and_back_button(monkeypatch):
    callback = SimpleNamespace(
        data='legal_contacts:hub',
        message=SimpleNamespace(photo=None, edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    monkeypatch.setattr(legal_handlers, 'contacts_text', lambda: 'Контакты: support@example.com')

    await legal_handlers.show_contacts(callback)

    call = callback.message.edit_text.await_args
    caption = call.kwargs.get('text') or call.args[0]
    keyboard = call.kwargs['reply_markup']
    assert 'support@example.com' in caption
    assert any(button.callback_data == 'legal_docs' for row in keyboard.inline_keyboard for button in row)
    callback.answer.assert_awaited_once()
