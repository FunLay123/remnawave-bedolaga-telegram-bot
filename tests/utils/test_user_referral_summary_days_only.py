"""Сводка рефералов считает оплативших по маркерам, а не по пополнениям баланса.

В схеме «только дни» оплата подписки не пополняет баланс, поэтому
``User.has_made_first_topup`` у оплатившего реферала так и остаётся ``False``.
Единственный след покупки — строка-маркер ``referral_first_purchase_registered``
в ledger'е: она принадлежит приглашённому, а ``referral_id`` указывает на
пригласившего.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.models import Base, ReferralEarning, User
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REGISTERED,
    REASON_REGISTRATION_DAYS_ONLY,
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
        # флаг не должен попасть в счётчик. Маркер второго — единственный:
        # уникальный индекс 0136 структурно не даёт продублировать его, поэтому
        # запрос считает строки без COUNT(DISTINCT).
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3, referred_by_id=1, has_made_first_topup=True)])
        db.add_all(
            [
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
async def test_reassigned_referral_moves_paid_count_with_the_invite(monkeypatch):
    """После передачи реферала счётчик оплативших переезжает вместе с ним.

    Раньше маркеры считались по ``referral_id``: старый пригласивший сохранял
    оплатившего, хотя приглашённых у него уже не было, и конверсия превышала
    100%. Теперь оплата считается только у того, к кому реферал привязан сейчас.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3)])
        db.add(_marker(2, 1))
        await db.commit()

        before = await get_user_referral_summary(db, 1)
        assert before['invited_count'] == 1
        assert before['paid_referrals_count'] == 1
        assert before['conversion_rate'] == 100.0

        # Админ передал реферала 2 другому пригласившему.
        referred = await db.get(User, 2)
        referred.referred_by_id = 3
        await db.commit()

        old_referrer = await get_user_referral_summary(db, 1)
        new_referrer = await get_user_referral_summary(db, 3)

        assert old_referrer['invited_count'] == 0
        assert old_referrer['paid_referrals_count'] == 0
        assert old_referrer['conversion_rate'] == 0.0
        assert new_referrer['invited_count'] == 1
        assert new_referrer['paid_referrals_count'] == 1
        assert new_referrer['conversion_rate'] == 100.0

        # Список рефералов обязан показывать то же, что сводка: маркер ищется
        # по текущей привязке реферала, а не по историческому referral_id.
        old_list = await get_detailed_referral_list(db, 1)
        new_list = await get_detailed_referral_list(db, 3)

        assert old_list['total_count'] == 0
        assert old_list['referrals'] == []
        assert new_list['total_count'] == 1
        assert new_list['referrals'][0]['id'] == 2
        assert new_list['referrals'][0]['has_first_purchase'] is True


@pytest.mark.asyncio
async def test_detailed_list_marks_first_purchase_per_referral(monkeypatch):
    """В списке рефералов оплата видна по маркеру, а не по флагу пополнения."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _user(3, referred_by_id=1), _user(4)])
        db.add_all(
            [
                _marker(2, 1),
                # Исторический referral_id маркера третьего реферала указывает
                # на прежнего пригласившего, но сам реферал сейчас привязан к
                # user 1 — список обязан показать покупку, как и сводка.
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
        assert by_id[3]['has_first_purchase'] is True
        assert by_id[2]['days_earned'] == 10
        # Старое поле остаётся: UI переводится на маркеры отдельной задачей.
        assert by_id[2]['has_made_first_topup'] is False


@pytest.mark.asyncio
async def test_summary_excludes_registration_anchor_from_recent_and_types(monkeypatch):
    """Якорь регистрации — не награда: он не должен вытеснять recent и попадать в типы.

    Фильтр стоит в SQL до LIMIT: иначе пять свежих якорей занимают всю пятёрку,
    и реальная награда исчезает из ленты, хотя она и есть единственная выплата.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        # Уникальный индекс 0135 разрешает только один якорь на пару, поэтому
        # пять свежих якорей — это пять разных приглашённых.
        db.add_all([_user(1)] + [_user(uid, referred_by_id=1) for uid in range(2, 7)])
        db.add_all(
            [
                ReferralEarning(
                    user_id=1,
                    referral_id=2 + i,
                    amount_kopeks=0,
                    reason=REASON_REGISTRATION_DAYS_ONLY,
                    reward_type='days',
                    days_granted=0,
                    created_at=now - timedelta(minutes=i),
                )
                for i in range(5)
            ]
            + [
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=0,
                    reason=REASON_FIRST_PURCHASE,
                    reward_type='days',
                    days_granted=10,
                    created_at=now - timedelta(hours=5),
                )
            ]
        )
        await db.commit()

        summary = await get_user_referral_summary(db, 1)

        assert [row['reason'] for row in summary['recent_earnings']] == [REASON_FIRST_PURCHASE]
        assert REASON_REGISTRATION_DAYS_ONLY not in summary['earnings_by_type']
        assert summary['earnings_by_type'][REASON_FIRST_PURCHASE]['count'] == 1
