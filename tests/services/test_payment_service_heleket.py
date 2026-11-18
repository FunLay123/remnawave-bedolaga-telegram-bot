import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud import heleket as heleket_crud
from app.services.payment.errors import PaymentStatusCheckError
from app.services.payment_service import PaymentService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummySession:
    def __init__(self) -> None:
        self.added_objects: list[Any] = []

    async def commit(self) -> None:  # pragma: no cover - behaviour is mocked in tests
        return None

    async def refresh(self, obj: Any) -> None:  # pragma: no cover
        return None

    def add(self, obj: Any) -> None:  # pragma: no cover
        self.added_objects.append(obj)


class DummyLocalPayment:
    def __init__(self, payment_id: int = 123) -> None:
        self.id = payment_id
        self.created_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


class StubHeleketService:
    def __init__(
        self,
        response: dict[str, Any] | None,
        *,
        info_response: dict[str, Any] | None = None,
        info_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.info_response = info_response
        self.info_error = info_error
        self.calls: list[dict[str, Any]] = []
        self.info_calls: list[dict[str, str | None]] = []
        self.list_response: dict[str, Any] | None = None
        self.list_calls: list[dict[str, str | None]] = []
        self.list_error: Exception | None = None

    async def create_payment(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append(payload)
        return self.response

    async def get_payment_info(
        self,
        *,
        uuid: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any] | None:
        self.info_calls.append({'uuid': uuid, 'order_id': order_id})
        if self.info_error is not None:
            raise self.info_error
        return self.info_response

    async def list_payments(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any] | None:
        self.list_calls.append({'date_from': date_from, 'date_to': date_to, 'cursor': cursor})
        if self.list_error is not None:
            raise self.list_error
        return self.list_response


def _make_service(stub: StubHeleketService | None) -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.heleket_service = stub
    service.yookassa_service = None
    service.stars_service = None
    service.cryptobot_service = None
    service.mulenpay_service = None
    service.pal24_service = None
    service.wata_service = None
    return service


@pytest.mark.anyio('asyncio')
async def test_create_heleket_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        'state': 0,
        'result': {
            'uuid': 'heleket-uuid',
            'order_id': 'order-123',
            'url': 'https://heleket/pay',
            'status': 'check',
            'payer_amount': '12.50',
            'payer_currency': 'USDT',
            'discount_percent': -5,
            'payer_amount_exchange_rate': '0.0125',
            'expired_at': 1750000000,
        },
    }
    stub = StubHeleketService(response)
    service = _make_service(stub)
    db = DummySession()

    captured_args: dict[str, Any] = {}

    async def fake_create_heleket_payment(**kwargs: Any) -> DummyLocalPayment:
        captured_args.update(kwargs)
        return DummyLocalPayment(payment_id=555)

    monkeypatch.setattr(
        heleket_crud,
        'create_heleket_payment',
        fake_create_heleket_payment,
        raising=False,
    )

    result = await service.create_heleket_payment(
        db=db,
        user_id=42,
        amount_kopeks=15000,
        description='Пополнение',
        language='ru',
    )

    assert result is not None
    assert result['local_payment_id'] == 555
    assert result['uuid'] == 'heleket-uuid'
    assert result['order_id'] == 'order-123'
    assert result['payment_url'] == 'https://heleket/pay'
    assert stub.calls and stub.calls[0]['amount'] == '150.00'
    assert captured_args['uuid'] == 'heleket-uuid'
    assert captured_args['user_id'] == 42


@pytest.mark.anyio('asyncio')
async def test_create_heleket_payment_returns_none_without_service() -> None:
    service = _make_service(None)
    db = DummySession()

    result = await service.create_heleket_payment(
        db=db,
        user_id=1,
        amount_kopeks=10000,
        description='Пополнение',
    )

    assert result is None


@pytest.mark.anyio('asyncio')
async def test_create_heleket_payment_handles_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubHeleketService(response=None)
    service = _make_service(stub)
    db = DummySession()

    called = False

    async def fake_create_heleket_payment(**kwargs: Any) -> DummyLocalPayment:
        nonlocal called
        called = True
        return DummyLocalPayment()

    monkeypatch.setattr(
        heleket_crud,
        'create_heleket_payment',
        fake_create_heleket_payment,
        raising=False,
    )

    result = await service.create_heleket_payment(
        db=db,
        user_id=1,
        amount_kopeks=20000,
        description='Пополнение',
    )

    assert result is None
    assert called is False


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_payment_status_success(monkeypatch: pytest.MonkeyPatch) -> None:
    info_response = {
        'state': 0,
        'result': {
            'uuid': 'heleket-uuid',
            'order_id': 'order-123',
            'status': 'paid',
            'payment_amount': '100.00',
        },
    }
    stub = StubHeleketService(response=None, info_response=info_response)
    service = _make_service(stub)
    db = DummySession()

    payment = SimpleNamespace(
        id=55,
        uuid='heleket-uuid',
        order_id='order-123',
        status='check',
        user_id=7,
    )

    async def fake_get_by_id(db, payment_id):
        assert payment_id == payment.id
        return payment

    captured: dict[str, Any] = {}

    async def fake_process(self, db, payload, *, metadata_key):
        captured['payload'] = payload
        captured['metadata_key'] = metadata_key
        return SimpleNamespace(transaction_id=999, **payload)

    monkeypatch.setattr(heleket_crud, 'get_heleket_payment_by_id', fake_get_by_id, raising=False)
    monkeypatch.setattr(PaymentService, '_process_heleket_payload', fake_process, raising=False)

    result = await service.sync_heleket_payment_status(db, local_payment_id=payment.id)

    assert result is not None
    assert result.transaction_id == 999
    assert captured['metadata_key'] == 'last_status_check'
    assert captured['payload']['uuid'] == payment.uuid
    assert stub.info_calls == [{'uuid': payment.uuid, 'order_id': payment.order_id}]


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_payment_status_without_response(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubHeleketService(response=None, info_response=None)
    service = _make_service(stub)
    db = DummySession()

    payment = SimpleNamespace(
        id=12,
        uuid='heleket-uuid',
        order_id='order-123',
        status='check',
        user_id=5,
    )

    async def fake_get_by_id(db, payment_id):
        assert payment_id == payment.id
        return payment

    async def fake_process(*args, **kwargs):  # pragma: no cover - ensure not called
        raise AssertionError('_process_heleket_payload should not be called')

    monkeypatch.setattr(heleket_crud, 'get_heleket_payment_by_id', fake_get_by_id, raising=False)
    monkeypatch.setattr(PaymentService, '_process_heleket_payload', fake_process, raising=False)

    result = await service.sync_heleket_payment_status(db, local_payment_id=payment.id)

    assert result is payment
    assert stub.info_calls == [{'uuid': payment.uuid, 'order_id': payment.order_id}]
    assert stub.list_calls  # fallback to history should be attempted


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_payment_status_history_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubHeleketService(response=None, info_response=None)
    stub.list_response = {
        'state': 0,
        'result': {
            'items': [
                {
                    'uuid': 'heleket-uuid',
                    'order_id': 'order-123',
                    'status': 'paid',
                    'payment_amount': '150.00',
                }
            ],
            'paginate': {'nextCursor': None},
        },
    }
    service = _make_service(stub)
    db = DummySession()

    payment = SimpleNamespace(
        id=77,
        uuid='heleket-uuid',
        order_id='order-123',
        status='check',
        user_id=8,
    )

    async def fake_get_by_id(db, payment_id):
        assert payment_id == payment.id
        return payment

    captured: dict[str, Any] = {}

    async def fake_process(self, db, payload, *, metadata_key):
        captured['payload'] = payload
        captured['metadata_key'] = metadata_key
        return SimpleNamespace(**payload)

    monkeypatch.setattr(heleket_crud, 'get_heleket_payment_by_id', fake_get_by_id, raising=False)
    monkeypatch.setattr(PaymentService, '_process_heleket_payload', fake_process, raising=False)

    result = await service.sync_heleket_payment_status(db, local_payment_id=payment.id)

    assert result is not None
    assert captured['payload']['status'] == 'paid'
    assert stub.list_calls


# ---------------------------------------------------------------------------
# Strict status checks for the background verification worker
# ---------------------------------------------------------------------------


def _heleket_status_payment(payment_id: int = 91) -> SimpleNamespace:
    return SimpleNamespace(
        id=payment_id,
        uuid='heleket-uuid',
        order_id='order-123',
        status='check',
        user_id=5,
    )


def _patch_heleket_lookup(monkeypatch: pytest.MonkeyPatch, payment: SimpleNamespace | None) -> None:
    async def fake_get_by_id(db: Any, payment_id: int) -> SimpleNamespace | None:
        return payment

    monkeypatch.setattr(heleket_crud, 'get_heleket_payment_by_id', fake_get_by_id, raising=False)


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_strict_raises_when_provider_and_history_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = StubHeleketService(response=None, info_response=None)
    service = _make_service(stub)
    payment = _heleket_status_payment()
    _patch_heleket_lookup(monkeypatch, payment)

    with pytest.raises(PaymentStatusCheckError):
        await service.sync_heleket_payment_status(DummySession(), local_payment_id=payment.id, strict=True)

    assert stub.info_calls
    assert stub.list_calls

    # Ручная/админская проверка по-прежнему получает неизменённую запись.
    result = await service.sync_heleket_payment_status(DummySession(), local_payment_id=payment.id)
    assert result is payment


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_strict_propagates_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubHeleketService(response=None, info_response=None, info_error=OSError('boom'))
    service = _make_service(stub)
    payment = _heleket_status_payment()
    _patch_heleket_lookup(monkeypatch, payment)

    with pytest.raises(PaymentStatusCheckError) as excinfo:
        await service.sync_heleket_payment_status(DummySession(), local_payment_id=payment.id, strict=True)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert 'boom' not in str(excinfo.value)

    # Manual/UI path keeps the old lenient behavior.
    lenient = await service.sync_heleket_payment_status(DummySession(), local_payment_id=payment.id)
    assert lenient is payment


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_strict_survives_history_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Резервный list-запрос нашёл платёж — проверка успешна, алерта нет."""

    stub = StubHeleketService(response=None, info_response=None, info_error=OSError('info failed'))
    stub.list_response = {
        'state': 0,
        'result': {
            'items': [
                {
                    'uuid': 'heleket-uuid',
                    'order_id': 'order-123',
                    'status': 'paid',
                    'payment_amount': '150.00',
                }
            ],
            'paginate': {'nextCursor': None},
        },
    }
    service = _make_service(stub)
    payment = _heleket_status_payment()
    _patch_heleket_lookup(monkeypatch, payment)

    captured: dict[str, Any] = {}

    async def fake_process(self: Any, db: Any, payload: dict[str, Any], *, metadata_key: str) -> SimpleNamespace:
        captured['payload'] = payload
        return SimpleNamespace(**payload)

    monkeypatch.setattr(PaymentService, '_process_heleket_payload', fake_process, raising=False)

    result = await service.sync_heleket_payment_status(DummySession(), local_payment_id=payment.id, strict=True)

    assert result is not None
    assert captured['payload']['status'] == 'paid'
    assert stub.info_calls
    assert stub.list_calls


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_strict_missing_local_payment_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Нет локальной строки — это не сбой провайдера, и алерта быть не должно."""

    stub = StubHeleketService(response=None, info_response=None)
    service = _make_service(stub)
    _patch_heleket_lookup(monkeypatch, None)

    result = await service.sync_heleket_payment_status(DummySession(), local_payment_id=404, strict=True)

    assert result is None
    assert stub.info_calls == []


@pytest.mark.anyio('asyncio')
async def test_sync_heleket_strict_accepts_unchanged_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Провайдер ответил тем же статусом — проверка успешна, ошибки нет."""

    stub = StubHeleketService(
        response=None,
        info_response={
            'state': 0,
            'result': {
                'uuid': 'heleket-uuid',
                'order_id': 'order-123',
                'status': 'check',
            },
        },
    )
    service = _make_service(stub)
    payment = _heleket_status_payment()
    _patch_heleket_lookup(monkeypatch, payment)

    async def fake_process(self: Any, db: Any, payload: dict[str, Any], *, metadata_key: str) -> None:
        return None

    monkeypatch.setattr(PaymentService, '_process_heleket_payload', fake_process, raising=False)

    result = await service.sync_heleket_payment_status(DummySession(), local_payment_id=payment.id, strict=True)

    assert result is payment
    assert stub.info_calls
    assert stub.list_calls == []
