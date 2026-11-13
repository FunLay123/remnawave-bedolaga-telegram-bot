"""Persistent, cycle-keyed retry for the initial ``SUBSCRIPTION_EXPIRED`` message.

The initial expired notification is the only chance to tell a user their
subscription just ended.  A transient Telegram failure must not lose it, while
an old EXPIRED row, a renewed subscription, or a blocked user must never
produce a retroactive/stale message.

The retry contract under test:

* pending state is persisted *before* every network attempt and records the
  attempt being started, so a restart consumes that recorded attempt instead
  of repeating it;
* at most three actual sends per ``end_date`` cycle, even across crashes;
* ``SENT`` persists a confirmed ``expired:<end_date>`` marker;
* retryable ``FAILED``/``TIMEOUT`` keep the last-consumed marker for the next
  monitoring cycle, the third unsuccessful attempt removes it;
* ``UNREACHABLE``/``SKIPPED`` (blocked, deactivated, permanent Telegram
  rejection) cancel the pending marker instead of retrying;
* recovery scans only explicit pending markers whose subscription is still
  EXPIRED in the very same cycle.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select

from app.config import settings
from app.database.models import SentNotification, Subscription, SubscriptionStatus, User, UserPromoGroup
from app.services import monitoring_service as ms
from app.services.monitoring_service import MonitoringService
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    UserPromoGroup.__table__,
    SentNotification.__table__,
)

PENDING_PREFIX = 'expired_retry:'


def _cycle(end_date: datetime) -> int:
    return int(end_date.timestamp())


def _pending_type(end_date: datetime) -> str:
    return f'{PENDING_PREFIX}{_cycle(end_date)}'


def _confirmed_type(end_date: datetime) -> str:
    return f'expired:{_cycle(end_date)}'


def _service(*, bot=None) -> MonitoringService:
    return MonitoringService(bot=bot if bot is not None else MagicMock())


async def _seed(db, *, end_date: datetime, status: str = SubscriptionStatus.EXPIRED.value, user_status: str = 'active'):
    user = User(
        id=42,
        telegram_id=555,
        auth_type='telegram',
        language='ru',
        status=user_status,
        balance_kopeks=0,
        notification_settings={},
    )
    subscription = Subscription(id=7, user_id=42, status=status, end_date=end_date)
    db.add_all([user, subscription])
    await db.commit()
    return user, subscription


async def _markers(db) -> dict[str, int | None]:
    result = await db.execute(select(SentNotification))
    return {row.notification_type: row.days_before for row in result.scalars().all()}


def _gates_on(monkeypatch) -> None:
    monkeypatch.setattr(
        ms.NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda user: True)


def _no_price_lookup(service: MonitoringService) -> None:
    service._resolve_renewal_cost_kopeks = AsyncMock(return_value=15000)


async def _initial(service: MonitoringService, db, user, subscription):
    return await service._send_initial_expired_notification(db, user, subscription)


async def _retry(service: MonitoringService, db):
    await service._retry_expired_notifications(db)


# ---------------------------------------------------------------------------
# Initial attempt semantics
# ---------------------------------------------------------------------------


async def test_initial_success_persists_confirmed_and_clears_pending(monkeypatch):
    """Break caught: a confirmed send must not leave pending state or resend."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )

        await _initial(service, db, user, subscription)

        assert await _markers(db) == {_confirmed_type(end_date): None}
        service._send_subscription_expired_notification_result.assert_awaited_once()


async def test_pending_attempt_one_is_persisted_before_the_network_call(monkeypatch):
    """Break caught: a crash during the send must not lose the attempt budget."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        service = _service()
        _no_price_lookup(service)
        observed: dict[str, int | None] = {}

        async def _send(_user, _subscription, **_kwargs):
            observed.update(await _markers(db))
            return ms.NotificationDeliveryStatus.FAILED

        service._send_subscription_expired_notification_result = _send

        await _initial(service, db, user, subscription)

        assert observed == {_pending_type(end_date): 1}
        # Last-consumed semantics: the marker keeps attempt 1, so recovery
        # advances to attempt 2 on the next cycle instead of repeating it.
        assert await _markers(db) == {_pending_type(end_date): 1}


async def test_initial_failure_records_consumed_attempt_one(monkeypatch):
    """Break caught: a retryable failure must schedule a later cycle, not stop."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.FAILED
        )

        await _initial(service, db, user, subscription)

        assert await _markers(db) == {_pending_type(end_date): 1}


# ---------------------------------------------------------------------------
# Later-cycle retries and the three-attempt budget
# ---------------------------------------------------------------------------


async def test_failure_failure_success_across_fresh_instances(monkeypatch):
    """Break caught: pending state must survive a restart and stop after success."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        attempts: list[int | None] = []

        def _result_sender():
            async def _send(_user, _subscription, **_kwargs):
                current = await _markers(db)
                attempts.append(current.get(_pending_type(end_date)))
                return outcome

            return _send

        # Attempt 1: initial failure.
        outcome = ms.NotificationDeliveryStatus.FAILED
        first = _service()
        _no_price_lookup(first)
        first._send_subscription_expired_notification_result = _result_sender()
        await _initial(first, db, user, subscription)

        # Attempt 2: a later monitoring cycle on a fresh service instance.
        second = _service()
        _no_price_lookup(second)
        second._send_subscription_expired_notification_result = _result_sender()
        await _retry(second, db)
        # Attempt 2 is now the last consumed attempt.
        assert await _markers(db) == {_pending_type(end_date): 2}

        # Attempt 3: another cycle, now successful.
        outcome = ms.NotificationDeliveryStatus.SENT
        third = _service()
        _no_price_lookup(third)
        third._send_subscription_expired_notification_result = _result_sender()
        await _retry(third, db)

        # A fourth cycle finds no pending marker and sends nothing.
        fourth = _service()
        _no_price_lookup(fourth)
        fourth._send_subscription_expired_notification_result = AsyncMock()
        await _retry(fourth, db)

        assert attempts == [1, 2, 3]
        assert await _markers(db) == {_confirmed_type(end_date): None}
        fourth._send_subscription_expired_notification_result.assert_not_awaited()


async def test_three_unsuccessful_attempts_remove_pending_and_never_send_fourth(monkeypatch):
    """Break caught: the third failure must end the retry loop operationally."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.FAILED
        )

        # Attempt 1 records pending=1; attempts 2 and 3 run on later cycles.
        await _initial(service, db, user, subscription)
        for _ in range(2):
            await _retry(service, db)

        assert service._send_subscription_expired_notification_result.await_count == 3
        assert await _markers(db) == {}

        # No fourth attempt after the budget is exhausted.
        await _retry(service, db)
        assert service._send_subscription_expired_notification_result.await_count == 3


# ---------------------------------------------------------------------------
# Crash during a send: the recorded attempt is consumed, never repeated
# ---------------------------------------------------------------------------


async def test_restart_with_in_flight_attempt_one_consumes_it_instead_of_repeating(monkeypatch):
    """Break caught: a crash after the first send must not resend attempt 1.

    The pending marker is written *before* the network call, so a crash between
    the send and the outcome commit leaves ``days_before=1`` on disk. Recovery
    must treat that attempt as consumed and continue with attempt 2; otherwise
    the same attempt can repeat on every restart and a fourth actual send
    becomes possible.
    """
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=end_date)
        # In-flight attempt 1: the send happened, the outcome commit did not.
        db.add(SentNotification(user_id=42, subscription_id=7, notification_type=_pending_type(end_date), days_before=1))
        await db.commit()

        observed_attempts: list[int | None] = []

        def _failing_sender():
            async def _send(_user, _subscription, **_kwargs):
                observed_attempts.append((await _markers(db)).get(_pending_type(end_date)))
                return ms.NotificationDeliveryStatus.FAILED

            return _send

        first = _service()
        _no_price_lookup(first)
        first._send_subscription_expired_notification_result = _failing_sender()
        await _retry(first, db)

        # Attempt 1 is not repeated: recovery advanced to attempt 2 and the
        # marker (last consumed attempt) stays at 2 after the failure.
        assert observed_attempts == [2]
        assert await _markers(db) == {_pending_type(end_date): 2}

        second = _service()
        _no_price_lookup(second)
        second._send_subscription_expired_notification_result = _failing_sender()
        await _retry(second, db)

        # Attempt 3 is the last one; its failure clears the marker.
        assert observed_attempts == [2, 3]
        assert await _markers(db) == {}

        # A further restart cannot produce a fourth send.
        third = _service()
        _no_price_lookup(third)
        third._send_subscription_expired_notification_result = AsyncMock()
        await _retry(third, db)
        assert observed_attempts == [2, 3]
        third._send_subscription_expired_notification_result.assert_not_awaited()


async def test_restart_with_in_flight_attempt_three_is_not_repeated(monkeypatch):
    """Break caught: a crash during the third send must not resend attempt 3."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=end_date)
        # In-flight attempt 3: all three sends may already have happened.
        db.add(SentNotification(user_id=42, subscription_id=7, notification_type=_pending_type(end_date), days_before=3))
        await db.commit()

        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )
        await _retry(service, db)

        service._send_subscription_expired_notification_result.assert_not_awaited()
        assert await _markers(db) == {}


# ---------------------------------------------------------------------------
# Stale state is cancelled by status / cycle checks
# ---------------------------------------------------------------------------


async def test_renewal_cancels_pending_without_sending(monkeypatch):
    """Break caught: a renewed subscription must not receive a stale message."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        _user, subscription = await _seed(db, end_date=end_date)
        subscription.status = SubscriptionStatus.ACTIVE.value
        db.add(SentNotification(user_id=42, subscription_id=7, notification_type=_pending_type(end_date), days_before=2))
        await db.commit()

        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )
        await _retry(service, db)

        service._send_subscription_expired_notification_result.assert_not_awaited()
        assert await _markers(db) == {}


async def test_changed_end_date_cancels_stale_pending(monkeypatch):
    """Break caught: a pending marker from an older cycle must not leak forward."""
    _gates_on(monkeypatch)
    old_end = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    new_end = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        _user, _subscription = await _seed(db, end_date=new_end)
        db.add(SentNotification(user_id=42, subscription_id=7, notification_type=_pending_type(old_end), days_before=2))
        await db.commit()

        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )
        await _retry(service, db)

        service._send_subscription_expired_notification_result.assert_not_awaited()
        assert await _markers(db) == {}


async def test_preexisting_expired_row_without_pending_is_not_retroactively_notified(monkeypatch):
    """Break caught: never infer a retry from an old EXPIRED row alone."""
    _gates_on(monkeypatch)
    end_date = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=end_date)

        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )
        await _retry(service, db)

        service._send_subscription_expired_notification_result.assert_not_awaited()
        assert await _markers(db) == {}


# ---------------------------------------------------------------------------
# Ambiguous timeouts: bounded later-cycle recovery, never an immediate retry
# ---------------------------------------------------------------------------


async def test_rich_timeout_keeps_pending_for_a_later_cycle(monkeypatch):
    """Break caught: rich timeout must be unconfirmed, not treated as sent."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    async def _rich_timeout(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(ms, 'try_send_rich_notification', _rich_timeout)

    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        service = _service()
        _no_price_lookup(service)

        status = await _initial(service, db, user, subscription)

        assert status == ms.NotificationDeliveryStatus.TIMEOUT
        assert await _markers(db) == {_pending_type(end_date): 1}


async def test_classic_timeout_keeps_pending_for_a_later_cycle(monkeypatch):
    """Break caught: a stuck classic send is ambiguous, so it retries later only."""
    _gates_on(monkeypatch)
    monkeypatch.setattr(settings, 'MONITORING_NOTIFICATION_SEND_TIMEOUT', 0.05)
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)

    async def _rich_unsupported(*_args, **_kwargs):
        return False

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(ms, 'try_send_rich_notification', _rich_unsupported)
    bot = MagicMock()
    bot.send_message = _hang
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        service = _service(bot=bot)
        _no_price_lookup(service)

        status = await _initial(service, db, user, subscription)

        assert status == ms.NotificationDeliveryStatus.TIMEOUT
        assert await _markers(db) == {_pending_type(end_date): 1}


# ---------------------------------------------------------------------------
# Permanent outcomes are not retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('error', 'expected_status'),
    [
        (TelegramForbiddenError(method=None, message='Forbidden: bot was blocked by the user'), 'unreachable'),
        (TelegramBadRequest(method=None, message='Bad Request: chat not found'), 'unreachable'),
        # A non-unreachable BadRequest is still a permanent Telegram rejection:
        # resending the exact same payload cannot fix it.
        (TelegramBadRequest(method=None, message='Bad Request: message is too long'), 'unreachable'),
    ],
)
async def test_permanent_telegram_errors_cancel_pending(monkeypatch, error, expected_status):
    """Break caught: permanent/blocked errors must not enter a retry loop."""
    _gates_on(monkeypatch)
    monkeypatch.setattr(settings, 'MONITORING_NOTIFICATION_SEND_TIMEOUT', 5)
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=False))
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=error)

    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        service = _service(bot=bot)
        _no_price_lookup(service)

        status = await _initial(service, db, user, subscription)

        assert status.value == expected_status
        assert await _markers(db) == {}
        bot.send_message.assert_awaited_once()


async def test_blocked_user_status_is_skipped_and_cancels_pending(monkeypatch):
    """Break caught: a blocked user must not be retried on later cycles."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date, user_status='blocked')
        service = _service()
        _no_price_lookup(service)

        status = await _initial(service, db, user, subscription)

        assert status == ms.NotificationDeliveryStatus.SKIPPED
        assert await _markers(db) == {}


# ---------------------------------------------------------------------------
# Gates and non-Telegram paths
# ---------------------------------------------------------------------------


async def test_global_gate_disabled_cancels_pending_without_sending(monkeypatch):
    """Break caught: retry must obey the global notification switch."""
    monkeypatch.setattr(
        ms.NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda user: True)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=end_date)
        db.add(SentNotification(user_id=42, subscription_id=7, notification_type=_pending_type(end_date), days_before=2))
        await db.commit()

        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )
        await _retry(service, db)

        service._send_subscription_expired_notification_result.assert_not_awaited()
        assert await _markers(db) == {}


async def test_user_gate_disabled_cancels_pending_without_sending(monkeypatch):
    """Break caught: retry must obey the per-user expiry preference."""
    monkeypatch.setattr(
        ms.NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda user: False)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=end_date)
        db.add(SentNotification(user_id=42, subscription_id=7, notification_type=_pending_type(end_date), days_before=2))
        await db.commit()

        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )
        await _retry(service, db)

        service._send_subscription_expired_notification_result.assert_not_awaited()
        assert await _markers(db) == {}


async def test_no_pending_state_when_no_initial_telegram_attempt_occurs(monkeypatch):
    """Break caught: email-only delivery must not create Telegram retry state."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _seed(db, end_date=end_date)
        user.telegram_id = None
        db.add(user)
        await db.commit()

        service = _service()

        async def _email(*_args, **_kwargs):
            return True

        monkeypatch.setattr(
            ms.notification_delivery_service,
            'send_notification',
            AsyncMock(side_effect=_email),
        )

        status = await _initial(service, db, user, subscription)

        assert status == ms.NotificationDeliveryStatus.SENT
        assert await _markers(db) == {}


async def test_expiring_end_date_still_matches_same_cycle_after_reload(monkeypatch):
    """Break caught: cycle key must use the subscription's stored end_date (tz-safe)."""
    _gates_on(monkeypatch)
    end_date = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=end_date)
        db.add(SentNotification(user_id=42, subscription_id=7, notification_type=_pending_type(end_date), days_before=2))
        await db.commit()

        service = _service()
        _no_price_lookup(service)
        service._send_subscription_expired_notification_result = AsyncMock(
            return_value=ms.NotificationDeliveryStatus.SENT
        )
        await _retry(service, db)

        service._send_subscription_expired_notification_result.assert_awaited_once()
        assert await _markers(db) == {_confirmed_type(end_date): None}
