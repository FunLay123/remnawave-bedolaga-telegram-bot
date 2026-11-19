"""Старый live-обработчик простого продления переносит только хвост триала.

``handle_simple_subscription_purchase`` регистрируется раньше нового набора
``simple_subscription``-обработчиков и продлевает существующую подписку через
``_extend_existing_subscription``. Раньше тот вручную прибавлял купленные дни к
``end_date``, из-за чего в ``days_only`` базовый бесплатный триал учитывался
повторно. Теперь дата считается общим правилом: платный срок стартует от
момента покупки, переносится только реферальный хвост.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.subscription.purchase as purchase_mod
from app.config import settings
from app.database.crud.subscription import get_trial_carry_over_delta


def _enable_days_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)


def _trial(now: datetime, *, base_days: int = 1, end_days: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        user_id=1,
        status='trial',
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=end_days),
        base_trial_end_date=now + timedelta(days=base_days),
        traffic_limit_gb=10,
        traffic_used_gb=0.0,
        device_limit=1,
        connected_squads=[],
        tariff_id=None,
        updated_at=now,
    )


# ── Расчёт даты (чистый помощник) ──


def test_days_only_known_base_starts_paid_period_from_purchase(monkeypatch):
    """База 1 день + хвост 19; покупка 30 дней даёт now+49, а не old_end+30."""
    _enable_days_only(monkeypatch)
    now = datetime.now(UTC)
    subscription = _trial(now)

    carry = get_trial_carry_over_delta(subscription, now)
    assert carry == timedelta(days=19)

    end = purchase_mod._calculate_simple_extension_end_date(
        subscription,
        30,
        now,
        converted_days_only_trial=True,
        trial_carry_over=carry,
    )
    assert end == now + timedelta(days=49)


@pytest.mark.parametrize('is_trial', [True, False])
def test_legacy_trial_and_paid_extension_keep_old_end_date_math(is_trial):
    """Legacy-триал (без базы) и платная подписка продлеваются от ``end_date``."""
    now = datetime.now(UTC)
    subscription = _trial(now)
    subscription.is_trial = is_trial
    subscription.base_trial_end_date = None

    end = purchase_mod._calculate_simple_extension_end_date(
        subscription,
        30,
        now,
        converted_days_only_trial=False,
    )
    assert end == subscription.end_date + timedelta(days=30)


def test_expired_subscription_extends_from_now():
    now = datetime.now(UTC)
    subscription = _trial(now)
    subscription.end_date = now - timedelta(days=1)

    end = purchase_mod._calculate_simple_extension_end_date(
        subscription,
        30,
        now,
        converted_days_only_trial=False,
    )
    assert end == now + timedelta(days=30)


# ── Реальный live-обработчик ──


async def _run_extend(
    monkeypatch,
    subscription: SimpleNamespace,
    *,
    undo=None,
    promo_percent: int = 0,
    charge=None,
) -> SimpleNamespace:
    user = SimpleNamespace(
        id=1,
        telegram_id=12345,
        language='ru',
        balance_kopeks=200_000,
        promo_offer_discount_percent=promo_percent,
        promo_offer_discount_source='offer' if promo_percent else None,
        promo_offer_discount_expires_at=None,
    )
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
        bot=MagicMock(),
    )
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    class _FakeSubscriptionService:
        async def update_remnawave_user(self, *_args, **_kwargs):
            return True

    notifications = SimpleNamespace(send_subscription_extension_notification=AsyncMock())

    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(
        purchase_mod,
        '_calculate_simple_subscription_price',
        AsyncMock(return_value=(10_000, {})),
    )
    monkeypatch.setattr(
        'app.database.crud.user.subtract_user_balance',
        charge or AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        'app.database.crud.transaction.create_transaction',
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(
        'app.services.referral_purchase_service.register_first_purchase_after_purchase',
        AsyncMock(return_value=SimpleNamespace(status='skipped')),
    )
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', _FakeSubscriptionService)
    monkeypatch.setattr(purchase_mod, 'AdminNotificationService', lambda *_args, **_kwargs: notifications)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(
        'app.services.grace_access_echo.undo_grace_overlay_echo',
        undo or AsyncMock(return_value=set()),
    )

    await purchase_mod._extend_existing_subscription(
        callback=callback,
        db_user=user,
        db=db,
        current_subscription=subscription,
        period_days=30,
        device_limit=1,
        traffic_limit_gb=100,
        squad_uuid='squad-1',
    )
    return user


@pytest.mark.asyncio
async def test_simple_extend_days_only_trial_carries_only_referral_tail(monkeypatch):
    """Live-путь: база 1 + хвост 19 + покупка 30 = now+49, не now+50."""
    _enable_days_only(monkeypatch)
    before = datetime.now(UTC)
    subscription = _trial(before)
    subscription.start_date = before - timedelta(days=5)

    await _run_extend(monkeypatch, subscription)
    after = datetime.now(UTC)

    assert subscription.is_trial is False
    assert before + timedelta(days=49) <= subscription.end_date <= after + timedelta(days=49)
    assert subscription.end_date < before + timedelta(days=50)
    # Платный период стартует от момента покупки, как в общем CRUD.
    assert before <= subscription.start_date <= after


@pytest.mark.asyncio
async def test_simple_extend_undoes_grace_overlay_before_carry_over_snapshot(monkeypatch):
    """Stale grace overlay снимается до снимка хвоста, а не после.

    Если бы дата считалась от ``end_date`` с осевшим оверлеем, хвост был бы
    +39 дней вместо +19 и итог ушёл бы к now+69.
    """
    _enable_days_only(monkeypatch)
    before = datetime.now(UTC)
    subscription = _trial(before)
    clean_end = subscription.end_date
    subscription.end_date = before + timedelta(days=40)  # stale grace overlay

    async def undo(_db, sub):
        sub.end_date = clean_end
        return {'end_date'}

    undo_mock = AsyncMock(side_effect=undo)
    seen_end_dates: list[datetime] = []
    real_delta = purchase_mod.get_trial_carry_over_delta

    def spy_delta(sub, now):
        seen_end_dates.append(sub.end_date)
        return real_delta(sub, now)

    monkeypatch.setattr(purchase_mod, 'get_trial_carry_over_delta', spy_delta)

    await _run_extend(monkeypatch, subscription, undo=undo_mock)
    after = datetime.now(UTC)

    undo_mock.assert_awaited_once()
    assert undo_mock.await_args.args[1] is subscription
    # Снимок дельты увидел уже очищенную дату.
    assert seen_end_dates == [clean_end]
    assert before + timedelta(days=49) <= subscription.end_date <= after + timedelta(days=49)
    assert subscription.end_date < before + timedelta(days=50)


@pytest.mark.asyncio
async def test_simple_extend_legacy_trial_keeps_end_date_math(monkeypatch):
    """Legacy-строка без базовой даты остаётся на прежнем ``end_date + period``."""
    _enable_days_only(monkeypatch)
    before = datetime.now(UTC)
    subscription = _trial(before)
    subscription.base_trial_end_date = None
    old_end = subscription.end_date

    await _run_extend(monkeypatch, subscription)

    assert subscription.is_trial is False
    assert subscription.end_date == old_end + timedelta(days=30)


@pytest.mark.asyncio
async def test_simple_extend_refunds_charge_when_grace_undo_fails(monkeypatch):
    """Сбой после списания обязан вернуть деньги, а не оставить их у бота."""
    _enable_days_only(monkeypatch)
    subscription = _trial(datetime.now(UTC))
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)

    await _run_extend(
        monkeypatch,
        subscription,
        undo=AsyncMock(side_effect=RuntimeError('db down')),
    )

    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_simple_extend_restores_promo_offer_on_refund(monkeypatch):
    """Возврат возвращает и сожжённый при списании promo offer."""
    _enable_days_only(monkeypatch)
    subscription = _trial(datetime.now(UTC))

    async def _consume_promo(_db, user, *_args, **_kwargs):
        user.promo_offer_discount_percent = 0
        return True

    refund = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)

    user = await _run_extend(
        monkeypatch,
        subscription,
        undo=AsyncMock(side_effect=RuntimeError('db down')),
        promo_percent=15,
        charge=AsyncMock(side_effect=_consume_promo),
    )

    refund.assert_awaited_once()
    assert user.promo_offer_discount_percent == 15


@pytest.mark.asyncio
async def test_simple_extend_persists_failed_refund(monkeypatch):
    """Если возврат не прошёл, остаётся failed_refund для ручной обработки."""
    _enable_days_only(monkeypatch)
    subscription = _trial(datetime.now(UTC))
    monkeypatch.setattr('app.database.crud.user.add_user_balance', AsyncMock(return_value=False))
    persist = AsyncMock()
    monkeypatch.setattr('app.handlers.subscription.tariff_purchase._persist_failed_refund', persist)

    await _run_extend(
        monkeypatch,
        subscription,
        undo=AsyncMock(side_effect=RuntimeError('db down')),
    )

    persist.assert_awaited_once()
