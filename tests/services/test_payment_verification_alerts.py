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
    ManualCheckOutcome,
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
    check = AsyncMock(
        side_effect=lambda db, method, local_id, payment_service, **kwargs: ManualCheckOutcome(record=by_id[local_id])
    )
    monkeypatch.setattr('app.services.payment_verification_service.run_manual_check_outcome', check)

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

    async def failing_check(db, method, local_id, payment_service, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr('app.services.payment_verification_service.run_manual_check_outcome', failing_check)

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


@pytest.mark.asyncio
async def test_provider_exception_from_real_router_alerts_and_checks_next_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real run_manual_check router must not hide adapter exceptions."""

    secret = 'yookassa-timeout-secret'
    now = datetime.now(UTC)
    broken = _record(1, now - timedelta(minutes=5), identifier='pay-broken')
    healthy = _record(2, now - timedelta(minutes=5), identifier='pay-healthy')
    refreshed = _record(2, now, identifier='pay-healthy', status='succeeded', is_paid=True)

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[broken, healthy]),
    )

    lookups = AsyncMock(return_value=refreshed)
    monkeypatch.setattr('app.services.payment_verification_service.get_payment_record', lookups)

    status_check = AsyncMock(side_effect=[OSError(secret), {'payment': SimpleNamespace(id=2)}])
    payment_service = SimpleNamespace(bot=MagicMock(), get_yookassa_payment_status=status_check)

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    service.set_payment_service(payment_service)

    await service._run_checks([PaymentMethod.YOOKASSA])

    # Both payments went through the provider check: the failure did not abort
    # the cycle, and the next fresh payment was checked normally.
    assert status_check.await_count == 2
    assert lookups.await_count == 1
    assert session.committed is True
    assert session.rolled_back is True

    alerts = captured['alerts']
    assert [alert.kind for alert in alerts] == ['check_error']
    assert alerts[0].local_id == 1
    assert alerts[0].identifier == 'pay-broken'
    # The failure is reported even below the pending-age threshold.
    assert alerts[0].age_minutes < settings.PAYMENT_ALERT_PENDING_MINUTES
    # Exception text never reaches the alert or the digest.
    assert secret not in repr(alerts)


@pytest.mark.asyncio
async def test_empty_provider_answer_reports_pending_not_check_error(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    old = _record(1, now - timedelta(minutes=45), identifier='pay-old')
    young = _record(2, now - timedelta(minutes=5), identifier='pay-young')

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[old, young]),
    )
    # Adapters may turn their own transport error into an empty answer.
    status_check = AsyncMock(return_value=None)
    payment_service = SimpleNamespace(bot=MagicMock(), get_yookassa_payment_status=status_check)

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    service.set_payment_service(payment_service)

    await service._run_checks([PaymentMethod.YOOKASSA])

    assert status_check.await_count == 2
    alerts = captured['alerts']
    assert [alert.kind for alert in alerts] == ['pending']
    assert alerts[0].local_id == 1
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_missing_record_after_refresh_is_not_check_error(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    old = _record(1, now - timedelta(minutes=45))

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[old]),
    )
    monkeypatch.setattr(
        'app.services.payment_verification_service.get_payment_record',
        AsyncMock(return_value=None),
    )
    payment_service = SimpleNamespace(
        bot=MagicMock(),
        get_yookassa_payment_status=AsyncMock(return_value={'payment': SimpleNamespace(id=1)}),
    )

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    service.set_payment_service(payment_service)

    await service._run_checks([PaymentMethod.YOOKASSA])

    assert [alert.kind for alert in captured['alerts']] == ['pending']
    assert session.rolled_back is False


# ---------------------------------------------------------------------------
# Real adapters: errors swallowed by the provider client must still alert
# ---------------------------------------------------------------------------


class _ProviderStub:
    """Stand-in for the SDK client at the transport/provider boundary."""

    def __init__(self, *, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[str] = []

    async def get_payment_info(self, payment_id: str) -> dict | None:
        self.calls.append(payment_id)
        if self.error is not None:
            raise self.error
        return self.response


def _yookassa_row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        'id': 1,
        'yookassa_payment_id': 'yk-1',
        'status': 'pending',
        'is_paid': False,
        'payment_method_type': 'sbp',
        'transaction_id': None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _real_yookassa_service(provider: _ProviderStub):
    from app.services.payment_service import PaymentService

    service = PaymentService.__new__(PaymentService)
    service.bot = MagicMock()
    service.yookassa_service = provider
    return service


@pytest.mark.asyncio
async def test_real_yookassa_adapter_empty_answer_is_a_check_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """YooKassa SDK swallows transport errors into None; the worker must see a failure."""

    now = datetime.now(UTC)
    broken = _record(1, now - timedelta(minutes=5), identifier='pay-broken')
    row = _yookassa_row()
    provider = _ProviderStub(response=None)

    monkeypatch.setattr(
        'app.services.payment_service.get_yookassa_payment_by_local_id',
        AsyncMock(return_value=row),
    )

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[broken]),
    )

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    service.set_payment_service(_real_yookassa_service(provider))

    await service._run_checks([PaymentMethod.YOOKASSA])

    assert provider.calls == ['yk-1']
    assert [alert.kind for alert in captured['alerts']] == ['check_error']
    assert captured['alerts'][0].local_id == 1
    assert session.rolled_back is True
    assert session.committed is True


@pytest.mark.asyncio
async def test_real_yookassa_adapter_unchanged_status_is_not_a_check_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider answer with the same status stays a silent no-op."""

    now = datetime.now(UTC)
    fresh = _record(1, now - timedelta(minutes=5))
    row = _yookassa_row()
    provider = _ProviderStub(
        response={'status': 'pending', 'paid': False, 'payment_method_type': 'sbp', 'payment_method_saved': False}
    )

    async def fake_update(db, payment_id, **kwargs):
        row.status = kwargs['status']
        row.is_paid = kwargs['is_paid']
        return row

    monkeypatch.setattr(
        'app.services.payment_service.get_yookassa_payment_by_local_id',
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        'app.services.payment_service.update_yookassa_payment_status',
        fake_update,
    )
    monkeypatch.setattr(
        'app.services.payment_verification_service.get_payment_record',
        AsyncMock(return_value=fresh),
    )

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[fresh]),
    )

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    service.set_payment_service(_real_yookassa_service(provider))

    await service._run_checks([PaymentMethod.YOOKASSA])

    assert provider.calls == ['yk-1']
    assert captured['calls'] == 0
    assert session.rolled_back is False
    assert session.committed is True


class _HeleketProviderStub:
    """Heleket HTTP client stub: both direct status and history come back empty."""

    def __init__(self) -> None:
        self.info_calls: list[dict] = []
        self.list_calls: list[dict] = []

    async def get_payment_info(self, *, uuid=None, order_id=None):
        self.info_calls.append({'uuid': uuid, 'order_id': order_id})

    async def list_payments(self, **kwargs):
        self.list_calls.append(kwargs)


@pytest.mark.asyncio
async def test_real_heleket_adapter_empty_answer_is_a_check_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heleket client also returns None on transport errors; the worker must alert."""

    from app.services.payment_service import PaymentService

    now = datetime.now(UTC)
    broken = _record(
        1,
        now - timedelta(minutes=5),
        method=PaymentMethod.HELEKET,
        identifier='heleket-1',
    )
    row = SimpleNamespace(id=1, uuid='h-1', order_id='o-1', status='check', user_id=1)
    provider = _HeleketProviderStub()

    payment_service = PaymentService.__new__(PaymentService)
    payment_service.bot = MagicMock()
    payment_service.heleket_service = provider

    monkeypatch.setattr(
        'app.database.crud.heleket.get_heleket_payment_by_id',
        AsyncMock(return_value=row),
    )

    events: list[str] = []
    session = _FakeSession(events)
    monkeypatch.setattr('app.services.payment_verification_service.AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(
        'app.services.payment_verification_service.list_recent_pending_payments',
        AsyncMock(return_value=[broken]),
    )

    captured = _install_dispatch_capture(monkeypatch, events)
    service = AutoPaymentVerificationService()
    service.set_payment_service(payment_service)

    await service._run_checks([PaymentMethod.HELEKET])

    assert provider.info_calls == [{'uuid': 'h-1', 'order_id': 'o-1'}]
    assert provider.list_calls  # history fallback was tried before failing
    assert [alert.kind for alert in captured['alerts']] == ['check_error']
    assert session.rolled_back is True
    assert session.committed is True
