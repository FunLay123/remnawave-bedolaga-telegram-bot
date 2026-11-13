from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.orm.exc import DetachedInstanceError


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
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

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or MissingGreenlet('lazy load is not available')

    @property
    def subscription(self):  # type: ignore[no-untyped-def]
        raise self.error


class _PaymentServiceStub(PaymentCommonMixin):
    def __init__(self) -> None:
        self.bot = _FakeBot()
        self.keyboard_user: SimpleNamespace | None = None

    async def build_topup_success_keyboard(self, user):  # type: ignore[no-untyped-def]
        self.keyboard_user = user
        return InlineKeyboardMarkup(inline_keyboard=[])


@pytest.mark.anyio
@pytest.mark.parametrize(
    'error',
    [
        pytest.param(MissingGreenlet('lazy load is not available'), id='missing-greenlet'),
        pytest.param(DetachedInstanceError('instance is detached'), id='detached-instance'),
    ],
)
async def test_send_payment_success_notification_recovers_subscription_access_error(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    service = _PaymentServiceStub()
    lazy_user = _LazyUser(error)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default', raising=False)

    reloaded_user = SimpleNamespace(
        id=lazy_user.id,
        telegram_id=lazy_user.telegram_id,
        language=lazy_user.language,
        subscription=SimpleNamespace(
            id=17,
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
    assert service.keyboard_user.subscription.id == 17

    keyboard = await PaymentCommonMixin().build_topup_success_keyboard(service.keyboard_user)
    assert _callbacks(keyboard) == ['back_to_menu', 'subscription_extend']


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str | None]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_success_keyboard_without_subscription_is_main_menu() -> None:
    """Без подписки после пополнения остаётся только безопасный переход в меню."""
    user = SimpleNamespace(id=42, language='ru', subscriptions=[], subscription=None)
    mixin = type('_TestMixin', (PaymentCommonMixin,), {})()

    keyboard = await mixin.build_topup_success_keyboard(user)

    assert _callbacks(keyboard) == ['back_to_menu']


@pytest.mark.asyncio
async def test_success_keyboard_adds_renew_next_to_main_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    """При наличии подписки продление стоит справа и повторяет дизайн экрана подписки."""
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default', raising=False)
    subscription = SimpleNamespace(id=7, is_active=True)
    user = SimpleNamespace(
        id=42,
        language='ru',
        subscriptions=[subscription],
        subscription=subscription,
    )
    mixin = type('_TestMixin', (PaymentCommonMixin,), {})()

    keyboard = await mixin.build_topup_success_keyboard(user)

    assert len(keyboard.inline_keyboard) == 1
    main_menu, renew = keyboard.inline_keyboard[0]
    assert main_menu.callback_data == 'back_to_menu'
    assert renew.text == 'Продлить подписку'
    assert renew.callback_data == 'subscription_extend'
    assert renew.style == 'primary'
    assert renew.icon_custom_emoji_id == '5334552404736104314'


@pytest.mark.asyncio
async def test_success_keyboard_multi_tariff_renew_targets_current_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Мультитарифная кнопка продлевает выбранную подписку, а не просит выбрать её повторно."""
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default', raising=False)
    expired = SimpleNamespace(id=6, is_active=False)
    active = SimpleNamespace(id=7, is_active=True)
    user = SimpleNamespace(
        id=42,
        language='ru',
        subscriptions=[expired, active],
        subscription=expired,
    )
    mixin = type('_TestMixin', (PaymentCommonMixin,), {})()

    keyboard = await mixin.build_topup_success_keyboard(user)

    assert _callbacks(keyboard) == ['back_to_menu', 'se:7']
