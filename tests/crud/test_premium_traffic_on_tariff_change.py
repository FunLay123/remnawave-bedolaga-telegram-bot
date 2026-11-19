"""Смена тарифа обновляет снимок премиум-лимита на реальной БД.

Проверяем именно проводку: `extend_subscription` и `replace_subscription` должны
довести новый потолок до `subscription_premium_traffic`, а не только поменять
`subscriptions.tariff_id`.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.database.crud.subscription import extend_subscription, replace_subscription
from app.database.models import Base, Subscription, SubscriptionPremiumTraffic, SubscriptionStatus, Tariff, User
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER_SQUAD = '82a12389-14d6-40c6-b320-4674f6bbb344'
NOW = datetime.now(UTC)


def _user() -> User:
    return User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0)


def _tariff(tariff_id: int, limit_gb: int) -> Tariff:
    return Tariff(
        id=tariff_id,
        name=f'Тариф {tariff_id}',
        description='',
        is_active=True,
        traffic_limit_gb=limit_gb,
        device_limit=3,
        allowed_squads=[SQUAD],
        period_prices={'30': 10_000},
        display_order=tariff_id,
        server_traffic_limits={SQUAD: {'traffic_limit_gb': limit_gb}},
    )


def _subscription(*, tariff_id: int = 1, limit_gb: int = 3) -> Subscription:
    return Subscription(
        id=10,
        remnawave_short_id='sub10',
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=NOW - timedelta(days=10),
        end_date=NOW + timedelta(days=20),
        traffic_limit_gb=limit_gb,
        traffic_used_gb=0.0,
        device_limit=3,
        tariff_id=tariff_id,
        connected_squads=[SQUAD],
    )


def _premium_state(limit_gb: int = 3) -> SubscriptionPremiumTraffic:
    return SubscriptionPremiumTraffic(
        subscription_id=10,
        squad_uuid=SQUAD,
        limit_bytes=limit_gb * BYTES_IN_GB,
        period_start_at=NOW,
    )


def _configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'TRAFFIC_SELECTION_MODE', 'fixed')
    monkeypatch.setattr(settings, 'FIXED_TRAFFIC_LIMIT_GB', 0)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)


async def _seed(db) -> None:
    db.add(_user())
    db.add(_tariff(1, 3))
    db.add(_tariff(2, 20))
    db.add(_subscription())
    db.add(_premium_state())
    await db.commit()


async def _premium_limit(db) -> int:
    result = await db.execute(
        select(SubscriptionPremiumTraffic).where(
            SubscriptionPremiumTraffic.subscription_id == 10,
            SubscriptionPremiumTraffic.squad_uuid == SQUAD,
        )
    )
    state = result.scalar_one()
    return state.limit_bytes


async def test_extend_subscription_refreshes_premium_snapshot(monkeypatch):
    _configure(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = await db.get(Subscription, 10)

        await extend_subscription(
            db,
            subscription,
            30,
            tariff_id=2,
            traffic_limit_gb=20,
            device_limit=3,
            connected_squads=[SQUAD],
            commit=False,
        )
        await db.commit()

        await db.refresh(subscription)
        assert subscription.tariff_id == 2
        assert await _premium_limit(db) == 20 * BYTES_IN_GB


async def test_replace_subscription_refreshes_premium_snapshot(monkeypatch):
    _configure(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = await db.get(Subscription, 10)

        await replace_subscription(
            db,
            subscription,
            duration_days=30,
            traffic_limit_gb=20,
            device_limit=3,
            connected_squads=[SQUAD],
            is_trial=False,
            tariff_id=2,
            commit=False,
        )
        await db.commit()

        await db.refresh(subscription)
        assert subscription.tariff_id == 2
        assert await _premium_limit(db) == 20 * BYTES_IN_GB


async def test_failed_premium_sync_does_not_roll_back_tariff_change(monkeypatch):
    """Auxiliary snapshot failure must not undo a paid tariff change.

    The savepoint wraps only the premium helper; the subscription's new
    tariff_id and limits are flushed before it and still commit.
    """
    _configure(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = await db.get(Subscription, 10)

        from app.database.crud import premium_traffic as premium_crud

        async def _boom(*_args, **_kwargs):
            raise RuntimeError('premium snapshot failed')

        monkeypatch.setattr(premium_crud, 'sync_premium_state_limits', _boom)

        await extend_subscription(
            db,
            subscription,
            30,
            tariff_id=2,
            traffic_limit_gb=20,
            device_limit=3,
            connected_squads=[SQUAD],
            commit=False,
        )
        await db.commit()

        await db.refresh(subscription)
        assert subscription.tariff_id == 2
        # Savepoint rolled the snapshot back; the periodic worker will retry.
        assert await _premium_limit(db) == 3 * BYTES_IN_GB


async def test_failed_orphan_cleanup_does_not_roll_back_tariff_change(monkeypatch):
    """A failing orphan delete is inside the same savepoint and cannot poison the purchase."""
    _configure(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        db.add(
            SubscriptionPremiumTraffic(
                subscription_id=10,
                squad_uuid=OTHER_SQUAD,
                limit_bytes=3 * BYTES_IN_GB,
                period_start_at=NOW,
            )
        )
        await db.commit()
        subscription = await db.get(Subscription, 10)

        from app.database.crud import premium_traffic as premium_crud

        async def _boom(*_args, **_kwargs):
            raise RuntimeError('orphan cleanup failed')

        monkeypatch.setattr(premium_crud, 'delete_states_for_squads', _boom)

        await extend_subscription(
            db,
            subscription,
            30,
            tariff_id=2,
            traffic_limit_gb=20,
            device_limit=3,
            connected_squads=[SQUAD],
            commit=False,
        )
        await db.commit()

        await db.refresh(subscription)
        assert subscription.tariff_id == 2
        # The savepoint rolled back the partial orphan cleanup; the row survives.
        assert await _premium_limit(db) == 3 * BYTES_IN_GB


async def test_replace_subscription_refreshes_same_tariff_after_limit_edit(monkeypatch):
    """Expired subscription replaced on the same tariff still takes its new limit."""
    _configure(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        tariff = await db.get(Tariff, 1)
        tariff.server_traffic_limits = {SQUAD: {'traffic_limit_gb': 20}}
        await db.commit()
        subscription = await db.get(Subscription, 10)

        await replace_subscription(
            db,
            subscription,
            duration_days=30,
            traffic_limit_gb=20,
            device_limit=3,
            connected_squads=[SQUAD],
            is_trial=False,
            tariff_id=1,
            commit=False,
        )
        await db.commit()

        assert await _premium_limit(db) == 20 * BYTES_IN_GB
