"""Горячие пути бота дёргают проектор PasarGuard.

Проектор гарантирует сходимость и сам по себе, но эти вызовы сокращают
задержку между событием в боте и панелью: revoke — синхронный (иначе старый
UUID живёт до следующего прохода), удаление — durable-интент до hard-delete,
остальное — дешёвый nudge.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.services.pasarguard_sync_service as sync_module
import app.services.subscription_service as subscription_service_module
from app.config import settings
from app.database.models import (
    CasheraSubscription,
    DiscountOffer,
    LavaSubscription,
    PasarguardDeleteIntent,
    PlategaSubscription,
    SentNotification,
    Subscription,
    SubscriptionEvent,
    SubscriptionServer,
    SubscriptionTemporaryAccess,
    TrafficPurchase,
    User,
    UserStatus,
)
from app.external.pasarguard_api import PasarguardAPIError
from app.services import subscription_deletion_service as deletion
from app.services.pasarguard_sync_service import pasarguard_sync_service
from app.services.premium_traffic_service import PremiumTrafficService, _Target
from app.services.subscription_service import SubscriptionService
from app.utils.premium_traffic import PremiumSquadConfig
from tests.fixtures.sqlite_memory import memory_session


SQUAD = '11111111-2222-3333-4444-555555555555'
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
END = NOW + timedelta(days=30)

# db.delete() подписки подтягивает все её связи — без этих таблиц падает SELECT.
DELETION_TABLES = (
    User.__table__,
    Subscription.__table__,
    DiscountOffer.__table__,
    SubscriptionTemporaryAccess.__table__,
    TrafficPurchase.__table__,
    PlategaSubscription.__table__,
    LavaSubscription.__table__,
    CasheraSubscription.__table__,
    SentNotification.__table__,
    SubscriptionEvent.__table__,
    SubscriptionServer.__table__,
    PasarguardDeleteIntent.__table__,
)


class _FakePasarguardAPI:
    """Минимальная поверхность, нужная синхронному revoke."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.revoke_calls: list[int] = []
        self.closed = False

    async def revoke_sub(self, user_id: int):
        self.revoke_calls.append(user_id)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=user_id, username='bx1')

    async def close(self) -> None:
        self.closed = True


class _FakeRemnaWaveAPI:
    def __init__(self, url: str = 'https://sub.example/new-link') -> None:
        self.url = url
        self.revoked: list[int] = []

    async def revoke_user_subscription(self, panel_user_id: int):
        self.revoked.append(panel_user_id)
        return SimpleNamespace(
            short_uuid='short-uuid',
            subscription_url=self.url,
            happ_crypto_link='happ-crypto',
        )


class _Db:
    """Сессия без SQL: горячие пути в этих тестах только flush/commit-ят."""

    def __init__(self, *, flush_error: Exception | None = None) -> None:
        self.commits = 0
        self.flushes = 0
        self.refreshes = 0
        self.flush_error = flush_error

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self, *args, **kwargs) -> None:
        self.flushes += 1
        if self.flush_error is not None:
            raise self.flush_error

    async def refresh(self, *args, **kwargs) -> None:
        self.refreshes += 1

    async def rollback(self) -> None:
        pass


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _TrafficDb:
    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self, *args, **kwargs) -> None:
        self.flushes += 1

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()


def _user(*, panel_user_id: int | None = 5, status: str = UserStatus.ACTIVE.value) -> User:
    return User(
        id=1,
        telegram_id=1000,
        first_name='U',
        language='ru',
        status=status,
        remnawave_id=panel_user_id,
        balance_kopeks=0,
    )


def _subscription(
    *,
    status: str = 'active',
    end_date: datetime = END,
    pasarguard_identity: tuple[int, str] | None = (42, 'bx1'),
    remnawave_id: int | None = None,
    is_trial: bool = False,
) -> Subscription:
    extra = {}
    if pasarguard_identity is not None:
        extra = {
            'pasarguard_user_id': pasarguard_identity[0],
            'pasarguard_username': pasarguard_identity[1],
            'pasarguard_created_at': NOW - timedelta(days=1),
        }
    subscription = Subscription(
        id=1,
        user_id=1,
        remnawave_short_id='short1',
        status=status,
        is_trial=is_trial,
        start_date=NOW - timedelta(days=10),
        end_date=end_date,
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=1,
        connected_squads=[SQUAD],
        tariff_id=None,
        remnawave_id=remnawave_id,
        **extra,
    )
    # Связь не должна тянуться лениво из async-контекста.
    subscription.tariff = None
    return subscription


def _remnawave_client(api: _FakeRemnaWaveAPI):
    @contextlib.asynccontextmanager
    async def _client(_self):
        yield api

    return _client


def _record_nudges(monkeypatch) -> list[int | None]:
    calls: list[int | None] = []

    def _nudge(subscription_id: int | None = None) -> None:
        calls.append(subscription_id)

    monkeypatch.setattr(pasarguard_sync_service, 'nudge', _nudge)
    return calls


# ---------------------------------------------------------------- revoke ----


@pytest.mark.asyncio
async def test_revoke_calls_pasarguard_and_records_timestamp(monkeypatch):
    fake_pg = _FakePasarguardAPI()
    monkeypatch.setattr(sync_module, '_make_api', lambda: fake_pg)
    fake_panel = _FakeRemnaWaveAPI()
    monkeypatch.setattr(SubscriptionService, 'get_api_client', _remnawave_client(fake_panel))
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', _async_return(_user()))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)

    subscription = _subscription()
    db = _Db()

    url = await SubscriptionService().revoke_subscription(db, subscription)

    assert url == 'https://sub.example/new-link'
    assert fake_panel.revoked == [5]
    assert fake_pg.revoke_calls == [42]
    assert subscription.pasarguard_last_revoke_at is not None
    assert subscription.pasarguard_revoke_pending_at is None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_revoke_failure_sets_pending_and_does_not_break_remnawave_revoke(monkeypatch):
    fake_pg = _FakePasarguardAPI(error=PasarguardAPIError('panel down', status_code=500))
    monkeypatch.setattr(sync_module, '_make_api', lambda: fake_pg)
    monkeypatch.setattr(SubscriptionService, 'get_api_client', _remnawave_client(_FakeRemnaWaveAPI()))
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', _async_return(_user()))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)

    subscription = _subscription()

    url = await SubscriptionService().revoke_subscription(_Db(), subscription)

    assert url == 'https://sub.example/new-link'
    assert fake_pg.revoke_calls == [42]
    assert subscription.pasarguard_revoke_pending_at is not None
    assert subscription.pasarguard_last_revoke_at is None


@pytest.mark.asyncio
async def test_malformed_pasarguard_url_never_breaks_the_user_facing_revoke(monkeypatch):
    """`_make_api` падает на кривом URL — revoke обязан остаться безопасным.

    Remnawave-revoke к этому моменту уже закоммичен; исключение из
    `revoke_pasarguard_subscription` перехватил бы широкий except
    `revoke_subscription`, и человек остался бы без новой ссылки.
    """
    monkeypatch.setattr(settings, 'PASARGUARD_API_URL', 'http://[::1')
    monkeypatch.setattr(settings, 'PASARGUARD_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'PASARGUARD_XDRIVE_GROUP_ID', 7)
    monkeypatch.setattr(SubscriptionService, 'get_api_client', _remnawave_client(_FakeRemnaWaveAPI()))
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', _async_return(_user()))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)

    subscription = _subscription()

    url = await SubscriptionService().revoke_subscription(_Db(), subscription)

    assert url == 'https://sub.example/new-link'
    assert subscription.pasarguard_revoke_pending_at is not None


# ---------------------------------------------------------------- delete ----


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


@pytest.mark.asyncio
async def test_delete_enqueues_intent_before_hard_delete(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions',
        _async_return(None),
    )
    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        _async_return(None),
    )
    monkeypatch.setattr(
        'app.services.payment.lava.cancel_lava_recurring_for_subscription_safe',
        _async_return(None),
    )
    monkeypatch.setattr(
        'app.services.cashera_recurring_cancel.cancel_cashera_recurring_for_subscription_safe',
        _async_return(None),
    )
    monkeypatch.setattr(deletion, 'decrement_subscription_server_counts', _async_return(None))

    async with memory_session(monkeypatch, DELETION_TABLES) as db:
        db.add(_user(panel_user_id=None))
        target = _subscription(status='expired')
        db.add(target)
        await db.commit()

        await deletion.delete_subscription_record(db, target, deleted_by='user:1')

        assert await db.scalar(select(Subscription.id).where(Subscription.id == 1)) is None
        intents = list((await db.execute(select(PasarguardDeleteIntent))).scalars().all())

    assert [(row.subscription_id, row.pasarguard_user_id, row.pasarguard_username) for row in intents] == [
        (1, 42, 'bx1')
    ]


# --------------------------------------------------------------- premium ----


class _TrafficState:
    def __init__(self, *, is_limited: bool) -> None:
        self.is_limited = is_limited
        self.used_bytes = 0
        self.total_limit_bytes = 10 * 1024**3


def _quiet_premium_service(monkeypatch) -> PremiumTrafficService:
    service = PremiumTrafficService()

    async def _push_squads(_db, _api, _target):
        return None

    async def _log_panel_failure(action, **_kwargs):
        await action()
        return True

    async def _notify_exhausted(_target, _state):
        return None

    monkeypatch.setattr(service, '_push_squads', _push_squads)
    monkeypatch.setattr(service, '_log_panel_failure', _log_panel_failure)
    monkeypatch.setattr(service, '_raise_if_deferred', lambda _result: None)
    monkeypatch.setattr(service, '_notify_exhausted', _notify_exhausted)
    return service


def _traffic_target() -> _Target:
    subscription = SimpleNamespace(id=9, updated_at=NOW)
    return _Target(subscription, PremiumSquadConfig(squad_uuid=SQUAD, limit_gb=10), 5, 'xdrive')


@pytest.mark.asyncio
async def test_premium_limit_and_restore_nudge_the_projector(monkeypatch):
    nudges = _record_nudges(monkeypatch)
    service = _quiet_premium_service(monkeypatch)
    db = _TrafficDb()

    limited = await service._limit_squad(db, object(), _traffic_target(), _TrafficState(is_limited=False))
    restored = await service._restore_squad(db, object(), _traffic_target(), _TrafficState(is_limited=True))

    assert limited is True
    assert restored is True
    assert nudges == [9, 9]


# ---------------------------------------------------------------- create ----


@pytest.mark.asyncio
async def test_create_remnawave_user_nudges_the_projector(monkeypatch):
    nudges = _record_nudges(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', _async_return(_user(panel_user_id=None)))
    monkeypatch.setattr(subscription_service_module, 'resolve_panel_user_tag', lambda _subscription: None)
    monkeypatch.setattr(
        subscription_service_module,
        'resolve_hwid_device_limit_for_payload',
        lambda _subscription: None,
    )
    monkeypatch.setattr(
        'app.services.grace_access_runtime.lock_grace_sensitive_panel_updates',
        _async_return(set()),
    )
    monkeypatch.setattr(
        SubscriptionService,
        'validate_and_clean_subscription',
        _async_return(True),
    )
    monkeypatch.setattr(SubscriptionService, 'get_api_client', _remnawave_client(_FakeRemnaWaveAPI()))

    async def _push(_self, _api, _user, _subscription, **_kwargs):
        return SimpleNamespace(
            id=77,
            short_uuid='short-uuid',
            subscription_url='https://sub.example/new-link',
            happ_crypto_link='happ-crypto',
        )

    monkeypatch.setattr(SubscriptionService, '_create_or_update_remnawave_user_single', _push)

    async def _free(_self, _db, _subscription, _panel_id):
        return True

    monkeypatch.setattr(SubscriptionService, '_panel_id_is_free_for', _free)

    subscription = _subscription(pasarguard_identity=None, remnawave_id=None)
    db = _Db()

    result = await SubscriptionService().create_remnawave_user(db, subscription)

    assert result is not None
    assert result.id == 77
    assert db.commits == 1
    assert nudges == [1]
