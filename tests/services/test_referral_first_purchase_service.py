from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.services.referral_purchase_service as purchase_module
from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_purchase_service import (
    has_first_purchase_marker,
    register_first_purchase,
)
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
)
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


def _sub(sub_id: int, user_id: int, *, trial: bool = False, days: int = 30) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        # shortId уникален; без него две подписки получили бы одинаковый '' и
        # упёрлись бы в уникальный индекс раньше, чем дойдёт до тестируемой логики.
        remnawave_short_id=f'sub{sub_id}',
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=trial,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=days),
        traffic_limit_gb=100,
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_first_30_day_purchase_rewards_both_sides(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'rewarded'
        assert result.referee_days == 10
        assert result.referrer_days == 10
        assert result.referee_subscription_id == 10
        assert result.referrer_subscription_id == 11

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        by_reason = {row.reason: row for row in rows}
        assert by_reason[REASON_FIRST_PURCHASE_REGISTERED].days_granted == 0
        assert by_reason[REASON_FIRST_PURCHASE].days_granted == 10
        assert by_reason[REASON_FIRST_PURCHASE_REFEREE].days_granted == 10
        # Строка приглашённому зеркалит пару: получатель — он сам.
        assert by_reason[REASON_FIRST_PURCHASE_REFEREE].user_id == 2
        assert by_reason[REASON_FIRST_PURCHASE_REFEREE].referral_id == 1
        # Строка пригласившему — в прежней ориентации ledger'а.
        assert by_reason[REASON_FIRST_PURCHASE].user_id == 1
        assert by_reason[REASON_FIRST_PURCHASE].referral_id == 2

        referee_sub = await db.get(Subscription, 10)
        referrer_sub = await db.get(Subscription, 11)
        assert referee_sub.end_date > datetime.now(UTC) + timedelta(days=39)
        assert referrer_sub.end_date > datetime.now(UTC) + timedelta(days=39)


@pytest.mark.asyncio
async def test_first_14_day_purchase_consumes_marker_without_reward(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()

        first = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=14,
            paid_amount_kopeks=5_000,
            source='test',
        )
        second = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert first.status == 'registered'
        assert first.referee_days == 0
        assert second.status == 'already_registered'
        assert second.referee_days == 0

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert [row.reason for row in rows] == [REASON_FIRST_PURCHASE_REGISTERED]


@pytest.mark.asyncio
async def test_referrer_without_subscription_skips_only_referrer(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2)])
        await db.commit()

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'rewarded'
        assert result.referee_days == 10
        assert result.referrer_days == 0
        assert result.referrer_subscription_id is None
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert REASON_FIRST_PURCHASE_REFEREE in {row.reason for row in rows}
        assert REASON_FIRST_PURCHASE not in {row.reason for row in rows}


@pytest.mark.asyncio
async def test_free_or_unreferred_purchase_does_not_write_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3), _sub(10, 2), _sub(11, 3)])
        await db.commit()

        free = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=0,
            source='test',
        )
        unreferred = await register_first_purchase(
            db,
            referee_id=3,
            subscription_id=11,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert free.status == 'skipped'
        assert unreferred.status == 'not_referred'
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_db_error_rolls_back_marker_and_returns_error(monkeypatch):
    """Сбой БД на выдаче дней не оставляет полусырой маркер.

    ``add_reward_days`` может нарушить частичный уникальный индекс в тарифном
    режиме, когда истёкшая подписка поднимается в ACTIVE рядом с живой подпиской
    того же тарифа. Маркер и обе строки наград обязаны откатиться вместе.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()

        referee_sub = await db.get(Subscription, 10)
        assert referee_sub is not None
        original_end = referee_sub.end_date

        async def failing_add_reward_days(session, subscription, days):
            # Как настоящий helper: успевает записать уже добавленное в БД,
            # и только затем упирается в ограничение.
            await session.flush()
            raise IntegrityError('UPDATE subscriptions', None, Exception('duplicate key value'))

        monkeypatch.setattr(purchase_module, 'add_reward_days', failing_add_reward_days)

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'error'
        assert result.referee_days == 0
        assert result.referrer_days == 0
        assert await has_first_purchase_marker(db, 2) is False

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []

        referee_sub = await db.get(Subscription, 10)
        assert abs((referee_sub.end_date - original_end).total_seconds()) < 1
