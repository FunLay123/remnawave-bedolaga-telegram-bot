from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, InaccessibleMessage, InlineKeyboardMarkup

import app.handlers.referral as referral_handler
from app.config import settings
from app.handlers.subscription.purchase import _edit_message_text_or_caption


@pytest.mark.asyncio
async def test_start_trial_rejects_when_trial_is_unavailable(monkeypatch):
    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: False, raising=False)
    callback = SimpleNamespace(answer=AsyncMock())
    await referral_handler.start_trial_from_referral(callback, SimpleNamespace(), None)
    callback.answer.assert_awaited_once_with('Триал недоступен', show_alert=True)


@pytest.mark.asyncio
async def test_start_trial_calls_activate_trial_from_referral(monkeypatch):
    import app.handlers.subscription.purchase as purchase_handler

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: True, raising=False)
    activate = AsyncMock()
    monkeypatch.setattr(purchase_handler, 'activate_trial', activate)

    callback = SimpleNamespace()
    db_user = SimpleNamespace()
    db = SimpleNamespace()

    await referral_handler.start_trial_from_referral(callback, db_user, db)

    activate.assert_awaited_once_with(callback, db_user, db, from_referral=True)


@pytest.mark.asyncio
async def test_start_trial_after_scheme_switch_skips_referral_tail(monkeypatch):
    """Кнопка пережила переключение на levels — экран days-only перерисовывать нельзя."""
    import app.handlers.subscription.purchase as purchase_handler

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: True, raising=False)
    activate = AsyncMock()
    monkeypatch.setattr(purchase_handler, 'activate_trial', activate)

    callback = SimpleNamespace()
    db_user = SimpleNamespace()
    db = SimpleNamespace()

    await referral_handler.start_trial_from_referral(callback, db_user, db)

    activate.assert_awaited_once_with(callback, db_user, db, from_referral=False)


@pytest.mark.asyncio
async def test_start_trial_answers_callback_when_activate_raises(monkeypatch):
    """Сбой отрисовки после активации не должен оставить callback без ответа."""
    import app.handlers.subscription.purchase as purchase_handler

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: True, raising=False)
    monkeypatch.setattr(
        purchase_handler,
        'activate_trial',
        AsyncMock(side_effect=RuntimeError('message edit failed')),
    )

    callback = SimpleNamespace(answer=AsyncMock())
    await referral_handler.start_trial_from_referral(callback, SimpleNamespace(), SimpleNamespace())

    callback.answer.assert_awaited_once()
    message = callback.answer.await_args.args[0]
    assert 'Проверьте' in message
    assert 'подписку' in message.lower()


@pytest.mark.asyncio
async def test_start_trial_survives_failing_callback_answer(monkeypatch):
    """Истёкший callback не должен превращать сбой экрана в необработанное исключение."""
    import app.handlers.subscription.purchase as purchase_handler

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(referral_handler, 'is_trial_available_for_referral', lambda user: True, raising=False)
    monkeypatch.setattr(
        purchase_handler,
        'activate_trial',
        AsyncMock(side_effect=RuntimeError('message edit failed')),
    )

    callback = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError('query is too old')))
    await referral_handler.start_trial_from_referral(callback, SimpleNamespace(), SimpleNamespace())

    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_helper_sends_new_message_for_inaccessible_message(monkeypatch):
    """InaccessibleMessage (старое сообщение) не редактируется — шлём новое."""
    message = InaccessibleMessage(
        chat=Chat(id=42, type='private'),
        message_id=7,
        date=0,
    )
    answer = AsyncMock()
    monkeypatch.setattr(InaccessibleMessage, 'answer', answer)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    await _edit_message_text_or_caption(message, 'Текст после истечения', keyboard)

    answer.assert_awaited_once_with(
        'Текст после истечения',
        reply_markup=keyboard,
        parse_mode='HTML',
    )
