"""Proactive and manual fallback offers after failed YooKassa/Heleket payments."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.balance import heleket as heleket_handlers, yookassa as yookassa_handlers
from app.services.payment_service import PaymentService


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshed: list[object] = []

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)


class _FakeCache:
    _connected = True

    def __init__(self) -> None:
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


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=7, telegram_id=777, language='ru')


def _yookassa_payment(
    status: str = 'pending',
    *,
    payment_method_type: str = 'bank_card',
) -> SimpleNamespace:
    return SimpleNamespace(
        id=17,
        yookassa_payment_id='yk_123456789',
        user_id=7,
        amount_kopeks=50000,
        transaction_id=None,
        status=status,
        is_paid=False,
        is_succeeded=False,
        is_pending=status == 'pending',
        is_failed=status in {'canceled', 'failed'},
        captured_at=None,
        created_at=datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC),
        payment_method_type=payment_method_type,
        confirmation_url=None,
        refundable=False,
        user=_user(),
    )


def _yookassa_service(
    bot: MagicMock,
    remote_status: str,
    *,
    payment_method_type: str = 'bank_card',
) -> PaymentService:
    service = PaymentService.__new__(PaymentService)
    service.bot = bot
    service.yookassa_service = SimpleNamespace(
        get_payment_info=AsyncMock(
            return_value={
                'id': 'yk_123456789',
                'status': remote_status,
                'paid': remote_status == 'succeeded',
                'payment_method_type': payment_method_type,
            }
        )
    )
    return service


# ---------------------------------------------------------------------------
# YooKassa proactive hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'payment_method_type', 'expected_method'),
    [
        ('canceled', 'bank_card', 'yookassa'),
        ('failed', 'bank_card', 'yookassa'),
        ('canceled', 'sbp', 'yookassa_sbp'),
    ],
)
async def test_yookassa_webhook_failure_sends_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    payment_method_type: str,
    expected_method: str,
) -> None:
    bot = MagicMock()
    service = _yookassa_service(bot, status, payment_method_type=payment_method_type)
    payment = _yookassa_payment(status='pending', payment_method_type=payment_method_type)

    async def fake_get_payment(db, payment_id):
        return payment

    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_id', fake_get_payment)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.payment_fallback_service.send_payment_fallback_once',
        sender,
    )

    result = await service.process_yookassa_webhook(
        _Session(),
        {
            'object': {
                'id': 'yk_123456789',
                'status': status,
                'paid': False,
                'payment_method': {'type': payment_method_type},
            }
        },
    )

    assert result is True
    sender.assert_awaited_once()
    args = sender.await_args.args
    assert args[0] is bot
    assert args[1] is payment.user
    assert args[2] == 50000
    assert args[3] == expected_method
    assert args[4] == 17


@pytest.mark.asyncio
async def test_yookassa_failure_is_offered_once_per_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cache = _FakeCache()
    monkeypatch.setattr('app.services.payment_fallback_service.cache', fake_cache)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    service = _yookassa_service(bot, 'canceled')
    payment = _yookassa_payment(status='pending')

    async def fake_get_payment(db, payment_id):
        return payment

    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_id', fake_get_payment)

    payload = {'object': {'id': 'yk_123456789', 'status': 'canceled', 'paid': False}}

    assert await service.process_yookassa_webhook(_Session(), payload) is True
    assert await service.process_yookassa_webhook(_Session(), payload) is True

    bot.send_message.assert_awaited_once()
    assert fake_cache.ttls['payment_fallback:yookassa:17'] == 24 * 3600


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'succeeded'])
async def test_yookassa_non_failure_status_never_sends_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    bot = MagicMock()
    service = _yookassa_service(bot, status)
    service._process_successful_yookassa_payment = AsyncMock(return_value=True)
    payment = _yookassa_payment(status='pending')

    async def fake_get_payment(db, payment_id):
        return payment

    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_id', fake_get_payment)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.payment_fallback_service.send_payment_fallback_once',
        sender,
    )

    await service.process_yookassa_webhook(
        _Session(),
        {'object': {'id': 'yk_123456789', 'status': status, 'paid': status == 'succeeded'}},
    )

    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_yookassa_status_sync_failure_sends_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = MagicMock()
    service = _yookassa_service(bot, 'failed')
    payment = _yookassa_payment(status='pending')

    async def fake_get_payment(db, local_id):
        return payment

    async def fake_update(db, payment_id, **kwargs):
        payment.status = kwargs['status']
        payment.is_paid = kwargs['is_paid']
        return payment

    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_local_id', fake_get_payment)
    monkeypatch.setattr('app.services.payment_service.update_yookassa_payment_status', fake_update)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.payment_fallback_service.send_payment_fallback_once',
        sender,
    )

    result = await service.get_yookassa_payment_status(_Session(), 17)

    assert result is not None
    assert result['status'] == 'failed'
    sender.assert_awaited_once()
    assert sender.await_args.args[3] == 'yookassa'


# ---------------------------------------------------------------------------
# Heleket proactive hook
# ---------------------------------------------------------------------------


def _heleket_payment(*, status: str = 'check', transaction_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=55,
        uuid='heleket-uuid',
        order_id='heleket-order',
        user_id=7,
        amount='500.00',
        amount_float=500.0,
        amount_kopeks=50000,
        status=status,
        transaction_id=transaction_id,
        payer_amount=None,
        payer_currency=None,
        exchange_rate=None,
        discount_percent=None,
        payment_url=None,
        paid_at=None,
        metadata_json={},
        user=_user(),
    )


def _install_heleket_crud(monkeypatch: pytest.MonkeyPatch, payment: SimpleNamespace) -> None:
    async def fake_get_by_uuid(db, uuid):
        return payment if uuid == payment.uuid else None

    async def fake_get_by_order_id(db, order_id):
        return payment if order_id == payment.order_id else None

    async def fake_update(db, uuid, *, status=None, **_kwargs):
        if status is not None:
            payment.status = status
        return payment

    async def fake_get_by_id_for_update(db, payment_id):
        return payment if payment_id == payment.id else None

    monkeypatch.setattr('app.database.crud.heleket.get_heleket_payment_by_uuid', fake_get_by_uuid)
    monkeypatch.setattr('app.database.crud.heleket.get_heleket_payment_by_order_id', fake_get_by_order_id)
    monkeypatch.setattr('app.database.crud.heleket.update_heleket_payment', fake_update)
    monkeypatch.setattr(
        'app.database.crud.heleket.get_heleket_payment_by_id_for_update',
        fake_get_by_id_for_update,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['cancel', 'fail', 'system_fail'])
async def test_heleket_payload_failure_sends_fallback_once(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    fake_cache = _FakeCache()
    monkeypatch.setattr('app.services.payment_fallback_service.cache', fake_cache)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    service = PaymentService.__new__(PaymentService)
    service.bot = bot
    service.heleket_service = None
    payment = _heleket_payment()
    _install_heleket_crud(monkeypatch, payment)
    payload = {'uuid': payment.uuid, 'status': status}

    assert await service._process_heleket_payload(_Session(), payload, metadata_key='test') is not None
    assert await service._process_heleket_payload(_Session(), payload, metadata_key='test') is not None

    bot.send_message.assert_awaited_once()
    assert fake_cache.ttls['payment_fallback:heleket:55'] == 24 * 3600


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'transaction_id'),
    [
        ('check', None),
        ('paid', 321),
        ('paid_over', 321),
        ('refund_paid', None),
    ],
)
async def test_heleket_non_failure_status_never_sends_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    transaction_id: int | None,
) -> None:
    bot = MagicMock()
    service = PaymentService.__new__(PaymentService)
    service.bot = bot
    service.heleket_service = None
    payment = _heleket_payment(transaction_id=transaction_id)
    _install_heleket_crud(monkeypatch, payment)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.payment_fallback_service.send_payment_fallback_once',
        sender,
    )

    await service._process_heleket_payload(
        _Session(),
        {'uuid': payment.uuid, 'status': status},
        metadata_key='test',
    )

    sender.assert_not_awaited()


# ---------------------------------------------------------------------------
# Manual status checks always render, independently of the proactive cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yookassa_manual_check_renders_fallback_after_proactive_message(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _yookassa_payment(status='canceled')

    async def fake_get_payment(db, local_id):
        return payment

    monkeypatch.setattr(
        'app.database.crud.yookassa.get_yookassa_payment_by_local_id',
        fake_get_payment,
    )
    monkeypatch.setattr(
        yookassa_handlers,
        'build_payment_fallback',
        lambda user, amount, method: ('fallback text', 'fallback markup'),
    )

    fake_cache = _FakeCache()
    fake_cache.values['payment_fallback:yookassa:17'] = '1'
    monkeypatch.setattr('app.services.payment_fallback_service.cache', fake_cache)

    callback = SimpleNamespace(
        data='check_yookassa_17',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    await yookassa_handlers.check_yookassa_payment_status(callback, _Session())
    await yookassa_handlers.check_yookassa_payment_status(callback, _Session())

    assert callback.message.edit_text.await_count == 2
    assert callback.message.edit_text.await_args.args[0] == 'fallback text'
    assert fake_cache.values  # the proactive claim is untouched by manual rendering


@pytest.mark.asyncio
async def test_yookassa_manual_check_does_not_render_for_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _yookassa_payment(status='pending')

    async def fake_get_payment(db, local_id):
        return payment

    monkeypatch.setattr(
        'app.database.crud.yookassa.get_yookassa_payment_by_local_id',
        fake_get_payment,
    )
    builder = MagicMock(return_value=('fallback text', 'fallback markup'))
    monkeypatch.setattr(yookassa_handlers, 'build_payment_fallback', builder)

    callback = SimpleNamespace(
        data='check_yookassa_17',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    await yookassa_handlers.check_yookassa_payment_status(callback, _Session())

    builder.assert_not_called()
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_heleket_manual_check_renders_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _yookassa_payment(status='cancel')
    payment.yookassa_payment_id = 'unused'
    payment.is_paid = False

    async def fake_get_payment(db, local_id):
        return payment

    monkeypatch.setattr(
        'app.database.crud.heleket.get_heleket_payment_by_id',
        fake_get_payment,
    )
    monkeypatch.setattr(
        heleket_handlers,
        'PaymentService',
        lambda bot: SimpleNamespace(sync_heleket_payment_status=AsyncMock(return_value=payment)),
    )
    monkeypatch.setattr(
        heleket_handlers,
        'build_payment_fallback',
        lambda user, amount, method: ('heleket fallback', 'fallback markup'),
    )

    callback = SimpleNamespace(
        data='check_heleket_55',
        bot=MagicMock(),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    await heleket_handlers.check_heleket_payment_status(callback, _Session())

    callback.message.edit_text.assert_awaited_once()
    assert callback.message.edit_text.await_args.args[0] == 'heleket fallback'
