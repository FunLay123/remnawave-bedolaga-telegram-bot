"""Платные и бесплатные пути конверсии триала используют общий контракт.

- ``SubscriptionRenewalService.finalize`` — классическое платное продление без
  ``tariff_id``: триал конвертируется, платный срок стартует от покупки и
  добавляет только реферальный хвост (``days_only``).
- Купон и тарифный бонус кампании — бесплатные выдачи: живой триал остаётся
  триалом и не проходит платный перенос хвоста.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.models import (
    AdvertisingCampaign,
    Base,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
)
from app.services.campaign_service import AdvertisingCampaignService
from app.services.coupon_service import _grant_subscription_days
from app.services.subscription_renewal_service import SubscriptionRenewalService
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _enable_days_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)


def _user(user_id: int = 1) -> User:
    now = datetime.now(UTC)
    return User(
        id=user_id,
        telegram_id=1000 + user_id,
        first_name=f'U{user_id}',
        language='ru',
        status='active',
        balance_kopeks=200_000,
        created_at=now,
    )


def _tariff() -> Tariff:
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


def _trial_referral_subscription(*, now: datetime, tariff_id: int | None) -> Subscription:
    """База 3 дня + реферальный хвост 20: жив до покупки, конец = now+23."""
    return Subscription(
        id=10,
        user_id=1,
        remnawave_short_id='sub10',
        status=SubscriptionStatus.TRIAL.value,
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=23),
        base_trial_end_date=now + timedelta(days=3),
        traffic_limit_gb=50,
        device_limit=1,
        connected_squads=['squad-1'],
        tariff_id=tariff_id,
    )


def _patch_renewal_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)

    class _FakeSubscriptionService:
        async def create_remnawave_user(self, *_args, **_kwargs) -> None:
            return None

        async def update_remnawave_user(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr('app.services.subscription_renewal_service.SubscriptionService', _FakeSubscriptionService)
    monkeypatch.setattr(
        'app.services.subscription_renewal_service.with_admin_notification_service',
        AsyncMock(),
    )
    monkeypatch.setattr(
        'app.services.referral_purchase_service.register_first_purchase_after_purchase',
        AsyncMock(return_value=SimpleNamespace(status='skipped')),
    )


# ── SubscriptionRenewalService.finalize (классическая платная покупка) ──


@pytest.mark.asyncio
async def test_renewal_finalize_converts_classic_trial_with_referral_tail(monkeypatch):
    """Оплаченное продление без tariff_id: триал → платная, now+30+20, не old_end+30."""
    _enable_days_only(monkeypatch)
    _patch_renewal_side_effects(monkeypatch)
    pricing = SimpleNamespace(final_total=10_000, period_days=30, promo_offer_discount=None, breakdown={})

    async with memory_session(monkeypatch, TABLES) as db:
        before = datetime.now(UTC)
        user = _user()
        subscription = _trial_referral_subscription(now=before, tariff_id=None)
        db.add_all([user, subscription])
        await db.commit()

        result = await SubscriptionRenewalService().finalize(
            db,
            user,
            subscription,
            pricing,
            charge_balance_amount=0,
        )
        after = datetime.now(UTC)

    assert result.subscription.is_trial is False
    # 30 купленных + 20 реферальных; базовые 3 дня не переносятся.
    assert before + timedelta(days=50) <= result.subscription.end_date <= after + timedelta(days=50)
    assert result.subscription.end_date < before + timedelta(days=53)


# ── Купон: бесплатная выдача ──


@pytest.mark.asyncio
async def test_coupon_grant_keeps_trial_and_extends_from_end_date(monkeypatch):
    """Купон не конвертирует триал и не перезапускает срок от now + хвост."""
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    async with memory_session(monkeypatch, TABLES) as db:
        before = datetime.now(UTC)
        user = _user()
        tariff = _tariff()
        subscription = _trial_referral_subscription(now=before, tariff_id=1)
        db.add_all([user, tariff, subscription])
        await db.commit()

        user = await db.get(User, 1)
        granted, renewed = await _grant_subscription_days(db, user, tariff, 30)

    assert renewed is True
    assert granted.is_trial is True
    assert granted.end_date == before + timedelta(days=23) + timedelta(days=30)


# ── Тарифный бонус кампании: бесплатная выдача ──


@pytest.mark.asyncio
async def test_campaign_tariff_bonus_keeps_trial_and_extends_from_end_date(monkeypatch):
    """Бонус кампании не конвертирует триал и не перезапускает срок от now."""
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr('app.services.campaign_service.SubscriptionService', MagicMock())

    async with memory_session(monkeypatch, TABLES) as db:
        before = datetime.now(UTC)
        user = _user()
        tariff = _tariff()
        campaign = AdvertisingCampaign(
            id=1,
            name='Camp',
            start_parameter='camp',
            bonus_type='tariff',
            tariff_id=1,
            tariff_duration_days=30,
            is_active=True,
        )
        subscription = _trial_referral_subscription(now=before, tariff_id=1)
        db.add_all([user, tariff, campaign, subscription])
        await db.commit()

        service = AdvertisingCampaignService()
        service.subscription_service = SimpleNamespace(update_remnawave_user=AsyncMock(return_value=True))
        user = await db.get(User, 1)
        result = await service._apply_tariff_bonus(db, user, campaign)

    assert result.success is True
    assert subscription.is_trial is True
    assert subscription.end_date == before + timedelta(days=23) + timedelta(days=30)
