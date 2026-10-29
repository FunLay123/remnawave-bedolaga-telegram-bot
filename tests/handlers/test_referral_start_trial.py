from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.referral as referral_handler
from app.config import settings


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
