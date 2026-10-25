from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.exc import MissingGreenlet


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.payment import common as common_module
from app.services.payment.common import PaymentCommonMixin


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs):  # type: ignore[no-untyped-def]
        self.messages.append(kwargs)


class _LazyUser:
    id = 99
    telegram_id = 555
    language = 'ru'

    @property
    def subscription(self):  # type: ignore[no-untyped-def]
        raise MissingGreenlet('lazy load is not available')


class _PaymentServiceStub(PaymentCommonMixin):
    def __init__(self) -> None:
        self.bot = _FakeBot()
        self.keyboard_user: SimpleNamespace | None = None

    async def build_topup_success_keyboard(self, user):  # type: ignore[no-untyped-def]
        self.keyboard_user = user
        return InlineKeyboardMarkup(inline_keyboard=[])


@pytest.mark.anyio
async def test_send_payment_success_notification_recovers_missing_greenlet(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _PaymentServiceStub()
    lazy_user = _LazyUser()

    reloaded_user = SimpleNamespace(
        id=lazy_user.id,
        telegram_id=lazy_user.telegram_id,
        language=lazy_user.language,
        subscription=SimpleNamespace(
            is_trial=False,
            is_active=True,
            actual_status='active',
        ),
    )

    sentinel_db = object()

    async def fake_get_user_by_telegram_id(db, telegram_id):  # type: ignore[no-untyped-def]
        assert db is sentinel_db
        assert telegram_id == lazy_user.telegram_id
        return reloaded_user

    async def fake_get_db():  # type: ignore[no-untyped-def]
        yield object()

    monkeypatch.setattr(
        'app.services.payment.common.get_user_by_telegram_id',
        fake_get_user_by_telegram_id,
    )
    monkeypatch.setattr(
        'app.services.payment.common.get_db',
        fake_get_db,
    )
    await service._send_payment_success_notification(
        lazy_user.telegram_id,
        12300,
        user=lazy_user,
        db=sentinel_db,
        payment_method_title='Тестовый метод',
    )

    assert service.bot.messages, 'Ожидалось, что уведомление будет отправлено'
    message = service.bot.messages[0]
    assert 'Баланс пополнен' in message['text']
    assert '+123 ₽' in message['text']
    assert service.keyboard_user is not None
    assert isinstance(service.keyboard_user, SimpleNamespace)


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str | None]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def _patch_success_keyboard_reload(monkeypatch, fetched_user) -> None:
    monkeypatch.setattr(
        common_module.user_cart_service,
        'get_user_cart',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        common_module,
        'has_subscription_checkout_draft',
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(common_module, 'AsyncSessionLocal', lambda: _FakeSessionCtx())
    monkeypatch.setattr(
        common_module,
        'get_user_by_id',
        AsyncMock(return_value=fetched_user),
    )


@pytest.mark.asyncio
async def test_success_keyboard_reload_prefers_active_subscription(monkeypatch) -> None:
    """Мультитариф: есть активная подписка — возврат к checkout не предлагаем."""
    lazy_user = _LazyUser()
    expired = SimpleNamespace(is_trial=False, is_active=False, actual_status='expired')
    active = SimpleNamespace(is_trial=False, is_active=True, actual_status='active')
    # `user.subscription` вернул бы истёкшую; резервный путь обязан выбрать активную.
    fetched_user = SimpleNamespace(subscriptions=[expired, active], subscription=expired)
    _patch_success_keyboard_reload(monkeypatch, fetched_user)

    mixin = type('_TestMixin', (PaymentCommonMixin,), {})()
    keyboard = await mixin.build_topup_success_keyboard(lazy_user)

    assert _callbacks(keyboard) == ['menu_subscription']


@pytest.mark.asyncio
async def test_success_keyboard_reload_offers_resume_for_expired_subscription(monkeypatch) -> None:
    lazy_user = _LazyUser()
    expired = SimpleNamespace(is_trial=False, is_active=False, actual_status='expired')
    fetched_user = SimpleNamespace(subscriptions=[expired], subscription=expired)
    _patch_success_keyboard_reload(monkeypatch, fetched_user)

    mixin = type('_TestMixin', (PaymentCommonMixin,), {})()
    keyboard = await mixin.build_topup_success_keyboard(lazy_user)

    assert _callbacks(keyboard) == ['subscription_resume_checkout']


@pytest.mark.asyncio
async def test_success_keyboard_reload_skips_pending_trial_like_normal_path(monkeypatch) -> None:
    """Pending-триал не считается подпиской: кнопка возврата к checkout остаётся."""
    lazy_user = _LazyUser()
    pending_trial = SimpleNamespace(is_trial=True, is_active=False, actual_status='pending')
    # `user.subscription` пропускает неоплаченный черновик триала и возвращает None.
    fetched_user = SimpleNamespace(subscriptions=[pending_trial], subscription=None)
    _patch_success_keyboard_reload(monkeypatch, fetched_user)

    mixin = type('_TestMixin', (PaymentCommonMixin,), {})()
    keyboard = await mixin.build_topup_success_keyboard(lazy_user)

    assert _callbacks(keyboard) == ['subscription_resume_checkout']
