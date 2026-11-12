"""Единый предикат «подписке можно начислить бонусные дни».

До него экран партнёрки проверял только статус: суточный тариф и EXPIRED-
пустышка с нулевой длительностью считались подходящей целью, и человек видел
ссылку, по которой бонус всё равно не начислился бы.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import Subscription, User
from app.utils.referral_eligibility import (
    ELIGIBLE_REFERRAL_STATUSES,
    is_daily_referral_tariff,
    is_referral_reward_target,
)
from app.utils.referral_ui import ensure_subscriptions_loaded


def _sub(status: str, *, tariff=None, start_date=None, end_date=None):
    return SimpleNamespace(
        status=status,
        tariff=tariff,
        start_date=start_date,
        end_date=end_date,
    )


def test_eligible_statuses_contract():
    assert {'active', 'trial', 'limited', 'expired'} == ELIGIBLE_REFERRAL_STATUSES


def test_daily_tariff_is_not_a_reward_target():
    assert not is_referral_reward_target(_sub('active', tariff=SimpleNamespace(is_daily=True)))
    assert is_referral_reward_target(_sub('active', tariff=SimpleNamespace(is_daily=False)))
    # Тариф может быть не привязан вовсе — это не повод отказывать.
    assert is_referral_reward_target(_sub('active', tariff=None))


def test_daily_tariff_helper_is_public_and_none_safe():
    assert is_daily_referral_tariff(_sub('active', tariff=SimpleNamespace(is_daily=True)))
    assert not is_daily_referral_tariff(_sub('active', tariff=SimpleNamespace(is_daily=False)))
    assert not is_daily_referral_tariff(_sub('active', tariff=None))
    assert not is_daily_referral_tariff(_sub('active'))


def test_expired_zero_duration_placeholder_is_not_a_reward_target():
    moment = datetime.now(UTC)
    assert not is_referral_reward_target(_sub('expired', start_date=moment, end_date=moment))
    assert is_referral_reward_target(_sub('expired', start_date=moment, end_date=moment + timedelta(days=30)))


def test_expired_with_null_dates_is_not_a_placeholder():
    # Backend `_lock_referrer_target` считает пустышкой только EXPIRED с обеими
    # датами и нулевым сроком. Без дат (или с одной) сравнивать нечего, поэтому
    # UI не должен отказывать — иначе расходится с начислением.
    assert is_referral_reward_target(_sub('expired'))
    assert is_referral_reward_target(_sub('expired', start_date=datetime.now(UTC), end_date=None))
    assert is_referral_reward_target(_sub('expired', start_date=None, end_date=datetime.now(UTC)))


def test_disallowed_status_is_not_a_reward_target():
    assert not is_referral_reward_target(_sub('disabled'))
    assert not is_referral_reward_target(_sub('pending'))


@pytest.mark.asyncio
async def test_ensure_subscriptions_loaded_refreshes_unloaded_subscriptions():
    user = User()
    db = SimpleNamespace(refresh=AsyncMock())

    await ensure_subscriptions_loaded(db, user)

    db.refresh.assert_awaited_once_with(user, ['subscriptions'])


@pytest.mark.asyncio
async def test_ensure_subscriptions_loaded_warms_subscription_tariffs():
    """Без tariff в загруженных связях UI упадёт на ленивой подгрузке."""
    subscription = Subscription()
    user = User()
    user.subscriptions = [subscription]
    db = SimpleNamespace(refresh=AsyncMock())

    await ensure_subscriptions_loaded(db, user)

    db.refresh.assert_awaited_once_with(subscription, ['tariff'])
