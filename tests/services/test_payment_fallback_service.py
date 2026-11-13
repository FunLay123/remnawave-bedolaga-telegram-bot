"""Alternative payment methods offered after a failed top-up."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.keyboards.inline import get_payment_methods_keyboard


def _fallback_module():
    import app.services.payment_fallback_service as module

    return module


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def _urls(markup) -> list[str]:
    return [button.url for row in markup.inline_keyboard for button in row if button.url]


@pytest.fixture
def payment_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable exactly Stars, both YooKassa rails and Heleket for the keyboard."""

    for key, value in {
        'TELEGRAM_STARS_ENABLED': True,
        'YOOKASSA_ENABLED': True,
        'YOOKASSA_SHOP_ID': 'shop-id',
        'YOOKASSA_SECRET_KEY': 'secret-key',
        'YOOKASSA_SBP_ENABLED': True,
        'HELEKET_ENABLED': True,
        'HELEKET_MERCHANT_ID': 'merchant-id',
        'HELEKET_API_KEY': 'api-key',
        'SUPPORT_TOPUP_ENABLED': True,
        'SUPPORT_USERNAME': 'support_team',
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)


class _FakeCache:
    """Minimal Redis double with SET NX semantics."""

    _connected = True

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.ttls: dict[str, int | None] = {}

    async def setnx(self, key: str, value: object, expire: int | None = None) -> bool:
        if key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = expire
        return True

    async def delete(self, key: str) -> bool:
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return True


class _DisconnectedCache:
    _connected = False


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    return bot


def _user(telegram_id: int | None = 777) -> SimpleNamespace:
    return SimpleNamespace(telegram_id=telegram_id, language='ru')


# ---------------------------------------------------------------------------
# Keyboard exclusion keeps the exact amount and other methods
# ---------------------------------------------------------------------------


def test_excluding_yookassa_removes_only_the_card_rail(payment_methods: None) -> None:
    callbacks = _callbacks(
        get_payment_methods_keyboard(
            50000,
            'ru',
            back_callback='balance_topup',
            exclude_methods={'yookassa'},
        )
    )

    assert 'topup_amount|yookassa|50000' not in callbacks
    assert 'topup_amount|yookassa_sbp|50000' in callbacks
    assert 'topup_amount|stars|50000' in callbacks
    assert 'topup_amount|heleket|50000' in callbacks
    assert 'balance_topup' in callbacks


def test_excluding_heleket_removes_only_heleket(payment_methods: None) -> None:
    callbacks = _callbacks(
        get_payment_methods_keyboard(
            50000,
            'ru',
            back_callback='balance_topup',
            exclude_methods={'heleket'},
        )
    )

    assert 'topup_amount|heleket|50000' not in callbacks
    assert 'topup_amount|yookassa|50000' in callbacks
    assert 'topup_amount|yookassa_sbp|50000' in callbacks
    assert 'topup_amount|stars|50000' in callbacks


def test_no_alternative_methods_still_leave_support_and_back(payment_methods: None) -> None:
    callbacks = _callbacks(
        get_payment_methods_keyboard(
            50000,
            'ru',
            back_callback='balance_topup',
            exclude_methods={'yookassa', 'yookassa_sbp', 'heleket', 'stars'},
        )
    )

    assert not [callback for callback in callbacks if callback.startswith('topup_amount|')]
    assert 'topup_support' in callbacks
    assert 'balance_topup' in callbacks


def test_existing_callers_keep_the_unfiltered_keyboard(payment_methods: None) -> None:
    callbacks = _callbacks(get_payment_methods_keyboard(50000, 'ru', back_callback='balance_topup'))

    assert 'topup_amount|yookassa|50000' in callbacks
    assert 'topup_amount|yookassa_sbp|50000' in callbacks
    assert 'topup_amount|heleket|50000' in callbacks


# ---------------------------------------------------------------------------
# Fallback screen builder
# ---------------------------------------------------------------------------


def test_build_payment_fallback_uses_amount_and_filters_only_failed_method(payment_methods: None) -> None:
    from app.services.payment_fallback_service import build_payment_fallback

    text, markup = build_payment_fallback(_user(), 50000, 'yookassa')

    assert '500' in text
    assert 'YooKassa' in text

    callbacks = _callbacks(markup)
    assert 'topup_amount|yookassa|50000' not in callbacks
    assert 'topup_amount|yookassa_sbp|50000' in callbacks
    assert 'topup_amount|heleket|50000' in callbacks
    assert 'balance_topup' in callbacks
    assert 'https://t.me/support_team' in _urls(markup)


# ---------------------------------------------------------------------------
# Proactive sender cooldown and release on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sender_claims_the_payment_for_24_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.payment_fallback_service import send_payment_fallback_once

    fake_cache = _FakeCache()
    monkeypatch.setattr(_fallback_module(), 'cache', fake_cache)
    bot = _bot()

    assert await send_payment_fallback_once(bot, _user(), 50000, 'yookassa', 42) is True
    assert await send_payment_fallback_once(bot, _user(), 50000, 'yookassa', 42) is False

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 777
    assert fake_cache.ttls['payment_fallback:yookassa:42'] == 24 * 3600


@pytest.mark.asyncio
async def test_sender_cooldown_works_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.payment_fallback_service import send_payment_fallback_once

    module = _fallback_module()
    monkeypatch.setattr(module, 'cache', _DisconnectedCache())
    module._MEMORY_CLAIMS.clear()
    bot = _bot()

    assert await send_payment_fallback_once(bot, _user(), 50000, 'heleket', 7) is True
    assert await send_payment_fallback_once(bot, _user(), 50000, 'heleket', 7) is False

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_send_releases_the_claim_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.payment_fallback_service import send_payment_fallback_once

    fake_cache = _FakeCache()
    monkeypatch.setattr(_fallback_module(), 'cache', fake_cache)
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError('telegram is down'))

    assert await send_payment_fallback_once(bot, _user(), 50000, 'yookassa', 11) is False
    assert fake_cache.values == {}

    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    assert await send_payment_fallback_once(bot, _user(), 50000, 'yookassa', 11) is True
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_sender_requires_a_telegram_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.payment_fallback_service import send_payment_fallback_once

    fake_cache = _FakeCache()
    monkeypatch.setattr(_fallback_module(), 'cache', fake_cache)
    bot = _bot()

    assert await send_payment_fallback_once(bot, _user(telegram_id=None), 50000, 'yookassa', 3) is False

    bot.send_message.assert_not_awaited()
    assert fake_cache.values == {}
