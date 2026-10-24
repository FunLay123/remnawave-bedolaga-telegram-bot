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
    return SimpleNamespace(id=1, telegram_id=1, language='ru', balance_kopeks=100_000, promo_group=None)


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
