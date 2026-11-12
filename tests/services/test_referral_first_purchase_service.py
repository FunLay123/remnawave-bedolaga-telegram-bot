from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.services.referral_purchase_service as purchase_module
from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, Tariff, User
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


def _sub(
    sub_id: int,
    user_id: int,
    *,
    trial: bool = False,
    days: int = 30,
    status: str = SubscriptionStatus.ACTIVE.value,
    tariff_id: int | None = None,
) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        # shortId уникален; без него две подписки получили бы одинаковый '' и
        # упёрлись бы в уникальный индекс раньше, чем дойдёт до тестируемой логики.
        remnawave_short_id=f'sub{sub_id}',
        status=status,
        is_trial=trial,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=days),
        tariff_id=tariff_id,
        traffic_limit_gb=100,
        device_limit=1,
    )


def _daily_tariff(tariff_id: int) -> Tariff:
    return Tariff(id=tariff_id, name=f'Daily {tariff_id}', is_active=True, is_daily=True)


def _expired_real(sub_id: int, user_id: int, *, trial: bool = False) -> Subscription:
    """Настоящая истёкшая подписка: ненулевой срок в прошлом, не заготовка."""
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=trial,
        start_date=now - timedelta(days=60),
        end_date=now - timedelta(days=30),
        traffic_limit_gb=100,
        device_limit=1,
    )


def _pin_common_first_purchase_days(monkeypatch, *, days: int = 10) -> None:
    """Зафиксировать общий бонус, стороны и порог длительности.

    Значения по умолчанию читаются из окружения разработчика/деплоя: без
    явных пинов локальный `.env` мог бы подменить ожидаемые +10 дней или
    порог, и тест проверял бы не то, что задумано.
    """
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', days)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)


def _expired_placeholder(sub_id: int, user_id: int) -> Subscription:
    """Брошенная заготовка СБП: EXPIRED с нулевым сроком."""
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        start_date=now,
        end_date=now,
        traffic_limit_gb=100,
        device_limit=1,
    )


@pytest.mark.asyncio
async def test_first_30_day_purchase_rewards_both_sides(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    _pin_common_first_purchase_days(monkeypatch)
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
    # Порог 30 дней: 14-дневная покупка обязана остаться ниже него.
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
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
    _pin_common_first_purchase_days(monkeypatch)
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
async def test_expired_zero_duration_placeholder_is_not_a_referrer_target(monkeypatch):
    """Брошенная заготовка СБП не должна оживать от награды.

    У заготовки ``start_date == end_date``, то есть подписки фактически не было.
    ``add_reward_days`` поднял бы её в ACTIVE и выдал пригласившему 10 живых
    дней, хотя он ни разу не платил. Приглашённый при этом награду получает.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    _pin_common_first_purchase_days(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _expired_placeholder(11, 1)])
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
        reasons = {row.reason for row in rows}
        assert REASON_FIRST_PURCHASE_REFEREE in reasons
        assert REASON_FIRST_PURCHASE not in reasons

        placeholder = await db.get(Subscription, 11)
        assert placeholder.status == SubscriptionStatus.EXPIRED.value
        assert placeholder.end_date == placeholder.start_date


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
    _pin_common_first_purchase_days(monkeypatch)
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


@pytest.mark.asyncio
async def test_daily_tariff_is_not_a_referrer_target(monkeypatch):
    """Суточная подписка не принимает награду: списание продолжалось бы.

    ``add_reward_days`` продлил бы ``end_date``, но ежедневная плата всё равно
    списывалась бы каждый день, и обещанные +10 дней пригласивший оплатил бы
    из своего баланса. Поэтому суточный тариф исключается из выбора цели.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_daily_tariff(7))
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1, days=5, tariff_id=7)])
        await db.commit()
        daily_end_before = (await db.get(Subscription, 11)).end_date

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

        daily_sub = await db.get(Subscription, 11)
        assert daily_sub.end_date == daily_end_before
        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert REASON_FIRST_PURCHASE not in {row.reason for row in rows}
        assert REASON_FIRST_PURCHASE_REFEREE in {row.reason for row in rows}


@pytest.mark.asyncio
async def test_live_trial_beats_expired_paid_referrer_subscription(monkeypatch):
    """Живой триал должен опережать истёкшую платную подписку.

    Старая сортировка ``bool(is_trial), -end_date`` ставила истёкшую платную
    впереди живого триала только потому, что у той ``is_trial=False``.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _sub(10, 2),
                _expired_real(11, 1),
                _sub(12, 1, trial=True, days=5, status=SubscriptionStatus.TRIAL.value),
            ]
        )
        await db.commit()
        trial_end_before = (await db.get(Subscription, 12)).end_date

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'rewarded'
        assert result.referrer_days == 10
        assert result.referrer_subscription_id == 12
        trial_sub = await db.get(Subscription, 12)
        assert (trial_sub.end_date - trial_end_before).days == 10
        # Истёкшая подписка не воскрешена: дни ушли живому триалу.
        expired_sub = await db.get(Subscription, 11)
        assert expired_sub.status == SubscriptionStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_live_paid_beats_live_trial_even_with_earlier_end(monkeypatch):
    """Живая платная впереди живого триала независимо от срока."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _sub(10, 2),
                _sub(11, 1, days=3),
                _sub(12, 1, trial=True, days=60, status=SubscriptionStatus.TRIAL.value),
            ]
        )
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
        assert result.referrer_days == 10
        assert result.referrer_subscription_id == 11


@pytest.mark.asyncio
async def test_zero_bonus_still_consumes_the_first_purchase(monkeypatch):
    """При нулевом бонусе маркер пишется, а повторная покупка уже не «первая».

    Иначе ``add_reward_days`` вернул бы ``None``, код откатил бы маркер, и
    каждая следующая покупка снова считалась бы первой — при включении бонуса
    задним числом награда досталась бы уже оплаченным покупкам.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()
        referee_end_before = (await db.get(Subscription, 10)).end_date

        first = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
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
        referee_sub = await db.get(Subscription, 10)
        assert referee_sub.end_date == referee_end_before


@pytest.mark.asyncio
async def test_daily_referee_subscription_is_not_counted(monkeypatch):
    """Суточная покупка — не «первая покупка»: ни маркера, ни дней.

    Daily-списание переиспользует ``SUBSCRIPTION_PAYMENT`` и происходит каждый
    день, поэтому первой покупкой считаться не может: иначе бонус сгорал бы на
    первом дне суточного тарифа. Маркер тоже не пишется — настоящая первая
    не-daily покупка впереди.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 10)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_daily_tariff(7))
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2, tariff_id=7), _sub(11, 1)])
        await db.commit()
        daily_end_before = (await db.get(Subscription, 10)).end_date

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=1,
            paid_amount_kopeks=1_000,
            source='test',
        )

        assert result.status == 'daily_not_counted'
        assert result.referee_days == 0
        assert result.referrer_days == 0
        assert await has_first_purchase_marker(db, 2) is False

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        assert rows == []

        daily_sub = await db.get(Subscription, 10)
        assert daily_sub.end_date == daily_end_before


@pytest.mark.asyncio
async def test_daily_not_counted_emits_structured_debug_event(monkeypatch):
    """Daily-списания частые: пропуск не должен спамить info — только debug.

    Событие стабильное и структурированное (``referee_id``, ``referrer_id``,
    ``source``), чтобы по логам можно было посчитать, сколько покупок не
    дошло до бонуса из-за суточного тарифа.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    debug_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        purchase_module,
        'logger',
        SimpleNamespace(debug=lambda message, **kwargs: debug_calls.append((message, kwargs))),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_daily_tariff(7))
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2, tariff_id=7), _sub(11, 1)])
        await db.commit()

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=1,
            paid_amount_kopeks=1_000,
            source='daily_scheduler',
        )

    assert result.status == 'daily_not_counted'
    assert debug_calls == [
        (
            'Referral first purchase not counted: daily tariff',
            {'referee_id': 2, 'referrer_id': 1, 'source': 'daily_scheduler'},
        )
    ]


@pytest.mark.asyncio
async def test_daily_purchase_returns_before_user_and_subscription_locks(monkeypatch):
    """Ежедневное списание не берёт FOR UPDATE до проверки тарифа.

    Daily-покупка повторяется каждый день; блокировать users/подписку под
    запрос ради немедленного ``daily_not_counted`` незачем.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_daily_tariff(7))
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2, tariff_id=7), _sub(11, 1)])
        await db.commit()

        async def _forbidden(*_args, **_kwargs):
            raise AssertionError('daily-покупка не должна брать блокировку')

        monkeypatch.setattr(purchase_module, '_lock_user', _forbidden)
        monkeypatch.setattr(purchase_module, '_lock_subscription', _forbidden)

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=1,
            paid_amount_kopeks=1_000,
            source='daily_scheduler',
        )

    assert result.status == 'daily_not_counted'
    assert result.referee_id == 2
    assert result.referrer_id == 1


@pytest.mark.asyncio
async def test_non_daily_purchase_still_locks_users(monkeypatch):
    """Ранний выход не должен обходить блокировки обычной покупки."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    _pin_common_first_purchase_days(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()

        locked: list[int] = []
        real_lock = purchase_module._lock_user

        async def spy_lock(db_, user_id):
            locked.append(user_id)
            return await real_lock(db_, user_id)

        monkeypatch.setattr(purchase_module, '_lock_user', spy_lock)

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=100_000,
            source='purchase',
        )

    assert result.status in {'rewarded', 'registered'}
    assert 1 in locked and 2 in locked


@pytest.mark.asyncio
async def test_daily_then_non_daily_purchase_rewards_once(monkeypatch):
    """Daily-покупка не сжигает бонус: первая нормальная покупка его получает."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_daily_tariff(7))
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _sub(10, 2, tariff_id=7),
                _sub(11, 1),
                _sub(12, 2),
            ]
        )
        await db.commit()

        daily = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=1,
            paid_amount_kopeks=1_000,
            source='daily_scheduler',
        )
        assert daily.status == 'daily_not_counted'
        assert await has_first_purchase_marker(db, 2) is False

        paid = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=12,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert paid.status == 'rewarded'
        assert paid.referee_days == 10
        assert paid.referrer_days == 10
        assert await has_first_purchase_marker(db, 2) is True

        reasons = {row.reason for row in (await db.execute(select(ReferralEarning))).scalars().all()}
        assert reasons == {
            REASON_FIRST_PURCHASE_REGISTERED,
            REASON_FIRST_PURCHASE_REFEREE,
            REASON_FIRST_PURCHASE,
        }


@pytest.mark.asyncio
async def test_separate_days_are_granted_per_side(monkeypatch):
    """Дни сторон настраиваются раздельно и попадают в ledger как есть."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 7)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 3)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()
        referee_end_before = (await db.get(Subscription, 10)).end_date
        referrer_end_before = (await db.get(Subscription, 11)).end_date

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'rewarded'
        assert result.referee_days == 7
        assert result.referrer_days == 3

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        by_reason = {row.reason: row for row in rows}
        assert by_reason[REASON_FIRST_PURCHASE_REGISTERED].days_granted == 0
        assert by_reason[REASON_FIRST_PURCHASE_REFEREE].days_granted == 7
        assert by_reason[REASON_FIRST_PURCHASE].days_granted == 3

        referee_sub = await db.get(Subscription, 10)
        referrer_sub = await db.get(Subscription, 11)
        assert (referee_sub.end_date - referee_end_before).days == 7
        assert (referrer_sub.end_date - referrer_end_before).days == 3


@pytest.mark.asyncio
async def test_unset_side_days_fall_back_to_common_bonus(monkeypatch):
    """Оба новых ключа пусты — обе стороны получают общий бонус."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', None)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
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


@pytest.mark.asyncio
async def test_zero_referee_days_still_rewards_referrer_and_marks_purchase(monkeypatch):
    """Ноль у приглашённого не отменяет награду пригласившему и маркер."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFEREE_DAYS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_REFERRER_DAYS', 5)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _sub(10, 2), _sub(11, 1)])
        await db.commit()
        referee_end_before = (await db.get(Subscription, 10)).end_date

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'rewarded'
        assert result.referee_days == 0
        assert result.referrer_days == 5
        assert await has_first_purchase_marker(db, 2) is True

        rows = (await db.execute(select(ReferralEarning))).scalars().all()
        by_reason = {row.reason: row for row in rows}
        assert REASON_FIRST_PURCHASE_REFEREE not in by_reason
        assert by_reason[REASON_FIRST_PURCHASE_REGISTERED].days_granted == 0
        assert by_reason[REASON_FIRST_PURCHASE].days_granted == 5

        referee_sub = await db.get(Subscription, 10)
        assert referee_sub.end_date == referee_end_before


@pytest.mark.asyncio
async def test_expired_zero_duration_referee_placeholder_is_skipped_without_marker(monkeypatch):
    """Заготовка СБП у приглашённого — не покупка и не цель награды."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _expired_placeholder(10, 2), _sub(11, 1)])
        await db.commit()

        result = await register_first_purchase(
            db,
            referee_id=2,
            subscription_id=10,
            duration_days=30,
            paid_amount_kopeks=10_000,
            source='test',
        )

        assert result.status == 'skipped'
        assert await has_first_purchase_marker(db, 2) is False
        assert (await db.execute(select(ReferralEarning))).scalars().all() == []


@pytest.mark.asyncio
async def test_lock_referrer_target_selects_in_one_query(monkeypatch):
    """Выбор цели не должен стоить SELECT на каждую подписку пригласившего."""
    from sqlalchemy import event

    from app.services.referral_purchase_service import _lock_referrer_target

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _sub(11, 1),
                _sub(12, 1, days=3),
                _sub(13, 1, trial=True, days=60, status=SubscriptionStatus.TRIAL.value),
            ]
        )
        await db.commit()

        subscription_selects = []

        def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
            if 'from subscriptions' in statement.lower():
                subscription_selects.append(statement)

        event.listen(db.bind.sync_engine, 'before_cursor_execute', before_cursor_execute)
        try:
            chosen = await _lock_referrer_target(db, 1)
        finally:
            event.remove(db.bind.sync_engine, 'before_cursor_execute', before_cursor_execute)

        assert len(subscription_selects) == 1, subscription_selects
        assert chosen is not None
        assert chosen.id == 11
