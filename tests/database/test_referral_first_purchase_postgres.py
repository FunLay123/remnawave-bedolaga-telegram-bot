"""Конкурентная проверка: первая покупка реферала награждается ровно один раз.

Тест идёт только на настоящем PostgreSQL из ``TEST_POSTGRES_DSN``: SQLite молча
игнорирует ``FOR UPDATE``, поэтому на нём гонка выглядит проверенной, не будучи
проверенной вовсе. База из ``TEST_POSTGRES_DSN`` обязана быть выделенной
тестовой — тест сносит и создаёт схему заново. Без переменной тест пропускается,
чтобы окружение без PostgreSQL не роняло прогон.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
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
from tests.fixtures.postgres_db import real_asyncpg


pytestmark = pytest.mark.skipif(
    not os.getenv('TEST_POSTGRES_DSN'),
    reason='PostgreSQL-only concurrency test',
)


def _test_dsn() -> str:
    """DSN тестовой базы; боевой DATABASE_URL отвергается до любых DDL."""
    dsn = os.environ['TEST_POSTGRES_DSN']
    if dsn.strip() == str(settings.DATABASE_URL or '').strip():
        pytest.fail(
            'TEST_POSTGRES_DSN совпадает с DATABASE_URL: тест пересоздаёт схему и не должен идти по боевой базе'
        )
    return dsn


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
        engine = create_async_engine(_test_dsn(), poolclass=NullPool)
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
                    # short_id уникален: у двух подписок не может быть одинакового ''.
                    Subscription(
                        id=10,
                        user_id=2,
                        remnawave_short_id='concurrency-referee',
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
                        remnawave_short_id='concurrency-referrer',
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
