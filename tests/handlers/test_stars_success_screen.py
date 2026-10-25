"""Stars success screen must not pretend a subscription payment topped up balance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers import stars_payments
from app.services.payment.stars import StarsPaymentResult


def _message(payload: str) -> MagicMock:
    message = MagicMock()
    message.successful_payment = SimpleNamespace(
        total_amount=100,
        invoice_payload=payload,
        telegram_payment_charge_id='charge-1',
    )
    message.from_user = SimpleNamespace(id=7)
    message.answer = AsyncMock()
    message.bot.delete_message = AsyncMock()
    return message


def _service(result: StarsPaymentResult) -> SimpleNamespace:
    return SimpleNamespace(
        process_stars_payment=AsyncMock(return_value=result),
        build_topup_success_keyboard=AsyncMock(return_value=None),
    )


def _user(balance_kopeks: int = 0) -> SimpleNamespace:
    return SimpleNamespace(id=7, telegram_id=7, language='ru', balance_kopeks=balance_kopeks)


@pytest.mark.asyncio
async def test_simple_subscription_payment_has_no_balance_topup_screen(monkeypatch):
    user = _user()
    monkeypatch.setattr(stars_payments, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    service = _service(StarsPaymentResult(success=True, amount_kopeks=10_000))
    monkeypatch.setattr(stars_payments, 'PaymentService', MagicMock(return_value=service))

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    message = _message('simple_sub_7_1_30')

    await stars_payments.handle_successful_payment(message, AsyncMock(), state)

    service.process_stars_payment.assert_awaited_once()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_balance_payload_still_sends_topup_screen(monkeypatch):
    user = _user(balance_kopeks=10_000)
    monkeypatch.setattr(stars_payments, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    service = _service(
        StarsPaymentResult(
            success=True,
            amount_kopeks=10_000,
            old_balance_kopeks=0,
            new_balance_kopeks=10_000,
        )
    )
    monkeypatch.setattr(stars_payments, 'PaymentService', MagicMock(return_value=service))

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    message = _message('balance_7_100')

    await stars_payments.handle_successful_payment(message, AsyncMock(), state)

    message.answer.assert_awaited_once()
    assert 'Баланс пополнен' in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_balance_screen_uses_amount_credited_by_service(monkeypatch):
    # Сервис зачислил 500 ₽ из payload, хотя Telegram вернул 100 ⭐ (100 ₽ по курсу).
    user = _user(balance_kopeks=50_000)
    monkeypatch.setattr(stars_payments, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    service = _service(
        StarsPaymentResult(
            success=True,
            amount_kopeks=50_000,
            old_balance_kopeks=0,
            new_balance_kopeks=50_000,
        )
    )
    monkeypatch.setattr(stars_payments, 'PaymentService', MagicMock(return_value=service))

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    message = _message('balance_7_50000')

    await stars_payments.handle_successful_payment(message, AsyncMock(), state)

    text = message.answer.await_args.args[0]
    assert '+500 ₽' in text
    assert 'Баланс: 0 ₽ → 500 ₽' in text


@pytest.mark.asyncio
async def test_balance_screen_uses_balance_at_credit_time_not_current(monkeypatch):
    """Автопродление/автопокупка списали деньги после зачисления — экран не должен это учитывать."""
    user = _user(balance_kopeks=5_000)  # текущий баланс уже после пост-топапного списания
    monkeypatch.setattr(stars_payments, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    service = _service(
        StarsPaymentResult(
            success=True,
            amount_kopeks=10_000,
            old_balance_kopeks=0,
            new_balance_kopeks=10_000,
        )
    )
    monkeypatch.setattr(stars_payments, 'PaymentService', MagicMock(return_value=service))

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    message = _message('balance_7_10000')

    await stars_payments.handle_successful_payment(message, AsyncMock(), state)

    text = message.answer.await_args.args[0]
    assert 'Баланс: 0 ₽ → 100 ₽' in text
    assert 'Баланс: 50 ₽' not in text


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_repeat_success_screen(monkeypatch):
    user = _user(balance_kopeks=42_000)
    monkeypatch.setattr(stars_payments, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    service = _service(
        StarsPaymentResult(
            success=True,
            amount_kopeks=10_000,
            already_processed=True,
        )
    )
    monkeypatch.setattr(stars_payments, 'PaymentService', MagicMock(return_value=service))

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    message = _message('balance_7_10000')

    await stars_payments.handle_successful_payment(message, AsyncMock(), state)

    service.process_stars_payment.assert_awaited_once()
    message.answer.assert_not_awaited()
