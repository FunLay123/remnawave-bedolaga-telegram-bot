"""Flow «сначала сумма, потом способ»: balance_topup → topup_sum → методы.

Кнопка «Пополнить» больше не открывает список способов с молчаливой подстановкой
суммы из корзины: сначала показывается экран суммы с быстрыми кнопками и ручным
вводом, и только выбранная сумма задаёт callback `topup_amount|<method>|<amount>`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database import database
from app.handlers.balance import main as balance_main
from app.states import BalanceStates


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


@pytest.fixture(autouse=True)
def _only_yookassa(monkeypatch):
    """Один автоматический способ: без Stars и поддержки клавиатура предсказуема."""
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', False)
    monkeypatch.setattr(settings, 'SUPPORT_TOPUP_ENABLED', False)
    monkeypatch.setattr(settings, 'YOOKASSA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', 'secret', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SBP_ENABLED', False, raising=False)


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))


def _user(balance_kopeks: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        telegram_id=7,
        language='ru',
        restriction_topup=False,
        balance_kopeks=balance_kopeks,
    )


def _callback(data: str) -> SimpleNamespace:
    message = MagicMock()
    message.edit_text = AsyncMock()
    message.edit_caption = AsyncMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock()
    message.message_id = 10
    message.chat = SimpleNamespace(id=7)
    return SimpleNamespace(
        data=data,
        message=message,
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=7),
    )


def _message(text: str | None) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.successful_payment = None
    message.message_id = 10
    message.chat = SimpleNamespace(id=7)
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_balance_topup_renders_amount_screen(monkeypatch, state):
    limits = AsyncMock(return_value={'yookassa': (5000, 1_000_000)})
    monkeypatch.setattr(balance_main, 'get_methods_amount_limits', limits)
    callback = _callback('balance_topup')

    await balance_main.show_topup_amount_select(callback, _user(balance_kopeks=12_300), AsyncMock(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert 'Пополнение от 50 ₽ до 10000 ₽' in text
    callbacks = _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert {
        'topup_sum|5000',
        'topup_sum|10000',
        'topup_sum|15000',
        'topup_sum|30000',
        'topup_sum|50000',
    } <= set(callbacks)
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert await state.get_data() == {'awaiting_topup_amount': True, 'payment_method': None}
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_quick_sum_renders_methods_with_amount(monkeypatch, state):
    monkeypatch.setattr(
        balance_main,
        'get_methods_amount_limits',
        AsyncMock(return_value={'yookassa': (5000, 1_000_000)}),
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(awaiting_topup_amount=True, payment_method=None)
    callback = _callback('topup_sum|5000')

    await balance_main.handle_topup_sum_choice(callback, _user(), AsyncMock(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert 'К пополнению: <b>50 ₽</b>' in text
    callbacks = _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'topup_amount|yookassa|5000' in callbacks
    assert await state.get_state() is None
    assert await state.get_data() == {}
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_typed_amount_below_global_min_shows_minimum(monkeypatch, state):
    render = AsyncMock(return_value=True)
    monkeypatch.setattr(balance_main, '_render_payment_methods', render)
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(awaiting_topup_amount=True, payment_method=None)
    message = _message('30')

    await balance_main.process_topup_amount(message, _user(), state)

    hint = message.answer.await_args.args[0]
    assert 'Минимальная сумма — 50 ₽' in hint
    render.assert_not_awaited()
    assert (await state.get_data())['awaiting_topup_amount'] is True


@pytest.mark.asyncio
async def test_typed_amount_renders_methods_for_chosen_sum(monkeypatch, state):
    monkeypatch.setattr(
        balance_main,
        'get_methods_amount_limits',
        AsyncMock(return_value={'yookassa': (5000, 1_000_000)}),
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(database, 'AsyncSessionLocal', MagicMock(return_value=session))
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(awaiting_topup_amount=True, payment_method=None)
    message = _message('50')

    await balance_main.process_topup_amount(message, _user(), state)

    text = message.answer.await_args.args[0]
    assert 'К пополнению: <b>50 ₽</b>' in text
    callbacks = _callbacks(message.answer.await_args.kwargs['reply_markup'])
    assert 'topup_amount|yookassa|5000' in callbacks
    assert await state.get_state() is None
    assert await state.get_data() == {}


@pytest.mark.asyncio
async def test_methods_are_filtered_by_amount_limits(monkeypatch, state):
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', 'token', raising=False)
    monkeypatch.setattr(
        balance_main,
        'get_methods_amount_limits',
        AsyncMock(return_value={'yookassa': (5000, 1_000_000), 'cryptobot': (10_000, 10_000_000)}),
    )
    callback = _callback('topup_sum|5000')

    await balance_main.handle_topup_sum_choice(callback, _user(), AsyncMock(), state)

    callbacks = _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'topup_amount|yookassa|5000' in callbacks
    assert not [value for value in callbacks if value.startswith('topup_amount|cryptobot|')]


@pytest.mark.asyncio
async def test_quick_sum_without_available_methods_shows_amount_screen(monkeypatch, state):
    """Способ не принимает выбранную сумму — показываем экран суммы, не алерт."""
    monkeypatch.setattr(
        balance_main,
        'get_methods_amount_limits',
        AsyncMock(return_value={'yookassa': (10_000, 1_000_000)}),
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(awaiting_topup_amount=True, payment_method=None)
    callback = _callback('topup_sum|5000')

    await balance_main.handle_topup_sum_choice(callback, _user(), AsyncMock(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert 'Пополнение от 100 ₽ до 10000 ₽' in text
    callback.answer.assert_awaited_once_with('Выберите сумму пополнения')
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert await state.get_data() == {'awaiting_topup_amount': True, 'payment_method': None}


@pytest.mark.asyncio
@pytest.mark.parametrize('amount_kopeks', [3000, 5_000_001])
async def test_quick_sum_outside_global_limits_shows_amount_screen(monkeypatch, state, amount_kopeks):
    """Прямой topup_sum| вне лимитов не должен упираться в алерт без выхода."""
    monkeypatch.setattr(
        balance_main,
        'get_methods_amount_limits',
        AsyncMock(return_value={'yookassa': (5000, 1_000_000)}),
    )
    callback = _callback(f'topup_sum|{amount_kopeks}')

    await balance_main.handle_topup_sum_choice(callback, _user(), AsyncMock(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert 'Пополнение от 50 ₽ до 10000 ₽' in text
    callback.answer.assert_awaited_once_with('Выберите сумму пополнения')
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert await state.get_data() == {'awaiting_topup_amount': True, 'payment_method': None}


@pytest.mark.asyncio
async def test_quick_sum_with_no_methods_for_amount_shows_amount_screen(monkeypatch, state):
    """`_render_payment_methods` вернул False — возврат к выбору суммы, не алерт."""
    monkeypatch.setattr(balance_main, '_render_payment_methods', AsyncMock(return_value=False))
    monkeypatch.setattr(
        balance_main,
        'get_methods_amount_limits',
        AsyncMock(return_value={'yookassa': (5000, 1_000_000)}),
    )
    callback = _callback('topup_sum|5000')

    await balance_main.handle_topup_sum_choice(callback, _user(), AsyncMock(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert 'Пополнение от 50 ₽ до 10000 ₽' in text
    callback.answer.assert_awaited_once_with('Выберите сумму пополнения')
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert await state.get_data() == {'awaiting_topup_amount': True, 'payment_method': None}


@pytest.mark.asyncio
async def test_typed_amount_without_available_methods_explains_and_keeps_state(monkeypatch, state):
    monkeypatch.setattr(
        balance_main,
        'get_methods_amount_limits',
        AsyncMock(return_value={'yookassa': (10_000, 1_000_000)}),
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(database, 'AsyncSessionLocal', MagicMock(return_value=session))
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(awaiting_topup_amount=True, payment_method=None)
    message = _message('50')

    await balance_main.process_topup_amount(message, _user(), state)

    hint = message.answer.await_args.args[0]
    assert 'Для этой суммы нет доступных способов оплаты' in hint
    assert message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'balance_topup'
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert (await state.get_data())['awaiting_topup_amount'] is True


def _support_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='Через поддержку', callback_data='topup_support')],
            [InlineKeyboardButton(text='Назад', callback_data='back_to_menu')],
        ]
    )


@pytest.mark.asyncio
async def test_support_only_deployment_shows_support_screen(monkeypatch, state):
    """Только поддержка: balance_topup не должен вести в тупик экрана суммы."""
    monkeypatch.setattr(settings, 'SUPPORT_TOPUP_ENABLED', True)
    monkeypatch.setattr(
        balance_main,
        'get_payment_methods_keyboard',
        MagicMock(return_value=_support_only_keyboard()),
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(awaiting_topup_amount=True)
    callback = _callback('balance_topup')

    await balance_main.show_topup_amount_select(callback, _user(balance_kopeks=12_300), AsyncMock(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert 'Автоматические способы оплаты недоступны' in text
    assert 'Напишите в поддержку' in text
    assert 'Пополнение от' not in text
    callbacks = _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'topup_support' in callbacks
    assert not [value for value in callbacks if value.startswith('topup_sum|')]
    assert await state.get_state() is None
    assert await state.get_data() == {}
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_methods_and_support_disabled_shows_unavailable_screen(monkeypatch, state):
    monkeypatch.setattr(settings, 'SUPPORT_TOPUP_ENABLED', False)
    monkeypatch.setattr(
        balance_main,
        'get_payment_methods_keyboard',
        MagicMock(
            return_value=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='Назад', callback_data='back_to_menu')]]
            )
        ),
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(awaiting_topup_amount=True)
    callback = _callback('balance_topup')

    await balance_main.show_topup_amount_select(callback, _user(), AsyncMock(), state)

    text = callback.message.edit_text.await_args.args[0]
    assert 'Способы оплаты временно недоступны' in text
    assert 'Пополнение от' not in text
    callbacks = _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup'])
    assert 'topup_support' not in callbacks
    assert await state.get_state() is None
    assert await state.get_data() == {}
    callback.answer.assert_awaited_once()
