"""Сводка рефералов считает оплативших по маркерам, а не по пополнениям баланса.

В схеме «только дни» оплата подписки не пополняет баланс, поэтому
``User.has_made_first_topup`` у оплатившего реферала так и остаётся ``False``.
Единственный след покупки — строка-маркер ``referral_first_purchase_registered``
в ledger'е: она принадлежит приглашённому, а ``referral_id`` указывает на
пригласившего.
"""

from datetime import UTC, datetime

import pytest

from app.database.models import Base, ReferralEarning, User
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REGISTERED,
)
from app.utils.user_utils import get_detailed_referral_list, get_user_referral_summary
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
async def test_summary_counts_markers_and_referrer_days(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3, referred_by_id=1)])
        db.add_all(
            [
                ReferralEarning(
                    user_id=2,
                    referral_id=1,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE_REGISTERED,
                    reward_type='days',
                    days_granted=0,
                ),
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
async def test_detailed_list_marks_first_purchase_per_referral(monkeypatch):
    """В списке рефералов оплата видна по маркеру, а не по флагу пополнения."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3, referred_by_id=1)])
        db.add_all(
            [
                ReferralEarning(
                    user_id=2,
                    referral_id=1,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE_REGISTERED,
                    reward_type='days',
                    days_granted=0,
                ),
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
