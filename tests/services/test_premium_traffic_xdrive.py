"""Учёт xdrive-трафика в премиум-пуле.

Бот не доверяет `used_traffic` PasarGuard для периода: он накопленный и не
сбрасывается вместе с premium-периодом. Считается разница lifetime-счётчика
от baseline, снятого при первой встрече или при смене личности.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database.crud.premium_traffic import start_new_period
from app.external.pasarguard_api import PasarguardUser
from app.services import premium_traffic_service
from app.services.premium_traffic_service import PremiumTrafficService, _Target
from app.utils.premium_traffic import BYTES_IN_GB, PremiumSquadConfig


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER_SQUAD = '82a12389-14d6-40c6-b320-4674f6bbb344'
PANEL_USER_ID = 42
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
PERIOD_START = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
IDENTITY = NOW - timedelta(days=2)


class _State:
    """Лёгкий двойник состояния: воркеру нужны только эти поля."""

    def __init__(
        self,
        *,
        limit_gb: int = 10,
        used_bytes: int = 0,
        baseline_bytes: int | None = None,
        pasarguard_baseline_bytes: int | None = None,
        pasarguard_identity_created_at: datetime | None = None,
    ) -> None:
        self.limit_bytes = limit_gb * BYTES_IN_GB
        self.extra_bytes = 0
        self.used_bytes = used_bytes
        self.baseline_bytes = baseline_bytes
        self.pasarguard_baseline_bytes = pasarguard_baseline_bytes
        self.pasarguard_identity_created_at = pasarguard_identity_created_at
        self.is_limited = False
        self.notified_80 = False
        self.notified_90 = False
        self.closed_at = None
        self.period_start_at = PERIOD_START
        self.panel_reset_ack_at = None
        self.last_checked_at = None

    @property
    def total_limit_bytes(self) -> int:
        return self.limit_bytes + self.extra_bytes

    @property
    def is_exhausted(self) -> bool:
        return self.total_limit_bytes > 0 and self.used_bytes >= self.total_limit_bytes


def _subscription(*, squad_count: int = 1, created_at: datetime | None = IDENTITY) -> SimpleNamespace:
    limits = {SQUAD: {'traffic_limit_gb': 10}}
    connected = [SQUAD]
    if squad_count > 1:
        limits[OTHER_SQUAD] = {'traffic_limit_gb': 5}
        connected.append(OTHER_SQUAD)
    return SimpleNamespace(
        id=1,
        user=SimpleNamespace(telegram_id=555, language='ru', remnawave_id=PANEL_USER_ID),
        connected_squads=connected,
        start_date=NOW - timedelta(days=10),
        tariff=SimpleNamespace(traffic_reset_mode='MONTH', server_traffic_limits=limits),
        remnawave_id=PANEL_USER_ID,
        is_trial=False,
        pasarguard_created_at=created_at,
    )


def _target(subscription: SimpleNamespace | None = None) -> _Target:
    subscription = subscription or _subscription()
    return _Target(subscription, PremiumSquadConfig(squad_uuid=SQUAD, limit_gb=10), PANEL_USER_ID, 'xdrive')


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Db:
    async def commit(self) -> None:
        pass

    async def flush(self) -> None:
        pass

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()


# ---- _xdrive_delta ---------------------------------------------------------


def test_delta_is_lifetime_minus_baseline():
    service = PremiumTrafficService()
    state = _State(pasarguard_baseline_bytes=100, pasarguard_identity_created_at=IDENTITY)

    delta = service._xdrive_delta(state, _subscription(), (50, 250))

    assert delta == 150


def test_first_observation_sets_baseline_and_returns_zero():
    service = PremiumTrafficService()
    state = _State()

    assert service._xdrive_delta(state, _subscription(), (50, 250)) == 0
    assert state.pasarguard_baseline_bytes == 250
    assert state.pasarguard_identity_created_at == IDENTITY


def test_identity_recreation_resets_baseline_once():
    service = PremiumTrafficService()
    state = _State(
        pasarguard_baseline_bytes=100,
        pasarguard_identity_created_at=IDENTITY - timedelta(days=1),
    )
    subscription = _subscription(created_at=IDENTITY)

    # Личность пересоздана: старый baseline относится к чужому счётчику.
    assert service._xdrive_delta(state, subscription, (50, 250)) == 0
    assert state.pasarguard_baseline_bytes == 250
    assert state.pasarguard_identity_created_at == IDENTITY

    # Маркер обновлён — следующий замер уже настоящая дельта, а не ноль.
    assert service._xdrive_delta(state, subscription, (80, 300)) == 50


def test_lifetime_below_baseline_resets_baseline():
    service = PremiumTrafficService()
    state = _State(pasarguard_baseline_bytes=300, pasarguard_identity_created_at=IDENTITY)

    # Панель сбросила lifetime: дельта не может быть отрицательной.
    assert service._xdrive_delta(state, _subscription(), (10, 200)) == 0
    assert state.pasarguard_baseline_bytes == 200
    assert state.pasarguard_identity_created_at == IDENTITY

    assert service._xdrive_delta(state, _subscription(), (20, 250)) == 50


def test_absent_pasarguard_usage_returns_zero():
    service = PremiumTrafficService()
    state = _State(pasarguard_baseline_bytes=100, pasarguard_identity_created_at=IDENTITY)

    assert service._xdrive_delta(state, _subscription(), None) == 0
    # Отсутствие данных не сдвигает baseline: следующий проход досчитает.
    assert state.pasarguard_baseline_bytes == 100
    assert state.pasarguard_identity_created_at == IDENTITY


def test_period_reset_clears_pasarguard_baseline():
    state = _State(pasarguard_baseline_bytes=100, pasarguard_identity_created_at=IDENTITY)

    start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=5 * BYTES_IN_GB)

    assert state.pasarguard_baseline_bytes is None
    assert state.pasarguard_identity_created_at is None


# ---- _apply_usage ----------------------------------------------------------


def _patch_state(monkeypatch, state):
    async def get_state(_db, _subscription_id, _squad_uuid, for_update=False):
        return state

    monkeypatch.setattr('app.database.crud.premium_traffic.get_state', get_state)


async def test_apply_usage_adds_xdrive_delta_to_squad_usage(monkeypatch):
    service = PremiumTrafficService()
    state = _State(pasarguard_baseline_bytes=100, pasarguard_identity_created_at=IDENTITY)
    _patch_state(monkeypatch, state)

    outcome = await service._apply_usage(
        _Db(),
        object(),
        _target(),
        used_bytes=1000,
        period_start=PERIOD_START,
        now=NOW,
        pasarguard_usage=(50, 250),
    )

    assert outcome is None
    assert state.used_bytes == 1000 + 150


async def test_multi_squad_subscription_gets_zero_delta(monkeypatch):
    service = PremiumTrafficService()
    state = _State(pasarguard_baseline_bytes=100, pasarguard_identity_created_at=IDENTITY)
    _patch_state(monkeypatch, state)

    outcome = await service._apply_usage(
        _Db(),
        object(),
        _target(_subscription(squad_count=2)),
        used_bytes=1000,
        period_start=PERIOD_START,
        now=NOW,
        pasarguard_usage=(50, 5000),
    )

    assert outcome is None
    assert state.used_bytes == 1000


# ---- _fetch_pasarguard_usage ----------------------------------------------


class _FakePasarguardApi:
    def __init__(self, users: list[PasarguardUser]) -> None:
        self.users = users
        self.calls: list[tuple[int, int]] = []

    async def list_users(self, offset: int = 0, limit: int = 200) -> list[PasarguardUser]:
        self.calls.append((offset, limit))
        return self.users[offset : offset + limit]


def _pasarguard_user(user_id: int, username: str, used: int, lifetime: int) -> PasarguardUser:
    return PasarguardUser(
        id=user_id,
        username=username,
        status='active',
        expire=None,
        data_limit=None,
        used_traffic=used,
        lifetime_used_traffic=lifetime,
        group_ids=[],
        note=None,
    )


async def test_fetch_maps_prefixed_usernames_and_paginates(monkeypatch):
    service = PremiumTrafficService()
    api = _FakePasarguardApi(
        [
            _pasarguard_user(1, 'bx7', 100, 300),
            _pasarguard_user(2, 'someone9', 10, 20),
            _pasarguard_user(3, 'bx-not-a-number', 1, 2),
        ]
    )
    monkeypatch.setattr(premium_traffic_service.settings, 'XDRIVE_ENABLED', True)
    monkeypatch.setattr(premium_traffic_service.settings, 'PASARGUARD_API_URL', 'http://panel')
    monkeypatch.setattr(premium_traffic_service.settings, 'PASARGUARD_API_KEY', 'test-key')
    monkeypatch.setattr(premium_traffic_service.settings, 'PASARGUARD_XDRIVE_GROUP_ID', 1)
    monkeypatch.setattr(premium_traffic_service.settings, 'PASARGUARD_USERNAME_PREFIX', 'bx')
    monkeypatch.setattr(premium_traffic_service.settings, 'PASARGUARD_SYNC_BATCH_SIZE', 2)

    usage = await service._fetch_pasarguard_usage(api)

    assert usage == {7: (100, 300)}
    assert api.calls == [(0, 2), (2, 2)]
