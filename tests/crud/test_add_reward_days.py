from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.crud.subscription import add_reward_days
from app.database.models import Base, Subscription, SubscriptionStatus, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int) -> User:
    return User(id=uid, telegram_id=1000 + uid, first_name=f'U{uid}', language='ru', balance_kopeks=0)


def _sub(
    sub_id: int,
    user_id: int,
    *,
    status: str,
    days: int,
    trial: bool = False,
    traffic_used: float = 0.0,
) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=status,
        is_trial=trial,
        start_date=now - timedelta(days=30),
        end_date=now + timedelta(days=days),
        traffic_limit_gb=100,
        traffic_used_gb=traffic_used,
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_active_subscription_gets_days_without_traffic_reset(monkeypatch):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.ACTIVE.value, days=10, traffic_used=42.0))
        await db.commit()

        sub = await db.get(Subscription, 10)
        before = sub.end_date
        got = await add_reward_days(db, sub, 10)

        assert got is not None
        assert got.end_date >= before + timedelta(days=9, hours=23)
        assert got.traffic_used_gb == 42.0
        assert got.is_trial is False
        assert got.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_expired_trial_reactivates_as_trial(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.EXPIRED.value, days=-5, trial=True))
        await db.commit()

        sub = await db.get(Subscription, 10)
        got = await add_reward_days(db, sub, 10)

        assert got is not None
        assert got.status == SubscriptionStatus.TRIAL.value
        assert got.is_trial is True
        assert got.end_date > datetime.now(UTC) + timedelta(days=9)


@pytest.mark.asyncio
async def test_limited_subscription_keeps_limited_status(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.LIMITED.value, days=5))
        await db.commit()

        sub = await db.get(Subscription, 10)
        got = await add_reward_days(db, sub, 10)

        assert got is not None
        assert got.status == SubscriptionStatus.LIMITED.value
        assert got.end_date > datetime.now(UTC) + timedelta(days=14)


@pytest.mark.asyncio
async def test_pending_and_disabled_are_not_touched(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_sub(10, 1, status=SubscriptionStatus.PENDING.value, days=5))
        db.add(_sub(11, 1, status=SubscriptionStatus.DISABLED.value, days=5))
        await db.commit()

        assert await add_reward_days(db, await db.get(Subscription, 10), 10) is None
        assert await add_reward_days(db, await db.get(Subscription, 11), 10) is None
