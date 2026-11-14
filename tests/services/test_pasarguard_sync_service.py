"""Проектор PasarGuard: приводит пользователей панели к состоянию бота.

Панель — раб: каждый проход читает её пользователей заново и переписывает
ручные правки (disable, группы, note), а при выключенном флаге xdrive гасит
известные личности, не создавая новых. Тесты работают на реальной in-memory
SQLite-сессии и фейковом API с поверхностью Task 1.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import app.services.pasarguard_sync_service as sync_module
from app.config import settings
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
from app.services.pasarguard_sync_service import PasarguardSyncService
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


def _configure(monkeypatch, fake: FakePasarguardAPI, *, enabled: bool = True) -> None:
    monkeypatch.setattr(settings, 'XDRIVE_ENABLED', enabled)
    monkeypatch.setattr(settings, 'PASARGUARD_API_URL', 'https://panel.example')
    monkeypatch.setattr(settings, 'PASARGUARD_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'PASARGUARD_XDRIVE_GROUP_ID', GROUP_ID)
    monkeypatch.setattr(settings, 'PASARGUARD_USERNAME_PREFIX', PREFIX)
    monkeypatch.setattr(settings, 'PASARGUARD_INSTANCE_ID', INSTANCE)
    monkeypatch.setattr(settings, 'PASARGUARD_SYNC_BATCH_SIZE', 200)
    monkeypatch.setattr(sync_module, '_make_api', lambda: fake)


def _use_session(monkeypatch, db) -> None:
    """Проектор открывает свою сессию — отдаём ему ту же in-memory сессию."""

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(sync_module, 'AsyncSessionLocal', _session)


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


def _seed_state(
    subscription_id: int = 1,
    *,
    total: int = 5 * GIB,
    used: int = 0,
    squad: str = SQUAD,
) -> SubscriptionPremiumTraffic:
    return SubscriptionPremiumTraffic(
        subscription_id=subscription_id,
        squad_uuid=squad,
        limit_bytes=total,
        used_bytes=used,
        period_start_at=START,
    )


def _panel_user(
    user_id: int = 42,
    subscription_id: int = 1,
    *,
    status: str = 'active',
    expire: datetime = END,
    data_limit: int | None = 5 * GIB,
    used: int = 0,
    group_ids=(GROUP_ID,),
    note: str | None = None,
) -> PasarguardUser:
    return PasarguardUser(
        id=user_id,
        username=f'{PREFIX}{subscription_id}',
        status=status,
        expire=expire,
        data_limit=data_limit,
        used_traffic=used,
        lifetime_used_traffic=used,
        group_ids=list(group_ids),
        note=note if note is not None else f'xdrive-bot:{INSTANCE}:sub:{subscription_id}',
    )


async def _process(monkeypatch, db, objects, fake: FakePasarguardAPI, *, enabled: bool = True) -> dict[str, int]:
    db.add_all(objects)
    await db.commit()
    _use_session(monkeypatch, db)
    _configure(monkeypatch, fake, enabled=enabled)
    return await PasarguardSyncService().process_once()


@pytest.mark.asyncio
async def test_creates_missing_user_with_group_expire_and_absolute_limit(monkeypatch):
    fake = FakePasarguardAPI()
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(), _seed_state(used=GIB)],
            fake,
        )

        assert stats['created'] == 1
        assert len(fake.create_calls) == 1
        call = fake.create_calls[0]
        assert call['username'] == 'bx1'
        assert call['group_ids'] == [GROUP_ID]
        assert call['expire'] == END
        # used_traffic панели (0) + остаток пула (5 GiB - 1 GiB).
        assert call['data_limit'] == 4 * GIB
        assert call['status'] == 'active'
        assert call['note'] == f'xdrive-bot:{INSTANCE}:sub:1'

        subscription = await db.get(Subscription, 1)
        assert subscription.pasarguard_user_id in fake.users
        assert subscription.pasarguard_username == 'bx1'
        assert subscription.pasarguard_created_at is not None
        assert subscription.pasarguard_synced_at is not None
        assert subscription.pasarguard_synced_hash


@pytest.mark.asyncio
async def test_manual_disable_is_overwritten_by_the_next_pass(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(status='disabled')])
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
            fake,
        )

        assert stats['enabled'] == 1
        assert len(fake.update_calls) == 1
        user_id, fields = fake.update_calls[0]
        assert user_id == 42
        assert fields['expire'] == END
        assert fields['data_limit'] == 5 * GIB
        assert fields['group_ids'] == [GROUP_ID]
        assert fields['note'] == f'xdrive-bot:{INSTANCE}:sub:1'
        assert fields['data_limit_reset_strategy'] == 'no_reset'
        assert fake.disabled_calls == [(42, False)]
        assert fake.users[42].status == 'active'


@pytest.mark.asyncio
async def test_manual_group_and_note_edits_are_overwritten(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(group_ids=(999,), note='manual edit')])
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
            fake,
        )

        assert stats['updated'] == 1
        user_id, fields = fake.update_calls[0]
        assert user_id == 42
        assert fields['group_ids'] == [GROUP_ID]
        assert fields['note'] == f'xdrive-bot:{INSTANCE}:sub:1'
        assert 'expire' not in fields
        assert 'data_limit' not in fields
        assert fake.disabled_calls == []


@pytest.mark.asyncio
async def test_manual_user_deletion_is_recreated(monkeypatch):
    fake = FakePasarguardAPI()
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
            fake,
        )

        assert stats['created'] == 1
        assert fake.create_calls[0]['username'] == 'bx1'
        subscription = await db.get(Subscription, 1)
        assert subscription.pasarguard_user_id in fake.users
        assert subscription.pasarguard_user_id != 42


@pytest.mark.asyncio
async def test_equal_state_makes_no_write_calls(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
            fake,
        )

        assert stats['checked'] == 1
        assert stats['created'] == 0
        assert stats['updated'] == 0
        assert stats['disabled'] == 0
        assert stats['enabled'] == 0
        assert stats['errors'] == 0
        assert fake.create_calls == []
        assert fake.update_calls == []
        assert fake.disabled_calls == []


@pytest.mark.parametrize('panel_status', ['limited', 'expired'])
@pytest.mark.asyncio
async def test_stale_limited_user_is_reenabled_with_active_status(monkeypatch, panel_status):
    fake = FakePasarguardAPI([_panel_user(status=panel_status)])
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
            fake,
        )

        assert stats['enabled'] == 1
        _, fields = fake.update_calls[0]
        assert fields['status'] == 'active'
        assert fields['expire'] == END
        assert fields['data_limit'] == 5 * GIB
        assert fields['data_limit_reset_strategy'] == 'no_reset'
        assert fake.disabled_calls == [(42, False)]


@pytest.mark.asyncio
async def test_ineligible_subscription_disables_an_existing_user(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(),
                _seed_subscription(status='expired', identity=(42, 'bx1')),
                _seed_state(),
            ],
            fake,
        )

        assert stats['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]
        assert fake.update_calls == []


@pytest.mark.asyncio
async def test_pasarguard_error_is_counted_and_does_not_raise(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(note='manual edit')])
    fake.fail_on['update_user'] = PasarguardTransientError('panel down')
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
            fake,
        )

        assert stats['errors'] == 1
        assert stats['updated'] == 0
        assert fake.update_calls == []


@pytest.mark.asyncio
async def test_flag_off_disables_known_users_and_creates_nothing(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(),
                _seed_tariff(2, limits={SQUAD2: {'traffic_limit_gb': 5}}),
                _seed_subscription(1, identity=(42, 'bx1')),
                _seed_subscription(2, tariff_id=2),
                _seed_state(1),
                _seed_state(2, squad=SQUAD2),
            ],
            fake,
            enabled=False,
        )

        assert stats['disabled'] == 1
        assert stats['created'] == 0
        assert fake.create_calls == []
        assert fake.disabled_calls == [(42, True)]


@pytest.mark.asyncio
async def test_pasarguard_api_error_on_create_is_counted(monkeypatch):
    fake = FakePasarguardAPI()
    fake.fail_on['create_user'] = PasarguardAPIError('rejected', status_code=422)
    async with memory_session(monkeypatch, TABLES) as db:
        stats = await _process(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(), _seed_state()],
            fake,
        )

        assert stats['errors'] >= 1
        assert stats['created'] == 0
