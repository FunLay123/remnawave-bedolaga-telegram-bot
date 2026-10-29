from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.payment.lava as lava_module
import app.services.referral_purchase_service as purchase_module
import app.services.remnawave_retry_queue as retry_module
import app.services.subscription_service as subscription_module
from app.services.referral_purchase_service import FirstPurchaseResult, _post_commit_effects


@pytest.mark.asyncio
async def test_post_commit_syncs_both_subscriptions(monkeypatch):
    subscriptions = {
        10: SimpleNamespace(id=10, user_id=2),
        11: SimpleNamespace(id=11, user_id=1),
    }
    fake_db = SimpleNamespace()
    fake_db.get = AsyncMock(side_effect=lambda model, subscription_id: subscriptions[subscription_id])

    @asynccontextmanager
    async def fake_session():
        yield fake_db

    sync = AsyncMock()
    shift = AsyncMock()
    enqueue = MagicMock()

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', fake_session)
    monkeypatch.setattr(
        subscription_module,
        'SubscriptionService',
        lambda: SimpleNamespace(sync_remnawave_user=sync),
    )
    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', shift)
    monkeypatch.setattr(retry_module.remnawave_retry_queue, 'enqueue', enqueue)

    result = FirstPurchaseResult(
        status='rewarded',
        referee_days=10,
        referrer_days=10,
        referee_subscription_id=10,
        referrer_subscription_id=11,
    )

    await _post_commit_effects(result, referee_id=2)

    assert sync.await_count == 2
    assert shift.await_count == 2
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_post_commit_enqueues_sync_failure_without_raising(monkeypatch):
    subscriptions = {10: SimpleNamespace(id=10, user_id=2)}
    fake_db = SimpleNamespace()
    fake_db.get = AsyncMock(side_effect=lambda model, subscription_id: subscriptions[subscription_id])

    @asynccontextmanager
    async def fake_session():
        yield fake_db

    sync = AsyncMock(side_effect=RuntimeError('panel down'))
    enqueue = MagicMock()

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', fake_session)
    monkeypatch.setattr(
        subscription_module,
        'SubscriptionService',
        lambda: SimpleNamespace(sync_remnawave_user=sync),
    )
    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', AsyncMock())
    monkeypatch.setattr(retry_module.remnawave_retry_queue, 'enqueue', enqueue)

    result = FirstPurchaseResult(
        status='rewarded',
        referee_days=10,
        referrer_days=0,
        referee_subscription_id=10,
    )

    await _post_commit_effects(result, referee_id=2)

    enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_wrapper_runs_post_commit_only_for_rewarded_result(monkeypatch):
    """Маркер короткой покупки не должен трогать панель и расписание Lava."""
    effects = AsyncMock()

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', fake_session)
    monkeypatch.setattr(purchase_module, '_post_commit_effects', effects)

    registered = AsyncMock(return_value=FirstPurchaseResult(status='registered'))
    monkeypatch.setattr(purchase_module, 'register_first_purchase', registered)
    result = await purchase_module.register_first_purchase_after_purchase(
        referee_id=2,
        subscription_id=10,
        duration_days=14,
        paid_amount_kopeks=5_000,
        source='test',
    )
    assert result.status == 'registered'
    effects.assert_not_awaited()

    rewarded = AsyncMock(return_value=FirstPurchaseResult(status='rewarded', referee_days=10))
    monkeypatch.setattr(purchase_module, 'register_first_purchase', rewarded)
    result = await purchase_module.register_first_purchase_after_purchase(
        referee_id=2,
        subscription_id=10,
        duration_days=30,
        paid_amount_kopeks=10_000,
        source='test',
    )
    assert result.status == 'rewarded'
    effects.assert_awaited_once()
