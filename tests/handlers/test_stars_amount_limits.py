"""Лимиты Stars: экран суммы и ручная валидация должны совпадать с конфигом метода.

Регрессия из ревью: быстрые суммы и кабинет ограничивают Stars 1–10 000 ₽,
а ручной ввод принимал до глобальных 50 000 ₽.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.balance import stars as stars_handler
from app.utils.payment_utils import DEFAULT_TOPUP_MAX_KOPEKS, DEFAULT_TOPUP_MIN_KOPEKS


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


def _message() -> MagicMock:
    message = MagicMock()
    message.message_id = 10
    message.chat = SimpleNamespace(id=7)
    message.bot = MagicMock()
    message.bot.delete_message = AsyncMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=11, chat=SimpleNamespace(id=7)),
    )
    return message


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=7, telegram_id=7, language='ru', restriction_topup=False)


def _state() -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


def _patch_limits(monkeypatch, limits: tuple[int, int]) -> None:
    monkeypatch.setattr(
        stars_handler,
        '_get_stars_limits',
        AsyncMock(return_value=limits),
    )


@pytest.mark.asyncio
async def test_amount_above_method_max_is_rejected(monkeypatch):
    _patch_limits(monkeypatch, (100, 1_000_000))
    payment_service_factory = MagicMock()
    monkeypatch.setattr(stars_handler, 'PaymentService', payment_service_factory)

    message = _message()
    await stars_handler.process_stars_payment_amount(message, _user(), 1_000_001, _state())

    hint = message.answer.await_args.args[0]
    assert 'от 1 ₽ до 10000 ₽' in hint
    payment_service_factory.assert_not_called()


@pytest.mark.asyncio
async def test_amount_at_method_max_creates_invoice(monkeypatch):
    _patch_limits(monkeypatch, (100, 1_000_000))
    service = SimpleNamespace(create_stars_invoice=AsyncMock(return_value='https://t.me/invoice/1'))
    monkeypatch.setattr(stars_handler, 'PaymentService', MagicMock(return_value=service))

    message = _message()
    await stars_handler.process_stars_payment_amount(message, _user(), 1_000_000, _state())

    service.create_stars_invoice.assert_awaited_once()
    assert service.create_stars_invoice.await_args.kwargs['amount_kopeks'] == 1_000_000
    assert '10000 ₽' in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_get_stars_limits_reads_method_config(monkeypatch):
    monkeypatch.setattr(stars_handler, 'AsyncSessionLocal', lambda: _FakeSessionCtx())
    limits = AsyncMock(return_value=(100, 1_000_000))
    monkeypatch.setattr(stars_handler, 'get_method_amount_limits', limits)

    assert await stars_handler._get_stars_limits() == (100, 1_000_000)
    limits.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_stars_limits_falls_back_to_global_on_db_error(monkeypatch):
    monkeypatch.setattr(stars_handler, 'AsyncSessionLocal', lambda: _FakeSessionCtx())
    monkeypatch.setattr(
        stars_handler,
        'get_method_amount_limits',
        AsyncMock(side_effect=RuntimeError('db down')),
    )

    assert await stars_handler._get_stars_limits() == (
        DEFAULT_TOPUP_MIN_KOPEKS,
        DEFAULT_TOPUP_MAX_KOPEKS,
    )
