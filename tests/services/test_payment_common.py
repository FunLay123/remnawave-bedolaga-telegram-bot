from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.orm.exc import DetachedInstanceError


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.database.models import Tariff
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
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
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
@pytest.mark.parametrize('menu_mode', ['default', 'cabinet'])
async def test_success_keyboard_adds_renew_next_to_main_menu(monkeypatch: pytest.MonkeyPatch, menu_mode: str) -> None:
    """При наличии подписки продление стоит справа и повторяет дизайн экрана подписки."""
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', menu_mode)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    subscription = SimpleNamespace(
        id=7, is_active=True, tariff_id=1, tariff=SimpleNamespace(is_active=True, is_daily=False)
    )
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
    assert renew.web_app is None
    assert renew.style == 'primary'
    assert renew.icon_custom_emoji_id == '5334552404736104314'
    snapshot = await mixin._ensure_user_snapshot(555, user)
    assert _callbacks(await mixin.build_topup_success_keyboard(snapshot)) == ['back_to_menu', 'subscription_extend']


@pytest.mark.asyncio
async def test_success_keyboard_trial_offers_buy_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    """Триал не продлевается, но после пополнения должен вести к покупке подписки."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    subscription = SimpleNamespace(
        id=7,
        is_trial=True,
        is_active=True,
        tariff_id=1,
        tariff=SimpleNamespace(is_active=True, is_daily=False),
    )
    user = SimpleNamespace(id=42, language='ru', subscription=subscription)
    service = PaymentCommonMixin()

    keyboard = await service.build_topup_success_keyboard(user)
    snapshot = await service._ensure_user_snapshot(555, user)
    snapshot_keyboard = await service.build_topup_success_keyboard(snapshot)

    for result in (keyboard, snapshot_keyboard):
        assert _callbacks(result) == ['back_to_menu', 'subscription_upgrade']
        main_menu, buy = result.inline_keyboard[0]
        assert main_menu.callback_data == 'back_to_menu'
        assert buy.text == 'Купить подписку'
        assert buy.style == 'primary'
        assert buy.icon_custom_emoji_id == '5334552404736104314'


@pytest.mark.asyncio
async def test_success_keyboard_multi_tariff_renew_targets_current_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Мультитарифная кнопка продлевает выбранную подписку, а не просит выбрать её повторно."""
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default', raising=False)
    expired = SimpleNamespace(id=6, is_active=False)
    active = SimpleNamespace(id=7, is_active=True, tariff_id=1, tariff=SimpleNamespace(is_active=True, is_daily=False))
    user = SimpleNamespace(
        id=42,
        language='ru',
        subscriptions=[expired, active],
        subscription=expired,
    )
    mixin = type('_TestMixin', (PaymentCommonMixin,), {})()

    keyboard = await mixin.build_topup_success_keyboard(user)

    assert _callbacks(keyboard) == ['back_to_menu', 'se:7']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('is_trial', 'tariff_id', 'tariff'),
    [
        pytest.param(False, None, None, id='legacy'),
        pytest.param(False, 1, SimpleNamespace(is_active=False, is_daily=False), id='inactive-tariff'),
        pytest.param(False, 1, SimpleNamespace(is_active=True, is_daily=True), id='daily'),
    ],
)
async def test_success_keyboard_omits_renew_when_current_tariff_cannot_be_renewed(
    monkeypatch: pytest.MonkeyPatch, is_trial: bool, tariff_id: int | None, tariff: SimpleNamespace | None
) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    subscription = SimpleNamespace(id=7, is_trial=is_trial, is_active=True, tariff_id=tariff_id, tariff=tariff)
    user = SimpleNamespace(language='ru', subscription=subscription)
    service = PaymentCommonMixin()

    keyboard = await service.build_topup_success_keyboard(user)
    snapshot = await service._ensure_user_snapshot(555, user)
    snapshot_keyboard = await service.build_topup_success_keyboard(snapshot)

    assert _callbacks(keyboard) == ['back_to_menu']
    assert _callbacks(snapshot_keyboard) == ['back_to_menu']


@pytest.mark.asyncio
async def test_topup_renew_opens_periods_for_current_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.database.crud import tariff as tariff_crud
    from app.handlers.subscription import purchase, tariff_purchase

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    tariff = Tariff(id=1, name='Текущий тариф', is_active=True, is_daily=False, period_prices={'30': 10000})
    current = SimpleNamespace(id=7, is_trial=False, tariff_id=tariff.id, tariff=tariff, device_limit=1, end_date=None)
    legacy = SimpleNamespace(id=6, tariff_id=None)
    user = SimpleNamespace(id=42, language='ru', subscription=current)
    db = object()

    async def get_owned_subscription(session, subscription_id, user_id):
        assert session is db
        return current if (subscription_id, user_id) == (current.id, user.id) else None

    get_tariff = AsyncMock(return_value=tariff)
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', get_tariff)
    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', get_tariff)
    monkeypatch.setattr(tariff_purchase, 'get_subscription_by_id_for_user', get_owned_subscription)
    monkeypatch.setattr(tariff_purchase, 'get_subscription_by_user_id', AsyncMock(return_value=legacy))
    monkeypatch.setattr(tariff_purchase, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))

    keyboard = await PaymentCommonMixin().build_topup_success_keyboard(user)
    callback = SimpleNamespace(
        data=keyboard.inline_keyboard[0][1].callback_data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await purchase.handle_extend_subscription(callback, user, db)

    screen = callback.message.edit_text.await_args
    assert _callbacks(screen.kwargs['reply_markup']) == ['tariff_extend:7:1:30', 'menu_subscription']
