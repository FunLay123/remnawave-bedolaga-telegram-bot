"""Снимок панели в SubscriptionService: один GET вместо двух на открытие экрана.

Экран подписки раньше делал GET пользователя дважды — в
``sync_subscription_usage`` и в ``ensure_subscription_synced``. Сервис теперь
умеет отдать готовый снимок и применить из него трафик, а синхронизация —
доверять загруженному пользователю вместо повторной проверки существования.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, User
from app.services import panel_snapshot_cache
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    panel_snapshot_cache.invalidate_all()
    yield
    panel_snapshot_cache.invalidate_all()


def _panel_user(used_traffic_bytes: int = 5 * 1024**3):
    return SimpleNamespace(used_traffic_bytes=used_traffic_bytes)


def _patch_api(monkeypatch, api) -> None:
    @asynccontextmanager
    async def fake_client(self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', fake_client)


def _forbid_api(monkeypatch) -> list[int]:
    calls: list[int] = []

    @asynccontextmanager
    async def fake_client(self):
        calls.append(1)
        yield SimpleNamespace()

    monkeypatch.setattr(SubscriptionService, 'get_api_client', fake_client)
    return calls


def _api(user: object, devices: object) -> SimpleNamespace:
    return SimpleNamespace(
        get_user_by_id=AsyncMock(return_value=user),
        get_user_devices=AsyncMock(return_value=devices),
    )


@pytest.mark.asyncio
async def test_snapshot_fetches_panel_once_and_serves_repeat_from_cache(monkeypatch):
    user = _panel_user()
    devices = {'total': 1, 'devices': []}
    api = _api(user, devices)
    _patch_api(monkeypatch, api)
    service = SubscriptionService()

    first = await service.get_panel_snapshot(42)
    second = await service.get_panel_snapshot(42)

    assert first == (user, devices)
    assert second == (user, devices)
    api.get_user_by_id.assert_awaited_once_with(42)
    api.get_user_devices.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_snapshot_refetches_after_invalidate_all(monkeypatch):
    user = _panel_user()
    api = _api(user, {'total': 0, 'devices': []})
    _patch_api(monkeypatch, api)
    service = SubscriptionService()

    await service.get_panel_snapshot(42)
    panel_snapshot_cache.invalidate_all()
    await service.get_panel_snapshot(42)

    assert api.get_user_by_id.await_count == 2
    assert api.get_user_devices.await_count == 2


@pytest.mark.asyncio
async def test_devices_failure_caches_user_without_devices(monkeypatch):
    user = _panel_user()
    api = _api(user, None)
    api.get_user_devices.side_effect = RuntimeError('panel devices down')
    _patch_api(monkeypatch, api)
    service = SubscriptionService()

    first = await service.get_panel_snapshot(42)
    second = await service.get_panel_snapshot(42)

    assert first == (user, None)
    assert second == (user, None)
    api.get_user_by_id.assert_awaited_once_with(42)
    api.get_user_devices.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_missing_panel_user_is_cached_without_devices(monkeypatch):
    api = _api(None, None)
    _patch_api(monkeypatch, api)
    service = SubscriptionService()

    first = await service.get_panel_snapshot(42)
    second = await service.get_panel_snapshot(42)

    assert first == (None, None)
    assert second == (None, None)
    api.get_user_by_id.assert_awaited_once_with(42)
    api.get_user_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_error_propagates_and_is_not_cached(monkeypatch):
    api = _api(None, None)
    api.get_user_by_id.side_effect = RuntimeError('panel down')
    _patch_api(monkeypatch, api)
    service = SubscriptionService()

    with pytest.raises(RuntimeError, match='panel down'):
        await service.get_panel_snapshot(42)

    assert panel_snapshot_cache.get(42) is None


@pytest.mark.asyncio
async def test_apply_panel_user_usage_writes_traffic_without_panel_access():
    db = SimpleNamespace(commit=AsyncMock())
    subscription = SimpleNamespace(id=7, traffic_used_gb=0.0)

    applied = await SubscriptionService().apply_panel_user_usage(db, subscription, _panel_user(3 * 1024**3))

    assert applied is True
    assert subscription.traffic_used_gb == pytest.approx(3.0)
    db.commit.assert_awaited_once()


def _user(remnawave_id: int | None = 4242) -> User:
    return User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
        remnawave_id=remnawave_id,
    )


def _subscription(*, remnawave_id: int | None = 4242, subscription_url: str | None = 'https://panel.test/sub'):
    now = datetime.now(UTC)
    return Subscription(
        remnawave_short_id='snap1',
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now,
        end_date=now + timedelta(days=3),
        traffic_limit_gb=10,
        device_limit=1,
        connected_squads=[],
        remnawave_id=remnawave_id,
        subscription_url=subscription_url,
    )


def _multi_tariff(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', enabled)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs' if enabled else 'classic')


@pytest.mark.asyncio
async def test_loaded_panel_user_skips_existence_get(monkeypatch):
    _multi_tariff(monkeypatch, False)
    api_calls = _forbid_api(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user())
        subscription = _subscription()
        db.add(subscription)
        await db.commit()

        result = await SubscriptionService().ensure_subscription_synced(
            db,
            subscription,
            panel_user=_panel_user(),
            panel_user_loaded=True,
        )

    assert result == (True, None)
    assert api_calls == []


@pytest.mark.asyncio
async def test_loaded_missing_panel_user_triggers_sync(monkeypatch):
    _multi_tariff(monkeypatch, False)
    _forbid_api(monkeypatch)
    update = AsyncMock(return_value='updated')
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', update)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user())
        subscription = _subscription()
        db.add(subscription)
        await db.commit()

        result = await SubscriptionService().ensure_subscription_synced(
            db,
            subscription,
            panel_user=None,
            panel_user_loaded=True,
        )

    assert result == (True, None)
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_path_still_checks_panel_user(monkeypatch):
    _multi_tariff(monkeypatch, False)
    api = _api(_panel_user(), None)
    _patch_api(monkeypatch, api)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user())
        subscription = _subscription()
        db.add(subscription)
        await db.commit()

        result = await SubscriptionService().ensure_subscription_synced(db, subscription)

    assert result == (True, None)
    api.get_user_by_id.assert_awaited_once_with(4242)
