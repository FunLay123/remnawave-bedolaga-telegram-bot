from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_purchase_service import (
    backfill_first_purchase_markers,
    ensure_marker_for_already_paid_referral,
    has_first_purchase_marker,
)
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
    )


def _paid_sub(sub_id: int, user_id: int) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=30),
        traffic_limit_gb=100,
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_marker_is_written_once_for_a_paid_referral(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_sub(10, 2)])
        await db.commit()

        assert await has_first_purchase_marker(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await has_first_purchase_marker(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is False

        rows = (
            (
                await db.execute(
                    select(ReferralEarning).where(
                        ReferralEarning.user_id == 2,
                        ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].days_granted == 0
        assert rows[0].amount_kopeks == 0


@pytest.mark.asyncio
async def test_backfill_is_idempotent(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3, referred_by_id=1),
                _paid_sub(10, 2),
            ]
        )
        await db.commit()

        assert await backfill_first_purchase_markers(db) == 1
        assert await backfill_first_purchase_markers(db) == 0
