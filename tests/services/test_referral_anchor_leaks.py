"""Якорь регистрации не должен протекать в «заработок» и ленты.

``referral_registration_days_only`` — нулевая строка идемпотентности, а не
награда. В суммах она нулевая и не мешает, но в:
  * дате первого реферального начисления (от неё считаются траты «из реф. баланса»),
  * ленте активности (строка выглядит как начисление),
  * разбивке по уровням и счётчиках строк (якорь раздувает статистику)
она обязана быть исключена.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.referral import get_referral_statistics, get_top_referrers_by_period
from app.database.models import Base, ReferralEarning, User
from app.services.partner_stats_service import PartnerStatsService
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
    REASON_REGISTRATION_DAYS_ONLY,
)
from app.services.referral_withdrawal_service import referral_withdrawal_service
from app.services.user_activity_service import collect_user_activity
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


def _earning(
    user_id: int,
    referral_id: int,
    *,
    reason: str,
    days_granted: int = 0,
    created_at: datetime | None = None,
) -> ReferralEarning:
    return ReferralEarning(
        user_id=user_id,
        referral_id=referral_id,
        amount_kopeks=0,
        reason=reason,
        reward_type='days',
        days_granted=days_granted,
        created_at=created_at or datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_first_referral_earning_date_ignores_registration_anchor(monkeypatch):
    """Первое «начисление» — реальная награда, а не якорь регистрации."""
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        reward_at = now - timedelta(days=1)
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        db.add_all(
            [
                _earning(1, 2, reason=REASON_REGISTRATION_DAYS_ONLY, created_at=now - timedelta(days=2)),
                _earning(1, 2, reason=REASON_FIRST_PURCHASE, days_granted=10, created_at=reward_at),
            ]
        )
        await db.commit()

        first_earning = await referral_withdrawal_service.get_first_referral_earning_date(db, 1)

        assert first_earning is not None
        assert abs((first_earning - reward_at).total_seconds()) < 1


@pytest.mark.asyncio
async def test_activity_referral_feed_skips_registration_anchor(monkeypatch):
    """В таймлайне якорь не выглядит начислением."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        db.add_all(
            [
                _earning(1, 2, reason=REASON_REGISTRATION_DAYS_ONLY),
                _earning(1, 2, reason=REASON_FIRST_PURCHASE, days_granted=10),
            ]
        )
        await db.commit()

        activity = await collect_user_activity(db, 1, types='referral_earning')

        assert activity.total == 1
        assert len(activity.items) == 1
        assert activity.items[0].subtype == REASON_FIRST_PURCHASE


@pytest.mark.asyncio
async def test_activity_referral_feed_skips_zero_first_purchase_marker(monkeypatch):
    """Нулевой маркер первой покупки — не награда и не строка ленты.

    Маркер ``referral_first_purchase_registered`` принадлежит приглашённому
    (``user_id`` — он сам), поэтому попадал в ЕГО ленту как «начисление» с
    нулевой суммой. В ленте должна остаться только реальная награда.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        db.add_all(
            [
                _earning(2, 1, reason=REASON_FIRST_PURCHASE_REGISTERED),
                _earning(2, 1, reason=REASON_FIRST_PURCHASE_REFEREE, days_granted=5),
            ]
        )
        await db.commit()

        activity = await collect_user_activity(db, 2, types='referral_earning')

        assert activity.total == 1
        assert [item.subtype for item in activity.items] == [REASON_FIRST_PURCHASE_REFEREE]


@pytest.mark.asyncio
async def test_top_referrers_by_period_skips_registration_anchor(monkeypatch):
    """Якорь регистрации не делает из нулевого получателя «топ по заработку»."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        db.add(_earning(1, 2, reason=REASON_REGISTRATION_DAYS_ONLY))
        await db.commit()

        top = await get_top_referrers_by_period(db, period='week', sort_by='earnings', limit=20)

    assert top == []


@pytest.mark.asyncio
async def test_partner_detailed_stats_skip_registration_anchor(monkeypatch):
    """Якорь регистрации не создаёт уровень/строку в партнёрской статистике."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        db.add(_earning(1, 2, reason=REASON_REGISTRATION_DAYS_ONLY))
        await db.commit()

        stats = await PartnerStatsService.get_referrer_detailed_stats(db, 1)

        # Без фильтра GROUP BY level вернул бы нулевую строку уровня 1.
        assert stats['earnings_by_level'] == []
        assert stats['earnings']['all_time_kopeks'] == 0
        assert stats['earnings']['all_time_days'] == 0


@pytest.mark.asyncio
async def test_referral_statistics_by_level_skips_registration_anchor(monkeypatch):
    """Разбивка по уровням считает строки наград, а не якоря регистрации."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        db.add_all(
            [
                _earning(1, 2, reason=REASON_REGISTRATION_DAYS_ONLY),
                _earning(1, 2, reason=REASON_FIRST_PURCHASE, days_granted=10),
            ]
        )
        await db.commit()

        stats = await get_referral_statistics(db)
        by_level = {row['level']: row for row in stats['by_level']}

        assert by_level[1]['rows'] == 1
        assert by_level[1]['days'] == 10
