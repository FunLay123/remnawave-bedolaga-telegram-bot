"""Потолок докупки премиум-трафика под настоящей блокировкой — на PostgreSQL.

Быстрая проверка потолка в ``quote_premium_topup`` читает состояние без
блокировки, поэтому два запроса (двойной тап, повтор по таймауту) успевают
увидеть один и тот же остаток и оба её пройти. Единственное, что не даёт им
обоим начислить, — перепроверка в ``apply_premium_topup`` по строке,
заблокированной ``FOR UPDATE``.

На SQLite это проверить нельзя: ``FOR UPDATE`` там молча игнорируется, и тест
зеленел бы с полностью снятой блокировкой. Чередование запросов без взаимного
исключения проверяет
``tests/services/test_premium_traffic_purchase.py::TestCeilingUnderConcurrency``,
а взаимное исключение — только этот файл.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.premium_traffic import get_or_create_state, get_state
from app.database.models import Subscription, SubscriptionPremiumTraffic, User
from app.services.premium_traffic_purchase import (
    PremiumTopupError,
    apply_premium_topup,
    quote_premium_topup,
)
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter


pytestmark = pytest.mark.postgres


TABLES = [User.__table__, Subscription.__table__, SubscriptionPremiumTraffic.__table__]

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
LOCK_WAIT_TIMEOUT_MS = 400

# Потолок 100 ГБ и пакет 20 ГБ: при уже докупленных 80 одна покупка проходит,
# две — пробивают потолок.
BIG_TOPUP = {
    'traffic_limit_gb': 50,
    'topup_enabled': True,
    'topup_packages': {'20': 4000},
    'max_topup_gb': 100,
}
ALREADY_BOUGHT_BYTES = 80 * BYTES_IN_GB
CEILING_BYTES = 100 * BYTES_IN_GB


async def _seed(db: AsyncSession) -> int:
    """Подписка с состоянием, где из 100 ГБ потолка уже докуплено 80."""
    user = User(telegram_id=3000101, first_name='Тест', language='ru')
    db.add(user)
    await db.flush()

    subscription = Subscription(
        user_id=user.id,
        status='active',
        connected_squads=[SQUAD],
        start_date=NOW - timedelta(days=10),
        end_date=NOW + timedelta(days=20),
    )
    db.add(subscription)
    await db.flush()

    state = await get_or_create_state(
        db,
        subscription.id,
        SQUAD,
        limit_bytes=50 * BYTES_IN_GB,
        period_start_at=NOW,
    )
    state.extra_bytes = ALREADY_BOUGHT_BYTES
    await db.commit()
    return subscription.id


def _subscription(subscription_id: int) -> SimpleNamespace:
    """Подписка в том виде, в каком её передаёт роут: объект, а не строка БД."""
    return SimpleNamespace(
        id=subscription_id,
        connected_squads=[SQUAD],
        tariff=SimpleNamespace(server_traffic_limits={SQUAD: BIG_TOPUP}),
    )


async def test_state_row_is_locked_until_the_topup_is_committed(postgres_database) -> None:
    """Пока одна докупка держит строку состояния, вторая ждёт."""
    async with postgres_sessions(postgres_database, TABLES, count=2) as (first, second):
        subscription_id = await _seed(first)
        subscription = _subscription(subscription_id)

        holder_quote = await quote_premium_topup(first, subscription, SQUAD, 20)
        await apply_premium_topup(first, subscription, holder_quote, period_start_at=NOW)

        await second.execute(text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_TIMEOUT_MS}ms'"))
        waiting_quote = await quote_premium_topup(second, subscription, SQUAD, 20)
        with pytest.raises(DBAPIError) as failure:
            await apply_premium_topup(second, subscription, waiting_quote, period_start_at=NOW)

        assert 'lock timeout' in str(failure.value).lower(), 'строка состояния не блокируется'


async def test_concurrent_topups_do_not_exceed_cap(postgres_database) -> None:
    """Два одновременных запроса не должны пробить потолок докупки.

    Обе покупки котируются до того, как хоть одна начислена: обе видят
    «докуплено 80 из 100» и обе проходят быструю проверку. Начислить обязана
    только одна.
    """
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        subscription_id = await _seed(first)
        subscription = _subscription(subscription_id)

        first_quote = await quote_premium_topup(first, subscription, SQUAD, 20)
        second_quote = await quote_premium_topup(second, subscription, SQUAD, 20)

        holder_took_the_row = asyncio.Event()

        async def purchase(session: AsyncSession, quote, *, hold: bool) -> str:
            if not hold:
                await holder_took_the_row.wait()

            try:
                await apply_premium_topup(session, subscription, quote, period_start_at=NOW)
            except PremiumTopupError as error:
                await session.rollback()
                return error.code
            finally:
                # Событие поднимаем в любом случае: иначе соперник ждал бы вечно
                # и вместо понятного падения тест повис бы.
                if hold:
                    holder_took_the_row.set()

            if hold:
                # Держим строку, пока соперник не встанет в очередь: иначе тест
                # проверял бы удачное совпадение по времени, а не блокировку.
                await wait_for_lock_waiter(watcher)
            await session.commit()
            return 'ok'

        outcomes = await asyncio.gather(
            purchase(first, first_quote, hold=True),
            purchase(second, second_quote, hold=False),
        )

        assert sorted(outcomes) == ['ok', 'topup_limit_reached'], f'начислили обе покупки: {outcomes}'

        state = await get_state(watcher, subscription_id, SQUAD)
        assert state.extra_bytes == CEILING_BYTES, 'потолок докупки пробит параллельными запросами'
