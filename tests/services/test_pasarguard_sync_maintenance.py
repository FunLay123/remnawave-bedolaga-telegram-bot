"""Обслуживающие проходы проектора PasarGuard.

Бэкфилл создаёт личности только там, где подписка действительно имеет право
на xdrive; уборка сирот трогает лишь пользователей с префиксом бота и его
note-маркером; очередь удалений и отложенные revoke переживают недоступность
панели и разбираются следующими проходами.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import app.services.pasarguard_sync_service as sync_module
from app.config import settings
from app.database.crud.pasarguard import add_delete_intent, list_delete_intents
from app.database.models import (
    PasarguardDeleteIntent,
    Subscription,
    SubscriptionPremiumTraffic,
    Tariff,
    User,
    UserStatus,
)
from app.external.pasarguard_api import (
    PasarguardAPIError,
    PasarguardNotFoundError,
    PasarguardTransientError,
    PasarguardUser,
)
from app.services.pasarguard_sync_service import (
    PasarguardSyncService,
    enqueue_pasarguard_delete,
    revoke_pasarguard_subscription,
)
from tests.fixtures.sqlite_memory import memory_session


GIB = 1024**3
SQUAD = '11111111-2222-3333-4444-555555555555'
SQUAD2 = '66666666-7777-8888-9999-000000000000'
START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
END = datetime.now(UTC) + timedelta(days=30)
GROUP_ID = 7
PREFIX = 'bx'
INSTANCE = 'inst'

TABLES = (
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    SubscriptionPremiumTraffic.__table__,
    PasarguardDeleteIntent.__table__,
)

_USER_FIELDS = {
    'username',
    'status',
    'expire',
    'data_limit',
    'used_traffic',
    'lifetime_used_traffic',
    'group_ids',
    'note',
}


class FakePasarguardAPI:
    """Поверхность `PasarguardAPI` из Task 1 поверх словаря пользователей."""

    def __init__(self, users=()):
        self.users = {user.id: user for user in users}
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[int, dict]] = []
        self.disabled_calls: list[tuple[int, bool]] = []
        self.revoke_calls: list[int] = []
        self.delete_calls: list[int] = []
        self.fail_on: dict[str, Exception] = {}
        self._next_id = max(self.users, default=100) + 1

    def _maybe_fail(self, method: str) -> None:
        error = self.fail_on.get(method)
        if error is not None:
            raise error

    async def list_users(self, offset: int = 0, limit: int = 200) -> list[PasarguardUser]:
        self._maybe_fail('list_users')
        return list(self.users.values())[offset : offset + limit]

    async def get_user_by_username(self, username: str) -> PasarguardUser:
        self._maybe_fail('get_user_by_username')
        for user in self.users.values():
            if user.username == username:
                return user
        raise PasarguardNotFoundError(f'no user {username}', status_code=404)

    async def create_user(
        self,
        username: str,
        group_ids: list[int],
        expire: datetime,
        data_limit: int,
        status: str = 'active',
        note: str | None = None,
    ) -> PasarguardUser:
        self._maybe_fail('create_user')
        self.create_calls.append(
            {
                'username': username,
                'group_ids': list(group_ids),
                'expire': expire,
                'data_limit': data_limit,
                'status': status,
                'note': note,
            }
        )
        user = PasarguardUser(
            id=self._next_id,
            username=username,
            status=status,
            expire=expire,
            data_limit=data_limit,
            used_traffic=0,
            lifetime_used_traffic=0,
            group_ids=list(group_ids),
            note=note,
        )
        self._next_id += 1
        self.users[user.id] = user
        return user

    async def update_user(self, user_id: int, **fields) -> PasarguardUser:
        self._maybe_fail('update_user')
        self.update_calls.append((user_id, dict(fields)))
        changes = {key: value for key, value in fields.items() if key in _USER_FIELDS}
        user = replace(self.users[user_id], **changes)
        self.users[user_id] = user
        return user

    async def set_disabled(self, user_id: int, disabled: bool) -> PasarguardUser:
        self._maybe_fail('set_disabled')
        self.disabled_calls.append((user_id, disabled))
        user = replace(self.users[user_id], status='disabled' if disabled else 'active')
        self.users[user_id] = user
        return user

    async def revoke_sub(self, user_id: int) -> PasarguardUser:
        self._maybe_fail('revoke_sub')
        self.revoke_calls.append(user_id)
        return self.users[user_id]

    async def delete_user(self, user_id: int) -> None:
        self._maybe_fail('delete_user')
        self.delete_calls.append(user_id)
        self.users.pop(user_id, None)

    async def close(self) -> None:
        return None


def _configure(monkeypatch, fake: FakePasarguardAPI) -> None:
    monkeypatch.setattr(settings, 'XDRIVE_ENABLED', True)
    monkeypatch.setattr(settings, 'PASARGUARD_API_URL', 'https://panel.example')
    monkeypatch.setattr(settings, 'PASARGUARD_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'PASARGUARD_XDRIVE_GROUP_ID', GROUP_ID)
    monkeypatch.setattr(settings, 'PASARGUARD_USERNAME_PREFIX', PREFIX)
    monkeypatch.setattr(settings, 'PASARGUARD_INSTANCE_ID', INSTANCE)
    monkeypatch.setattr(settings, 'PASARGUARD_SYNC_BATCH_SIZE', 200)
    monkeypatch.setattr(sync_module, '_make_api', lambda **_kwargs: fake)


def _seed_user(user_id: int = 1, status: str = UserStatus.ACTIVE.value) -> User:
    return User(
        id=user_id,
        telegram_id=1000 + user_id,
        first_name=f'U{user_id}',
        language='ru',
        status=status,
        balance_kopeks=0,
    )


def _seed_tariff(tariff_id: int = 1, limits=None) -> Tariff:
    if limits is None:
        limits = {SQUAD: {'traffic_limit_gb': 5}}
    return Tariff(id=tariff_id, name=f'Tariff {tariff_id}', server_traffic_limits=limits)


def _seed_subscription(
    subscription_id: int = 1,
    *,
    status: str = 'active',
    end_date: datetime = END,
    identity: tuple[int, str] | None = None,
    tariff_id: int = 1,
) -> Subscription:
    extra = {}
    if identity is not None:
        extra = {
            'pasarguard_user_id': identity[0],
            'pasarguard_username': identity[1],
            'pasarguard_created_at': START,
        }
    return Subscription(
        id=subscription_id,
        user_id=1,
        remnawave_short_id=f'sid{subscription_id}',
        status=status,
        is_trial=False,
        start_date=START,
        end_date=end_date,
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=1,
        connected_squads=[SQUAD],
        tariff_id=tariff_id,
        **extra,
    )


def _seed_state(subscription_id: int = 1, *, total: int = 5 * GIB, used: int = 0) -> SubscriptionPremiumTraffic:
    return SubscriptionPremiumTraffic(
        subscription_id=subscription_id,
        squad_uuid=SQUAD,
        limit_bytes=total,
        used_bytes=used,
        period_start_at=START,
    )


def _panel_user(
    user_id: int = 42,
    subscription_id: int = 1,
    *,
    username: str | None = None,
    status: str = 'active',
    note: str | None = None,
) -> PasarguardUser:
    return PasarguardUser(
        id=user_id,
        username=username if username is not None else f'{PREFIX}{subscription_id}',
        status=status,
        expire=END,
        data_limit=5 * GIB,
        used_traffic=0,
        lifetime_used_traffic=0,
        group_ids=[GROUP_ID],
        note=note if note is not None else f'xdrive-bot:{INSTANCE}:sub:{subscription_id}',
    )


@pytest.mark.asyncio
async def test_backfill_creates_only_eligible_subscriptions(monkeypatch):
    fake = FakePasarguardAPI()
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _seed_user(),
                _seed_tariff(),
                _seed_tariff(2, limits={SQUAD2: {'traffic_limit_gb': 5}}),
                # У третьего тарифа премиум-сквад есть, но состояния ещё нет.
                _seed_tariff(3, limits={SQUAD2: {'traffic_limit_gb': 5}}),
                _seed_subscription(1),
                _seed_subscription(2, status='expired', end_date=END - timedelta(days=40), tariff_id=2),
                _seed_subscription(3, tariff_id=3),
                _seed_state(1),
                # У подписки 3 premium-состояния нет: воркер ещё не замерил.
            ]
        )
        await db.commit()
        _configure(monkeypatch, fake)

        created = await PasarguardSyncService()._backfill(db, fake, list(fake.users.values()))
        await db.commit()

        assert created == 1
        assert [call['username'] for call in fake.create_calls] == ['bx1']
        assert (await db.get(Subscription, 1)).pasarguard_user_id is not None
        assert (await db.get(Subscription, 2)).pasarguard_user_id is None
        assert (await db.get(Subscription, 3)).pasarguard_user_id is None


@pytest.mark.asyncio
async def test_orphan_cleanup_deletes_only_prefixed_and_noted_users(monkeypatch):
    fake = FakePasarguardAPI(
        [
            _panel_user(42, 1),
            # Ручной пользователь с префиксом, но без note-маркера — трогать нельзя.
            _panel_user(43, 999, note='manual user'),
            # Наш пользователь удалённой подписки — сирота.
            _panel_user(44, 2),
            # Чужое имя без префикса бота — даже с похожим note не трогаем.
            _panel_user(45, 5, username='alice'),
        ]
    )
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _seed_user(),
                _seed_tariff(),
                _seed_subscription(1, identity=(42, 'bx1')),
                _seed_state(1),
            ]
        )
        await db.commit()
        _configure(monkeypatch, fake)

        deleted = await PasarguardSyncService()._cleanup_orphans(db, fake, list(fake.users.values()))
        await db.commit()

        assert deleted == 1
        assert fake.delete_calls == [44]
        assert 43 in fake.users
        assert 45 in fake.users
        assert 42 in fake.users


@pytest.mark.asyncio
async def test_delete_intent_is_drained_after_subscription_is_gone(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(55, 5)])
    async with memory_session(monkeypatch, TABLES) as db:
        await add_delete_intent(db, subscription_id=5, user_id=55, username='bx5')
        await db.commit()
        _configure(monkeypatch, fake)

        drained = await PasarguardSyncService()._drain_delete_intents(db, fake)
        await db.commit()

        assert drained == 1
        assert fake.delete_calls == [55]
        assert await list_delete_intents(db) == []


@pytest.mark.asyncio
async def test_pending_revoke_is_retried_and_cleared(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(42, 1)])
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = _seed_subscription(1, identity=(42, 'bx1'))
        subscription.pasarguard_revoke_pending_at = START
        db.add_all([_seed_user(), _seed_tariff(), subscription, _seed_state(1)])
        await db.commit()
        _configure(monkeypatch, fake)

        retried = await PasarguardSyncService()._retry_pending_revokes(db, fake)
        await db.commit()

        assert retried == 1
        assert fake.revoke_calls == [42]
        stored = await db.get(Subscription, 1)
        assert stored.pasarguard_revoke_pending_at is None
        assert stored.pasarguard_last_revoke_at is not None


@pytest.mark.asyncio
async def test_pasarguard_down_leaves_intents_in_place(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(55, 5)])
    fake.fail_on['delete_user'] = PasarguardTransientError('panel down')
    async with memory_session(monkeypatch, TABLES) as db:
        await add_delete_intent(db, subscription_id=5, user_id=55, username='bx5')
        await db.commit()
        _configure(monkeypatch, fake)

        drained = await PasarguardSyncService()._drain_delete_intents(db, fake)
        await db.commit()

        assert drained == 0
        assert len(await list_delete_intents(db)) == 1


@pytest.mark.asyncio
async def test_enqueue_pasarguard_delete_records_identity_before_hard_delete(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = _seed_subscription(1, identity=(42, 'bx1'))
        plain = _seed_subscription(2, status='expired', end_date=END - timedelta(days=1), tariff_id=2)
        db.add_all([_seed_user(), _seed_tariff(), subscription, plain])
        await db.commit()

        await enqueue_pasarguard_delete(db, subscription)
        await enqueue_pasarguard_delete(db, plain)
        await db.commit()

        intents = await list_delete_intents(db)
        assert [
            (intent.subscription_id, intent.pasarguard_user_id, intent.pasarguard_username) for intent in intents
        ] == [(1, 42, 'bx1')]


@pytest.mark.asyncio
async def test_revoke_pasarguard_subscription_marks_success_and_failure(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(42, 1)])
    _configure(monkeypatch, fake)
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = _seed_subscription(1, identity=(42, 'bx1'))
        db.add(subscription)
        await db.commit()

        assert await revoke_pasarguard_subscription(subscription, db) is True
        assert fake.revoke_calls == [42]
        assert subscription.pasarguard_last_revoke_at is not None
        assert subscription.pasarguard_revoke_pending_at is None

        fake.fail_on['revoke_sub'] = PasarguardAPIError('boom', status_code=400)
        assert await revoke_pasarguard_subscription(subscription, db) is False
        assert subscription.pasarguard_revoke_pending_at is not None
