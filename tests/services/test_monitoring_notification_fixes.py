"""Регрессии уведомлений мониторинга: жадная загрузка промогрупп и шум в логах.

1. Выборки follow-up/expiring обязаны тянуть ``user.promo_group`` и
   ``user.user_promo_groups -> promo_group``. Иначе
   ``PricingEngine.resolve_promo_group()`` лениво грузит связь в async-контексте,
   получает ``MissingGreenlet``, и цена продления в уведомлении молча
   откатывается на ``PRICE_30_DAYS`` (скидка теряется).
2. ``_handle_unreachable_user`` и fallback ``_send_message_with_logo`` логируют
   ``str(exc)``, чтобы ``_auto_capture_exc_info`` не рисовал traceback для
   штатной недоступности пользователя.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from structlog.testing import capture_logs

from app.config import settings
from app.database.models import (
    PromoGroup,
    ServerSquad,
    Subscription,
    Tariff,
    User,
    UserPromoGroup,
    server_squad_promo_groups,
    tariff_promo_groups,
)
from app.services import monitoring_service as ms
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    ServerSquad.__table__,
    server_squad_promo_groups,
    Subscription.__table__,
)

PRICE_PER_30_DAYS = 10_000
DISCOUNT_PERCENT = 50
EXPECTED_DISCOUNTED_PRICE = 5_000


async def _seed(db, *, end_date: datetime, status: str) -> None:
    """Пользователь с промогруппой, тарифом и одной подпиской."""
    db.add(User(id=9, telegram_id=9009, first_name='Иван', language='ru', status='active', balance_kopeks=0))
    db.add(
        PromoGroup(
            id=3,
            name='Льготная',
            priority=10,
            period_discounts={'30': DISCOUNT_PERCENT},
        )
    )
    db.add(UserPromoGroup(user_id=9, promo_group_id=3, assigned_by='test'))
    db.add(
        Tariff(
            id=7,
            name='Стартовый',
            description='',
            is_active=True,
            traffic_limit_gb=50,
            device_limit=3,
            allowed_squads=['sq'],
            period_prices={'30': PRICE_PER_30_DAYS},
            display_order=1,
        )
    )
    await db.flush()
    db.add(
        Subscription(
            id=8,
            remnawave_short_id='s8',
            user_id=9,
            status=status,
            is_trial=False,
            start_date=end_date - timedelta(days=30),
            end_date=end_date,
            traffic_limit_gb=50,
            traffic_used_gb=0.0,
            device_limit=3,
            tariff_id=7,
            autopay_period_days=30,
            connected_squads=['sq'],
        )
    )
    await db.commit()
    # Дальше читаем только запросами — как в проде, где в identity map
    # нет заранее созданных объектов со «случайно» загруженными связями.
    db.expunge_all()


def _assert_promo_groups_loaded(user: User) -> None:
    assert [link.promo_group.name for link in user.user_promo_groups] == ['Льготная']
    assert user.promo_group is None  # legacy-связь в этом сценарии не заполнена


@pytest.mark.asyncio
async def test_expiring_query_eager_loads_promo_groups_and_prices_renewal(monkeypatch):
    """``_get_expiring_paid_subscriptions`` -> ``_resolve_renewal_cost_kopeks``.

    Без жадной загрузки обращение к промогруппам упало бы с MissingGreenlet,
    цена откатилась бы на PRICE_30_DAYS.
    """
    monkeypatch.setattr(settings, 'PRICE_30_DAYS', 99900)
    now = datetime.now(UTC)

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=now + timedelta(days=2), status='active')
        service = MonitoringService(bot=object())
        subscriptions = await service._get_expiring_paid_subscriptions(db, days_before=3)

        assert len(subscriptions) == 1
        subscription = subscriptions[0]
        user = subscription.user
        _assert_promo_groups_loaded(user)

        price_kopeks = await service._resolve_renewal_cost_kopeks(db, subscription, user)

    assert price_kopeks == EXPECTED_DISCOUNTED_PRICE
    assert price_kopeks != settings.PRICE_30_DAYS


@pytest.mark.asyncio
async def test_expired_followups_query_eager_loads_promo_groups(monkeypatch):
    """``_check_expired_subscription_followups`` отдаёт в скидочное уведомление
    пользователя с загруженными промогруппами, и цена считается со скидкой."""
    monkeypatch.setattr(settings, 'PRICE_30_DAYS', 99900)
    monkeypatch.setattr(NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(NotificationSettingsService, 'is_expired_1d_enabled', lambda: False)
    monkeypatch.setattr(NotificationSettingsService, 'is_second_wave_enabled', lambda: True)
    monkeypatch.setattr(NotificationSettingsService, 'is_third_wave_enabled', lambda: False)
    monkeypatch.setattr(NotificationSettingsService, 'get_second_wave_discount_percent', lambda: 10)
    monkeypatch.setattr(NotificationSettingsService, 'get_second_wave_valid_hours', lambda: 24)
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())
    monkeypatch.setattr(
        ms,
        'upsert_discount_offer',
        AsyncMock(return_value=SimpleNamespace(id=1, expires_at=datetime.now(UTC) + timedelta(hours=24))),
    )

    captured: dict = {}

    async def fake_send_discount(user, subscription, percent, expires_at, offer_id, wave, **kwargs):
        captured.update(user=user, subscription=subscription, db=kwargs.get('db'))
        return True

    now = datetime.now(UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=now - timedelta(days=2, hours=12), status='expired')
        service = MonitoringService(bot=object())
        service._send_expired_discount_notification = fake_send_discount
        service._log_monitoring_event = AsyncMock()

        await service._check_expired_subscription_followups(db)

        assert captured, 'follow-up должен дойти до скидочного уведомления'
        user = captured['user']
        subscription = captured['subscription']
        _assert_promo_groups_loaded(user)

        price_kopeks = await service._resolve_renewal_cost_kopeks(db, subscription, user)

    assert captured['db'] is not None
    assert price_kopeks == EXPECTED_DISCOUNTED_PRICE
    assert price_kopeks != settings.PRICE_30_DAYS


@pytest.mark.asyncio
async def test_reload_subscription_eager_loads_promo_groups(monkeypatch):
    """Перечитывание после отката не должно терять промогруппы."""
    now = datetime.now(UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, end_date=now - timedelta(hours=1), status='expired')
        subscription = await ms._reload_subscription(db, 8)

        assert subscription is not None
        _assert_promo_groups_loaded(subscription.user)


@pytest.mark.asyncio
async def test_handle_unreachable_user_logs_error_as_string():
    """Штатный «chat not found» — предупреждение со строкой, не объект исключения:
    иначе ``_auto_capture_exc_info`` синтезирует exc_info и печатает traceback."""
    service = MonitoringService(bot=None)
    user = SimpleNamespace(telegram_id=9009)
    error = TelegramBadRequest(method=None, message='Bad Request: chat not found')

    # В проде исключение приходит из except-блока — уже с traceback'ом.
    try:
        raise error
    except TelegramBadRequest as exc:
        with capture_logs() as logs:
            handled = await service._handle_unreachable_user(user, exc, 'тест')

    assert handled is True
    warning = next(record for record in logs if 'Пользователь недоступен' in record['event'])
    assert isinstance(warning['error'], str)
    assert not isinstance(warning['error'], BaseException)
    assert 'chat not found' in warning['error']
    assert warning['context'] == 'тест'


@pytest.mark.asyncio
async def test_discount_notification_treats_unreachable_user_as_handled():
    """Недоступный пользователь в скидочном уведомлении — handled, не исключение."""
    service = MonitoringService(bot=object())
    user = SimpleNamespace(telegram_id=9009, language='ru')
    subscription = SimpleNamespace(id=8, tariff=None)
    service._send_message_with_logo = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message='Bad Request: chat not found')
    )

    with capture_logs():
        result = await service._send_expired_discount_notification(
            user,
            subscription,
            10,
            datetime.now(UTC) + timedelta(hours=24),
            offer_id=1,
            wave='second',
            db=None,
        )

    assert result is True


@pytest.mark.asyncio
async def test_logo_fallback_logs_exception_as_string(monkeypatch, tmp_path):
    """Fallback с send_photo на send_message логирует строку, а не исключение."""
    logo_path = tmp_path / 'logo.png'
    logo_path.write_bytes(b'\x89PNG\r\n\x1a\n')
    monkeypatch.setattr(ms, 'LOGO_PATH', logo_path)
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    monkeypatch.setattr(ms, 'try_send_rich_notification', AsyncMock(return_value=False))

    from app.utils import message_patch

    monkeypatch.setattr(message_patch, 'get_logo_media', lambda: 'media')

    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=TelegramBadRequest(method=None, message='Bad Request: chat not found')),
        send_message=AsyncMock(return_value='sent'),
    )
    service = MonitoringService(bot=bot)

    with capture_logs() as logs:
        result = await service._send_message_with_logo(chat_id=1, text='hi')

    assert result == 'sent'
    warning = next(record for record in logs if 'с логотипом' in record['event'])
    assert isinstance(warning['exc'], str)
    assert not isinstance(warning['exc'], BaseException)
    assert 'chat not found' in warning['exc']
