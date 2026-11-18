"""Tests for the independent pending-payment / provider-error admin alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.models import PaymentMethod
from app.services import payment_alert_service
from app.services.admin_notification_service import (
    AdminNotificationService,
    NotificationCategory,
)
from app.services.payment_alert_service import (
    PaymentAlert,
    build_check_error_alert,
    build_pending_alert,
    dispatch_payment_alerts,
    format_payment_alerts_digest,
)
from app.services.payment_verification_service import PendingPayment


NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


def _record(
    *,
    method: PaymentMethod = PaymentMethod.YOOKASSA,
    local_id: int = 1,
    identifier: str = 'pay-1',
    amount_kopeks: int = 29900,
    status: str = 'pending',
    created_at: datetime | None = None,
    is_paid: bool = False,
) -> PendingPayment:
    return PendingPayment(
        method=method,
        local_id=local_id,
        identifier=identifier,
        amount_kopeks=amount_kopeks,
        status=status,
        is_paid=is_paid,
        created_at=created_at or NOW,
        user=SimpleNamespace(id=1),
        payment=None,
    )


def _alert(local_id: int = 1, *, kind: str = 'pending') -> PaymentAlert:
    return PaymentAlert(
        kind=kind,
        method=PaymentMethod.YOOKASSA,
        local_id=local_id,
        identifier=f'pay-{local_id}',
        amount_kopeks=29900,
        status='pending',
        age_minutes=30,
    )


class _FakeCache:
    """Minimal in-memory Redis double with setnx semantics."""

    def __init__(self) -> None:
        self._connected = True
        self.values: dict[str, object] = {}
        self.ttls: dict[str, int | None] = {}

    async def setnx(self, key: str, value: object, expire: int | None = None) -> bool:
        if key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = expire
        return True

    async def delete(self, key: str) -> bool:
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return True


class _DisconnectedCache:
    _connected = False


def _install_sender(monkeypatch: pytest.MonkeyPatch, *, result: bool = True) -> list[str]:
    sent: list[str] = []

    async def fake_send(self: AdminNotificationService, text: str) -> bool:
        sent.append(text)
        return result

    monkeypatch.setattr(AdminNotificationService, 'send_payment_alert', fake_send)
    return sent


# ---------------------------------------------------------------------------
# Alert construction boundaries
# ---------------------------------------------------------------------------


def test_pending_alert_boundary_uses_configured_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_PENDING_MINUTES', 30)

    record_29 = _record(created_at=NOW - timedelta(minutes=29))
    record_30 = _record(created_at=NOW - timedelta(minutes=30))

    assert build_pending_alert(record_29, NOW) is None

    alert = build_pending_alert(record_30, NOW)
    assert alert is not None
    assert alert.kind == 'pending'
    assert alert.method == PaymentMethod.YOOKASSA
    assert alert.local_id == 1
    assert alert.identifier == 'pay-1'
    assert alert.amount_kopeks == 29900
    assert alert.status == 'pending'
    assert alert.age_minutes == 30


def test_check_error_alert_has_no_exception_payload() -> None:
    record = _record(
        method=PaymentMethod.HELEKET,
        identifier='heleket-42',
        status='pending',
        created_at=NOW - timedelta(minutes=45),
    )

    alert = build_check_error_alert(record, NOW)

    assert alert.kind == 'check_error'
    assert alert.method == PaymentMethod.HELEKET
    assert alert.identifier == 'heleket-42'
    assert alert.status == 'pending'
    assert alert.age_minutes == 45
    # The builder receives only the payment record: nothing from an exception
    # can leak into the alert fields that end up in the digest.
    assert not hasattr(alert, 'error')


@pytest.mark.asyncio
async def test_digest_contains_sanitized_payment_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payment_alert_service, 'cache', _DisconnectedCache())
    payment_alert_service._MEMORY_CLAIMS.clear()
    sent = _install_sender(monkeypatch)

    alert = build_check_error_alert(
        _record(
            method=PaymentMethod.HELEKET,
            identifier='heleket-42',
            status='pending',
            created_at=NOW - timedelta(minutes=45),
        ),
        NOW,
    )

    assert await dispatch_payment_alerts(MagicMock(), [alert]) is True
    assert len(sent) == 1
    text = sent[0]
    assert 'Heleket' in text
    assert 'heleket-42' in text
    assert 'pending' in text
    assert '45' in text


# ---------------------------------------------------------------------------
# Cooldown: Redis and single-process memory fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_digest_sends_and_identical_keys_are_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cache = _FakeCache()
    monkeypatch.setattr(payment_alert_service, 'cache', fake_cache)
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_REPEAT_HOURS', 6)
    sent = _install_sender(monkeypatch)

    alert = _alert()

    assert await dispatch_payment_alerts(MagicMock(), [alert]) is True
    assert await dispatch_payment_alerts(MagicMock(), [alert]) is False

    assert len(sent) == 1
    key = 'payment_alert:pending:yookassa:1'
    assert fake_cache.ttls[key] == 6 * 3600


@pytest.mark.asyncio
async def test_cooldown_works_with_redis_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payment_alert_service, 'cache', _DisconnectedCache())
    payment_alert_service._MEMORY_CLAIMS.clear()
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_REPEAT_HOURS', 6)
    sent = _install_sender(monkeypatch)

    alert = _alert()

    assert await dispatch_payment_alerts(MagicMock(), [alert]) is True
    assert await dispatch_payment_alerts(MagicMock(), [alert]) is False

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_failed_delivery_releases_claim_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cache = _FakeCache()
    monkeypatch.setattr(payment_alert_service, 'cache', fake_cache)

    sent = _install_sender(monkeypatch, result=False)
    alert = _alert()

    assert await dispatch_payment_alerts(MagicMock(), [alert]) is False
    assert fake_cache.values == {}

    # Next cycle retries the same payment after the failed delivery.
    sent = _install_sender(monkeypatch, result=True)
    assert await dispatch_payment_alerts(MagicMock(), [alert]) is True
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Digest line cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_limits_item_lines_and_reports_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payment_alert_service, 'cache', _DisconnectedCache())
    payment_alert_service._MEMORY_CLAIMS.clear()
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_MAX_PER_CYCLE', 20)
    sent = _install_sender(monkeypatch)

    alerts = [_alert(local_id=index) for index in range(25)]

    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    text = sent[0]
    item_lines = [line for line in text.splitlines() if line.lstrip().startswith('•')]
    assert len(item_lines) == 20
    assert '… и ещё 5' in text


@pytest.mark.asyncio
async def test_cap_claims_only_delivered_items_and_delivers_the_rest_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(payment_alert_service, 'cache', _DisconnectedCache())
    payment_alert_service._MEMORY_CLAIMS.clear()
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_MAX_PER_CYCLE', 20)
    sent = _install_sender(monkeypatch)

    alerts = [_alert(local_id=index) for index in range(25)]

    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert await dispatch_payment_alerts(MagicMock(), alerts) is False

    assert len(sent) == 2
    first_lines = [line for line in sent[0].splitlines() if line.lstrip().startswith('•')]
    second_lines = [line for line in sent[1].splitlines() if line.lstrip().startswith('•')]
    assert len(first_lines) == 20
    assert len(second_lines) == 5
    # The five hidden payments are not locked out by the first digest's cooldown.
    assert 'pay-24' in sent[1]
    assert 'pay-0' not in sent[1]
    assert '… и ещё' not in sent[1]


@pytest.mark.asyncio
async def test_cap_keeps_hidden_items_unclaimed_with_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cache = _FakeCache()
    monkeypatch.setattr(payment_alert_service, 'cache', fake_cache)
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_MAX_PER_CYCLE', 1)
    sent = _install_sender(monkeypatch)

    alerts = [_alert(local_id=1), _alert(local_id=2)]

    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert 'pay-1' in sent[0]
    assert 'pay-2' not in sent[0]
    # Only the delivered payment holds a cooldown key.
    assert list(fake_cache.values) == ['payment_alert:pending:yookassa:1']

    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert 'pay-2' in sent[1]
    assert 'pay-1' not in sent[1]

    assert await dispatch_payment_alerts(MagicMock(), alerts) is False
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_cap_keeps_hidden_items_unclaimed_with_memory_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payment_alert_service, 'cache', _DisconnectedCache())
    payment_alert_service._MEMORY_CLAIMS.clear()
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_MAX_PER_CYCLE', 1)
    sent = _install_sender(monkeypatch)

    alerts = [_alert(local_id=1), _alert(local_id=2)]

    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert await dispatch_payment_alerts(MagicMock(), alerts) is False

    assert len(sent) == 2
    assert 'pay-1' in sent[0] and 'pay-2' not in sent[0]
    assert 'pay-2' in sent[1] and 'pay-1' not in sent[1]


@pytest.mark.asyncio
async def test_failed_digest_does_not_claim_items_beyond_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cache = _FakeCache()
    monkeypatch.setattr(payment_alert_service, 'cache', fake_cache)
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_MAX_PER_CYCLE', 1)

    _install_sender(monkeypatch, result=False)
    alerts = [_alert(local_id=1), _alert(local_id=2)]

    assert await dispatch_payment_alerts(MagicMock(), alerts) is False
    assert fake_cache.values == {}

    sent = _install_sender(monkeypatch, result=True)
    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert await dispatch_payment_alerts(MagicMock(), alerts) is True
    assert len(sent) == 2
    assert 'pay-1' in sent[0]
    assert 'pay-2' in sent[1]


def test_format_digest_still_caps_direct_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_MAX_PER_CYCLE', 2)

    text = format_payment_alerts_digest([_alert(local_id=index) for index in range(5)])

    item_lines = [line for line in text.splitlines() if line.lstrip().startswith('•')]
    assert len(item_lines) == 2
    assert '… и ещё 3' in text


# ---------------------------------------------------------------------------
# Admin delivery path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_payment_alert_calls_bypass_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYMENT_ALERTS_ENABLED', True)
    service = AdminNotificationService(MagicMock())
    service.chat_id = -100123456
    service.enabled = False
    service._send_message = AsyncMock(return_value=True)

    assert await service.send_payment_alert('payments') is True
    service._send_message.assert_awaited_once_with(
        'payments',
        category=NotificationCategory.PAYMENTS,
        bypass_master=True,
    )


@pytest.mark.asyncio
async def test_send_payment_alert_reaches_chat_with_master_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'PAYMENT_ALERTS_ENABLED', True)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=None)
    service = AdminNotificationService(bot)
    service.chat_id = -100123456
    service.enabled = False

    assert await service.send_payment_alert('payments') is True
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_send_message_stays_master_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', False)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=None)
    service = AdminNotificationService(bot)
    service.chat_id = -100123456
    service.enabled = False

    assert await service._send_message('ordinary', category=NotificationCategory.PAYMENTS) is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_payment_alert_requires_switch_and_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', False)
    bot = MagicMock()
    service = AdminNotificationService(bot)
    service.chat_id = -100123456
    service.enabled = False
    service._send_message = AsyncMock(return_value=True)

    monkeypatch.setattr(settings, 'PAYMENT_ALERTS_ENABLED', False)
    assert await service.send_payment_alert('payments') is False
    service._send_message.assert_not_awaited()

    monkeypatch.setattr(settings, 'PAYMENT_ALERTS_ENABLED', True)
    service.chat_id = None
    assert await service.send_payment_alert('payments') is False
    service._send_message.assert_not_awaited()
