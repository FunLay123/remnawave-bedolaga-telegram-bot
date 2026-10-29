"""Бонус +3 дня к триалу для приглашённых (схема days_only)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.crud.subscription import activate_pending_trial_subscription
from app.services.trial_activation_service import get_effective_trial_days


def test_referred_user_gets_three_extra_days(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    user = SimpleNamespace(referred_by_id=1)
    assert get_effective_trial_days(user, 3) == 6
    assert get_effective_trial_days(user, 7) == 10


def test_non_referred_or_disabled_program_gets_base_days(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    assert get_effective_trial_days(SimpleNamespace(referred_by_id=None), 3) == 3

    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    assert get_effective_trial_days(SimpleNamespace(referred_by_id=1), 3) == 3


def test_zero_base_days_does_not_create_a_trial(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    assert get_effective_trial_days(SimpleNamespace(referred_by_id=1), 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('age_days', [2, 10])
async def test_late_pending_trial_keeps_its_original_duration(monkeypatch, age_days):
    """Поздняя оплата pending-триала не должна откатывать бонус приглашённого.

    Длительность фиксируется датами, созданными при оформлении заказа; если
    активация произошла позже (в том числе после исходного end_date), срок
    должен пересчитаться целиком от момента активации, а не от глобального
    TRIAL_DURATION_DAYS.
    """
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)

    created_at = datetime.now(UTC) - timedelta(days=age_days)
    pending = SimpleNamespace(
        id=10,
        user_id=7,
        status='pending',
        start_date=created_at,
        end_date=created_at + timedelta(days=6),
        duration_days=None,
    )

    result = MagicMock()
    result.scalar_one_or_none.return_value = pending
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    subscription = await activate_pending_trial_subscription(db, subscription_id=10, user_id=7)

    assert subscription is pending
    assert pending.status == 'active'
    assert (pending.end_date - pending.start_date) == timedelta(days=6)
    db.commit.assert_awaited_once()
