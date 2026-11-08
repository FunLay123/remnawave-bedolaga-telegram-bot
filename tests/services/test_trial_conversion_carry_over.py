"""Остаток триала переносится в платный период ровно один раз.

Классическая покупка (`MiniAppSubscriptionPurchaseService.submit_purchase`)
раньше при конверсии триала считала базу продления от его `end_date` и
добавляла остаток ещё и в `bonus_period` — остаток триала учитывался дважды.
Теперь при `was_trial_conversion` база — «сейчас», а обычное продление
платной подписки по-прежнему идёт от её `end_date`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.subscription_purchase_service import MiniAppSubscriptionPurchaseService


class DummyTexts:
    def t(self, key: str, default: str) -> str:
        return default

    def format_price(self, value: int) -> str:
        return f'{value / 100:.0f} ₽'


class DummySubscriptionService:
    async def create_remnawave_user(self, *args, **kwargs):
        return None

    async def update_remnawave_user(self, *args, **kwargs):
        return None


def _pricing(period_days: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        final_total=100_000,
        base_original_total=100_000,
        promo_discount_value=0,
        promo_discount_percent=0,
        months=1,
        server_ids=[],
        server_prices_for_period=[],
        selection=SimpleNamespace(
            period=SimpleNamespace(days=period_days),
            traffic_value=100,
            servers=['squad'],
            devices=1,
        ),
    )


def _subscription(*, is_trial: bool, start_date: datetime, end_date: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        user_id=1,
        is_trial=is_trial,
        start_date=start_date,
        end_date=end_date,
        status='active',
        traffic_limit_gb=0,
        device_limit=1,
        connected_squads=[],
        updated_at=start_date,
        traffic_used_gb=0.0,
        remnawave_id=None,
        autopay_enabled=False,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        telegram_id=12345,
        language='ru',
        balance_kopeks=200_000,
        # Панельный аккаунт уже есть — sync пойдёт в update, не в create.
        remnawave_id=555,
    )


def _patch_purchase_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.get_texts',
        lambda language: DummyTexts(),
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.subtract_user_balance',
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.create_subscription_conversion',
        AsyncMock(),
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.apply_trial_conversion_defaults',
        lambda subscription: None,
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.should_carry_trial_remaining_days',
        lambda: True,
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.SubscriptionService',
        DummySubscriptionService,
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.create_transaction',
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(
        'app.services.grace_access_echo.undo_grace_overlay_echo',
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.deactivate_user_trial_subscriptions',
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        'app.services.referral_purchase_service.register_first_purchase_after_purchase',
        AsyncMock(),
    )


async def _submit(monkeypatch, subscription: SimpleNamespace):
    _patch_purchase_dependencies(monkeypatch)
    db = AsyncMock(spec=AsyncSession)
    locked = MagicMock()
    locked.scalar_one_or_none.return_value = subscription
    db.execute = AsyncMock(return_value=locked)
    context = SimpleNamespace(user=_user(), subscription=subscription, payload={})
    result = await MiniAppSubscriptionPurchaseService().submit_purchase(db, context, _pricing())
    return db, result


async def test_trial_conversion_counts_remaining_trial_once(monkeypatch):
    before = datetime.now(UTC)
    trial_end = before + timedelta(days=2)
    subscription = _subscription(
        is_trial=True,
        start_date=before - timedelta(days=7),
        end_date=trial_end,
    )

    db, result = await _submit(monkeypatch, subscription)

    assert result['was_trial_conversion'] is True
    # 30 купленных дней + 2 оставшихся триальных, но остаток ровно один раз:
    # end = now + 30 + (trial_end - now) == trial_end + 30.
    assert subscription.end_date == trial_end + timedelta(days=30)
    assert before <= subscription.start_date <= datetime.now(UTC)
    db.commit.assert_awaited()


async def test_paid_extension_still_extends_from_end_date(monkeypatch):
    before = datetime.now(UTC)
    paid_end = before + timedelta(days=10)
    original_start = before - timedelta(days=20)
    subscription = _subscription(
        is_trial=False,
        start_date=original_start,
        end_date=paid_end,
    )

    _, result = await _submit(monkeypatch, subscription)

    assert result['was_trial_conversion'] is False
    assert subscription.end_date == paid_end + timedelta(days=30)
    assert subscription.start_date == original_start
