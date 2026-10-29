import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, ReferralEarning, User
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
