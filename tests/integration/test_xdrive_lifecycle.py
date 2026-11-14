"""Матрица жизненного цикла xdrive целиком, на реальной in-memory БД.

Каждая строка матрицы из дизайна проверяется через настоящий
``PasarguardSyncService.process_once()``: сверка известных личностей,
бэкфилл новых, уборка сирот и очередь удалений. Панель PasarGuard —
фейковый API, который записывает вызовы; проектор — единственный писатель.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import app.services.pasarguard_sync_service as sync_module
from app.config import settings
from app.database.crud.premium_traffic import add_extra_bytes, start_new_period
from app.database.models import (
    PasarguardDeleteIntent,
    Subscription,
    SubscriptionPremiumTraffic,
    Tariff,
    User,
    UserStatus,
)
from app.external.pasarguard_api import PasarguardNotFoundError, PasarguardUser
from app.services.pasarguard_sync_service import PasarguardSyncService
from tests.fixtures.sqlite_memory import memory_session


GIB = 1024**3
SQUAD = '11111111-2222-3333-4444-555555555555'
SQUAD2 = '66666666-7777-8888-9999-000000000000'
START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
NOW = datetime.now(UTC)
END = NOW + timedelta(days=30)
NEW_END = END + timedelta(days=30)
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
    """Поверхность `PasarguardAPI` поверх словаря, с журналом вызовов."""

    def __init__(self, users=()):
        self.users = {user.id: user for user in users}
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[int, dict]] = []
        self.disabled_calls: list[tuple[int, bool]] = []
        self.revoke_calls: list[int] = []
        self.delete_calls: list[int] = []
        self._next_id = max(self.users, default=100) + 1

    async def list_users(self, offset: int = 0, limit: int = 200) -> list[PasarguardUser]:
        return list(self.users.values())[offset : offset + limit]

    async def get_user_by_username(self, username: str) -> PasarguardUser:
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
        self.update_calls.append((user_id, dict(fields)))
        changes = {key: value for key, value in fields.items() if key in _USER_FIELDS}
        user = replace(self.users[user_id], **changes)
        self.users[user_id] = user
        return user

    async def set_disabled(self, user_id: int, disabled: bool) -> PasarguardUser:
        self.disabled_calls.append((user_id, disabled))
        user = replace(self.users[user_id], status='disabled' if disabled else 'active')
        self.users[user_id] = user
        return user

    async def revoke_sub(self, user_id: int) -> PasarguardUser:
        self.revoke_calls.append(user_id)
        return self.users[user_id]

    async def delete_user(self, user_id: int) -> None:
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
    monkeypatch.setattr(sync_module, '_make_api', lambda **_kwargs: fake)


def _use_session(monkeypatch, db) -> None:
    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(sync_module, 'AsyncSessionLocal', _session)


async def _pass(monkeypatch, db, fake: FakePasarguardAPI, *, enabled: bool = True) -> dict[str, int]:
    _use_session(monkeypatch, db)
    _configure(monkeypatch, fake, enabled=enabled)
    return await PasarguardSyncService().process_once()


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
    is_trial: bool = False,
    device_limit: int = 1,
    squads: list[str] | None = None,
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
        is_trial=is_trial,
        start_date=START,
        end_date=end_date,
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=device_limit,
        connected_squads=squads or [SQUAD],
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


async def _seed(monkeypatch, db, objects) -> None:
    db.add_all(objects)
    await db.commit()


# ---------------------------------------------------------- покупка/продление


@pytest.mark.asyncio
async def test_purchase_creates_and_enables_user(monkeypatch):
    fake = FakePasarguardAPI()
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(monkeypatch, db, [_seed_user(), _seed_tariff(), _seed_subscription(), _seed_state()])

        stats = await _pass(monkeypatch, db, fake)

        assert stats['created'] == 1
        assert fake.create_calls[0]['username'] == 'bx1'
        assert fake.create_calls[0]['group_ids'] == [GROUP_ID]
        assert fake.create_calls[0]['expire'] == END
        assert fake.create_calls[0]['data_limit'] == 5 * GIB
        subscription = await db.get(Subscription, 1)
        assert subscription.pasarguard_user_id in fake.users
        assert fake.users[subscription.pasarguard_user_id].status == 'active'


@pytest.mark.asyncio
async def test_renewal_updates_expire_and_keeps_uuid(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(expire=END)])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(),
                _seed_subscription(identity=(42, 'bx1'), end_date=NEW_END),
                _seed_state(),
            ],
        )

        stats = await _pass(monkeypatch, db, fake)

        assert stats['updated'] == 1
        assert fake.update_calls == [(42, {'expire': NEW_END})]
        assert fake.create_calls == []
        assert fake.revoke_calls == []
        assert fake.delete_calls == []
        assert fake.users[42].expire == NEW_END


@pytest.mark.asyncio
async def test_tariff_switch_updates_expire_and_data_limit(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(expire=END, data_limit=5 * GIB)])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(limits={SQUAD: {'traffic_limit_gb': 8}}),
                _seed_subscription(identity=(42, 'bx1'), end_date=NEW_END),
                _seed_state(total=8 * GIB, used=GIB),
            ],
        )

        stats = await _pass(monkeypatch, db, fake)

        assert stats['updated'] == 1
        assert fake.update_calls == [
            (42, {'expire': NEW_END, 'data_limit': 7 * GIB, 'data_limit_reset_strategy': 'no_reset'})
        ]
        assert fake.users[42].data_limit == 7 * GIB


@pytest.mark.asyncio
async def test_remnawave_webhook_import_is_projected(monkeypatch):
    """Импорт «панель — истина» меняет БД, проектор применяет новое состояние."""
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
        )
        subscription = await db.get(Subscription, 1)
        subscription.status = 'disabled'
        subscription.last_webhook_update_at = NOW
        await db.commit()

        stats = await _pass(monkeypatch, db, fake)

        assert stats['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]
        assert fake.create_calls == []


# -------------------------------------------------------------- истечение


@pytest.mark.asyncio
async def test_expiry_disables_user(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(),
                _seed_subscription(status='expired', end_date=NOW - timedelta(days=1), identity=(42, 'bx1')),
                _seed_state(),
            ],
        )

        stats = await _pass(monkeypatch, db, fake)

        assert stats['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]
        assert fake.users[42].status == 'disabled'


@pytest.mark.asyncio
async def test_general_traffic_limit_disables_user(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(),
                _seed_subscription(status='limited', identity=(42, 'bx1')),
                _seed_state(total=5 * GIB, used=5 * GIB),
            ],
        )

        stats = await _pass(monkeypatch, db, fake)

        assert stats['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]


# ------------------------------------------------------------------- бан/разбан


@pytest.mark.asyncio
async def test_ban_disables_and_unban_enables(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(status=UserStatus.BLOCKED.value),
                _seed_tariff(),
                _seed_subscription(identity=(42, 'bx1')),
                _seed_state(),
            ],
        )

        first = await _pass(monkeypatch, db, fake)
        assert first['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]

        user = await db.get(User, 1)
        user.status = UserStatus.ACTIVE.value
        await db.commit()

        second = await _pass(monkeypatch, db, fake)

        assert second['enabled'] == 1
        assert fake.disabled_calls == [(42, True), (42, False)]
        assert fake.users[42].status == 'active'
        assert fake.users[42].id == 42


# -------------------------------------------------------------- премиум-пул


@pytest.mark.asyncio
async def test_premium_topup_reenables_user(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(status='disabled')])
    async with memory_session(monkeypatch, TABLES) as db:
        state = _seed_state(total=5 * GIB, used=5 * GIB)
        state.is_limited = True
        await _seed(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), state],
        )
        await db.commit()

        first = await _pass(monkeypatch, db, fake)
        assert first['disabled'] == 0
        assert fake.users[42].status == 'disabled'

        add_extra_bytes(state, 2 * GIB)
        await db.commit()

        second = await _pass(monkeypatch, db, fake)

        assert second['enabled'] == 1
        assert fake.disabled_calls == [(42, False)]
        assert fake.users[42].status == 'active'
        # Остаток пула после докупки: (5 + 2) - 5 = 2 GiB.
        assert fake.users[42].data_limit == 2 * GIB
        assert fake.users[42].id == 42


@pytest.mark.asyncio
async def test_disconnected_premium_squad_disables_and_restore_enables(monkeypatch):
    """Premium-сквад убран из ``connected_squads`` — xdrive выключается.

    Иначе проектор продолжал бы включать пользователя по устаревшей строке
    состояния, которую premium-воркер уже не замеряет: расход xdrive перестал
    бы попадать в пул.
    """
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(),
                _seed_subscription(identity=(42, 'bx1'), squads=[SQUAD2]),
                _seed_state(),
            ],
        )

        first = await _pass(monkeypatch, db, fake)
        assert first['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]

        subscription = await db.get(Subscription, 1)
        subscription.connected_squads = [SQUAD]
        await db.commit()

        second = await _pass(monkeypatch, db, fake)

        assert second['enabled'] == 1
        assert fake.disabled_calls == [(42, True), (42, False)]
        assert fake.users[42].id == 42


@pytest.mark.asyncio
async def test_missing_premium_state_blocks_creation_until_state_exists(monkeypatch):
    """Покупка без строки premium-состояния: xdrive не создаётся до замера."""
    fake = FakePasarguardAPI()
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(monkeypatch, db, [_seed_user(), _seed_tariff(), _seed_subscription()])

        first = await _pass(monkeypatch, db, fake)

        assert first['created'] == 0
        assert fake.create_calls == []
        assert (await db.get(Subscription, 1)).pasarguard_user_id is None

        # Premium-воркер отработал (его будит покупка) и создал строку состояния.
        db.add(_seed_state())
        await db.commit()

        second = await _pass(monkeypatch, db, fake)

        assert second['created'] == 1
        assert fake.create_calls[0]['username'] == 'bx1'


@pytest.mark.asyncio
async def test_premium_period_reset_resets_baseline(monkeypatch):
    """Смена периода обнуляет xdrive-baseline — иначе расход прошлого периода
    попал бы в новый пул."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = _seed_state(total=5 * GIB, used=3 * GIB)
        state.pasarguard_baseline_bytes = 1234
        state.pasarguard_identity_created_at = START
        state.notified_80 = True
        state.notified_90 = True
        await _seed(monkeypatch, db, [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), state])

        start_new_period(state, period_start_at=NEW_END, limit_bytes=5 * GIB)
        await db.commit()

        stored = await db.get(SubscriptionPremiumTraffic, state.id)
        assert stored.pasarguard_baseline_bytes is None
        assert stored.pasarguard_identity_created_at is None
        assert stored.notified_80 is False
        assert stored.notified_90 is False


@pytest.mark.asyncio
async def test_closed_premium_access_disables_and_reopened_enables(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        state = _seed_state()
        state.closed_at = NOW
        await _seed(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), state],
        )

        first = await _pass(monkeypatch, db, fake)
        assert first['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]

        state.closed_at = None
        await db.commit()

        second = await _pass(monkeypatch, db, fake)

        assert second['enabled'] == 1
        assert fake.disabled_calls == [(42, True), (42, False)]
        assert fake.users[42].id == 42


# --------------------------------------------------------------------- grace


@pytest.mark.asyncio
async def test_grace_disables_user(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    open_ids: set[int] = {1}

    async def _grace_ids(_db):
        return set(open_ids)

    monkeypatch.setattr(sync_module, 'get_open_grace_subscription_ids', _grace_ids)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [_seed_user(), _seed_tariff(), _seed_subscription(identity=(42, 'bx1')), _seed_state()],
        )

        first = await _pass(monkeypatch, db, fake)
        assert first['disabled'] == 1
        assert fake.disabled_calls == [(42, True)]

        open_ids.clear()
        second = await _pass(monkeypatch, db, fake)

        assert second['enabled'] == 1
        assert fake.disabled_calls == [(42, True), (42, False)]
        assert fake.users[42].id == 42


# --------------------------------------------------------------- исключения


@pytest.mark.asyncio
async def test_trial_without_premium_squad_gets_no_xdrive(monkeypatch):
    fake = FakePasarguardAPI()
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(limits={SQUAD: {'traffic_limit_gb': 0}}),
                _seed_subscription(status='trial', is_trial=True),
            ],
        )

        stats = await _pass(monkeypatch, db, fake)

        assert stats['created'] == 0
        assert fake.create_calls == []
        assert (await db.get(Subscription, 1)).pasarguard_user_id is None


@pytest.mark.asyncio
async def test_multi_squad_tariff_gets_no_xdrive(monkeypatch):
    fake = FakePasarguardAPI()
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(
                    limits={SQUAD: {'traffic_limit_gb': 5}, SQUAD2: {'traffic_limit_gb': 5}},
                ),
                _seed_subscription(squads=[SQUAD, SQUAD2]),
                _seed_state(squad=SQUAD),
                _seed_state(subscription_id=1, squad=SQUAD2),
            ],
        )

        stats = await _pass(monkeypatch, db, fake)

        assert stats['created'] == 0
        assert fake.create_calls == []
        assert (await db.get(Subscription, 1)).pasarguard_user_id is None


@pytest.mark.asyncio
async def test_device_limit_change_does_not_touch_pasarguard(monkeypatch):
    fake = FakePasarguardAPI([_panel_user()])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                _seed_tariff(),
                _seed_subscription(identity=(42, 'bx1'), device_limit=1),
                _seed_state(),
            ],
        )
        subscription = await db.get(Subscription, 1)
        subscription.device_limit = 4
        await db.commit()

        stats = await _pass(monkeypatch, db, fake)

        # Устройства — забота Remnawave: в PasarGuard писать нечего.
        assert stats['checked'] == 1
        assert fake.create_calls == []
        assert fake.update_calls == []
        assert fake.disabled_calls == []
        assert fake.revoke_calls == []
        assert fake.delete_calls == []


@pytest.mark.asyncio
async def test_feature_flag_off_disables_all_known_users(monkeypatch):
    fake = FakePasarguardAPI([_panel_user(42, 1), _panel_user(43, 2)])
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            monkeypatch,
            db,
            [
                _seed_user(),
                # Частично-уникальный индекс (user_id, tariff_id) для живых
                # подписок: у каждой строки свой тариф, как в реальном мультитарифе.
                _seed_tariff(1),
                _seed_tariff(2),
                _seed_tariff(3),
                _seed_subscription(1, identity=(42, 'bx1'), tariff_id=1),
                _seed_subscription(2, identity=(43, 'bx2'), tariff_id=2),
                _seed_subscription(3, tariff_id=3),
                _seed_state(1),
                _seed_state(2),
                _seed_state(3),
            ],
        )

        stats = await _pass(monkeypatch, db, fake, enabled=False)

        assert stats['created'] == 0
        assert sorted(fake.disabled_calls) == [(42, True), (43, True)]
        assert fake.create_calls == []
        assert (await db.get(Subscription, 3)).pasarguard_user_id is None
