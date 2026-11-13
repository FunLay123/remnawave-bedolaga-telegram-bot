"""Renewal invalidation for outstanding expired-discount wave offers.

A wave discount is only offered while the subscription it points at is expired.
Once a positive-day renewal succeeds, unclaimed ``expired_discount_wave2`` /
``expired_discount_wave3`` offers for that subscription must be deactivated in
the same transaction.  Non-wave offers, offers for other subscriptions, and a
discount the user claimed while still eligible must all be left alone.

These tests drive the real ``extend_subscription`` against an in-memory SQLite
database: the assertions look at persisted offer rows, not at call mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.crud import discount_offer as discount_offer_crud
from app.database.crud.subscription import extend_subscription
from app.database.models import Base, DiscountOffer, Subscription, SubscriptionStatus, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)

WAVE2 = 'expired_discount_wave2'
WAVE3 = 'expired_discount_wave3'


def _configure(monkeypatch) -> None:
    """Keep the renewal on the classic, no-traffic-reset path."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'TRAFFIC_SELECTION_MODE', 'selectable')


def _user(
    *,
    promo_percent: int = 0,
    promo_source: str | None = None,
    promo_expires_at: datetime | None = None,
) -> User:
    return User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
        promo_offer_discount_percent=promo_percent,
        promo_offer_discount_source=promo_source,
        promo_offer_discount_expires_at=promo_expires_at,
    )


def _subscription(subscription_id: int = 10, *, status: str = SubscriptionStatus.EXPIRED.value) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=subscription_id,
        remnawave_short_id=f'sub-{subscription_id}',
        user_id=1,
        status=status,
        is_trial=False,
        start_date=now - timedelta(days=30),
        end_date=now - timedelta(days=1),
        traffic_limit_gb=50,
        traffic_used_gb=0.0,
        device_limit=1,
        tariff_id=None,
        connected_squads=[],
    )


def _offer(
    offer_id: int,
    *,
    subscription_id: int = 10,
    notification_type: str = WAVE2,
    is_active: bool = True,
    claimed_at: datetime | None = None,
) -> DiscountOffer:
    return DiscountOffer(
        id=offer_id,
        user_id=1,
        subscription_id=subscription_id,
        notification_type=notification_type,
        discount_percent=20,
        bonus_amount_kopeks=0,
        expires_at=datetime.now(UTC) + timedelta(hours=5),
        claimed_at=claimed_at,
        is_active=is_active,
        effect_type='percent_discount',
        extra_data={'offer_type': 'extend_discount', 'active_discount_hours': 24},
    )


async def _reload_offer(db, offer_id: int) -> DiscountOffer:
    offer = await db.get(DiscountOffer, offer_id)
    await db.refresh(offer)
    return offer


async def test_renewal_deactivates_only_outstanding_wave_offers(monkeypatch):
    _configure(monkeypatch)
    now = datetime.now(UTC)
    claimed_deadline = now + timedelta(hours=5)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(
            _user(
                promo_percent=20,
                promo_source=WAVE2,
                promo_expires_at=claimed_deadline,
            )
        )
        db.add(_subscription(10))
        db.add(_subscription(11))
        db.add_all(
            [
                _offer(1, subscription_id=10, notification_type=WAVE2),
                _offer(2, subscription_id=10, notification_type=WAVE3),
                _offer(3, subscription_id=10, notification_type='manual_offer'),
                _offer(4, subscription_id=11, notification_type=WAVE2),
                # Claimed while the subscription was genuinely expired: the
                # outstanding-offer sweep must not touch it, even if a legacy
                # row still carries is_active=True.
                _offer(5, subscription_id=10, notification_type=WAVE2, is_active=True, claimed_at=now),
            ]
        )
        await db.commit()

        subscription = await db.get(Subscription, 10)
        await extend_subscription(db, subscription, 30)

        reloaded = {offer_id: await _reload_offer(db, offer_id) for offer_id in (1, 2, 3, 4, 5)}
        user = await db.get(User, 1)
        await db.refresh(user)

    assert reloaded[1].is_active is False
    assert reloaded[2].is_active is False
    assert reloaded[3].is_active is True
    assert reloaded[4].is_active is True
    assert reloaded[5].is_active is True
    assert reloaded[5].claimed_at is not None

    # The claimed-before-renewal discount is bounded by its original deadline
    # and is not revoked retroactively.
    assert user.promo_offer_discount_percent == 20
    assert user.promo_offer_discount_source == WAVE2
    assert user.promo_offer_discount_expires_at == claimed_deadline


async def test_deactivate_unclaimed_wave_offers_returns_affected_count(monkeypatch):
    _configure(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user())
        db.add(_subscription(10))
        db.add_all(
            [
                _offer(1, subscription_id=10, notification_type=WAVE2),
                _offer(2, subscription_id=10, notification_type=WAVE3),
                _offer(3, subscription_id=10, notification_type='manual_offer'),
            ]
        )
        await db.commit()

        count = await discount_offer_crud.deactivate_unclaimed_wave_offers(db, 10, commit=True)

        reloaded = {offer_id: await _reload_offer(db, offer_id) for offer_id in (1, 2, 3)}

    assert count == 2
    assert reloaded[1].is_active is False
    assert reloaded[2].is_active is False
    assert reloaded[3].is_active is True


@pytest.mark.parametrize('days', [0, -5])
async def test_non_positive_extension_does_not_invalidate_wave_offers(monkeypatch, days):
    _configure(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user())
        db.add(_subscription(10))
        db.add(_offer(1, subscription_id=10, notification_type=WAVE2))
        await db.commit()

        subscription = await db.get(Subscription, 10)
        await extend_subscription(db, subscription, days)

        offer = await _reload_offer(db, 1)

    assert offer.is_active is True


async def test_invalidation_rolls_back_with_the_callers_transaction(monkeypatch):
    """commit=False callers still own the transaction: a rollback re-activates the offer."""
    _configure(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user())
        db.add(_subscription(10))
        db.add(_offer(1, subscription_id=10, notification_type=WAVE2))
        await db.commit()

        subscription = await db.get(Subscription, 10)
        await extend_subscription(db, subscription, 30, commit=False)

        offer = await _reload_offer(db, 1)
        assert offer.is_active is False

        await db.rollback()

        offer = await _reload_offer(db, 1)

    assert offer.is_active is True
