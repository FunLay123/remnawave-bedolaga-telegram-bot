"""Сводка рефералов считает оплативших по маркерам, а не по пополнениям баланса.

В схеме «только дни» оплата подписки не пополняет баланс, поэтому
``User.has_made_first_topup`` у оплатившего реферала так и остаётся ``False``.
Единственный след покупки — строка-маркер ``referral_first_purchase_registered``
в ledger'е: она принадлежит приглашённому, а ``referral_id`` указывает на
пригласившего.
"""

from datetime import UTC, datetime

import pytest

from app.config import settings
from app.database.models import Base, ReferralEarning, User
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REGISTERED,
)
from app.utils.user_utils import get_detailed_referral_list, get_user_referral_summary
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None, *, has_made_first_topup: bool = False) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
        has_made_first_topup=has_made_first_topup,
    )


def _marker(user_id: int, referral_id: int) -> ReferralEarning:
    return ReferralEarning(
        user_id=user_id,
        referral_id=referral_id,
        amount_kopeks=0,
        reason=REASON_FIRST_PURCHASE_REGISTERED,
        reward_type='days',
        days_granted=0,
    )


@pytest.mark.asyncio
async def test_summary_counts_markers_and_referrer_days(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        # У третьего реферала флаг пополнения True, но маркера нет: в days-only
        # флаг не должен попасть в счётчик. Маркер второго продублирован —
        # COUNT(DISTINCT user_id) обязан посчитать его один раз.
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3, referred_by_id=1, has_made_first_topup=True)])
        db.add_all(
            [
                _marker(2, 1),
                _marker(2, 1),
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE,
                    reward_type='days',
                    days_granted=10,
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await db.commit()

        summary = await get_user_referral_summary(db, 1)

        assert summary['invited_count'] == 2
        assert summary['paid_referrals_count'] == 1
        assert summary['total_earned_days'] == 10
        assert summary['conversion_rate'] == 50.0


@pytest.mark.asyncio
async def test_legacy_summary_keeps_first_topup_counter(monkeypatch):
    """Вне days-only маркеров нет, поэтому счётчик остаётся на флаге пополнения."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
    async with memory_session(monkeypatch, TABLES) as db:
        # Оба реферала с флагом, но маркер только у второго: маркерный счётчик
        # дал бы 1, флаговый — 2. Ожидание 2 доказывает, что режим не перепутан.
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1, has_made_first_topup=True),
                _user(3, referred_by_id=1, has_made_first_topup=True),
            ]
        )
        db.add(_marker(2, 1))
        await db.commit()

        summary = await get_user_referral_summary(db, 1)

        assert summary['invited_count'] == 2
        assert summary['paid_referrals_count'] == 2
        assert summary['conversion_rate'] == 100.0


@pytest.mark.asyncio
async def test_detailed_list_marks_first_purchase_per_referral(monkeypatch):
    """В списке рефералов оплата видна по маркеру, а не по флагу пополнения."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3, referred_by_id=1), _user(4)])
        db.add_all(
            [
                _marker(2, 1),
                # Маркер третьего реферала принадлежит ДРУГОМУ пригласившему:
                # после передачи реферала он не должен всплывать в списке user 1.
                _marker(3, 4),
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE,
                    reward_type='days',
                    days_granted=10,
                ),
            ]
        )
        await db.commit()

        detailed = await get_detailed_referral_list(db, 1)
        by_id = {row['id']: row for row in detailed['referrals']}

        assert by_id[2]['has_first_purchase'] is True
        assert by_id[3]['has_first_purchase'] is False
        assert by_id[2]['days_earned'] == 10
        # Старое поле остаётся: UI переводится на маркеры отдельной задачей.
        assert by_id[2]['has_made_first_topup'] is False
