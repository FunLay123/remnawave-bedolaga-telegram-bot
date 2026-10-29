from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.payment.lava as lava_module
import app.services.referral_purchase_service as purchase_module
import app.services.remnawave_retry_queue as retry_module
import app.services.subscription_service as subscription_module
from app.config import settings
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


@pytest.mark.asyncio
async def test_wrapper_keeps_rewarded_when_post_commit_raises(monkeypatch):
    """Сбой панели/сети после коммита не превращает покупку в ошибку."""

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', fake_session)
    monkeypatch.setattr(
        purchase_module,
        'register_first_purchase',
        AsyncMock(return_value=FirstPurchaseResult(status='rewarded', referee_days=10, referrer_days=10)),
    )
    monkeypatch.setattr(
        purchase_module,
        '_post_commit_effects',
        AsyncMock(side_effect=RuntimeError('panel down')),
    )

    result = await purchase_module.register_first_purchase_after_purchase(
        referee_id=2,
        subscription_id=10,
        duration_days=30,
        paid_amount_kopeks=10_000,
        source='test',
    )

    assert result.status == 'rewarded'
    assert result.referee_days == 10
    assert result.referrer_days == 10


@pytest.mark.parametrize('setting_name', ['ENABLE_NOTIFICATIONS', 'REFERRAL_NOTIFICATIONS_ENABLED'])
@pytest.mark.asyncio
async def test_post_commit_skips_notifications_when_disabled(monkeypatch, setting_name):
    """Выключенные уведомления глушат сообщения, а не синхронизацию и Lava."""
    monkeypatch.setattr(settings, setting_name, False)
    bot = SimpleNamespace(send_message=AsyncMock())

    result = FirstPurchaseResult(
        status='rewarded',
        referee_days=10,
        referrer_days=10,
        referee_id=2,
        referrer_id=1,
    )

    await _post_commit_effects(result, referee_id=2, bot=bot)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_commit_notifies_with_actual_granted_days(monkeypatch):
    """В тексте стоят реально выданные дни, а не захардкоженные 10."""

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    async def fake_get_user_by_id(db, user_id):
        return SimpleNamespace(id=user_id, telegram_id=1000 + user_id, language='ru')

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', fake_session)
    monkeypatch.setattr('app.database.crud.user.get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(
        'app.localization.texts.get_texts',
        lambda language=None: SimpleNamespace(t=lambda key, default: default),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    result = FirstPurchaseResult(
        status='rewarded',
        referee_days=7,
        referrer_days=3,
        referee_id=2,
        referrer_id=1,
    )

    await _post_commit_effects(result, referee_id=2, bot=bot)

    assert bot.send_message.await_count == 2
    referee_text = bot.send_message.await_args_list[0].kwargs['text']
    referrer_text = bot.send_message.await_args_list[1].kwargs['text']
    assert 'Вам +7 дней' in referee_text
    assert 'Вам +3 дня к текущей подписке.' in referrer_text
