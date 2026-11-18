"""Provider failures must stay distinguishable from "nothing changed"."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod
from app.services.payment_verification_service import (
    PendingPayment,
    run_manual_check,
    run_manual_check_outcome,
)


class _FakeSession:
    def __init__(self) -> None:
        self.rolled_back = False

    def in_transaction(self) -> bool:
        return True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeDb(_FakeSession):
    async def get(self, model: object, pk: int) -> object | None:
        return None


def _record(local_id: int = 1) -> PendingPayment:
    return PendingPayment(
        method=PaymentMethod.YOOKASSA,
        local_id=local_id,
        identifier=f'pay-{local_id}',
        amount_kopeks=29900,
        status='pending',
        is_paid=False,
        created_at=datetime.now(UTC),
        user=SimpleNamespace(id=1),
        payment=None,
    )


@pytest.mark.asyncio
async def test_provider_exception_is_reported_as_failed_outcome() -> None:
    secret = 'yookassa-secret-42'
    session = _FakeSession()
    payment_service = SimpleNamespace(
        get_yookassa_payment_status=AsyncMock(side_effect=OSError(secret)),
    )

    outcome = await run_manual_check_outcome(session, PaymentMethod.YOOKASSA, 5, payment_service)

    assert outcome.failed is True
    assert outcome.record is None
    assert session.rolled_back is True
    # The admin UI contract stays the same: a failed check looks like None there.
    assert await run_manual_check(session, PaymentMethod.YOOKASSA, 5, payment_service) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'adapter_result',
    [
        None,
        {'success': False, 'message': 'provider unavailable'},
    ],
    ids=['none', 'empty-dict'],
)
async def test_empty_provider_answer_is_not_a_failure(adapter_result: object) -> None:
    """Adapters may turn their own transport errors into an empty answer."""

    session = _FakeSession()
    payment_service = SimpleNamespace(
        get_yookassa_payment_status=AsyncMock(return_value=adapter_result),
    )

    outcome = await run_manual_check_outcome(session, PaymentMethod.YOOKASSA, 5, payment_service)

    assert outcome.failed is False
    assert outcome.record is None
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_missing_local_payment_is_not_a_failure() -> None:
    session = _FakeDb()

    outcome = await run_manual_check_outcome(session, PaymentMethod.SEVERPAY, 3, SimpleNamespace())

    assert outcome.failed is False
    assert outcome.record is None
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_unsupported_method_is_not_a_failure() -> None:
    session = _FakeSession()

    outcome = await run_manual_check_outcome(session, PaymentMethod.TRIBUTE, 1, SimpleNamespace())

    assert outcome.failed is False
    assert outcome.record is None


@pytest.mark.asyncio
async def test_successful_refresh_returns_record(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    record = _record()
    check = AsyncMock(return_value=record)
    monkeypatch.setattr('app.services.payment_verification_service.get_payment_record', check)
    payment_service = SimpleNamespace(
        get_yookassa_payment_status=AsyncMock(return_value={'payment': SimpleNamespace(id=1)}),
    )

    outcome = await run_manual_check_outcome(session, PaymentMethod.YOOKASSA, 5, payment_service)

    assert outcome.failed is False
    assert outcome.record is record
    assert session.rolled_back is False
    check.assert_awaited_once_with(session, PaymentMethod.YOOKASSA, 5)
    # Existing callers still get the record itself, not the wrapper.
    assert await run_manual_check(session, PaymentMethod.YOOKASSA, 5, payment_service) is record
