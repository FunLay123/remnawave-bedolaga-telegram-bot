"""Regression tests for paid guest-purchase fulfillment orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.database.models import GuestPurchaseStatus
from app.services.guest_purchase_service import GuestPurchaseError, fulfill_purchase
from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason


@pytest.mark.asyncio
async def test_fulfill_purchase_passes_purchase_context_to_user_resolution():
    purchase = SimpleNamespace(
        id=17,
        token='a' * 64,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=False,
        gift_recipient_type=None,
        gift_recipient_value=None,
        contact_type='email',
        contact_value='buyer@example.com',
        tariff_id=23,
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = purchase
    db = AsyncMock()
    db.execute.return_value = result
    user = SimpleNamespace(id=42, language='ru')

    with (
        patch(
            'app.services.guest_purchase_service._find_or_create_user',
            AsyncMock(return_value=(user, True)),
        ) as find_or_create_user,
        patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=None)),
        # Перед созданием юзера fulfillment перепроверяет invite-only политику. На
        # моке сессии чтение настройки падает и честно закрывается 503 — это своя
        # проверка (test_invite_only_landing), здесь она увела бы тест от предмета.
        patch(
            'app.services.guest_purchase_service.evaluate_guest_purchase_registration',
            AsyncMock(
                return_value=(None, RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_ONLY_DISABLED))
            ),
        ),
    ):
        with pytest.raises(GuestPurchaseError, match='Tariff not found'):
            await fulfill_purchase(db, purchase.token)

    find_or_create_user.assert_awaited_once_with(
        db,
        'email',
        'buyer@example.com',
        purchase=purchase,
        pre_resolved_telegram_id=None,
        tariff_id=23,
    )
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_fulfill_expired_subscription_passes_tariff_and_wakes_premium_worker(monkeypatch):
    """Landing fulfillment must not pre-assign tariff_id and must wake the worker.

    The expired-subscription branch used to set ``existing_subscription.tariff_id``
    before calling ``replace_subscription`` without ``tariff_id``. The premium
    snapshot refresh keys off that argument, so the old 3 GB trial limit survived.
    """
    purchase = SimpleNamespace(
        id=17,
        token='b' * 64,
        status=GuestPurchaseStatus.PAID.value,
        is_gift=False,
        gift_recipient_type=None,
        gift_recipient_value=None,
        contact_type='email',
        contact_value='buyer@example.com',
        tariff_id=23,
        period_days=30,
        payment_method='yookassa',
        payment_id='pay-1',
        amount_kopeks=10_000,
        subid=None,
        cabinet_password=None,
        subscription_url=None,
        subscription_crypto_link=None,
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = purchase
    db = AsyncMock()
    db.execute.return_value = result

    user = SimpleNamespace(id=42, language='ru', remnawave_id=777)
    tariff = SimpleNamespace(
        id=23,
        name='Тариф',
        allowed_squads=['squad-1'],
        traffic_limit_gb=20,
        device_limit=3,
        get_price_for_period=lambda days: 10_000,
    )
    expired = SimpleNamespace(id=10, is_active=False)
    subscription = SimpleNamespace(
        id=10,
        remnawave_id=None,
        subscription_url='https://sub.example/u',
        subscription_crypto_link=None,
    )
    replace = AsyncMock(return_value=subscription)
    wake = MagicMock()
    create_panel_user = AsyncMock()

    class _Service:
        async def create_remnawave_user(self, db, subscription):
            await create_panel_user(db, subscription)

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    with (
        patch(
            'app.services.guest_purchase_service._find_or_create_user',
            AsyncMock(return_value=(user, False)),
        ),
        patch(
            'app.services.guest_purchase_service.evaluate_guest_purchase_registration',
            AsyncMock(
                return_value=(None, RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_ONLY_DISABLED))
            ),
        ),
        patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=tariff)),
        patch('app.services.guest_purchase_service.get_subscription_by_user_id', AsyncMock(return_value=expired)),
        patch('app.services.guest_purchase_service.replace_subscription', replace),
        patch('app.services.guest_purchase_service.SubscriptionService', return_value=_Service()),
        patch('app.services.guest_purchase_service.request_premium_traffic_refresh', wake),
        patch('app.services.guest_purchase_service.send_guest_notification', AsyncMock()),
        patch('app.services.guest_purchase_service._send_admin_notification', AsyncMock()),
        patch('app.services.guest_purchase_service._create_nalogo_receipt_for_purchase', AsyncMock()),
        patch('app.services.guest_purchase_service._attribute_purchase_campaign', AsyncMock()),
        patch('app.services.guest_purchase_service.create_transaction', AsyncMock()),
        patch('app.utils.cache.cache.get', AsyncMock(return_value=None)),
    ):
        await fulfill_purchase(db, purchase.token)

    assert replace.await_args.kwargs['tariff_id'] == 23
    wake.assert_called_once_with(subscription, 777, tariff=tariff)
    create_panel_user.assert_awaited_once_with(db, subscription)
