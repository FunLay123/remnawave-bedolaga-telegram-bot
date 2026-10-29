"""Конкурентная проверка: первая покупка реферала награждается ровно один раз.

Тест идёт только на настоящем PostgreSQL: SQLite молча игнорирует ``FOR UPDATE``,
поэтому на нём гонка выглядит проверенной, не будучи проверенной вовсе. База
берётся из ``TEST_POSTGRES_DSN`` (явное переопределение) или из общей тестовой
переменной ``TEST_DATABASE_URL``; без базы тест пропускается, а под
``REQUIRE_POSTGRES_TESTS=1`` — падает, чтобы CI не считал пропуск успехом.

База обязана быть выделенной тестовой: тест сносит и создаёт схему заново.
DSN, совпавший с боевым ``DATABASE_URL``, отвергается до любых DDL.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_purchase_service import register_first_purchase
from app.services.referral_reward_service import (
    REASON_FIRST_PURCHASE,
    REASON_FIRST_PURCHASE_REFEREE,
    REASON_FIRST_PURCHASE_REGISTERED,
)
from tests.fixtures.postgres_db import real_asyncpg, require_postgres_dsn


pytestmark = pytest.mark.postgres


def _same_database(left: URL, right: URL) -> bool:
    """Одна и та же база, даже если драйвер в URL записан по-разному."""
    return (
        left.get_backend_name() == right.get_backend_name()
        and left.username == right.username
        and left.password == right.password
        and left.host == right.host
        and left.port == right.port
        and left.database == right.database
    )


def _concurrency_dsn() -> str:
    """DSN выделенной тестовой базы для конкурентной проверки."""
    override = os.environ.get('TEST_POSTGRES_DSN', '').strip()
    # require_postgres_dsn() = TEST_DATABASE_URL, иначе skip; под
    # REQUIRE_POSTGRES_TESTS=1 — падение. Так CI без базы не зеленеет.
    raw = override or require_postgres_dsn()

    test_url = make_url(raw)
    if test_url.get_backend_name() != 'postgresql':
        pytest.fail(f'Для конкурентной проверки нужен PostgreSQL, а не {test_url.get_backend_name()}')

    configured = settings.DATABASE_URL
    if configured and _same_database(test_url, make_url(str(configured))):
        pytest.fail('Тестовая база совпадает с DATABASE_URL: тест пересоздаёт схему и не должен идти по боевой базе')

    return raw


@pytest.mark.asyncio
async def test_concurrent_first_purchase_grants_once(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    # Значения фиксируем: локальный .env не должен менять ожидаемые +10 дней
    # и порог в 30 дней, от которых зависят утверждения теста.
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    now = datetime.now(UTC)

    # NullPool: два конкурентных вызова обязаны получить два разных соединения,
    # иначе блокировка строки проверялась бы на соединении самой себя.
    with real_asyncpg():
        engine = create_async_engine(_concurrency_dsn(), poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with maker() as db:
            db.add_all(
                [
                    User(id=1, telegram_id=1001, first_name='R', language='ru', balance_kopeks=0),
                    User(id=2, telegram_id=1002, first_name='F', language='ru', balance_kopeks=0, referred_by_id=1),
                    # short_id уникален и ограничен 16 символами.
                    Subscription(
                        id=10,
                        user_id=2,
                        remnawave_short_id='conc-referee',
                        status=SubscriptionStatus.ACTIVE.value,
                        is_trial=False,
                        start_date=now - timedelta(days=1),
                        end_date=now + timedelta(days=30),
                        traffic_limit_gb=100,
                        device_limit=1,
                    ),
                    Subscription(
                        id=11,
                        user_id=1,
                        remnawave_short_id='conc-referrer',
                        status=SubscriptionStatus.ACTIVE.value,
                        is_trial=False,
                        start_date=now - timedelta(days=1),
                        end_date=now + timedelta(days=30),
                        traffic_limit_gb=100,
                        device_limit=1,
                    ),
                ]
            )
            await db.commit()

        async def call() -> object:
            async with maker() as db:
                return await register_first_purchase(
                    db,
                    referee_id=2,
                    subscription_id=10,
                    duration_days=30,
                    paid_amount_kopeks=10_000,
                    source='concurrency-test',
                )

        results = await asyncio.gather(call(), call())

        async with maker() as db:
            rows = (
                (
                    await db.execute(
                        select(ReferralEarning).where(
                            ReferralEarning.reason.in_(
                                (
                                    REASON_FIRST_PURCHASE_REGISTERED,
                                    REASON_FIRST_PURCHASE_REFEREE,
                                    REASON_FIRST_PURCHASE,
                                )
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            referee_sub = await db.get(Subscription, 10)
            referrer_sub = await db.get(Subscription, 11)

        markers = [row for row in rows if row.reason == REASON_FIRST_PURCHASE_REGISTERED]
        referee_rewards = [row for row in rows if row.reason == REASON_FIRST_PURCHASE_REFEREE]
        referrer_rewards = [row for row in rows if row.reason == REASON_FIRST_PURCHASE]

        assert len(markers) == 1
        # Маркер и дни пишутся одной транзакцией: дубль маркера означал бы и
        # дубль награды, но проверяем оба инварианта явно.
        assert len(referee_rewards) == 1
        assert len(referrer_rewards) == 1
        assert sorted(result.status for result in results) == ['already_registered', 'rewarded']
        # Дни продлили подписки ровно один раз: +10 к исходным 30.
        assert referee_sub.end_date == now + timedelta(days=40)
        assert referrer_sub.end_date == now + timedelta(days=40)
    finally:
        await engine.dispose()
