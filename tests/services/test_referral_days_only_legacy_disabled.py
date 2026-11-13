from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_contest_service import referral_contest_service
from app.services.referral_reward_service import REASON_REGISTRATION_DAYS_ONLY, award_referral_rewards
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
async def test_registration_writes_single_idempotency_anchor_row(monkeypatch):
    """В days_only регистрация пишет ровно одну нулевую строку-якорь.

    Это не награда, а отметка «регистрация уже учтена»: сумма и дни нулевые,
    баланс и флаги участников не меняются.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1) is True
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert len(rows) == 1
        anchor = rows[0]
        assert anchor.user_id == 1
        assert anchor.referral_id == 2
        assert anchor.reason == REASON_REGISTRATION_DAYS_ONLY
        assert anchor.amount_kopeks == 0
        assert anchor.days_granted == 0
        assert anchor.reward_type == 'days'
        assert anchor.level == 1

        referee = await db.get(User, 2)
        referrer = await db.get(User, 1)
        assert referee.balance_kopeks == 0
        assert referrer.balance_kopeks == 0
        assert referee.has_made_first_topup is False
        assert referee.has_had_paid_subscription is False


@pytest.mark.asyncio
async def test_registration_sends_days_only_notices_and_records_contest(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 5)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
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
        assert len(rows) == 1
        assert rows[0].reason == REASON_REGISTRATION_DAYS_ONLY
        assert rows[0].amount_kopeks == 0
        assert rows[0].days_granted == 0
        referee = await db.get(User, 2)
        referrer = await db.get(User, 1)
        assert referee.balance_kopeks == 0
        assert referrer.balance_kopeks == 0
        assert referee.has_made_first_topup is False
        assert referee.has_had_paid_subscription is False


@pytest.mark.asyncio
async def test_registered_notice_has_main_menu_but_welcome_does_not(monkeypatch):
    """Пригласивший получает переход в меню; приветствие новичка — без клавиатуры.

    Сразу после приветствия пользователь видит либо экран регистрации со своей
    клавиатурой, либо главное меню, поэтому вешать кнопку на REFERRAL_WELCOME
    нельзя — она продублировала бы навигацию.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 5)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)

    contest_mock = AsyncMock()
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', contest_mock)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True

    welcome_call, registered_call = bot.send_message.await_args_list
    assert 'Добро пожаловать' in welcome_call.args[1]
    assert 'reply_markup' not in welcome_call.kwargs

    keyboard = registered_call.kwargs['reply_markup']
    assert len(keyboard.inline_keyboard) == 1
    assert len(keyboard.inline_keyboard[0]) == 1
    button = keyboard.inline_keyboard[0][0]
    assert button.text == 'Главное меню'
    assert button.callback_data == 'back_to_menu'
    assert button.icon_custom_emoji_id == '5314310000531766389'
    assert button.style == 'primary'


@pytest.mark.asyncio
async def test_repeated_registration_does_not_duplicate_notices_or_contest(monkeypatch):
    """Повторная обработка (DELETED-restore) не шлёт сообщения и не задваивает конкурс."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)

    contest_mock = AsyncMock()
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', contest_mock)
    bot = SimpleNamespace(send_message=AsyncMock())

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        # Повторная обработка ретраит маркер в отдельной сессии: направляем её
        # в ту же in-memory БД, чтобы тест не ходил в реальную базу.
        from sqlalchemy.ext.asyncio import async_sessionmaker

        monkeypatch.setattr(
            'app.database.database.AsyncSessionLocal',
            async_sessionmaker(db.bind, expire_on_commit=False),
        )

        assert await process_referral_registration(db, 2, 1, bot) is True
        assert await process_referral_registration(db, 2, 1, bot) is True

        contest_mock.assert_awaited_once()
        assert contest_mock.await_args.args == (db, 2)
        assert bot.send_message.await_count == 2

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert len(rows) == 1
        assert rows[0].reason == REASON_REGISTRATION_DAYS_ONLY
        assert rows[0].amount_kopeks == 0
        assert rows[0].days_granted == 0


@pytest.mark.asyncio
async def test_registration_swallows_concurrent_anchor_insert(monkeypatch):
    """Конкурентная вставка якоря: IntegrityError не ломает регистрацию."""
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)

    contest_mock = AsyncMock()
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', contest_mock)
    bot = SimpleNamespace(send_message=AsyncMock())

    async def _raise_integrity(*args, **kwargs):
        raise IntegrityError('INSERT INTO referral_earnings', {}, Exception('unique'))

    monkeypatch.setattr('app.services.referral_service.create_referral_earning', _raise_integrity)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True
        contest_mock.assert_not_awaited()
        assert bot.send_message.await_count == 0
        assert (await db.execute(select(ReferralEarning))).scalars().all() == []


@pytest.mark.asyncio
async def test_registration_anchor_race_refreshes_entities_after_rollback(monkeypatch):
    """После rollback в IntegrityError-ветке участники перечитываются.

    Caller продолжает работать с теми же ORM-объектами (``attach_referrer_if_missing``
    читает ``user.telegram_id``/``user.id`` после возврата). Rollback их
    экспайрит, и без refresh обращение к полю в async-сессии даёт MissingGreenlet.
    """
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', False)

    async def _raise_integrity(*args, **kwargs):
        raise IntegrityError('INSERT INTO referral_earnings', {}, Exception('unique'))

    monkeypatch.setattr('app.services.referral_service.create_referral_earning', _raise_integrity)

    async with memory_session(monkeypatch, TABLES) as db:
        new_user = _user(2, referred_by_id=1)
        referrer = _user(1)
        db.add_all([referrer, new_user])
        await db.commit()

        refreshed: list[int] = []
        real_refresh = db.refresh

        async def _spy_refresh(obj, *args, **kwargs):
            # id() до refresh: обращение к атрибутам протухшего объекта само
            # упало бы MissingGreenlet и попало в best-effort except кода.
            result = await real_refresh(obj, *args, **kwargs)
            refreshed.append(obj.id)
            return result

        monkeypatch.setattr(db, 'refresh', _spy_refresh)

        assert await process_referral_registration(db, 2, 1, bot=None) is True
        assert sorted(refreshed) == [1, 2]
        # Без refresh эти обращения упали бы MissingGreenlet после rollback.
        assert new_user.language == 'ru'
        assert referrer.telegram_id == 1001
        assert (await db.execute(select(ReferralEarning))).scalars().all() == []


@pytest.mark.asyncio
async def test_registration_continues_after_anchor_write_error(monkeypatch):
    """Временная ошибка записи якоря не лишает регистрацию уведомлений и конкурса."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)

    contest_mock = AsyncMock()
    monkeypatch.setattr(referral_contest_service, 'on_referral_registration', contest_mock)
    bot = SimpleNamespace(send_message=AsyncMock())

    async def _raise_runtime(*args, **kwargs):
        raise RuntimeError('database is temporarily unavailable')

    monkeypatch.setattr('app.services.referral_service.create_referral_earning', _raise_runtime)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1)])
        await db.commit()

        assert await process_referral_registration(db, 2, 1, bot) is True
        contest_mock.assert_awaited_once()
        assert bot.send_message.await_count == 2
        assert (await db.execute(select(ReferralEarning))).scalars().all() == []


def test_days_only_registration_anchor_index_is_declared_in_metadata():
    """Fresh-install parity: индекс якоря объявлен в модели, а не только в миграции."""
    indexes = {index.name: index for index in ReferralEarning.__table__.indexes}
    index = indexes['uq_referral_earnings_days_only_registration']
    assert index.unique is True
    assert [column.name for column in index.columns] == ['user_id', 'referral_id']
    for dialect in ('postgresql', 'sqlite'):
        where_clause = str(index.dialect_kwargs[f'{dialect}_where'])
        assert "reason = 'referral_registration_days_only'" in where_clause


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

    Строка про первую покупку в письме пригласившему не меняется, кроме случая
    выключенной программы: тогда невыполнимо и это обещание.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
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
        if setting_name == 'REFERRAL_PROGRAM_ENABLED':
            assert 'Когда он впервые купит' not in inviter
            assert '+7 дней' not in inviter
        else:
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
