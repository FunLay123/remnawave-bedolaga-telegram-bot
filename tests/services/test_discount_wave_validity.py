"""Expired-discount wave validity, bounded legacy backfill, and cleanup order.

The wave discount is a one-shot personal offer whose deadline is the deadline
printed on the card.  Legacy rows may carry that deadline as NULL: the backfill
must copy the matching claimed offer's ``expires_at``, or grant exactly one
24-hour migration window when no matching offer exists.  Monitoring performs
the backfill immediately before the existing expired-discount cleanup, so a
matched deadline that is already past is cleared in the same cycle.

The tests exercise the real CRUD/pricing code on an in-memory SQLite database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.crud import user as user_crud
from app.database.models import (
    DiscountOffer,
    PromoGroup,
    PromoOfferLog,
    ServerSquad,
    Subscription,
    Tariff,
    User,
    UserPromoGroup,
    server_squad_promo_groups,
    tariff_promo_groups,
)
from app.services import monitoring_service as ms
from app.services.subscription_auto_purchase_service import _prepare_auto_extend_context
from app.utils.promo_offer import consume_user_promo_offer, get_user_active_promo_discount_percent
from tests.fixtures.sqlite_memory import memory_session


WAVE2 = 'expired_discount_wave2'
WAVE3 = 'expired_discount_wave3'

TABLES = (
    User.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    ServerSquad.__table__,
    server_squad_promo_groups,
    Subscription.__table__,
    DiscountOffer.__table__,
    PromoOfferLog.__table__,
)


def _wave_user(
    *,
    user_id: int = 1,
    percent: int = 20,
    source: str = WAVE2,
    expires_at: datetime | None = None,
) -> User:
    return User(
        id=user_id,
        telegram_id=1000 + user_id,
        first_name=f'U{user_id}',
        language='ru',
        status='active',
        balance_kopeks=0,
        promo_offer_discount_percent=percent,
        promo_offer_discount_source=source if percent else None,
        promo_offer_discount_expires_at=expires_at,
    )


def _claimed_offer(
    *,
    offer_id: int = 1,
    user_id: int = 1,
    notification_type: str = WAVE2,
    expires_at: datetime | None,
) -> DiscountOffer:
    return DiscountOffer(
        id=offer_id,
        user_id=user_id,
        subscription_id=None,
        notification_type=notification_type,
        discount_percent=20,
        bonus_amount_kopeks=0,
        expires_at=expires_at,
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
        is_active=False,
        effect_type='percent_discount',
    )


async def _reload_user(db, user_id: int = 1) -> User:
    user = await db.get(User, user_id)
    await db.refresh(user)
    return user


# ── validity ────────────────────────────────────────────────────────────


def test_expired_wave_discount_percent_is_zero():
    user = _wave_user(expires_at=datetime.now(UTC) - timedelta(seconds=1))

    assert get_user_active_promo_discount_percent(user) == 0


def test_valid_wave_discount_percent_is_returned():
    user = _wave_user(expires_at=datetime.now(UTC) + timedelta(hours=1))

    assert get_user_active_promo_discount_percent(user) == 20


@pytest.mark.parametrize(('delta_hours', 'expected'), [(-1, 0), (1, 20)])
def test_naive_wave_deadline_is_compared_as_utc(delta_hours, expected):
    """Legacy naive datetimes must not raise on the aware ``now`` comparison."""
    naive_deadline = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=delta_hours)

    user = _wave_user(expires_at=naive_deadline)

    assert get_user_active_promo_discount_percent(user) == expected


async def test_one_shot_consumption_clears_the_wave_discount(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(expires_at=datetime.now(UTC) + timedelta(hours=2)))
        await db.commit()

        assert await consume_user_promo_offer(db, 1) is True
        user = await _reload_user(db)

        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_source is None
        assert user.promo_offer_discount_expires_at is None
        assert await consume_user_promo_offer(db, 1) is False


# ── bounded legacy backfill ─────────────────────────────────────────────


async def test_backfill_copies_the_matching_claimed_offer_deadline(monkeypatch):
    deadline = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=5)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(source=WAVE2, expires_at=None))
        db.add(_claimed_offer(notification_type=WAVE2, expires_at=deadline))
        await db.commit()

        result = await user_crud.backfill_wave_discount_expiry(db, now=deadline)

        user = await _reload_user(db)

    assert result == (1, 0)
    assert user.promo_offer_discount_expires_at == deadline


async def test_backfill_grants_one_bounded_window_and_is_idempotent(monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(source=WAVE3, expires_at=None))
        await db.commit()

        first = await user_crud.backfill_wave_discount_expiry(db, now=now)
        user = await _reload_user(db)
        assert user.promo_offer_discount_expires_at == now + timedelta(hours=24)

        # A later cycle must not refresh the grace window.
        second = await user_crud.backfill_wave_discount_expiry(db, now=now + timedelta(hours=3))
        user = await _reload_user(db)

    assert first == (0, 1)
    assert second == (0, 0)
    assert user.promo_offer_discount_expires_at == now + timedelta(hours=24)


async def test_backfill_accepts_a_naive_reference_time(monkeypatch):
    naive_now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(source=WAVE2, expires_at=None))
        await db.commit()

        await user_crud.backfill_wave_discount_expiry(db, now=naive_now)
        user = await _reload_user(db)

    assert user.promo_offer_discount_expires_at is not None
    assert user.promo_offer_discount_expires_at.tzinfo is not None
    assert user.promo_offer_discount_expires_at == naive_now.replace(tzinfo=UTC) + timedelta(hours=24)


async def test_backfill_ignores_non_wave_and_already_bounded_users(monkeypatch):
    bounded_deadline = datetime.now(UTC) + timedelta(hours=2)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(user_id=1, percent=20, source='manual_offer', expires_at=None))
        db.add(_wave_user(user_id=2, percent=20, source=WAVE2, expires_at=bounded_deadline))
        await db.commit()

        result = await user_crud.backfill_wave_discount_expiry(db)

        first = await _reload_user(db, 1)
        second = await _reload_user(db, 2)

    assert result == (0, 0)
    assert first.promo_offer_discount_expires_at is None
    assert second.promo_offer_discount_expires_at == bounded_deadline


async def test_backfill_requires_a_claimed_offer_of_the_same_source(monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(source=WAVE2, expires_at=None))
        db.add(
            _claimed_offer(
                notification_type=WAVE3,
                expires_at=now + timedelta(days=3),
            )
        )
        await db.commit()

        result = await user_crud.backfill_wave_discount_expiry(db, now=now)
        user = await _reload_user(db)

    assert result == (0, 1)
    assert user.promo_offer_discount_expires_at == now + timedelta(hours=24)


async def test_past_matched_deadline_is_cleared_by_the_existing_cleanup(monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)
    past_deadline = now - timedelta(hours=1)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(source=WAVE3, expires_at=None))
        db.add(_claimed_offer(notification_type=WAVE3, expires_at=past_deadline))
        await db.commit()

        assert await user_crud.backfill_wave_discount_expiry(db, now=now) == (1, 0)
        cleared = await user_crud.cleanup_expired_promo_offer_discounts(db)

        user = await _reload_user(db)

    assert cleared == 1
    assert user.promo_offer_discount_percent == 0
    assert user.promo_offer_discount_source is None
    assert user.promo_offer_discount_expires_at is None


# ── no revival from a saved checkout/topup intent ───────────────────────


async def _seed_saved_cart_user(db, *, expires_at: datetime) -> None:
    now = datetime.now(UTC)
    db.add(_wave_user(source=WAVE2, expires_at=expires_at))
    db.add(
        Tariff(
            id=3,
            name='Tariff',
            description='',
            is_active=True,
            traffic_limit_gb=50,
            device_limit=1,
            allowed_squads=[],
            period_prices={'30': 100_000},
            display_order=1,
        )
    )
    await db.flush()
    db.add(
        Subscription(
            id=4,
            remnawave_short_id='sub-4',
            user_id=1,
            status='expired',
            is_trial=False,
            start_date=now - timedelta(days=60),
            end_date=now - timedelta(days=1),
            traffic_limit_gb=50,
            traffic_used_gb=0.0,
            device_limit=1,
            tariff_id=3,
            autopay_period_days=30,
            connected_squads=[],
        )
    )
    await db.commit()


def _saved_cart(*, expired_deadline: datetime) -> dict:
    """A saved intent may echo stale discount fields; they must not be trusted."""
    return {
        'period_days': 30,
        'tariff_id': 3,
        'subscription_id': 4,
        'promo_offer_discount_percent': 20,
        'promo_offer_discount_expires_at': expired_deadline.isoformat(),
    }


def _disable_multi_tariff(monkeypatch) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)


async def test_saved_cart_does_not_revive_an_expired_wave_discount(monkeypatch):
    _disable_multi_tariff(monkeypatch)
    expired_deadline = datetime.now(UTC) - timedelta(hours=1)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_saved_cart_user(db, expires_at=expired_deadline)
        user = await _reload_user(db)

        context = await _prepare_auto_extend_context(db, user, _saved_cart(expired_deadline=expired_deadline))

    assert context is not None
    assert context.price_kopeks == 100_000
    assert context.consume_promo_offer is False


async def test_saved_cart_still_applies_a_valid_wave_discount(monkeypatch):
    _disable_multi_tariff(monkeypatch)
    valid_deadline = datetime.now(UTC) + timedelta(hours=2)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_saved_cart_user(db, expires_at=valid_deadline)
        user = await _reload_user(db)

        context = await _prepare_auto_extend_context(db, user, _saved_cart(expired_deadline=valid_deadline))

    assert context is not None
    assert context.price_kopeks == 80_000
    assert context.consume_promo_offer is True


# ── monitoring order: backfill immediately before cleanup ───────────────


async def test_monitoring_backfills_before_cleanup(monkeypatch):
    calls: list[str] = []

    async def fake_backfill(db, *, now=None):
        calls.append('backfill')
        return (1, 0)

    async def fake_cleanup(db):
        calls.append('cleanup')
        return 2

    monkeypatch.setattr(ms, 'backfill_wave_discount_expiry', fake_backfill)
    monkeypatch.setattr(ms, 'cleanup_expired_promo_offer_discounts', fake_cleanup)

    service = ms.MonitoringService(bot=None)

    async with memory_session(monkeypatch, TABLES) as db:
        result = await service._backfill_and_cleanup_promo_offer_discounts(db)

    assert calls == ['backfill', 'cleanup']
    assert result == (1, 0, 2)


async def test_monitoring_clears_a_past_matched_deadline_in_one_pass(monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_wave_user(source=WAVE2, expires_at=None))
        db.add(_claimed_offer(notification_type=WAVE2, expires_at=now - timedelta(hours=1)))
        await db.commit()

        service = ms.MonitoringService(bot=None)
        matched, unmatched, cleared = await service._backfill_and_cleanup_promo_offer_discounts(db)

        user = await _reload_user(db)

    assert (matched, unmatched) == (1, 0)
    assert cleared == 1
    assert user.promo_offer_discount_percent == 0
    assert user.promo_offer_discount_expires_at is None
