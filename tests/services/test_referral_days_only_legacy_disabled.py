from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_contest_service import referral_contest_service
from app.services.referral_reward_service import award_referral_rewards
from app.services.referral_service import (
    attach_referrer_if_missing,
    process_referral_registration,
    process_referral_topup,
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


def _paid_subscription(sub_id: int, user_id: int) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=30),
        traffic_limit_gb=100,
        device_limit=1,
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
async def test_legacy_attach_writes_no_first_purchase_marker(monkeypatch):
    """В legacy поздняя привязка не пишет маркер новой схемы.

    Маркер принадлежит только режиму «только дни». В legacy/levels он не нужен,
    а после переключения схемы исказил бы счётчик оплативших рефералов.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2), _paid_subscription(10, 2)])
        await db.commit()
        user = await db.get(User, 2)

        with (
            patch(
                'app.services.referral_service.get_pending_referral',
                AsyncMock(return_value={'referrer_id': 1}),
            ),
            patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
            patch('app.services.referral_service.process_referral_registration', AsyncMock()),
        ):
            attached = await attach_referrer_if_missing(db, user, source='unit_test')

        assert attached == 1
        assert (await db.execute(select(ReferralEarning))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('setting_name', 'setting_value'),
    [
        ('TRIAL_DISABLED_FOR', 'all'),
        ('REFERRAL_PROGRAM_ENABLED', False),
        ('TRIAL_DURATION_DAYS', 0),
    ],
)
async def test_welcome_omits_trial_promise_when_trial_is_unavailable(monkeypatch, setting_name, setting_value):
    """Приветствие не обещает бонус к триалу, которого пользователь не получит.

    Строка про первую покупку в письме пригласившему не меняется.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    monkeypatch.setattr(settings, setting_name, setting_value)

    contest_mock = AsyncMock()
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', contest_mock)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True

        assert bot.send_message.await_count == 2
        referee_call, referrer_call = bot.send_message.await_args_list

        welcome = referee_call.args[1]
        assert 'Добро пожаловать' in welcome
        assert 'пробному периоду' not in welcome
        assert '+3 дня' not in welcome

        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert '+7 дней' in inviter


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
