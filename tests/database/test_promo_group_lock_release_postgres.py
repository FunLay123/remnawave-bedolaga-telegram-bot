"""Регрессия C1: пост-коммит хук create_transaction не должен держать блокировку.

``create_transaction(commit=True)`` коммитит платёж и затем вызывает
``maybe_assign_promo_group_by_total_spent``. Если у пользователя нет подходящей
промогруппы, та раньше выходила без commit/rollback, оставляя за сессией
вызывающего ``SELECT ... FOR UPDATE`` строки пользователя. Реферальный хук,
который открывает собственную сессию и блокирует ту же строку, вставал в
ожидание навсегда: внешний код ждал хук, а хук ждал сессию, которую сам
вызывающий не отпускал. Здесь это проверяется на настоящем PostgreSQL.

Тест идёт только на PostgreSQL: SQLite молча игнорирует ``FOR UPDATE``, и
незакрытая блокировка в нём не воспроизводится. База берётся из
``TEST_POSTGRES_DSN`` (явное переопределение) или из общей тестовой переменной
``TEST_DATABASE_URL``; без базы тест пропускается, а под
``REQUIRE_POSTGRES_TESTS=1`` — падает, чтобы CI не считал пропуск успехом.

База обязана быть выделенной тестовой: тест пересоздаёт схему. DSN, совпавший
с боевым ``DATABASE_URL``, отвергается до любых DDL.
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
from app.database.crud.transaction import create_transaction
from app.database.models import (
    Base,
    ReferralEarning,
    Subscription,
    SubscriptionStatus,
    TransactionType,
    User,
)
from app.services.referral_purchase_service import register_first_purchase
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED
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
    """DSN выделенной тестовой базы для проверки блокировок."""
    override = os.environ.get('TEST_POSTGRES_DSN', '').strip()
    # require_postgres_dsn() = TEST_DATABASE_URL, иначе skip; под
    # REQUIRE_POSTGRES_TESTS=1 — падение. Так CI без базы не зеленеет.
    raw = override or require_postgres_dsn()

    test_url = make_url(raw)
    if test_url.get_backend_name() != 'postgresql':
        pytest.fail(f'Для проверки блокировок нужен PostgreSQL, а не {test_url.get_backend_name()}')

    configured = settings.DATABASE_URL
    if configured and _same_database(test_url, make_url(str(configured))):
        pytest.fail('Тестовая база совпадает с DATABASE_URL: тест пересоздаёт схему и не должен идти по боевой базе')

    return raw


@pytest.mark.asyncio
async def test_create_transaction_releases_user_lock_for_referral_hook(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_BONUS_DAYS', 10)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PURCHASE_MIN_PERIOD_DAYS', 30)
    now = datetime.now(UTC)

    # NullPool: платёжная и реферальная сессии обязаны получить разные
    # соединения, иначе блокировка строки проверялась бы на самой себе.
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
                    User(id=1, telegram_id=1001, first_name='R', language='ru', balance_kopeks=0, referred_by_id=2),
                    User(id=2, telegram_id=1002, first_name='F', language='ru', balance_kopeks=0),
                    # short_id уникален и ограничен 16 символами.
                    Subscription(
                        id=10,
                        user_id=1,
                        remnawave_short_id='lock-referee',
                        status=SubscriptionStatus.ACTIVE.value,
                        is_trial=False,
                        start_date=now - timedelta(days=1),
                        end_date=now + timedelta(days=30),
                        traffic_limit_gb=100,
                        device_limit=1,
                    ),
                    Subscription(
                        id=11,
                        user_id=2,
                        remnawave_short_id='lock-referrer',
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

        async def register() -> object:
            async with maker() as db:
                return await register_first_purchase(
                    db,
                    referee_id=1,
                    subscription_id=10,
                    duration_days=30,
                    paid_amount_kopeks=10_000,
                    source='lock-regression',
                )

        # Платёжная сессия остаётся открытой: именно её транзакция держала
        # блокировку строки пользователя до фикса. Без коммита на раннем выходе
        # реферальный хук ниже встанет в ожидание и не уложится в таймаут.
        async with maker() as payment_db:
            await create_transaction(
                db=payment_db,
                user_id=1,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=10_000,
                description='Тестовая оплата перед реферальным хуком',
            )

            # Ни одна промогруппа не настроена, траты уже ненулевые: это
            # ранний выход «нет подходящей группы», который раньше не
            # коммитил и не откатывал транзакцию.
            result = await asyncio.wait_for(register(), timeout=5)

        assert result.status == 'rewarded'
        assert result.referee_days == 10
        assert result.referrer_days == 10

        async with maker() as db:
            markers = (
                (
                    await db.execute(
                        select(ReferralEarning).where(
                            ReferralEarning.user_id == 1,
                            ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
                        )
                    )
                )
                .scalars()
                .all()
            )
            referee_sub = await db.get(Subscription, 10)

        assert len(markers) == 1
        assert referee_sub.end_date == now + timedelta(days=40)
    finally:
        await engine.dispose()
