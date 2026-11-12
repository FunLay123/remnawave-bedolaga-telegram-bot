"""Первая платная покупка через классическое продление (finalize).

``SubscriptionRenewalService.finalize`` — общий путь для классического
«продлить» (бот, cryptobot, кабинет/miniapp). До этого хука первая оплата
через продление не ставила маркер первой покупки, и следующая покупка могла
получить бонус повторно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import Base, ReferralEarning, Subscription, SubscriptionStatus, User
from app.services.referral_purchase_service import has_first_purchase_marker
from app.services.subscription_renewal_service import SubscriptionRenewalService
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int = 2, *, balance_kopeks: int = 0) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        status='active',
        language='ru',
        balance_kopeks=balance_kopeks,
        created_at=datetime.now(UTC),
    )


def _subscription(sub_id: int = 10, user_id: int = 2) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=10),
        traffic_limit_gb=100,
        device_limit=1,
    )


def _pricing(final_total: int, period_days: int = 30) -> SimpleNamespace:
    # RenewalPricing-ветка finalize: promo_offer_discount/breakdown читаются.
    return SimpleNamespace(
        final_total=final_total,
        period_days=period_days,
        promo_offer_discount=None,
        breakdown={},
    )


def _patch_finalize_side_effects(monkeypatch) -> None:
    """Убрать панель, сброс устройств и уведомления из unit-теста."""
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


@pytest.mark.asyncio
async def test_paid_renewal_calls_first_purchase_hook_once(monkeypatch):
    """Оплаченное продление вызывает хук ровно один раз с фактическими аргументами."""
    _patch_finalize_side_effects(monkeypatch)
    hook = AsyncMock()
    monkeypatch.setattr('app.services.referral_purchase_service.register_first_purchase_after_purchase', hook)

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user()
        subscription = _subscription()
        db.add_all([user, subscription])
        await db.commit()

        result = await SubscriptionRenewalService().finalize(
            db,
            user,
            subscription,
            _pricing(10_000),
            charge_balance_amount=0,
        )

    hook.assert_awaited_once_with(
        referee_id=2,
        subscription_id=10,
        duration_days=30,
        paid_amount_kopeks=10_000,
        source='renewal_finalize',
    )
    assert result.total_amount_kopeks == 10_000


@pytest.mark.asyncio
async def test_skip_first_purchase_hook_flag_suppresses_the_call(monkeypatch):
    """menu вызывает хук сам после своих веток — finalize не дублирует сессию."""
    _patch_finalize_side_effects(monkeypatch)
    hook = AsyncMock()
    monkeypatch.setattr('app.services.referral_purchase_service.register_first_purchase_after_purchase', hook)

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user()
        subscription = _subscription()
        db.add_all([user, subscription])
        await db.commit()

        await SubscriptionRenewalService().finalize(
            db,
            user,
            subscription,
            _pricing(10_000),
            charge_balance_amount=0,
            skip_first_purchase_hook=True,
        )

    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_amount_renewal_hook_returns_skipped_without_marker(monkeypatch):
    """Бесплатное продление (0 ₽) не является покупкой: маркера и дней нет."""
    _patch_finalize_side_effects(monkeypatch)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user()
        subscription = _subscription()
        db.add_all([user, subscription])
        await db.commit()

        # Хук пишет в отдельной сессии — направляем её в ту же in-memory БД.
        monkeypatch.setattr(
            'app.database.database.AsyncSessionLocal',
            async_sessionmaker(db.bind, expire_on_commit=False),
        )

        await SubscriptionRenewalService().finalize(
            db,
            user,
            subscription,
            _pricing(0),
            charge_balance_amount=0,
        )

        assert await has_first_purchase_marker(db, 2) is False
        earnings = (await db.execute(select(ReferralEarning))).scalars().all()

    assert earnings == []
