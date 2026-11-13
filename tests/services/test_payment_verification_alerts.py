"""AutoPaymentVerificationService emits payment alerts only after the DB commit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.models import PaymentMethod
from app.services import payment_alert_service
from app.services.admin_notification_service import AdminNotificationService
from app.services.payment_alert_service import dispatch_payment_alerts as real_dispatch
from app.services.payment_verification_service import (
    AutoPaymentVerificationService,
    PendingPayment,
)


class _FakeSession:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events: list[str] = events if events is not None else []
        self.committed = False
        self.rolled_back = False

    def in_transaction(self) -> bool:
        return True

    async def commit(self) -> None:
        self.committed = True
        self.events.append('commit')

    async def rollback(self) -> None:
        self.rolled_back = True
        self.events.append('rollback')

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _record(
    local_id: int,
    created_at: datetime,
    *,
    method: PaymentMethod = PaymentMethod.YOOKASSA,
    identifier: str | None = None,
    status: str = 'pending',
    is_paid: bool = False,
) -> PendingPayment:
    return PendingPayment(
        method=method,
        local_id=local_id,
        identifier=identifier or f'pay-{local_id}',
        amount_kopeks=29900,
        status=status,
        is_paid=is_paid,
        created_at=created_at,
        user=SimpleNamespace(id=1),
        payment=None,
    )


def _payment_service() -> SimpleNamespace:
    return SimpleNamespace(bot=MagicMock())


def _install_dispatch_capture(monkeypatch: pytest.MonkeyPatch, events: list[str] | None = None) -> dict:
    captured: dict = {'events': events if events is not None else [], 'calls': 0}

    async def fake_dispatch(bot, alerts) -> bool:
        captured['calls'] += 1
        captured['events'].append('dispatch')
        captured['bot'] = bot
        captured['alerts'] = list(alerts)
        return True

    monkeypatch.setattr(payment_alert_service, 'dispatch_payment_alerts', fake_dispatch)
    return captured


@pytest.mark.asyncio
async def test_pending_alert_is_dispatched_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PAYMENT_ALERT_PENDING_MINUTES', 30)
    now = datetime.now(UTC)
    old = _record(1, now - timedelta(minutes=40))
    young = _record(2, now - timedelta(minutes=5))
    by_id = {1: old, 2: young}

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[old, young]),
    )
    check = AsyncMock(side_effect=lambda db, method, local_id, payment_service: by_id[local_id])
    monkeypatch.setattr('app.services.payment_verification_service.run_manual_check', check)

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    payment_service = _payment_service()
    service.set_payment_service(payment_service)

    await service._run_checks([PaymentMethod.YOOKASSA])

    # Existing verification behavior is untouched: both candidates are checked
    # and the successful session is committed.
    assert check.await_count == 2
    assert session.committed is True
    assert session.rolled_back is False
    assert session.events == ['commit', 'dispatch']
    assert captured['calls'] == 1
    assert captured['bot'] is payment_service.bot

    alerts = captured['alerts']
    assert [alert.kind for alert in alerts] == ['pending']
    assert alerts[0].local_id == 1
    assert alerts[0].age_minutes >= 30


@pytest.mark.asyncio
async def test_check_error_alert_is_sanitized_and_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = 'provider-secret-abc123'
    now = datetime.now(UTC)
    record = _record(7, now - timedelta(minutes=45), identifier='heleket-7', method=PaymentMethod.HELEKET)

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[record]),
    )

    async def failing_check(db, method, local_id, payment_service):
        raise RuntimeError(secret)

    monkeypatch.setattr('app.services.payment_verification_service.run_manual_check', failing_check)

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    service.set_payment_service(_payment_service())

    await service._run_checks([PaymentMethod.HELEKET])

    assert captured['calls'] == 1
    alerts = captured['alerts']
    assert [alert.kind for alert in alerts] == ['check_error']
    assert alerts[0].identifier == 'heleket-7'
    assert secret not in repr(alerts)
    assert 'dispatch' in session.events

    # Render the real digest for the collected alert: provider details are
    # present, the exception payload is not.
    sent: list[str] = []

    async def fake_send(self: AdminNotificationService, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(AdminNotificationService, 'send_payment_alert', fake_send)
    monkeypatch.setattr(payment_alert_service, 'cache', SimpleNamespace(_connected=False))
    payment_alert_service._MEMORY_CLAIMS.clear()

    assert await real_dispatch(MagicMock(), alerts) is True
    assert len(sent) == 1
    text = sent[0]
    assert 'Heleket' in text
    assert 'heleket-7' in text
    assert 'pending' in text
    assert '45' in text
    assert secret not in text
