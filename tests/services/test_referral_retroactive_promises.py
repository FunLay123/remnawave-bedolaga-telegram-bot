"""Поздняя привязка реферала: не обещать бонусы, которые уже невозможны.

При ``source='bot_start_retroactive'`` (и любой другой поздней привязке)
человек мог уже потратить триал или купить подписку до появления пригласившего.
Обещание «+N дней к пробному периоду» тогда невыполнимо, а обещание бонуса за
первую покупку — тем более: ``ensure_marker_for_already_paid_referral`` сразу
после отправки сообщений запишет маркер, и награда не придёт.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Transaction, TransactionType, User
from app.services.referral_contest_service import referral_contest_service
from app.services.referral_purchase_service import (
    has_first_purchase_marker,
    has_paid_subscription_before_attach,
)
from app.services.referral_service import attach_referrer_if_missing, process_referral_registration
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None, *, has_had_paid_subscription: bool = False) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
        has_had_paid_subscription=has_had_paid_subscription,
    )


def _paid_transaction(
    transaction_id: int,
    user_id: int,
    amount_kopeks: int = -10000,
    *,
    description: str = 'Оплата подписки',
) -> Transaction:
    return Transaction(
        id=transaction_id,
        user_id=user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT.value,
        amount_kopeks=amount_kopeks,
        description=description,
        is_completed=True,
    )


def _daily_transaction(transaction_id: int, user_id: int) -> Transaction:
    return _paid_transaction(
        transaction_id,
        user_id,
        description='Суточная оплата тарифа «Базовый»',
    )


def _use_test_session_factory(monkeypatch, db) -> None:
    """Marker-helper в отдельной сессии должен смотреть в ту же in-memory SQLite."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(
        'app.database.database.AsyncSessionLocal',
        async_sessionmaker(db.bind, expire_on_commit=False),
    )


def _subscription(sub_id: int, user_id: int, *, is_trial: bool) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=is_trial,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=30),
        traffic_limit_gb=100,
        device_limit=1,
    )


def _enable_days_only_notifications(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', AsyncMock())


@pytest.mark.asyncio
async def test_trial_used_referee_gets_no_trial_bonus_promise(monkeypatch):
    _enable_days_only_notifications(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _subscription(10, 2, is_trial=True)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True
        assert await has_paid_subscription_before_attach(db, 2) is False

        referee_call, referrer_call = bot.send_message.await_args_list
        welcome = referee_call.args[1]
        assert 'Добро пожаловать' in welcome
        assert 'пробному периоду' not in welcome
        assert '+3 дня' not in welcome

        # Триал — ещё не покупка: обещание за первую оплату остаётся в силе.
        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert '+7 дней' in inviter


@pytest.mark.asyncio
async def test_already_paid_referee_gets_no_first_purchase_promise(monkeypatch):
    _enable_days_only_notifications(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _subscription(10, 2, is_trial=False),
                _paid_transaction(100, 2),
            ]
        )
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        assert await process_referral_registration(db, 2, 1, bot) is True
        assert await has_paid_subscription_before_attach(db, 2) is True
        # Маркер пишется отдельной сессией: без подмены фабрики тест ушёл бы
        # в реальную БД, а утверждение проверяло бы только текст сообщений.
        assert await has_first_purchase_marker(db, 2) is True

        referee_call, referrer_call = bot.send_message.await_args_list
        welcome = referee_call.args[1]
        assert 'Добро пожаловать' in welcome
        assert 'пробному периоду' not in welcome

        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert 'Когда он впервые купит' not in inviter
        assert '+7 дней' not in inviter


@pytest.mark.asyncio
async def test_daily_only_payer_keeps_first_purchase_promise(monkeypatch):
    """Daily-списание до привязки не «оплата»: обещание и право на бонус живы."""
    _enable_days_only_notifications(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _subscription(10, 2, is_trial=False),
                _daily_transaction(100, 2),
            ]
        )
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        assert await process_referral_registration(db, 2, 1, bot) is True
        assert await has_paid_subscription_before_attach(db, 2) is False
        # Daily-платёж не покупка: маркер не появляется, право на бонус живо.
        assert await has_first_purchase_marker(db, 2) is False

        _, referrer_call = bot.send_message.await_args_list
        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert 'Когда он впервые купит' in inviter
        assert '+7 дней' in inviter
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_retroactive_attach_of_paid_user_promises_nothing_and_writes_marker(monkeypatch):
    """Полный путь ``bot_start_retroactive``: сообщения и маркер согласованы."""
    _enable_days_only_notifications(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2),
                _subscription(10, 2, is_trial=False),
                _paid_transaction(100, 2),
            ]
        )
        await db.commit()
        _use_test_session_factory(monkeypatch, db)
        user = await db.get(User, 2)

        with (
            patch(
                'app.services.referral_service.get_pending_referral',
                AsyncMock(return_value={'referrer_id': 1}),
            ),
            patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        ):
            attached = await attach_referrer_if_missing(db, user, bot=bot, source='bot_start_retroactive')

        assert attached == 1
        _, referrer_call = bot.send_message.await_args_list
        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert 'Когда он впервые купит' not in inviter
        # Маркер записан сразу после сообщений — бонус действительно не придёт.
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_new_user_still_gets_both_promises(monkeypatch):
    """Обычная (не поздняя) привязка не меняется: обещания остаются."""
    _enable_days_only_notifications(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True

        referee_call, referrer_call = bot.send_message.await_args_list
        assert '+3 дня' in referee_call.args[1]
        assert '+7 дней' in referrer_call.args[1]


@pytest.mark.asyncio
async def test_gift_subscription_keeps_first_purchase_promise(monkeypatch):
    """Нетриальная подписка без флага и транзакции — подарок, а не оплата.

    Обещание бонуса за первую покупку должно остаться: настоящая оплата ещё
    впереди, и маркер на подарок её бы «съел».
    """
    _enable_days_only_notifications(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _subscription(10, 2, is_trial=False)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True
        assert await has_paid_subscription_before_attach(db, 2) is False

        _, referrer_call = bot.send_message.await_args_list
        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert '+7 дней' in inviter


@pytest.mark.asyncio
async def test_zero_first_purchase_bonus_sends_no_purchase_promise(monkeypatch):
    """Нулевой бонус — не награда: обещать «+0 дней» нельзя."""
    _enable_days_only_notifications(monkeypatch)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 0)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True

        referee_call, referrer_call = bot.send_message.await_args_list
        assert '+3 дня' in referee_call.args[1]
        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert 'Когда он впервые купит' not in inviter
        assert '+0' not in inviter


@pytest.mark.asyncio
async def test_disabled_program_sends_no_purchase_promise(monkeypatch):
    """Выключенная программа ничего не платит — обещание невыполнимо."""
    _enable_days_only_notifications(monkeypatch)
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True

        referee_call, referrer_call = bot.send_message.await_args_list
        assert 'пробному периоду' not in referee_call.args[1]
        inviter = referrer_call.args[1]
        assert 'Новый реферал' in inviter
        assert 'Когда он впервые купит' not in inviter
        assert '+7 дней' not in inviter
