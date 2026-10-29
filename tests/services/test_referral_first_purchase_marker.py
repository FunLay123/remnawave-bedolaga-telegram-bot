from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
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


def _subscription(
    sub_id: int,
    user_id: int,
    *,
    status: str = SubscriptionStatus.ACTIVE.value,
    is_trial: bool = False,
    start_offset_days: int = -1,
    end_offset_days: int = 30,
) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=status,
        is_trial=is_trial,
        start_date=now + timedelta(days=start_offset_days),
        end_date=now + timedelta(days=end_offset_days),
        traffic_limit_gb=100,
        device_limit=1,
    )


def _paid_sub(sub_id: int, user_id: int) -> Subscription:
    return _subscription(sub_id, user_id)


@pytest.mark.asyncio
async def test_marker_is_written_once_for_a_paid_referral(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
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


@pytest.mark.asyncio
async def test_pending_subscription_does_not_write_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _subscription(10, 2, status=SubscriptionStatus.PENDING.value),
            ]
        )
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_expired_zero_duration_placeholder_does_not_write_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _subscription(
                    10,
                    2,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=0,
                    end_offset_days=0,
                ),
            ]
        )
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_expired_real_subscription_writes_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _subscription(
                    10,
                    2,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=-60,
                    end_offset_days=-30,
                ),
            ]
        )
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
@pytest.mark.parametrize('scheme', ['legacy', 'levels'])
async def test_non_days_only_scheme_writes_no_marker(monkeypatch, scheme):
    """Маркер принадлежит только схеме «только дни».

    В legacy/levels оплата уже учитывается деньгами и флагами пополнения;
    маркер там исказил бы счётчик оплативших после переключения схемы.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', scheme)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_sub(10, 2)])
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False
        assert (await db.execute(select(ReferralEarning))).scalars().all() == []


@pytest.mark.asyncio
async def test_backfill_skips_placeholders_and_marks_real_subscriptions(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3, referred_by_id=1),
                _user(4, referred_by_id=1),
                _user(5, referred_by_id=1),
                _subscription(10, 2, status=SubscriptionStatus.PENDING.value),
                _subscription(
                    11,
                    3,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=0,
                    end_offset_days=0,
                ),
                _subscription(
                    12,
                    4,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=-60,
                    end_offset_days=-30,
                ),
                _subscription(13, 5, status=SubscriptionStatus.ACTIVE.value),
            ]
        )
        await db.commit()

        assert await backfill_first_purchase_markers(db) == 2

        marked_user_ids = set((await db.execute(select(ReferralEarning.user_id))).scalars().all())
        assert marked_user_ids == {4, 5}
        assert await backfill_first_purchase_markers(db) == 0
