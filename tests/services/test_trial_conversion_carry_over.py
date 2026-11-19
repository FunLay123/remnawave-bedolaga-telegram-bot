"""Остаток триала переносится в платный период ровно один раз.

Классическая покупка (`MiniAppSubscriptionPurchaseService.submit_purchase`)
раньше при конверсии триала считала базу продления от его `end_date` и
добавляла остаток ещё и в `bonus_period` — остаток триала учитывался дважды.

Теперь все пути используют общий помощник `get_trial_carry_over_delta`:
в `days_only` с известной базовой датой переносится только реферальный хвост,
а платное продление по-прежнему идёт от `end_date`.
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


def _subscription(
    *,
    is_trial: bool,
    start_date: datetime,
    end_date: datetime,
    base_trial_end_date: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        user_id=1,
        is_trial=is_trial,
        start_date=start_date,
        end_date=end_date,
        base_trial_end_date=base_trial_end_date,
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


def _enable_days_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)


def _patch_purchase_dependencies(monkeypatch) -> AsyncMock:
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
    # Сервис читает именно триалы через get_active_trial_subscriptions_by_user_id;
    # патч другого имени проходил только потому, что MagicMock молча итерировался пустым.
    trials_lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(
        'app.database.crud.subscription.get_active_trial_subscriptions_by_user_id',
        trials_lookup,
    )
    monkeypatch.setattr(
        'app.services.referral_purchase_service.register_first_purchase_after_purchase',
        AsyncMock(),
    )
    return trials_lookup


async def _submit(monkeypatch, subscription: SimpleNamespace):
    trials_lookup = _patch_purchase_dependencies(monkeypatch)
    db = AsyncMock(spec=AsyncSession)
    locked = MagicMock()
    locked.scalar_one_or_none.return_value = subscription
    db.execute = AsyncMock(return_value=locked)
    context = SimpleNamespace(user=_user(), subscription=subscription, payload={})
    result = await MiniAppSubscriptionPurchaseService().submit_purchase(db, context, _pricing())
    return db, result, trials_lookup


async def test_trial_conversion_carries_only_referral_tail(monkeypatch):
    """База 1 день, реферальный хвост 19: покупка 30 дней даёт now+49, не now+50."""
    _enable_days_only(monkeypatch)
    before = datetime.now(UTC)
    subscription = _subscription(
        is_trial=True,
        start_date=before,
        end_date=before + timedelta(days=20),
        base_trial_end_date=before + timedelta(days=1),
    )

    db, result, trials_lookup = await _submit(monkeypatch, subscription)
    after = datetime.now(UTC)

    assert result['was_trial_conversion'] is True
    # 30 купленных + только 19 реферальных; базовый день не переносится.
    assert before + timedelta(days=49) <= subscription.end_date <= after + timedelta(days=49)
    assert subscription.end_date < before + timedelta(days=50)
    assert before <= subscription.start_date <= after
    db.commit.assert_awaited()
    trials_lookup.assert_awaited_once()


async def test_referral_tail_carries_even_when_trial_add_is_disabled(monkeypatch):
    """``TRIAL_ADD_REMAINING_DAYS_TO_PAID=false`` не сжигает реферальный хвост days_only."""
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    before = datetime.now(UTC)
    subscription = _subscription(
        is_trial=True,
        start_date=before,
        end_date=before + timedelta(days=20),
        base_trial_end_date=before + timedelta(days=1),
    )

    _, result, _ = await _submit(monkeypatch, subscription)
    after = datetime.now(UTC)

    assert result['was_trial_conversion'] is True
    assert before + timedelta(days=49) <= subscription.end_date <= after + timedelta(days=49)


async def test_paid_extension_still_extends_from_end_date(monkeypatch):
    before = datetime.now(UTC)
    paid_end = before + timedelta(days=10)
    original_start = before - timedelta(days=20)
    subscription = _subscription(
        is_trial=False,
        start_date=original_start,
        end_date=paid_end,
    )

    _, result, _ = await _submit(monkeypatch, subscription)

    assert result['was_trial_conversion'] is False
    assert subscription.end_date == paid_end + timedelta(days=30)
    assert subscription.start_date == original_start
