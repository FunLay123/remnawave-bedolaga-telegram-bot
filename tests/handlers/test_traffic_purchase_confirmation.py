"""Экраны подтверждения покупки трафика.

Тап по пакету больше не списывает деньги сразу: сначала подтверждение
(как у изменения лимита устройств) — цена, что изменится, blockquote-подсказка,
primary-кнопка «Подтвердить» и «Отмена».
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.subscription.traffic as traffic_mod
from app.utils.premium_traffic import BYTES_IN_GB


CONFIRM_ICON = '5334552404736104314'


def _callback(data: str):
    cb = MagicMock()
    cb.data = data
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _user():
    return SimpleNamespace(
        id=1,
        telegram_id=1,
        language='ru',
        balance_kopeks=100_000,
        promo_group=None,
        remnawave_id=None,
    )


def _subscription(**overrides):
    values = {
        'id': 1,
        'is_trial': False,
        'tariff_id': None,
        'traffic_limit_gb': 300,
        'end_date': datetime.now(UTC) + timedelta(days=30),
        'connected_squads': [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


@pytest.mark.parametrize(
    ('gb', 'expected'),
    [
        (2.0, '2 ГБ'),
        (1.4, '1.4 ГБ'),
        (1.46, '1.5 ГБ'),
        (0.4, '0.4 ГБ'),
        (0.0, '0 ГБ'),
        (1024.0, '1 ТБ'),
        (1536.0, '1.5 ТБ'),
    ],
)
def test_traffic_value_uses_decimals_only_when_needed(gb, expected):
    assert traffic_mod._format_traffic_gb(gb) == expected


@pytest.mark.anyio('asyncio')
async def test_ordinary_confirmation_shows_new_limit_and_price(monkeypatch):
    subscription = _subscription(tariff_id=42)
    tariff = MagicMock()
    tariff.can_topup_traffic.return_value = True
    tariff.get_traffic_topup_price.return_value = 10_000

    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr('app.config.Settings.is_tariffs_mode', lambda self: True)

    callback = _callback('add_traffic_50')
    await traffic_mod.show_add_traffic_confirmation(callback, _user(), AsyncMock(), None)

    text = callback.message.edit_text.await_args.args[0]
    assert '300 ГБ → 350 ГБ' in text
    assert '100' in text, 'цена должна быть видна до подтверждения'
    assert 'Пакет действует 30 дней' in text

    buttons = _buttons(callback.message.edit_text.await_args.kwargs['reply_markup'])
    confirm = buttons[0]
    assert confirm.text == 'Подтвердить'
    assert confirm.callback_data == 'confirm_add_traffic_50'
    assert confirm.style == 'primary'
    assert confirm.icon_custom_emoji_id == CONFIRM_ICON
    assert buttons[1].callback_data == 'ordinary_traffic'


@pytest.mark.anyio('asyncio')
async def test_premium_confirmation_shows_remaining_and_price(monkeypatch):
    subscription = _subscription()
    row = {
        'squad_uuid': 'sq',
        'config': MagicMock(),
        'state': SimpleNamespace(remaining_bytes=10 * BYTES_IN_GB),
        'name': 'DE-1',
    }
    quote = SimpleNamespace(base_price_kopeks=2_000)

    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(traffic_mod, '_get_purchasable_premium_squads', AsyncMock(return_value=[row]))
    monkeypatch.setattr(
        'app.services.premium_traffic_purchase.quote_premium_topup',
        AsyncMock(return_value=quote),
    )

    callback = _callback('premium_traffic_buy_0_5')
    await traffic_mod.show_premium_traffic_confirmation(callback, _user(), AsyncMock(), None)

    text = callback.message.edit_text.await_args.args[0]
    assert 'DE-1' in text
    assert 'Остаток: 10 ГБ → 15 ГБ' in text
    assert '20' in text, 'цена должна быть видна до подтверждения'
    assert 'до конца текущего периода' in text

    buttons = _buttons(callback.message.edit_text.await_args.kwargs['reply_markup'])
    confirm = buttons[0]
    assert confirm.text == 'Подтвердить'
    assert confirm.callback_data == 'confirm_premium_traffic_0_5'
    assert confirm.style == 'primary'
    assert confirm.icon_custom_emoji_id == CONFIRM_ICON
    assert buttons[1].callback_data == 'premium_traffic_topup'


# ============================= Статусные гейты трафика =============================
#
# Экран подтверждения мог пережить истечение/отключение подписки. Все
# money-ветки (обычная докупка, премиум-докупка, сброс) обязаны отказывать до
# списания и до расчёта цены.

INACTIVE_STATUSES = ('expired', 'disabled')


@pytest.mark.parametrize('actual_status', INACTIVE_STATUSES)
@pytest.mark.anyio('asyncio')
async def test_ordinary_confirmation_refuses_inactive_subscription(monkeypatch, actual_status):
    subscription = _subscription(tariff_id=42, status=actual_status, actual_status=actual_status)
    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))

    callback = _callback('add_traffic_50')

    await traffic_mod.show_add_traffic_confirmation(callback, _user(), AsyncMock(), None)

    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    assert callback.answer.await_args.args[0] == 'Сначала продлите подписку'


@pytest.mark.parametrize('actual_status', INACTIVE_STATUSES)
@pytest.mark.anyio('asyncio')
async def test_premium_confirmation_refuses_inactive_subscription(monkeypatch, actual_status):
    subscription = _subscription(status=actual_status, actual_status=actual_status)
    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    squads = AsyncMock(return_value=[])
    monkeypatch.setattr(traffic_mod, '_get_purchasable_premium_squads', squads)

    callback = _callback('premium_traffic_buy_0_5')

    await traffic_mod.show_premium_traffic_confirmation(callback, _user(), AsyncMock(), None)

    squads.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    assert callback.answer.await_args.args[0] == 'Сначала продлите подписку'


@pytest.mark.parametrize('actual_status', INACTIVE_STATUSES)
@pytest.mark.anyio('asyncio')
async def test_reset_traffic_refuses_inactive_subscription(monkeypatch, actual_status):
    subscription = _subscription(status=actual_status, actual_status=actual_status)
    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=_user()),
    )
    subtract = AsyncMock()
    monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)
    calculate_price = MagicMock()
    monkeypatch.setattr(traffic_mod, '_calculate_traffic_reset_price', calculate_price)

    callback = _callback('confirm_reset_traffic')

    await traffic_mod.confirm_reset_traffic(callback, _user(), AsyncMock(), None)

    calculate_price.assert_not_called()
    subtract.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    assert callback.answer.await_args.args[0] == 'Сначала продлите подписку'


@pytest.mark.parametrize('actual_status', INACTIVE_STATUSES)
@pytest.mark.anyio('asyncio')
async def test_add_traffic_refuses_inactive_subscription_after_lock(monkeypatch, actual_status):
    """Гонка: подписку отключили между подтверждением и нажатием «Подтвердить»."""
    subscription = _subscription(tariff_id=42, status=actual_status, actual_status=actual_status)
    tariff = MagicMock()
    tariff.can_topup_traffic.return_value = True
    tariff.get_traffic_topup_price.return_value = 10_000

    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=_user()),
    )
    subtract = AsyncMock()
    monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)

    callback = _callback('confirm_add_traffic_50')

    await traffic_mod.add_traffic(callback, _user(), AsyncMock(), None)

    subtract.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    assert callback.answer.await_args.args[0] == 'Сначала продлите подписку'


@pytest.mark.anyio('asyncio')
async def test_add_traffic_does_not_reactivate_disabled_subscription(monkeypatch):
    """DISABLED-подписку докупка не «оживляет»: оплаченного периода у неё нет."""
    subscription = _subscription(
        tariff_id=42,
        status='disabled',
        actual_status='disabled',
        end_date=datetime.now(UTC) + timedelta(days=30),
    )
    tariff = MagicMock()
    tariff.can_topup_traffic.return_value = True
    tariff.get_traffic_topup_price.return_value = 10_000

    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=_user()),
    )
    subtract = AsyncMock()
    monkeypatch.setattr(traffic_mod, 'subtract_user_balance', subtract)
    reactivate = AsyncMock()
    monkeypatch.setattr(traffic_mod, 'reactivate_subscription', reactivate)

    callback = _callback('confirm_add_traffic_50')

    await traffic_mod.add_traffic(callback, _user(), AsyncMock(), None)

    subtract.assert_not_awaited()
    reactivate.assert_not_awaited()


@pytest.mark.parametrize(('status', 'expected_reactivations'), [('limited', 1), ('active', 0)])
@pytest.mark.anyio('asyncio')
async def test_add_traffic_reactivates_only_limited_subscription(monkeypatch, status, expected_reactivations):
    """Докупка снимает исчерпание только у LIMITED; ACTIVE реактивации не требует."""
    subscription = _subscription(tariff_id=42, status=status, actual_status=status)
    tariff = MagicMock()
    tariff.can_topup_traffic.return_value = True
    tariff.get_traffic_topup_price.return_value = 10_000

    monkeypatch.setattr(traffic_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=_user()),
    )

    async def _fake_subtract(db, user, amount, description):
        return True

    monkeypatch.setattr(traffic_mod, 'subtract_user_balance', _fake_subtract)
    monkeypatch.setattr(traffic_mod, 'add_subscription_traffic', AsyncMock())
    reactivate = AsyncMock()
    monkeypatch.setattr(traffic_mod, 'reactivate_subscription', reactivate)

    subscription_service = MagicMock()
    subscription_service.update_remnawave_user = AsyncMock()
    subscription_service.enable_remnawave_user = AsyncMock()
    monkeypatch.setattr(traffic_mod, 'SubscriptionService', MagicMock(return_value=subscription_service))
    monkeypatch.setattr(traffic_mod, 'create_transaction', AsyncMock())

    notification = MagicMock()
    notification.send_subscription_update_notification = AsyncMock()
    monkeypatch.setattr(
        'app.services.admin_notification_service.AdminNotificationService',
        MagicMock(return_value=notification),
    )
    monkeypatch.setattr(traffic_mod, '_get_purchasable_premium_squads', AsyncMock(return_value=[]))
    monkeypatch.setattr(traffic_mod, '_render_ordinary_traffic_screen', AsyncMock())

    callback = _callback('confirm_add_traffic_50')

    await traffic_mod.add_traffic(callback, _user(), AsyncMock(), None)

    assert reactivate.await_count == expected_reactivations
    if expected_reactivations:
        assert reactivate.await_args.args[1] is subscription
    assert callback.answer.await_args.args[0] == 'Трафик добавлен'
