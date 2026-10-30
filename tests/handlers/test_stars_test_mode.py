"""Stars dev-mode: те же экраны, но без реального платежа Telegram."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.balance import stars
from app.services.payment.stars import StarsPaymentResult


@pytest.fixture(autouse=True)
def stars_test_environment(monkeypatch):
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_TEST_MODE', True)
    monkeypatch.setattr(settings, 'ADMIN_IDS', '7')
    monkeypatch.setattr(stars, '_get_stars_limits', AsyncMock(return_value=(10_000, 5_000_000)))


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=7, telegram_id=7, language='ru', balance_kopeks=0)


def _message() -> MagicMock:
    message = MagicMock()
    message.message_id = 10
    message.chat = SimpleNamespace(id=7)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=11, chat=SimpleNamespace(id=7)))
    message.delete = AsyncMock()
    message.bot.delete_message = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_invoice_uses_callback_button_instead_of_stars_link(monkeypatch):
    payment_service_cls = MagicMock()
    monkeypatch.setattr(stars, 'PaymentService', payment_service_cls)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'stars_prompt_message_id': None})
    message = _message()

    await stars.process_stars_payment_amount(message, _user(), 50_000, state)

    payment_service_cls.assert_not_called()
    keyboard = message.answer.await_args.kwargs['reply_markup']
    pay_button = keyboard.inline_keyboard[0][0]
    assert pay_button.callback_data.startswith('stars_test_pay|50000|')
    assert pay_button.url is None
    assert pay_button.text == 'Оплатить 500 ★'
    assert 'Счёт на оплату' in message.answer.await_args.args[0]
    test_state = next(call.kwargs for call in state.update_data.await_args_list if 'stars_test_token' in call.kwargs)
    assert test_state['stars_test_amount_kopeks'] == 50_000
    assert test_state['stars_test_stars_amount'] == 500
    assert test_state['stars_test_token']


@pytest.mark.asyncio
async def test_non_admin_gets_real_stars_invoice(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    payment_service = SimpleNamespace(
        create_stars_invoice=AsyncMock(return_value='https://t.me/invoice'),
    )
    monkeypatch.setattr(stars, 'PaymentService', MagicMock(return_value=payment_service))

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'stars_prompt_message_id': None})
    message = _message()

    await stars.process_stars_payment_amount(message, _user(), 50_000, state)

    payment_service.create_stars_invoice.assert_awaited_once()
    keyboard = message.answer.await_args.kwargs['reply_markup']
    assert keyboard.inline_keyboard[0][0].url == 'https://t.me/invoice'


@pytest.mark.asyncio
async def test_confirm_credits_once_and_shows_standard_success(monkeypatch):
    payment_service = SimpleNamespace(
        process_stars_payment=AsyncMock(
            return_value=StarsPaymentResult(
                success=True,
                amount_kopeks=50_000,
                old_balance_kopeks=0,
                new_balance_kopeks=50_000,
            )
        ),
        build_topup_success_keyboard=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(stars, 'PaymentService', MagicMock(return_value=payment_service))

    state = AsyncMock()
    state.get_data = AsyncMock(
        return_value={
            'stars_test_token': 'token',
            'stars_test_amount_kopeks': 50_000,
            'stars_test_stars_amount': 500,
        }
    )
    callback = SimpleNamespace(
        data='stars_test_pay|50000|token',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        bot=MagicMock(),
    )
    db = AsyncMock()
    db.refresh = AsyncMock()

    await stars.confirm_stars_test_payment(callback, _user(), db, state)

    payment_service.process_stars_payment.assert_awaited_once()
    assert payment_service.process_stars_payment.await_args.kwargs['telegram_payment_charge_id'] == 'stars_test_token'
    text = callback.message.edit_text.await_args.args[0]
    assert 'Баланс пополнен' in text
    assert '+500 ₽' in text
    assert '0 ₽ → 500 ₽' in text


@pytest.mark.asyncio
async def test_confirm_is_not_available_to_non_admin(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    payment_service = MagicMock()
    monkeypatch.setattr(stars, 'PaymentService', payment_service)

    callback = SimpleNamespace(
        data='stars_test_pay|50000|token',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        bot=MagicMock(),
    )
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'stars_test_token': 'token', 'stars_test_amount_kopeks': 50_000})

    await stars.confirm_stars_test_payment(callback, _user(), AsyncMock(), state)

    payment_service.assert_not_called()
    callback.answer.assert_awaited_once_with('❌ Тестовый режим недоступен', show_alert=True)
