from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, ReferralEarning, User
from app.services.referral_contest_service import referral_contest_service
from app.services.referral_reward_service import award_referral_rewards
from app.services.referral_service import process_referral_registration, process_referral_topup
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


@pytest.mark.asyncio
async def test_registration_writes_no_pending_row(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1) is True
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_registration_sends_days_only_notices_and_records_contest(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 5)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)

    contest_mock = AsyncMock()
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', contest_mock)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True

        contest_mock.assert_awaited_once()
        assert contest_mock.await_args.args == (db, 2)

        assert bot.send_message.await_count == 2
        referee_call, referrer_call = bot.send_message.await_args_list
        assert referee_call.args[0] == 1002
        assert 'Добро пожаловать' in referee_call.args[1]
        assert '+5 дней' in referee_call.args[1]
        assert referrer_call.args[0] == 1001
        assert 'Новый реферал' in referrer_call.args[1]
        assert 'от 30 дней' in referrer_call.args[1]
        assert '+7 дней' in referrer_call.args[1]

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []
        referee = await db.get(User, 2)
        referrer = await db.get(User, 1)
        assert referee.balance_kopeks == 0
        assert referrer.balance_kopeks == 0
        assert referee.has_made_first_topup is False
        assert referee.has_had_paid_subscription is False


@pytest.mark.asyncio
async def test_registration_still_rejects_unattached_user(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1) is False
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_topup_does_not_change_balance_or_flags(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_topup(db, 2, 10_000) is True
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []
        referee = await db.get(User, 2)
        referrer = await db.get(User, 1)
        assert referee.balance_kopeks == 0
        assert referrer.balance_kopeks == 0
        assert referee.has_made_first_topup is False


@pytest.mark.asyncio
async def test_award_rewards_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()
        referee = await db.get(User, 2)
        assert await award_referral_rewards(db, referee, event='first_topup', topup_amount_kopeks=10_000) == []
