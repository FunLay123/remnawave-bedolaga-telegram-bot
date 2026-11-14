"""Xdrive: поиск entitlement-цели и кеш конфигов PasarGuard.

Поиск цели — горячий путь выдачи подписки: он повторяет те же правила, что и
проектор, но читает их из БД, а не из панели. Кеш провайдера обязан учитывать
поколение личности: revoke ротирует креды, и старый конфиг клиенту уже не
подходит.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import (
    Subscription,
    SubscriptionPremiumTraffic,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
)
from app.external.pasarguard_api import PasarguardAPIError
from app.services import xdrive_service
from app.services.xdrive_service import PasarguardConfigProvider, XdriveTarget, lookup_xdrive_target
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SHORT_UUID = 'Ab3dEf9hJk'
SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'


class FakePasarguardApi:
    def __init__(self, payload: bytes = b'[]', error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[int, str]] = []
        self.closed = False

    async def get_subscription_config(self, user_id: int, client_type: str = 'xray') -> bytes:
        self.calls.append((user_id, client_type))
        if self.error is not None:
            raise self.error
        return self.payload

    async def close(self) -> None:
        self.closed = True


# ---- PasarguardConfigProvider ----------------------------------------------


async def test_provider_caches_by_user_and_revoke_version():
    api = FakePasarguardApi(payload=b'[{"outbounds":[{"protocol":"vless"}]}]')
    provider = PasarguardConfigProvider(api, cache_seconds=60)
    target = XdriveTarget(subscription_id=1, pasarguard_user_id=42, cache_version='created|revoke-a')

    assert await provider.get(target) == api.payload
    assert await provider.get(target) == api.payload
    assert api.calls == [(42, 'xray')]

    # Новое поколение личности — другой ключ кеша, конфиг перечитывается.
    revoked = XdriveTarget(subscription_id=1, pasarguard_user_id=42, cache_version='created|revoke-b')
    await provider.get(revoked)
    assert api.calls == [(42, 'xray'), (42, 'xray')]

    # Другой пользователь панели — тоже другой ключ.
    other = XdriveTarget(subscription_id=2, pasarguard_user_id=43, cache_version='created|revoke-a')
    await provider.get(other)
    assert api.calls[-1] == (43, 'xray')


async def test_provider_returns_none_on_api_error():
    api = FakePasarguardApi(error=PasarguardAPIError('panel down'))
    provider = PasarguardConfigProvider(api, cache_seconds=60)

    assert await provider.get(XdriveTarget(1, 42, 'v')) is None
    # Ошибка не кешируется: следующий запрос снова идёт в панель.
    assert await provider.get(XdriveTarget(1, 42, 'v')) is None
    assert len(api.calls) == 2


async def test_provider_closes_wrapped_api():
    api = FakePasarguardApi()
    provider = PasarguardConfigProvider(api, cache_seconds=60)

    await provider.aclose()

    assert api.closed is True


# ---- lookup_xdrive_target --------------------------------------------------


class _SessionContext:
    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc) -> bool:
        return False


def _patch_session(monkeypatch, db) -> None:
    monkeypatch.setattr(xdrive_service, 'AsyncSessionLocal', lambda: _SessionContext(db))


def _patch_grace(monkeypatch, subscription_ids: set[int]) -> None:
    async def get_open_grace_subscription_ids(_db):
        return set(subscription_ids)

    monkeypatch.setattr(xdrive_service, 'get_open_grace_subscription_ids', get_open_grace_subscription_ids)


async def _seed(db, *, case: str = 'eligible') -> None:
    db.add(User(id=1, telegram_id=1, status=UserStatus.ACTIVE.value))
    db.add(Tariff(id=1, name='T', server_traffic_limits={SQUAD: {'traffic_limit_gb': 10}}))
    await db.flush()
    db.add(
        Subscription(
            id=1,
            user_id=1,
            tariff_id=1,
            remnawave_short_uuid=SHORT_UUID,
            remnawave_short_id='short',
            status=SubscriptionStatus.ACTIVE.value,
            end_date=NOW + timedelta(days=30),
            pasarguard_user_id=42,
            pasarguard_username='bx1',
            pasarguard_created_at=NOW,
        )
    )
    await db.flush()
    if case != 'missing_state':
        fields = {'limit_bytes': 10 * BYTES_IN_GB, 'period_start_at': NOW}
        if case == 'limited':
            fields['is_limited'] = True
        if case == 'closed':
            fields['closed_at'] = NOW
        db.add(SubscriptionPremiumTraffic(subscription_id=1, squad_uuid=SQUAD, **fields))
    await db.commit()


TABLES = (
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    SubscriptionPremiumTraffic.__table__,
)


@pytest.mark.parametrize('case', ['grace', 'limited', 'closed', 'missing_state'])
async def test_lookup_skips_grace_limited_closed_and_missing_state(monkeypatch, case):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, case=case)
        _patch_session(monkeypatch, db)
        _patch_grace(monkeypatch, {1} if case == 'grace' else set())
        monkeypatch.setattr(xdrive_service.settings, 'XDRIVE_ENABLED', True)

        assert await lookup_xdrive_target([SHORT_UUID]) is None


async def test_lookup_returns_target_for_eligible_subscription(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        _patch_session(monkeypatch, db)
        _patch_grace(monkeypatch, set())
        monkeypatch.setattr(xdrive_service.settings, 'XDRIVE_ENABLED', True)

        target = await lookup_xdrive_target([SHORT_UUID])

        assert target is not None
        assert target.subscription_id == 1
        assert target.pasarguard_user_id == 42
        assert target.cache_version


async def test_lookup_cache_version_changes_after_revoke(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        _patch_session(monkeypatch, db)
        _patch_grace(monkeypatch, set())
        monkeypatch.setattr(xdrive_service.settings, 'XDRIVE_ENABLED', True)

        before = await lookup_xdrive_target([SHORT_UUID])

        subscription = await db.get(Subscription, 1)
        subscription.pasarguard_last_revoke_at = NOW + timedelta(hours=1)
        await db.commit()

        after = await lookup_xdrive_target([SHORT_UUID])

        assert before is not None and after is not None
        assert after.cache_version != before.cache_version


async def test_lookup_returns_none_when_feature_is_disabled(monkeypatch):
    def explode():
        raise AssertionError('DB must not be touched when xdrive is off')

    monkeypatch.setattr(xdrive_service.settings, 'XDRIVE_ENABLED', False)
    monkeypatch.setattr(xdrive_service, 'AsyncSessionLocal', explode)

    assert await lookup_xdrive_target([SHORT_UUID]) is None
