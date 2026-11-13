"""Autopay notification cadence: one merged card per expiry cycle, typed delivery outcomes.

The cadence is driven by persistent ``SentNotification`` markers, not by the
three-hour in-memory/Redis timestamp.  A confirmed delivery records
``autopay_attention`` (plus the planned ``expiring`` marker) for the warning
day; an ambiguous timeout records ``autopay_attention_unconfirmed`` and stops
the cycle; an explicit failure records nothing so a later eligible attempt can
retry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.config import settings
from app.services import monitoring_service as ms
from app.services.monitoring_service import MonitoringService


ATTENTION = 'autopay_attention'
ATTENTION_UNCONFIRMED = 'autopay_attention_unconfirmed'
EXPIRING = 'expiring'


class MarkerStore:
    """Minimal in-memory stand-in for the SentNotification CRUD boundary."""

    def __init__(self) -> None:
        self.markers: set[tuple] = set()

    async def notification_sent(self, db, user_id, subscription_id, notification_type, days_before=None):
        return (user_id, subscription_id, notification_type, days_before) in self.markers

    async def record_notification(self, db, user_id, subscription_id, notification_type, days_before=None, *, commit=True):
        self.markers.add((user_id, subscription_id, notification_type, days_before))


def _install_policy(monkeypatch, *, warning_days='3,1') -> MarkerStore:
    store = MarkerStore()
    monkeypatch.setattr(settings, 'AUTOPAY_WARNING_DAYS', warning_days)
    monkeypatch.setattr(
        ms.NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda user: True)
    monkeypatch.setattr(ms, 'notification_sent', store.notification_sent)
    monkeypatch.setattr(ms, 'record_notification', store.record_notification)
    return store


def _user(*, telegram_id=555, expiry_days=None) -> SimpleNamespace:
    prefs = {} if expiry_days is None else {'subscription_expiry_days': expiry_days}
    return SimpleNamespace(
        id=1,
        telegram_id=telegram_id,
        balance_kopeks=0,
        language='ru',
        notification_settings=prefs,
    )


def _sub(*, end_date: datetime, subscription_id=7, autopay_enabled=True) -> SimpleNamespace:
    return SimpleNamespace(
        id=subscription_id,
        user_id=1,
        end_date=end_date,
        autopay_enabled=autopay_enabled,
        tariff=None,
    )


async def _run_failure(
    monkeypatch,
    *,
    hours_left: float,
    status,
    now: datetime | None = None,
    user=None,
    subscription=None,
    cause='insufficient_balance',
):
    """Invoke the failure handler against the persistent-marker store."""
    now = now or datetime.now(UTC)
    user = user if user is not None else _user()
    subscription = subscription if subscription is not None else _sub(end_date=now + timedelta(hours=hours_left))
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=status)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)
    await service._maybe_notify_autopay_failure(
        user,
        15000,
        subscription,
        now,
        cause=cause,
        db=object(),
    )
    return send


# ── Delivery outcome contract ──────────────────────────────────────────


def test_delivery_status_enum_exposes_required_outcomes():
    assert {status.name for status in ms.NotificationDeliveryStatus} == {
        'SENT',
        'FAILED',
        'TIMEOUT',
        'SKIPPED',
        'UNREACHABLE',
    }


async def test_rich_delivery_without_message_is_still_sent(monkeypatch):
    """A rich send returns no aiogram Message, but it did deliver."""
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=True))
    bot = MagicMock()
    bot.send_message = AsyncMock()

    service = MonitoringService(bot=bot)
    result = await service._send_message_with_logo_result(chat_id=1, text='hi')

    assert result.status is ms.NotificationDeliveryStatus.SENT
    assert result.message is None
    bot.send_message.assert_not_awaited()


async def test_delivery_timeout_is_distinct_from_sent(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(settings, 'MONITORING_NOTIFICATION_SEND_TIMEOUT', 0.01)
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=False))

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(5)

    bot = MagicMock()
    bot.send_message = _hang

    service = MonitoringService(bot=bot)
    result = await service._send_message_with_logo_result(chat_id=1, text='hi')

    assert result.status is ms.NotificationDeliveryStatus.TIMEOUT
    assert result.message is None


async def test_skipped_without_chat_id(monkeypatch):
    service = MonitoringService(bot=MagicMock())
    result = await service._send_message_with_logo_result(chat_id=None, text='hi')
    assert result.status is ms.NotificationDeliveryStatus.SKIPPED


async def test_legacy_sender_returns_message_and_accepts_notification_type(monkeypatch):
    """Compatibility wrapper keeps returning the aiogram Message as before."""
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=False))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value='the-message')

    service = MonitoringService(bot=bot)
    result = await service._send_message_with_logo(
        chat_id=1,
        text='hi',
        notification_type='expired_discount_wave2',
    )

    assert result == 'the-message'


async def test_autopay_failed_result_is_sent_when_rich_delivers_without_message(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=True))
    bot = MagicMock()
    bot.send_message = AsyncMock()

    now = datetime.now(UTC)
    service = MonitoringService(bot=bot)
    status = await service._send_autopay_failed_notification_result(
        _user(),
        0,
        15000,
        subscription=_sub(end_date=now + timedelta(hours=72)),
        merge_with_expiry=True,
    )

    assert status is ms.NotificationDeliveryStatus.SENT
    bot.send_message.assert_not_awaited()


async def test_autopay_failed_result_maps_blocked_user_to_unreachable(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=False))
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramForbiddenError(method=None, message='Forbidden: bot was blocked by the user')
    )

    now = datetime.now(UTC)
    service = MonitoringService(bot=bot)
    status = await service._send_autopay_failed_notification_result(
        _user(),
        0,
        15000,
        subscription=_sub(end_date=now + timedelta(hours=72)),
        merge_with_expiry=True,
    )

    assert status is ms.NotificationDeliveryStatus.UNREACHABLE


async def test_autopay_failed_result_maps_other_bad_request_to_failed(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=False))
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message='Bad Request: message text is empty')
    )

    now = datetime.now(UTC)
    service = MonitoringService(bot=bot)
    status = await service._send_autopay_failed_notification_result(
        _user(),
        0,
        15000,
        subscription=_sub(end_date=now + timedelta(hours=72)),
        merge_with_expiry=True,
    )

    assert status is ms.NotificationDeliveryStatus.FAILED


# ── Day computation and exact warning-day membership ───────────────────


def test_warning_day_uses_ceil_hours_over_24():
    assert ms.autopay_warning_day(72.0) == 3
    assert ms.autopay_warning_day(71.9) == 3
    assert ms.autopay_warning_day(48.0) == 2
    assert ms.autopay_warning_day(47.9) == 2
    assert ms.autopay_warning_day(24.0) == 1
    assert ms.autopay_warning_day(23.9) == 1
    assert ms.autopay_warning_day(0.0) == 1
    assert ms.autopay_warning_day(-5.0) == 1


async def test_unconfigured_intermediate_day_is_silent(monkeypatch):
    """48h is day 2, which is absent from the default [3, 1] list."""
    store = _install_policy(monkeypatch)

    send = await _run_failure(monkeypatch, hours_left=48, status=ms.NotificationDeliveryStatus.SENT)

    send.assert_not_awaited()
    assert store.markers == set()


async def test_configured_day_three_sends_one_merged_card(monkeypatch):
    store = _install_policy(monkeypatch)

    send = await _run_failure(monkeypatch, hours_left=72, status=ms.NotificationDeliveryStatus.SENT)

    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs['merge_with_expiry'] is True
    assert kwargs.get('is_final', False) is False
    assert store.markers == {
        (1, 7, ATTENTION, 3),
        (1, 7, EXPIRING, 3),
    }


async def test_user_window_can_exclude_an_otherwise_configured_day(monkeypatch):
    _install_policy(monkeypatch)
    user = _user(expiry_days=1)

    outside = await _run_failure(
        monkeypatch,
        hours_left=72,
        status=ms.NotificationDeliveryStatus.SENT,
        user=user,
    )
    inside = await _run_failure(
        monkeypatch,
        hours_left=24,
        status=ms.NotificationDeliveryStatus.SENT,
        user=user,
    )

    outside.assert_not_awaited()
    inside.assert_awaited_once()


# ── One card per cycle ─────────────────────────────────────────────────


async def test_failures_on_days_three_two_one_send_exactly_once(monkeypatch):
    store = _install_policy(monkeypatch)
    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=72))
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.SENT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)

    for elapsed_hours in (0, 24, 48):  # 72h, 48h and 24h left → days 3, 2, 1
        await service._maybe_notify_autopay_failure(
            _user(),
            15000,
            subscription,
            now + timedelta(hours=elapsed_hours),
            db=object(),
        )

    assert send.await_count == 1
    assert (1, 7, ATTENTION, 3) in store.markers


async def test_unconfigured_day_then_configured_day_notifies_once(monkeypatch):
    _install_policy(monkeypatch)
    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=48))
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.SENT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)

    # 48h → day 2 (unconfigured), then 24h → day 1 (configured).
    await service._maybe_notify_autopay_failure(_user(), 15000, subscription, now, db=object())
    await service._maybe_notify_autopay_failure(
        _user(), 15000, subscription, now + timedelta(hours=24), db=object()
    )

    assert send.await_count == 1


async def test_late_schedule_does_not_fabricate_an_earlier_failure(monkeypatch):
    _install_policy(monkeypatch)
    now = datetime.now(UTC)
    # autopay_days_before=1: the first attempt can only land in the last day.
    subscription = _sub(end_date=now + timedelta(hours=40))
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.SENT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)

    await service._maybe_notify_autopay_failure(_user(), 15000, subscription, now, db=object())
    await service._maybe_notify_autopay_failure(
        _user(), 15000, subscription, now + timedelta(hours=20), db=object()
    )

    assert send.await_count == 1


async def test_direct_jump_into_warning_window_sends_one_card(monkeypatch):
    _install_policy(monkeypatch)
    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=2))
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.SENT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)

    await service._maybe_notify_autopay_failure(_user(), 15000, subscription, now, db=object())
    await service._maybe_notify_autopay_failure(_user(), 15000, subscription, now, db=object())

    assert send.await_count == 1
    assert send.await_args.kwargs.get('is_final', False) is False


async def test_renewal_clears_attention_and_new_cycle_notifies(monkeypatch):
    store = _install_policy(monkeypatch)
    now = datetime.now(UTC)
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.SENT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)

    await service._maybe_notify_autopay_failure(
        _user(), 15000, _sub(end_date=now + timedelta(hours=72)), now, db=object()
    )
    assert send.await_count == 1

    # A successful renewal advances end_date and existing cleanup calls clear_notifications.
    store.markers.clear()
    later = now + timedelta(days=30)
    renewed_end_date = later + timedelta(hours=72)

    await service._maybe_notify_autopay_failure(
        _user(), 15000, _sub(end_date=renewed_end_date), later, db=object()
    )

    assert send.await_count == 2
    assert (1, 7, ATTENTION, 3) in store.markers


# ── Autopay-off manual cadence stays; autopay-on generic warnings do not ──


def _expiring_check_service(subscription, user):
    service = MonitoringService(bot=object())
    service._get_expiring_paid_subscriptions = AsyncMock(return_value=[subscription])
    service._resolve_renewal_cost_kopeks = AsyncMock(return_value=15000)
    service._log_monitoring_event = AsyncMock()
    service._send_subscription_expiring_notification = AsyncMock(return_value=True)
    return service


async def _run_expiring_check(monkeypatch, service, user):
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())
    await service._check_expiring_subscriptions(SimpleNamespace(execute=AsyncMock()))


async def test_autopay_off_keeps_manual_expiry_warning(monkeypatch):
    _install_policy(monkeypatch)
    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=20), autopay_enabled=False)
    service = _expiring_check_service(subscription, _user())

    await _run_expiring_check(monkeypatch, service, _user())

    service._send_subscription_expiring_notification.assert_awaited_once()


async def test_autopay_enabled_skips_all_generic_planned_warnings(monkeypatch):
    _install_policy(monkeypatch)
    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=20), autopay_enabled=True)
    service = _expiring_check_service(subscription, _user())

    await _run_expiring_check(monkeypatch, service, _user())

    service._send_subscription_expiring_notification.assert_not_awaited()
    service._resolve_renewal_cost_kopeks.assert_not_awaited()


# ── Failure causes and retry semantics ─────────────────────────────────


async def test_charge_error_is_quiet_and_consumes_no_warning(monkeypatch):
    store = _install_policy(monkeypatch)

    send = await _run_failure(
        monkeypatch,
        hours_left=72,
        status=ms.NotificationDeliveryStatus.SENT,
        cause='charge_error',
    )

    send.assert_not_awaited()
    assert store.markers == set()


async def test_charge_error_is_quiet_for_email_users_too(monkeypatch):
    _install_policy(monkeypatch)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(ms.notification_delivery_service, 'notify_autopay_failed', notify)

    await _run_failure(
        monkeypatch,
        hours_left=72,
        status=ms.NotificationDeliveryStatus.SENT,
        cause='charge_error',
        user=_user(telegram_id=None),
    )

    notify.assert_not_awaited()


async def test_email_user_gets_one_balance_warning_per_cycle(monkeypatch):
    store = _install_policy(monkeypatch)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(ms.notification_delivery_service, 'notify_autopay_failed', notify)

    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=72))
    service = MonitoringService(bot=None)

    await service._maybe_notify_autopay_failure(
        _user(telegram_id=None), 15000, subscription, now, db=object()
    )
    await service._maybe_notify_autopay_failure(
        _user(telegram_id=None), 15000, subscription, now + timedelta(hours=48), db=object()
    )

    assert notify.await_count == 1
    assert (1, 7, ATTENTION, 3) in store.markers


async def test_explicit_failure_records_nothing_and_retries(monkeypatch):
    store = _install_policy(monkeypatch)
    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=72))
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.FAILED)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)

    await service._maybe_notify_autopay_failure(_user(), 15000, subscription, now, db=object())
    await service._maybe_notify_autopay_failure(_user(), 15000, subscription, now, db=object())

    assert send.await_count == 2
    assert store.markers == set()


async def test_timeout_suppresses_later_attempts_for_the_cycle(monkeypatch):
    store = _install_policy(monkeypatch)
    now = datetime.now(UTC)
    subscription = _sub(end_date=now + timedelta(hours=72))
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.TIMEOUT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)

    await service._maybe_notify_autopay_failure(_user(), 15000, subscription, now, db=object())
    # A later eligible tick must not duplicate a possibly-delivered card.
    await service._maybe_notify_autopay_failure(
        _user(), 15000, subscription, now + timedelta(hours=48), db=object()
    )

    assert send.await_count == 1
    assert store.markers == {(1, 7, ATTENTION_UNCONFIRMED, 3)}
    assert (1, 7, ATTENTION, 3) not in store.markers
    assert (1, 7, EXPIRING, 3) not in store.markers
