"""Меню «Выберите приложение»: кнопка Happ не должна пропадать.

Remnawave 2.8+ больше не отдаёт готовый ``cryptoLink``, а массовая
синхронизация пользователей намеренно не шифрует ссылки
(``enrich_happ_links=False``). У перенесённых/синхронизированных подписок
``subscription_crypto_link`` остаётся пустым, и меню показывало только INCY
(его ссылка генерируется на лету). Happ должен достраиваться при показе
меню, как это уже делает кабинет (``subscription_modules/status.py``).
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote, unquote

import pytest

import app.handlers.subscription.links as links_mod
from app.utils.incy_crypt1 import _KEY_B64, INCY_CRYPT1_DEEP_LINK_PREFIX


def _make_callback() -> SimpleNamespace:
    return SimpleNamespace(
        data='subscription_connect',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _make_subscription(*, crypto_link: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        user_id=1,
        subscription_url='https://panel.example/sub/abc123',
        subscription_crypto_link=crypto_link,
    )


def _button_texts(callback: SimpleNamespace) -> list[str]:
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    return [button.text for row in keyboard.inline_keyboard for button in row]


def _incy_deep_link(callback: SimpleNamespace, redirect_template: str) -> dict:
    """Раскодировать вложение INCY-кнопки в том же виде, что видит клиент."""
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    button = next(button for row in keyboard.inline_keyboard for button in row if button.text == 'INCY')
    deep_link = unquote(button.url.split('#', 1)[0].removeprefix(redirect_template))
    payload = deep_link.removeprefix(INCY_CRYPT1_DEEP_LINK_PREFIX)
    raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plaintext = AESGCM(base64.b64decode(_KEY_B64)).decrypt(raw[:12], raw[12:], None)
    return json.loads(plaintext)


@pytest.mark.anyio('asyncio')
async def test_happ_button_appears_when_stored_crypto_link_is_missing(monkeypatch):
    callback = _make_callback()
    user = SimpleNamespace(language='ru', id=1)
    subscription = _make_subscription(crypto_link=None)

    monkeypatch.setattr(links_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(links_mod.settings, 'CONNECT_BUTTON_MODE', 'happ_incy_cryptolink')
    monkeypatch.setattr(links_mod.settings, 'HAPP_CRYPTOLINK_REDIRECT_TEMPLATE', 'https://example.test/r?to=')
    monkeypatch.setattr(links_mod.settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(links_mod, 'get_happ_download_button_row', lambda texts: None)
    generate = AsyncMock(return_value='happ://crypt4/generated')
    monkeypatch.setattr(links_mod, '_ensure_happ_crypto_link', generate)

    await links_mod.handle_connect_subscription(callback, user, AsyncMock())

    generate.assert_awaited_once()
    labels = _button_texts(callback)
    assert 'Happ' in labels, labels
    assert 'INCY' in labels, labels


@pytest.mark.anyio('asyncio')
async def test_stored_crypto_link_is_used_without_generation(monkeypatch):
    subscription = _make_subscription(crypto_link='happ://crypt5/stored')
    db = SimpleNamespace(commit=AsyncMock())

    def _unexpected_service():
        raise AssertionError('панель не должна дёргаться при сохранённой ссылке')

    import app.services.remnawave_service as remnawave_service_mod

    monkeypatch.setattr(remnawave_service_mod, 'RemnaWaveService', _unexpected_service)

    assert await links_mod._ensure_happ_crypto_link(subscription, db) == 'happ://crypt5/stored'
    db.commit.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_missing_crypto_link_is_generated_and_persisted(monkeypatch):
    subscription = _make_subscription(crypto_link=None)
    db = SimpleNamespace(commit=AsyncMock())
    api = SimpleNamespace(encrypt_happ_crypto_link=AsyncMock(return_value='happ://crypt4/generated'))

    class _Client:
        async def __aenter__(self):
            return api

        async def __aexit__(self, *exc_info):
            return False

    class _Service:
        def get_api_client(self):
            return _Client()

    import app.services.remnawave_service as remnawave_service_mod

    monkeypatch.setattr(remnawave_service_mod, 'RemnaWaveService', _Service)

    assert await links_mod._ensure_happ_crypto_link(subscription, db) == 'happ://crypt4/generated'
    assert subscription.subscription_crypto_link == 'happ://crypt4/generated'
    db.commit.assert_awaited_once()
    api.encrypt_happ_crypto_link.assert_awaited_once_with('https://panel.example/sub/abc123')


@pytest.mark.anyio('asyncio')
async def test_incy_link_carries_configured_provider_name(monkeypatch):
    """Имя провайдера берётся из INCY_CRYPTOLINK_PROVIDER_NAME, а не хардкода."""
    callback = _make_callback()
    user = SimpleNamespace(language='ru', id=1)
    subscription = _make_subscription(crypto_link='happ://crypt5/stored')
    redirect_template = 'https://example.test/r?to='

    monkeypatch.setattr(links_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(links_mod.settings, 'CONNECT_BUTTON_MODE', 'happ_incy_cryptolink')
    monkeypatch.setattr(links_mod.settings, 'HAPP_CRYPTOLINK_REDIRECT_TEMPLATE', redirect_template)
    monkeypatch.setattr(links_mod.settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(links_mod.settings, 'INCY_CRYPTOLINK_ENABLED', True)
    monkeypatch.setattr(links_mod.settings, 'INCY_CRYPTOLINK_PROVIDER_NAME', 'Asteria')
    monkeypatch.setattr(links_mod, 'get_happ_download_button_row', lambda texts: None)
    monkeypatch.setattr(
        links_mod,
        '_ensure_happ_crypto_link',
        AsyncMock(return_value='happ://crypt4/generated'),
    )

    await links_mod.handle_connect_subscription(callback, user, AsyncMock())

    assert _incy_deep_link(callback, redirect_template)['n'] == 'Asteria'


@pytest.mark.anyio('asyncio')
async def test_redirect_urls_carry_subscription_page_in_fragment(monkeypatch):
    """Ссылка на страницу подписки идёт якорем: сервер её не видит."""
    callback = _make_callback()
    user = SimpleNamespace(language='ru', id=1)
    subscription = _make_subscription(crypto_link='happ://crypt5/stored')
    redirect_template = 'https://example.test/r?to='

    monkeypatch.setattr(links_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(links_mod.settings, 'CONNECT_BUTTON_MODE', 'happ_incy_cryptolink')
    monkeypatch.setattr(links_mod.settings, 'HAPP_CRYPTOLINK_REDIRECT_TEMPLATE', redirect_template)
    monkeypatch.setattr(links_mod.settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(links_mod.settings, 'INCY_CRYPTOLINK_ENABLED', True)
    monkeypatch.setattr(links_mod.settings, 'INCY_CRYPTOLINK_PROVIDER_NAME', 'Asteria')
    monkeypatch.setattr(links_mod, 'get_happ_download_button_row', lambda texts: None)
    monkeypatch.setattr(
        links_mod,
        '_ensure_happ_crypto_link',
        AsyncMock(return_value='happ://crypt4/generated'),
    )

    await links_mod.handle_connect_subscription(callback, user, AsyncMock())

    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    urls = {button.text: button.url for row in keyboard.inline_keyboard for button in row}
    expected_fragment = f'#sub={quote(subscription.subscription_url, safe="")}'

    assert urls['Happ'].endswith(expected_fragment), urls['Happ']
    assert urls['INCY'].endswith(expected_fragment), urls['INCY']
    # В query, который уходит на сервер, ссылки подписки быть не должно.
    assert 'sub=' not in urls['Happ'].split('#', 1)[0]
