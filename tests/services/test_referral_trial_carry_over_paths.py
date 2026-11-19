"""Прямые пути покупки (бот, Mini App, кабинет) используют единый расчёт остатка.

Эти тесты поднимают реальные entry points на in-memory SQLite: Mini App
``submit_purchase`` (ветка текущей подписки и create-ветка) и кабинетный
``purchase_tariff``. Внешние эффекты панели/оплаты замоканы, но выбор подписок,
конверсия, деактивация триалов и итоговый ``end_date`` — настоящие.

Ключевые инварианты:
- реферальный хвост триала переносится ровно один раз;
- текущая/конвертированная подписка не учитывается повторно;
- хвосты триалов, деактивированных как внешней, так и внутренней
  (внутри ``create_paid_subscription``) чисткой, не теряются.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.cabinet.routes.subscription_modules.purchase as cabinet_purchase_mod
import app.handlers.subscription.purchase as purchase_mod
from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from app.services.pricing_engine import PricingEngine
from app.services.subscription_purchase_service import MiniAppSubscriptionPurchaseService
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _enable_days_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)


class _DummyTexts:
    def t(self, key: str, default: str) -> str:
        return default

    def format_price(self, value: int) -> str:
        return f'{value / 100:.0f} ₽'


class _DummySubscriptionService:
    disabled_panel_ids: list[int] = []

    async def create_remnawave_user(self, *args, **kwargs):
        return MagicMock()

    async def update_remnawave_user(self, *args, **kwargs):
        return MagicMock()

    async def disable_remnawave_user(self, panel_id, *args, **kwargs):
        type(self).disabled_panel_ids.append(panel_id)
        return True


class _DummyAdminNotifications:
    def __init__(self, *args, **kwargs):
        pass

    async def send_subscription_purchase_notification(self, *args, **kwargs):
        return None


def _seed_user(db, *, balance_kopeks: int = 200_000) -> User:
    user = User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=balance_kopeks,
        remnawave_id=555,
    )
    db.add(user)
    return user


def _trial(
    *,
    sub_id: int,
    user_id: int = 1,
    now: datetime,
    tail_days: int,
    status: str = SubscriptionStatus.ACTIVE.value,
    tariff_id: int | None = None,
) -> Subscription:
    """Триал с одномоментной базой: base = now+1, end = base + tail_days."""
    base = now + timedelta(days=1)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=status,
        is_trial=True,
        start_date=now,
        end_date=base + timedelta(days=tail_days),
        base_trial_end_date=base,
        traffic_limit_gb=50,
        device_limit=1,
        tariff_id=tariff_id,
        connected_squads=['squad-1'],
    )


# ── Mini App: current-subscription branch ──


def _miniapp_pricing(days: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        final_total=100_000,
        base_original_total=100_000,
        promo_discount_value=0,
        promo_discount_percent=0,
        months=1,
        server_ids=[],
        server_prices_for_period=[],
        selection=SimpleNamespace(
            period=SimpleNamespace(days=days),
            traffic_value=100,
            devices=1,
            servers=['squad-1'],
        ),
    )


def _patch_miniapp_dependencies(monkeypatch) -> None:
    _DummySubscriptionService.disabled_panel_ids = []
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.get_texts',
        lambda language: _DummyTexts(),
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
        'app.services.subscription_purchase_service.create_transaction',
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.SubscriptionService',
        _DummySubscriptionService,
    )
    monkeypatch.setattr(
        'app.services.subscription_purchase_service.add_subscription_servers',
        AsyncMock(),
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.decrement_subscription_server_counts',
        AsyncMock(),
    )
    monkeypatch.setattr(
        'app.services.referral_purchase_service.register_first_purchase_after_purchase',
        AsyncMock(),
    )
    monkeypatch.setattr(
        'app.services.grace_access_echo.undo_grace_overlay_echo',
        AsyncMock(return_value=set()),
    )


@pytest.mark.asyncio
async def test_miniapp_current_trial_plus_extra_trial_carries_each_tail_once(monkeypatch):
    """Текущий триал (19) + дополнительный (9) + покупка 30 = now+58, ровно один раз."""
    _enable_days_only(monkeypatch)
    _patch_miniapp_dependencies(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    async with memory_session(monkeypatch, TABLES) as db:
        user = _seed_user(db)
        before = datetime.now(UTC)
        current = _trial(sub_id=10, now=before, tail_days=19)
        extra = _trial(
            sub_id=11,
            now=before,
            tail_days=9,
            status=SubscriptionStatus.TRIAL.value,
        )
        db.add_all([current, extra])
        await db.commit()

        current = await db.get(Subscription, 10)
        context = SimpleNamespace(user=user, subscription=current, payload={})
        result = await MiniAppSubscriptionPurchaseService().submit_purchase(db, context, _miniapp_pricing())
        after = datetime.now(UTC)

        end = result['subscription'].end_date
        # 30 purchased + 19 current referral tail + 9 extra tail.
        assert before + timedelta(days=58) <= end <= after + timedelta(days=58)
        # A double count of the current 19-day tail would land near now+77.
        assert end < before + timedelta(days=59)
        assert (await db.get(Subscription, 11)).is_trial is False


@pytest.mark.asyncio
async def test_miniapp_create_branch_keeps_trials_killed_inside_create_paid_subscription(monkeypatch):
    """Create-ветка: inner deactivation триалов не должен терять их хвосты.

    ``context.subscription`` is None (multi-tariff stale id), while alive trials
    exist. ``create_paid_subscription`` deactivates them internally before the
    outer cleanup, so the tail snapshot must happen before the create call.
    """
    _enable_days_only(monkeypatch)
    _patch_miniapp_dependencies(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    async with memory_session(monkeypatch, TABLES) as db:
        user = _seed_user(db)
        before = datetime.now(UTC)
        first = _trial(sub_id=11, now=before, tail_days=9)
        first.remnawave_id = 1011
        second = _trial(
            sub_id=12,
            now=before,
            tail_days=5,
            status=SubscriptionStatus.TRIAL.value,
        )
        second.remnawave_id = 1012
        db.add_all([first, second])
        await db.commit()

        context = SimpleNamespace(user=user, subscription=None, payload={'subscription_id': 999})
        result = await MiniAppSubscriptionPurchaseService().submit_purchase(db, context, _miniapp_pricing())
        after = datetime.now(UTC)

        end = result['subscription'].end_date
        # 30 purchased + 9 + 5 referral tails from trials killed inside create.
        assert before + timedelta(days=44) <= end <= after + timedelta(days=44)
        # Without the pre-create snapshot the tails are lost and end is now+30.
        assert end > before + timedelta(days=43)
        # Внутренняя деактивация тоже должна отключить ОБА панельных аккаунта.
        assert set(_DummySubscriptionService.disabled_panel_ids) == {1011, 1012}


# ── Cabinet: purchase_tariff route ──


def _cabinet_tariff() -> Tariff:
    return Tariff(
        id=1,
        name='Тариф',
        description='',
        is_active=True,
        traffic_limit_gb=50,
        device_limit=3,
        allowed_squads=['squad-1'],
        period_prices={'30': 10_000},
        display_order=1,
    )


def _patch_cabinet_dependencies(monkeypatch, user: User) -> None:
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(cabinet_purchase_mod, 'subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr(
        cabinet_purchase_mod,
        'create_transaction',
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(cabinet_purchase_mod, 'SubscriptionService', _DummySubscriptionService)
    monkeypatch.setattr(cabinet_purchase_mod, 'should_create_panel_account', AsyncMock(return_value=False))
    monkeypatch.setattr(
        cabinet_purchase_mod,
        'decrement_subscription_server_counts',
        AsyncMock(),
    )
    monkeypatch.setattr(
        cabinet_purchase_mod,
        'user_cart_service',
        SimpleNamespace(save_user_cart=AsyncMock()),
    )
    monkeypatch.setattr(
        cabinet_purchase_mod.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(
            return_value=SimpleNamespace(
                final_total=100_000,
                original_total=100_000,
                breakdown={},
                promo_offer_discount=0,
            )
        ),
    )
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr('app.services.yandex_offline_conv_service.store_cid_only', AsyncMock())
    monkeypatch.setattr(User, 'get_primary_promo_group', lambda self: None)


@pytest.mark.asyncio
async def test_cabinet_purchase_route_carries_current_and_extra_tail_once(monkeypatch):
    """Маршрут кабинета: текущий триал (19) + extra (9) + 30 = now+58, не now+77."""
    _enable_days_only(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        user = _seed_user(db)
        db.add(_cabinet_tariff())
        before = datetime.now(UTC)
        current = _trial(sub_id=10, now=before, tail_days=19, tariff_id=1)
        extra = _trial(
            sub_id=11,
            now=before,
            tail_days=9,
            status=SubscriptionStatus.TRIAL.value,
        )
        db.add_all([current, extra])
        await db.commit()

        user = await db.get(User, 1)
        _patch_cabinet_dependencies(monkeypatch, user)
        request = TariffPurchaseRequest(tariff_id=1, period_days=30, subscription_id=10)

        response = await cabinet_purchase_mod.purchase_tariff(request, user, db)
        after = datetime.now(UTC)

        final = await db.get(Subscription, 10)
        assert response['success'] is True
        assert before + timedelta(days=58) <= final.end_date <= after + timedelta(days=58)
        # Current-tail double count would be near now+77.
        assert final.end_date < before + timedelta(days=59)


# ── Bot: confirm_purchase ──


def _pricing_details() -> dict:
    return {
        'base_price': 100_000,
        'base_price_original': 100_000,
        'base_discount_percent': 0,
        'base_discount_total': 0,
        'traffic_price_per_month': 0,
        'traffic_discount_percent': 0,
        'traffic_discount_total': 0,
        'total_traffic_price': 0,
        'servers_price_per_month': 0,
        'servers_discount_percent': 0,
        'servers_discount_total': 0,
        'total_servers_price': 0,
        'devices_price_per_month': 0,
        'devices_discount_percent': 0,
        'devices_discount_total': 0,
        'total_devices_price': 0,
        'months_in_period': 1,
        'servers_individual_prices': [],
    }


async def _run_bot_confirm(
    monkeypatch,
    trial: SimpleNamespace,
    *,
    undo=None,
    existing_subscription: bool = True,
    countries: list[str] | None = None,
) -> None:
    user = SimpleNamespace(
        id=1,
        telegram_id=12345,
        language='ru',
        balance_kopeks=200_000,
        remnawave_id=555,
        restriction_subscription=False,
        subscription=trial if existing_subscription else None,
    )

    state = MagicMock()
    state.get_data = AsyncMock(
        return_value={
            'period_days': 30,
            'countries': ['squad'] if countries is None else countries,
            'devices': 1,
            'final_traffic_gb': 100,
            'total_price': 100_000,
        }
    )
    state.clear = AsyncMock()
    callback = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.bot = MagicMock()

    db = AsyncMock()
    pricing_result = SimpleNamespace(final_total=100_000, promo_offer_discount=0, breakdown={'offer_discount_pct': 0})

    monkeypatch.setattr(purchase_mod, 'save_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr(purchase_mod, 'should_offer_checkout_resume', lambda *a, **k: False)
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(
        purchase_mod.pricing_engine,
        'calculate_classic_new_subscription_price',
        AsyncMock(return_value=pricing_result),
    )
    monkeypatch.setattr(
        PricingEngine, 'classic_pricing_to_purchase_details', staticmethod(lambda pricing: _pricing_details())
    )
    monkeypatch.setattr(purchase_mod, 'subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr(purchase_mod, 'create_transaction', AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr('app.database.crud.subscription_conversion.create_subscription_conversion', AsyncMock())
    monkeypatch.setattr(purchase_mod, 'apply_trial_conversion_defaults', lambda subscription: None)
    monkeypatch.setattr(
        'app.services.grace_access_echo.undo_grace_overlay_echo',
        undo or AsyncMock(return_value=set()),
    )
    monkeypatch.setattr('app.database.crud.server_squad.get_server_ids_by_uuids', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.database.crud.server_squad.add_user_to_servers', AsyncMock())
    monkeypatch.setattr('app.database.crud.subscription.add_subscription_servers', AsyncMock())
    monkeypatch.setattr('app.services.referral_purchase_service.register_first_purchase_after_purchase', AsyncMock())
    monkeypatch.setattr(purchase_mod, 'SubscriptionService', _DummySubscriptionService)
    monkeypatch.setattr(purchase_mod, 'AdminNotificationService', _DummyAdminNotifications)
    monkeypatch.setattr(purchase_mod, 'get_display_subscription_link', lambda *a, **k: None)
    monkeypatch.setattr(purchase_mod, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr(type(settings), 'is_devices_selection_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_traffic_fixed', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'get_support_contact_url', lambda self: None)
    monkeypatch.setattr(type(settings), 'should_hide_subscription_link', lambda self: False)

    await purchase_mod.confirm_purchase(callback, state, user, db)


def _confirm_trial(now: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        user_id=1,
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=20),
        base_trial_end_date=now + timedelta(days=1),
        status=SubscriptionStatus.ACTIVE.value,
        traffic_limit_gb=0,
        device_limit=1,
        connected_squads=[],
        updated_at=now,
        traffic_used_gb=0.0,
        remnawave_id=555,
        autopay_enabled=False,
    )


@pytest.mark.asyncio
async def test_bot_confirm_purchase_carries_only_referral_tail_once(monkeypatch):
    """Бот: база 1 день + хвост 19 + покупка 30 = now+49, не now+50/now+53."""
    _enable_days_only(monkeypatch)
    before = datetime.now(UTC)
    trial = _confirm_trial(before)

    await _run_bot_confirm(monkeypatch, trial)
    after = datetime.now(UTC)

    assert trial.is_trial is False
    assert before + timedelta(days=49) <= trial.end_date <= after + timedelta(days=49)
    assert trial.end_date < before + timedelta(days=50)


@pytest.mark.asyncio
async def test_bot_confirm_purchase_refunds_when_post_charge_step_fails(monkeypatch):
    """Сбой после списания не должен оставлять деньги у бота без подписки."""
    _enable_days_only(monkeypatch)
    trial = _confirm_trial(datetime.now(UTC))
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)

    await _run_bot_confirm(
        monkeypatch,
        trial,
        undo=AsyncMock(side_effect=RuntimeError('db down')),
    )

    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_confirm_purchase_refunds_when_countries_missing(monkeypatch):
    """Списание состоялось, но покупка отменена валидацией — деньги возвращаются."""
    _enable_days_only(monkeypatch)
    trial = _confirm_trial(datetime.now(UTC))
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)

    await _run_bot_confirm(
        monkeypatch,
        trial,
        existing_subscription=False,
        countries=[],
    )

    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_confirm_countries_missing_keeps_trial_unconverted(monkeypatch):
    """Возврат за отмену по странам не коммитит конверсию триала."""
    _enable_days_only(monkeypatch)
    trial = _confirm_trial(datetime.now(UTC))
    end_before = trial.end_date
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', refund)

    await _run_bot_confirm(monkeypatch, trial, existing_subscription=True, countries=[])

    refund.assert_awaited_once()
    assert trial.is_trial is True
    assert trial.end_date == end_before
