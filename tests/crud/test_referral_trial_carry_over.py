"""Перенос реферального остатка триала (days_only).

Проверяем базовую дату триала (срок без реферального бонуса приглашённого) и
единый расчёт переносимого остатка. Полный срок подписки (``end_date``) остаётся
суммой базового триала и реферальных дней; переносится только реферальный хвост.
"""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database.crud.subscription import (
    add_reward_days,
    create_paid_subscription,
    create_trial_subscription,
    extend_subscription,
    get_trial_carry_over_delta,
    replace_subscription,
)
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite, memory_session


TABLES = list(Base.metadata.sorted_tables)


@contextlib.asynccontextmanager
async def _file_sessions(monkeypatch, tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Файловая SQLite вместо ``:memory:``: две сессии видят один и тот же коммит."""
    ensure_real_aiosqlite(monkeypatch)
    db_path = (tmp_path / 'carry-over.db').as_posix()
    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}', connect_args={'timeout': 15})
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield maker
    finally:
        await engine.dispose()


def _user(uid: int, *, referred_by: int | None = None) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by,
    )


def _sub(
    sub_id: int,
    user_id: int,
    *,
    is_trial: bool,
    end_in_days: int,
    base_in_days: int | None,
    status: str = SubscriptionStatus.TRIAL.value,
    start: datetime | None = None,
) -> Subscription:
    start = start or datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=status,
        is_trial=is_trial,
        start_date=start,
        end_date=start + timedelta(days=end_in_days),
        base_trial_end_date=None if base_in_days is None else start + timedelta(days=base_in_days),
        traffic_limit_gb=100,
        device_limit=1,
    )


def _enable_days_only(monkeypatch, *, bonus_days: int = 20, program: bool = True) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', program)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', bonus_days)


@pytest.mark.asyncio
async def test_referral_trial_carries_only_referral_tail(monkeypatch):
    """База 3 дня + бонус 20: создаётся срок 23 дня, переносится только 20."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        await db.commit()

        created = await create_trial_subscription(
            db,
            2,
            duration_days=23,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )

        assert created.base_trial_end_date == created.start_date + timedelta(days=3)
        assert get_trial_carry_over_delta(created, created.start_date) == timedelta(days=20)
        assert get_trial_carry_over_delta(created, created.base_trial_end_date + timedelta(days=5)) == timedelta(
            days=15
        )
        assert get_trial_carry_over_delta(created, created.end_date) == timedelta()


@pytest.mark.asyncio
async def test_referred_trial_without_bonus_has_base_equal_to_end(monkeypatch):
    """Base-only вызову реферальный бонус не вычитается: базовая дата == end_date."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        await db.commit()

        # duration_days_include_referral_bonus по умолчанию False.
        created = await create_trial_subscription(
            db,
            2,
            duration_days=3,
            connected_squads=['squad-x'],
        )

        assert created.base_trial_end_date == created.end_date


@pytest.mark.asyncio
async def test_pending_reactivation_recomputes_base_from_new_start(monkeypatch):
    """Повторная активация PENDING пересчитывает базовую дату от нового start_date."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        old_start = datetime.now(UTC) - timedelta(days=10)
        db.add(
            _sub(
                10,
                2,
                is_trial=True,
                end_in_days=13,
                base_in_days=3,
                status=SubscriptionStatus.PENDING.value,
                start=old_start,
            )
        )
        await db.commit()

        created = await create_trial_subscription(
            db,
            2,
            duration_days=23,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )

        assert created.id == 10
        assert created.status == SubscriptionStatus.ACTIVE.value
        assert created.start_date > old_start
        assert created.base_trial_end_date == created.start_date + timedelta(days=3)
        assert get_trial_carry_over_delta(created, created.start_date) == timedelta(days=20)


@pytest.mark.asyncio
async def test_base_only_and_referral_aware_modes_differ(monkeypatch):
    """False (по умолчанию) не вычитает бонус, True — вычитает фактически применимый."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        db.add(_user(3, referred_by=1))
        await db.commit()

        base_only = await create_trial_subscription(
            db,
            2,
            duration_days=23,
            connected_squads=['squad-x'],
        )
        referral_aware = await create_trial_subscription(
            db,
            3,
            duration_days=23,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )

        # Base-only: 23 дня считаются базой, базовая дата совпадает с концом.
        assert base_only.base_trial_end_date == base_only.end_date
        # Referral-aware: бонус 20 вычтен, база — 3 дня.
        assert referral_aware.base_trial_end_date == referral_aware.start_date + timedelta(days=3)


@pytest.mark.asyncio
async def test_referral_bonus_not_applied_for_other_scheme(monkeypatch):
    """Вне days_only фактический бонус равен нулю — база не уменьшается."""
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        await db.commit()

        created = await create_trial_subscription(
            db,
            2,
            duration_days=23,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )

        assert created.base_trial_end_date == created.end_date


@pytest.mark.asyncio
async def test_add_reward_days_keeps_base_date_and_trial_flag(monkeypatch):
    """add_reward_days продлевает только общий end_date, база и is_trial не меняются."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        await db.commit()

        created = await create_trial_subscription(
            db,
            2,
            duration_days=23,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )
        base_before = created.base_trial_end_date
        end_before = created.end_date

        got = await add_reward_days(db, created, 10)

        assert got is not None
        assert got.is_trial is True
        assert got.base_trial_end_date == base_before
        assert got.end_date == end_before + timedelta(days=10)


def test_legacy_levels_uses_should_carry_behaviour(monkeypatch):
    """Схема levels с известной базой ведёт себя как прежний should_carry."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    now = datetime.now(UTC)
    sub = _sub(1, 1, is_trial=True, end_in_days=5, base_in_days=3, start=now)

    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    assert get_trial_carry_over_delta(sub, now) == timedelta(days=5)

    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    assert get_trial_carry_over_delta(sub, now) == timedelta()


def test_legacy_missing_base_date_uses_should_carry_behaviour(monkeypatch):
    """Старая строка без базовой даты в days_only ведёт себя как прежний should_carry."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    now = datetime.now(UTC)
    sub = _sub(1, 1, is_trial=True, end_in_days=5, base_in_days=None, start=now)

    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    assert get_trial_carry_over_delta(sub, now) == timedelta(days=5)

    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)
    assert get_trial_carry_over_delta(sub, now) == timedelta()


def test_non_trial_and_missing_end_date_have_no_carry(monkeypatch):
    """Не-триал и отсутствующий end_date не переносят ничего."""
    _enable_days_only(monkeypatch)
    now = datetime.now(UTC)

    paid = _sub(1, 1, is_trial=False, end_in_days=10, base_in_days=3, status=SubscriptionStatus.ACTIVE.value)
    assert get_trial_carry_over_delta(paid, now) == timedelta()

    no_end = _sub(2, 1, is_trial=True, end_in_days=10, base_in_days=3)
    no_end.end_date = None
    assert get_trial_carry_over_delta(no_end, now) == timedelta()


def test_expired_referral_tail_is_not_restored(monkeypatch):
    """Истёкший до покупки реферальный хвост не восстанавливается."""
    _enable_days_only(monkeypatch)
    now = datetime.now(UTC)
    start = now - timedelta(days=30)
    sub = Subscription(
        id=1,
        user_id=1,
        remnawave_short_id='sub1',
        status=SubscriptionStatus.TRIAL.value,
        is_trial=True,
        start_date=start,
        end_date=start + timedelta(days=23),
        base_trial_end_date=start + timedelta(days=3),
        traffic_limit_gb=100,
        device_limit=1,
    )
    assert get_trial_carry_over_delta(sub, now) == timedelta()


# ── Центральная конверсия: покупка того же единственного тарифа ──


def _tariff(tariff_id: int = 1) -> Tariff:
    return Tariff(
        id=tariff_id,
        name='Тариф',
        description='',
        is_active=True,
        traffic_limit_gb=50,
        device_limit=3,
        allowed_squads=['squad-1'],
        period_prices={'30': 10_000},
        display_order=1,
    )


def _purchasable_trial(*, sub_id: int, user_id: int, now: datetime, tariff_id: int = 1) -> Subscription:
    """Триал 3 базовых + 20 реферальных дней, ещё живой на момент покупки."""
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=SubscriptionStatus.TRIAL.value,
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=23),
        base_trial_end_date=now + timedelta(days=3),
        traffic_limit_gb=50,
        device_limit=1,
        tariff_id=tariff_id,
        connected_squads=['squad-1'],
    )


@pytest.mark.asyncio
async def test_same_tariff_purchase_carries_referral_tail_once(monkeypatch):
    """3 базовых + 20 реферальных + 30 купленных = now+50, а не now+53.

    Конверсия того же тарифа обязана стартовать от момента покупки и добавить
    только реферальный хвост. Старая generic-ветка прибавляла купленные дни к
    полному ``end_date`` и второй раз учитывала базовый триал.
    """
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_tariff())
        before = datetime.now(UTC)
        db.add(_purchasable_trial(sub_id=10, user_id=1, now=before))
        await db.commit()

        trial = await db.get(Subscription, 10)
        result = await extend_subscription(db, trial, 30, tariff_id=1, traffic_limit_gb=50)
        after = datetime.now(UTC)

        assert result is trial
        assert result.is_trial is False
        assert result.status == SubscriptionStatus.ACTIVE.value
        assert before + timedelta(days=50) <= result.end_date <= after + timedelta(days=50)
        # Не 53: базовый триал и полный end_date не переносятся повторно.
        assert result.end_date < before + timedelta(days=53)
        assert before <= result.start_date <= after


@pytest.mark.asyncio
async def test_same_tariff_conversion_keeps_legacy_null_base_behaviour(monkeypatch):
    """days_only + NULL база: старые строки по-прежнему продлеваются от ``end_date``.

    Спец-ветка «now + только реферальный хвост» применима только к строкам с
    известной ``base_trial_end_date``. Без базы (и при выключенном переносе)
    generic-ветка того же тарифа оставляет прежнее ``end_date + purchased_days``.
    """
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_tariff())
        now = datetime.now(UTC)
        trial = _purchasable_trial(sub_id=10, user_id=1, now=now)
        trial.base_trial_end_date = None  # старая строка без истории
        db.add(trial)
        await db.commit()

        trial = await db.get(Subscription, 10)
        original_end = trial.end_date
        result = await extend_subscription(db, trial, 30, tariff_id=1, traffic_limit_gb=50)

        assert result.is_trial is False
        assert result.end_date == original_end + timedelta(days=30)


@pytest.mark.asyncio
async def test_same_tariff_conversion_keeps_levels_behaviour(monkeypatch):
    """Схема levels с известной базой также идёт прежней generic-веткой."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_tariff())
        now = datetime.now(UTC)
        db.add(_purchasable_trial(sub_id=10, user_id=1, now=now))
        await db.commit()

        trial = await db.get(Subscription, 10)
        original_end = trial.end_date
        result = await extend_subscription(db, trial, 30, tariff_id=1, traffic_limit_gb=50)

        assert result.is_trial is False
        assert result.end_date == original_end + timedelta(days=30)


@pytest.mark.asyncio
async def test_free_relabel_without_conversion_keeps_old_extension_behaviour(monkeypatch):
    """``convert_trial=False`` — не покупка: триал остаётся и продлевается от end_date."""
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_tariff())
        now = datetime.now(UTC)
        db.add(_purchasable_trial(sub_id=10, user_id=1, now=now))
        await db.commit()

        trial = await db.get(Subscription, 10)
        original_end = trial.end_date
        result = await extend_subscription(
            db,
            trial,
            30,
            tariff_id=1,
            traffic_limit_gb=50,
            convert_trial=False,
        )

        assert result.is_trial is True
        assert result.end_date == original_end + timedelta(days=30)


@pytest.mark.asyncio
async def test_explicit_classic_purchase_carries_referral_tail_once(monkeypatch):
    """Оплата классической подписки без tariff_id тоже переносит только хвост."""
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        before = datetime.now(UTC)
        trial = _purchasable_trial(sub_id=10, user_id=1, now=before)
        trial.tariff_id = None
        db.add(trial)
        await db.commit()

        result = await extend_subscription(db, trial, 30, convert_trial=True)
        after = datetime.now(UTC)

        assert result.is_trial is False
        assert before + timedelta(days=50) <= result.end_date <= after + timedelta(days=50)
        assert result.end_date < before + timedelta(days=53)


@pytest.mark.asyncio
async def test_free_classic_extension_keeps_trial_and_old_end_date_math(monkeypatch):
    """Без явной оплаты отсутствие tariff_id не запускает конвертацию триала."""
    _enable_days_only(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        now = datetime.now(UTC)
        trial = _purchasable_trial(sub_id=10, user_id=1, now=now)
        trial.tariff_id = None
        db.add(trial)
        await db.commit()

        original_end = trial.end_date
        result = await extend_subscription(db, trial, 30)

        assert result.is_trial is True
        assert result.end_date == original_end + timedelta(days=30)


# ── Свежее состояние подписки под блокировкой (две реальные сессии) ──


@pytest.mark.asyncio
async def test_purchase_reads_fresh_end_after_other_session_extension(monkeypatch, tmp_path):
    """3 базовых + 3 реферальных, другая сессия уже добавила +10: покупка даёт 43, не 33."""
    _enable_days_only(monkeypatch, bonus_days=3)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with _file_sessions(monkeypatch, tmp_path) as maker:
        now = datetime.now(UTC)
        async with maker() as seed:
            seed.add(_user(1))
            seed.add(_user(2, referred_by=1))
            seed.add(_tariff())
            seed.add(
                Subscription(
                    id=10,
                    user_id=2,
                    remnawave_short_id='sub10',
                    status=SubscriptionStatus.TRIAL.value,
                    is_trial=True,
                    start_date=now,
                    end_date=now + timedelta(days=6),
                    base_trial_end_date=now + timedelta(days=3),
                    traffic_limit_gb=50,
                    device_limit=1,
                    tariff_id=1,
                    connected_squads=['squad-1'],
                )
            )
            await seed.commit()

        async with maker() as db:
            # Эта сессия читает строку ДО коммита второй сессии — её end_date устарел.
            stale_trial = await db.get(Subscription, 10)
            stale_end = stale_trial.end_date

            async with maker() as other:
                fresh = await other.get(Subscription, 10)
                granted = await add_reward_days(other, fresh, 10)
                assert granted is not None
                await other.commit()

            before = datetime.now(UTC)
            result = await extend_subscription(db, stale_trial, 30, tariff_id=1, traffic_limit_gb=50)
            after = datetime.now(UTC)

        assert result.is_trial is False
        # 30 купленных + 13 реферальных (3 + 10), а не 30 + устаревшие 3.
        assert before + timedelta(days=43) <= result.end_date <= after + timedelta(days=43)
        assert result.end_date > before + timedelta(days=42)
        assert result.end_date > (stale_end or before) + timedelta(days=30)


@pytest.mark.asyncio
async def test_purchase_after_free_nonreferral_extension_carries_nothing(monkeypatch):
    """Новый триал без реферала + 7 бесплатных дней → покупка 30 даёт 30, не 37."""
    _enable_days_only(monkeypatch, bonus_days=0)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_tariff())
        await db.commit()

        trial = await create_trial_subscription(
            db,
            1,
            duration_days=3,
            connected_squads=['squad-1'],
            tariff_id=1,
            duration_days_include_referral_bonus=True,
        )
        assert trial.trial_referral_seconds == 0

        # Бесплатное (нереферальное) продление: 7 дней не становятся реферальным остатком.
        await extend_subscription(db, trial, 7)

        before = datetime.now(UTC)
        result = await extend_subscription(db, trial, 30, tariff_id=1, traffic_limit_gb=50)
        after = datetime.now(UTC)

        assert result.is_trial is False
        assert before + timedelta(days=30) <= result.end_date <= after + timedelta(days=30)
        assert result.end_date < before + timedelta(days=31)


@pytest.mark.asyncio
async def test_purchase_after_free_extension_keeps_only_live_referral_tail(monkeypatch):
    """3 базовых + 3 реферальных + 7 бесплатных → покупка 30 даёт 33, не 40."""
    _enable_days_only(monkeypatch, bonus_days=3)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        db.add(_tariff())
        await db.commit()

        trial = await create_trial_subscription(
            db,
            2,
            duration_days=6,
            connected_squads=['squad-1'],
            tariff_id=1,
            duration_days_include_referral_bonus=True,
        )
        assert trial.trial_referral_seconds == int(3 * 86400)

        await extend_subscription(db, trial, 7)

        before = datetime.now(UTC)
        result = await extend_subscription(db, trial, 30, tariff_id=1, traffic_limit_gb=50)
        after = datetime.now(UTC)

        assert result.is_trial is False
        assert before + timedelta(days=33) <= result.end_date <= after + timedelta(days=33)
        assert result.end_date < before + timedelta(days=34)


@pytest.mark.asyncio
async def test_expired_referral_tail_is_not_restored_by_free_extension(monkeypatch):
    """Истёкший реферальный хвост не возвращается после бесплатного продления."""
    _enable_days_only(monkeypatch, bonus_days=3)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        db.add(_tariff())
        await db.commit()

        now = datetime.now(UTC)
        expired = Subscription(
            id=10,
            user_id=2,
            remnawave_short_id='sub10',
            status=SubscriptionStatus.EXPIRED.value,
            is_trial=True,
            start_date=now - timedelta(days=30),
            end_date=now - timedelta(days=24),
            base_trial_end_date=now - timedelta(days=27),
            trial_referral_seconds=int(3 * 86400),
            traffic_limit_gb=50,
            device_limit=1,
            tariff_id=1,
            connected_squads=['squad-1'],
        )
        db.add(expired)
        await db.commit()

        extended = await extend_subscription(db, expired, 10)
        assert extended.end_date >= datetime.now(UTC) + timedelta(days=10) - timedelta(seconds=5)
        assert extended.trial_referral_seconds == 0

        before = datetime.now(UTC)
        result = await extend_subscription(db, extended, 30, tariff_id=1, traffic_limit_gb=50)
        after = datetime.now(UTC)

        assert result.is_trial is False
        assert before + timedelta(days=30) <= result.end_date <= after + timedelta(days=30)
        assert result.end_date < before + timedelta(days=31)


@pytest.mark.asyncio
async def test_explicit_applied_referral_bonus_survives_config_change(monkeypatch):
    """Учёт следует снимку бонуса при активации, а не пересчёту от изменившегося конфига."""
    _enable_days_only(monkeypatch, bonus_days=3)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        await db.commit()

        # Срок уже посчитан как 3 базовых + 3 реферальных. Конфиг успевает
        # измениться до вызова CRUD: пересчёт после await дал бы 20 дней.
        monkeypatch.setattr(settings, 'REFERRAL_TRIAL_BONUS_DAYS', 20)
        created = await create_trial_subscription(
            db,
            2,
            duration_days=6,
            connected_squads=['squad-x'],
            trial_referral_days=3,
        )

        assert created.trial_referral_seconds == int(3 * 86400)
        assert created.base_trial_end_date == created.start_date + timedelta(days=3)
        assert get_trial_carry_over_delta(created, created.start_date) == timedelta(days=3)


@pytest.mark.asyncio
async def test_new_free_trial_initializes_referral_accounting_even_without_bonus(monkeypatch):
    """Новый бесплатный триал хранит явный ноль, а не NULL: NULL — только legacy."""
    _enable_days_only(monkeypatch, bonus_days=0)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        await db.commit()

        created = await create_trial_subscription(
            db,
            1,
            duration_days=3,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )

        assert created.trial_referral_seconds == 0
        assert created.base_trial_end_date == created.end_date


@pytest.mark.asyncio
async def test_add_reward_days_tracks_referral_tail(monkeypatch):
    """Реферальные дни добавляются к учтённому остатку в той же транзакции."""
    _enable_days_only(monkeypatch, bonus_days=3)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        await db.commit()

        created = await create_trial_subscription(
            db,
            2,
            duration_days=6,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )
        assert created.trial_referral_seconds == int(3 * 86400)

        got = await add_reward_days(db, created, 10)

        assert got is not None
        assert got.trial_referral_seconds == int(13 * 86400)
        assert get_trial_carry_over_delta(got, datetime.now(UTC)) == timedelta(days=13)


@pytest.mark.asyncio
async def test_conversion_clears_referral_accounting(monkeypatch):
    """После конверсии триала учёт реферальных секунд больше не нужен (NULL)."""
    _enable_days_only(monkeypatch, bonus_days=3)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        db.add(_tariff())
        await db.commit()

        trial = await create_trial_subscription(
            db,
            2,
            duration_days=6,
            connected_squads=['squad-1'],
            tariff_id=1,
            duration_days_include_referral_bonus=True,
        )
        result = await extend_subscription(db, trial, 30, tariff_id=1, traffic_limit_gb=50)

        assert result.is_trial is False
        assert result.trial_referral_seconds is None


@pytest.mark.asyncio
async def test_replace_subscription_resets_referral_accounting(monkeypatch):
    """Новый триал после replace не наследует старый реферальный остаток."""
    _enable_days_only(monkeypatch, bonus_days=3)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add(_user(2, referred_by=1))
        await db.commit()

        trial = await create_trial_subscription(
            db,
            2,
            duration_days=6,
            connected_squads=['squad-x'],
            duration_days_include_referral_bonus=True,
        )
        assert trial.trial_referral_seconds == int(3 * 86400)

        replaced = await replace_subscription(
            db,
            trial,
            duration_days=3,
            traffic_limit_gb=50,
            device_limit=1,
            connected_squads=['squad-x'],
            is_trial=True,
        )

        assert replaced.trial_referral_seconds == 0
        assert replaced.base_trial_end_date == replaced.end_date


def test_legacy_row_without_referral_accounting_keeps_previous_behaviour(monkeypatch):
    """Старая строка с NULL в обоих полях учёта живёт по прежнему should_carry."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    now = datetime.now(UTC)
    legacy = _sub(1, 1, is_trial=True, end_in_days=5, base_in_days=None, start=now)
    # У legacy-строки нет ни базы, ни явного учёта реферальных секунд.
    assert legacy.trial_referral_seconds is None

    assert get_trial_carry_over_delta(legacy, now) == timedelta(days=5)


@pytest.mark.asyncio
async def test_extend_commit_false_leaves_trial_deactivation_to_caller(monkeypatch, tmp_path):
    """commit=False не должен сам коммитить деактивацию и мешать откату."""
    _enable_days_only(monkeypatch, bonus_days=3)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)

    async with _file_sessions(monkeypatch, tmp_path) as maker:
        now = datetime.now(UTC)
        async with maker() as seed:
            seed.add(_user(1))
            seed.add(_tariff())
            seed.add(_purchasable_trial(sub_id=10, user_id=1, now=now))
            seed.add(
                Subscription(
                    id=11,
                    user_id=1,
                    remnawave_short_id='sub11',
                    status=SubscriptionStatus.TRIAL.value,
                    is_trial=True,
                    start_date=now,
                    end_date=now + timedelta(days=6),
                    base_trial_end_date=now + timedelta(days=3),
                    trial_referral_seconds=0,
                    traffic_limit_gb=10,
                    device_limit=1,
                    tariff_id=2,
                    connected_squads=['squad-1'],
                )
            )
            await seed.commit()

        async with maker() as db:
            converted = await db.get(Subscription, 10)
            await extend_subscription(db, converted, 30, tariff_id=1, traffic_limit_gb=50, commit=False)
            other = await db.get(Subscription, 11)
            # Внутри транзакции второй триал уже деактивирован — но не закоммичен.
            assert other.is_trial is False
            assert converted.is_trial is False
            await db.rollback()

        async with maker() as check:
            fresh = await check.get(Subscription, 11)
            fresh_converted = await check.get(Subscription, 10)
            assert fresh is not None and fresh.is_trial is True
            assert fresh.status == SubscriptionStatus.TRIAL.value
            assert fresh_converted is not None and fresh_converted.is_trial is True


@pytest.mark.asyncio
async def test_create_paid_subscription_commit_false_is_atomic(monkeypatch, tmp_path):
    """create_paid_subscription(commit=False) не коммитит деактивацию триала сам."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    async with _file_sessions(monkeypatch, tmp_path) as maker:
        now = datetime.now(UTC)
        async with maker() as seed:
            seed.add(_user(1))
            seed.add(_purchasable_trial(sub_id=10, user_id=1, now=now))
            await seed.commit()

        async with maker() as db:
            await create_paid_subscription(
                db,
                user_id=1,
                duration_days=30,
                traffic_limit_gb=50,
                connected_squads=['squad-1'],
                commit=False,
            )
            trial = await db.get(Subscription, 10)
            assert trial is not None and trial.is_trial is False
            await db.rollback()

        async with maker() as check:
            trial = await check.get(Subscription, 10)
            assert trial is not None and trial.is_trial is True
