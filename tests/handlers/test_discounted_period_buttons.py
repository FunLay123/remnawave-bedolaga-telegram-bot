"""Активная скидка на кнопках выбора периода.

Пока скидка действует, строки периодов показывают итоговую цену и эффективный
процент («Скидка N%») с отдельной иконкой; старой цены, стрелки и «🔥−N%» нет.
Когда скидка истекает, а групповой скидки не остаётся, возвращается прежний
вид. Клавиатура смены тарифа и служебные кнопки не затронуты.

Процент берётся из реальных сумм до и после последовательных скидок, поэтому
10% группы и 20% личного оффера дают 28%, а не 30%.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.subscription.purchase as purchase_mod
import app.keyboards.inline as inline_mod
from app.handlers.subscription.tariff_purchase import (
    get_tariff_extend_keyboard,
    get_tariff_periods_keyboard,
    get_tariff_periods_keyboard_with_traffic,
    get_tariff_switch_periods_keyboard,
)
from app.keyboards.inline import (
    get_extend_subscription_keyboard_with_prices,
    get_subscription_period_keyboard,
)
from app.services.pricing_engine import PricingEngine


DISCOUNT_ICON = '5335020345717973061'
USUAL_ICON = '5334829606220354437'
BADGE = '⭐ выгодно'
NOW = datetime.now(UTC)


class _Group:
    def __init__(self, period_percent: int) -> None:
        self.period_percent = period_percent

    def get_discount_percent(self, category: str, period_days: int | None = None) -> int:
        return self.period_percent if category == 'period' else 0


class _User:
    """Минимальный пользователь: промогруппа + личный промо-оффер."""

    def __init__(
        self,
        group_percent: int = 0,
        offer_percent: int = 0,
        offer_expires_at: datetime | None = None,
    ) -> None:
        self.telegram_id = None
        self._group = _Group(group_percent)
        self.promo_offer_discount_percent = offer_percent
        self.promo_offer_discount_expires_at = offer_expires_at

    def get_primary_promo_group(self) -> _Group:
        return self._group

    def get_promo_discount(self, category: str, period_days: int | None = None) -> int:
        return self._group.get_discount_percent(category, period_days)


def _active_user(group_percent: int = 0, offer_percent: int = 0) -> _User:
    return _User(group_percent, offer_percent, NOW + timedelta(hours=1))


def _expired_offer_user(group_percent: int = 0, offer_percent: int = 20) -> _User:
    return _User(group_percent, offer_percent, NOW - timedelta(minutes=1))


def _tariff(highlight_period: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        name='Базовый',
        period_prices={'30': 60000, '90': 150000},
        highlight_period_days=highlight_period,
        device_limit=1,
        device_price_kopeks=None,
    )


def _periods_keyboard(tariff, user):
    return get_tariff_periods_keyboard(tariff, 'ru', user)


def _periods_with_traffic_keyboard(tariff, user):
    return get_tariff_periods_keyboard_with_traffic(tariff, 'ru', user)


def _extend_keyboard(tariff, user):
    return get_tariff_extend_keyboard(
        tariff,
        'ru',
        user,
        subscription_device_limit=None,
        subscription_id=1,
    )


TARIFF_KEYBOARDS = (
    _periods_keyboard,
    _periods_with_traffic_keyboard,
    _extend_keyboard,
)


def _buttons(keyboard) -> list:
    return [button for row in keyboard.inline_keyboard for button in row]


def _period_buttons(keyboard) -> list:
    return _buttons(keyboard)[:-1]


def _back(keyboard):
    return _buttons(keyboard)[-1]


def _visible(text: str | None) -> str:
    """Убирает выравнивающий padding кнопок с кастомной иконкой."""
    return (text or '').replace(' ', '').replace('‍', '').rstrip()


def _signature(button) -> tuple:
    return (button.text, button.icon_custom_emoji_id, button.style, button.callback_data)


@pytest.fixture
def classic_period_prices(monkeypatch):
    monkeypatch.setattr(inline_mod.settings, 'AVAILABLE_SUBSCRIPTION_PERIODS', '30,90')
    monkeypatch.setitem(inline_mod.PERIOD_PRICES, 30, 60000)
    monkeypatch.setitem(inline_mod.PERIOD_PRICES, 90, 150000)


@pytest.fixture
def renewal_periods(monkeypatch):
    monkeypatch.setattr(type(inline_mod.settings), 'get_available_renewal_periods', lambda self: [30, 90])


# ==================== классическая покупка/продление ====================


def test_classic_keyboard_shows_final_price_and_effective_discount(classic_period_prices):
    user = _active_user(group_percent=10, offer_percent=20)

    keyboard = get_subscription_period_keyboard('ru', user)
    periods = _period_buttons(keyboard)

    assert [_visible(button.text) for button in periods] == [
        '1 месяц — 432 ₽ · Скидка 28%',
        '3 месяца — 1080 ₽ · Скидка 28%',
    ]
    assert all(button.icon_custom_emoji_id == DISCOUNT_ICON for button in periods)
    for button in periods:
        assert '➜' not in button.text
        assert '🔥' not in button.text
        assert '600' not in button.text
        assert '1500' not in button.text
        assert button.style is None


def test_classic_back_button_is_unchanged_by_the_discount(classic_period_prices):
    plain = get_subscription_period_keyboard('ru', _User())
    discounted = get_subscription_period_keyboard('ru', _active_user(10, 20))

    assert _signature(_back(discounted)) == _signature(_back(plain))
    assert _back(discounted).callback_data == 'back_to_menu'


def test_classic_returns_to_price_only_labels_after_offer_expiry(classic_period_prices):
    plain = get_subscription_period_keyboard('ru', _User())
    expired = get_subscription_period_keyboard('ru', _expired_offer_user())

    assert [_signature(button) for button in _buttons(expired)] == [_signature(button) for button in _buttons(plain)]
    assert [_visible(button.text) for button in _period_buttons(expired)] == [
        '📅 1 месяц - 600 ₽',
        '📅 3 месяца - 1500 ₽',
    ]
    assert all(button.icon_custom_emoji_id == USUAL_ICON for button in _period_buttons(expired))


def test_classic_keeps_discount_for_remaining_group_after_offer_expiry(classic_period_prices):
    periods = _period_buttons(get_subscription_period_keyboard('ru', _expired_offer_user(group_percent=10)))

    assert [_visible(button.text) for button in periods] == [
        '1 месяц — 540 ₽ · Скидка 10%',
        '3 месяца — 1350 ₽ · Скидка 10%',
    ]
    assert all(button.icon_custom_emoji_id == DISCOUNT_ICON for button in periods)


# ==================== классическое продление (get_extend_...) ====================


def _renewal_prices(discounted: bool = True) -> dict:
    if discounted:
        # original = до всех скидок, final = после последовательных скидок.
        return {
            30: {'original': 60000, 'final': 43200},
            90: {'original': 150000, 'final': 108000},
        }
    return {
        30: {'original': 60000, 'final': 60000},
        90: {'original': 150000, 'final': 150000},
    }


def test_renewal_keyboard_shows_final_price_and_effective_discount(renewal_periods):
    keyboard = get_extend_subscription_keyboard_with_prices('ru', _renewal_prices())
    periods = _period_buttons(keyboard)

    assert [_visible(button.text) for button in periods] == [
        '1 месяц — 432 ₽ · Скидка 28%',
        '3 месяца — 1080 ₽ · Скидка 28%',
    ]
    assert all(button.icon_custom_emoji_id == DISCOUNT_ICON for button in periods)
    assert [button.callback_data for button in periods] == ['extend_period_30', 'extend_period_90']
    for button in periods:
        assert '➜' not in button.text
        assert '🔥' not in button.text
        assert '600' not in button.text
        assert '1500' not in button.text
        assert button.style is None


def test_renewal_keyboard_returns_to_plain_labels_without_discount(renewal_periods):
    keyboard = get_extend_subscription_keyboard_with_prices('ru', _renewal_prices(discounted=False))
    periods = _period_buttons(keyboard)

    assert [_visible(button.text) for button in periods] == [
        '📅 1 месяц - 600 ₽',
        '📅 3 месяца - 1500 ₽',
    ]
    assert all(button.icon_custom_emoji_id == USUAL_ICON for button in periods)
    assert [button.callback_data for button in periods] == ['extend_period_30', 'extend_period_90']


def test_renewal_integer_prices_keep_the_previous_appearance(renewal_periods):
    keyboard = get_extend_subscription_keyboard_with_prices('ru', {30: 60000, 90: 150000})
    periods = _period_buttons(keyboard)

    assert [_visible(button.text) for button in periods] == [
        '📅 1 месяц - 600 ₽',
        '📅 3 месяца - 1500 ₽',
    ]
    assert all(button.icon_custom_emoji_id == USUAL_ICON for button in periods)


def test_renewal_back_button_is_unchanged_by_the_discount(renewal_periods):
    discounted = get_extend_subscription_keyboard_with_prices('ru', _renewal_prices())
    plain = get_extend_subscription_keyboard_with_prices('ru', _renewal_prices(discounted=False))

    assert _signature(_back(discounted)) == _signature(_back(plain))
    assert _back(discounted).callback_data == 'menu_subscription'


@pytest.mark.asyncio
async def test_live_renewal_screen_uses_discounted_period_buttons(monkeypatch):
    """Живой handle_extend_subscription рисует продление обновлённой клавиатурой."""
    settings_cls = type(inline_mod.settings)
    monkeypatch.setattr(settings_cls, 'get_available_renewal_periods', lambda self: [30, 90])
    monkeypatch.setattr(settings_cls, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings_cls, 'is_tariffs_mode', lambda self: False)
    monkeypatch.setattr(settings_cls, 'is_devices_selection_enabled', lambda self: False)
    monkeypatch.setattr(purchase_mod, '_get_promo_offer_discount_percent', lambda user: 0)
    monkeypatch.setattr(purchase_mod, '_build_promo_group_discount_text', AsyncMock(return_value=''))
    monkeypatch.setattr(purchase_mod, '_get_promo_offer_hint', AsyncMock(return_value=None))

    async def fake_renewal_price(self, db, subscription, days, user=None):
        prices = {
            30: SimpleNamespace(original_total=60000, final_total=43200),
            90: SimpleNamespace(original_total=150000, final_total=108000),
        }
        return prices[days]

    monkeypatch.setattr(PricingEngine, 'calculate_renewal_price', fake_renewal_price)

    subscription = SimpleNamespace(
        is_trial=False,
        tariff_id=None,
        connected_squads=[],
        traffic_limit_gb=100,
        end_date=datetime.now(UTC) + timedelta(days=10),
    )
    db_user = SimpleNamespace(language='ru', subscription=subscription)
    callback = MagicMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()

    await purchase_mod.handle_extend_subscription(callback, db_user, SimpleNamespace())

    markup = callback.message.edit_text.await_args.kwargs['reply_markup']
    periods = _period_buttons(markup)
    assert [_visible(button.text) for button in periods] == [
        '1 месяц — 432 ₽ · Скидка 28%',
        '3 месяца — 1080 ₽ · Скидка 28%',
    ]
    assert all(button.icon_custom_emoji_id == DISCOUNT_ICON for button in periods)


# ==================== тарифные покупка/продление ====================


@pytest.mark.parametrize('build', TARIFF_KEYBOARDS, ids=lambda build: build.__name__)
def test_tariff_keyboards_show_final_price_discount_and_icon(build):
    keyboard = build(_tariff(highlight_period=30), _active_user(10, 20))
    periods = _period_buttons(keyboard)

    assert [_visible(button.text) for button in periods] == [
        f'{BADGE} · 30 дней — 432 ₽ · Скидка 28%',
        '90 дней — 1080 ₽ · Скидка 28%',
    ]
    assert all(button.icon_custom_emoji_id == DISCOUNT_ICON for button in periods)
    # Стиль primary остаётся только у 30-дневного периода.
    assert [button.style for button in periods] == ['primary', None]
    for button in periods:
        assert '➜' not in button.text
        assert '🔥' not in button.text
        assert '600' not in button.text
        assert '1500' not in button.text


@pytest.mark.parametrize('build', TARIFF_KEYBOARDS, ids=lambda build: build.__name__)
def test_tariff_back_button_is_unchanged_by_the_discount(build):
    plain = build(_tariff(), _User())
    discounted = build(_tariff(), _active_user(10, 20))

    assert _signature(_back(discounted)) == _signature(_back(plain))


@pytest.mark.parametrize('build', TARIFF_KEYBOARDS, ids=lambda build: build.__name__)
def test_tariff_returns_to_plain_labels_after_offer_expiry(build):
    plain = build(_tariff(highlight_period=30), _User())
    expired = build(_tariff(highlight_period=30), _expired_offer_user())

    assert [_signature(button) for button in _buttons(expired)] == [_signature(button) for button in _buttons(plain)]
    periods = _period_buttons(expired)
    assert [_visible(button.text) for button in periods] == [
        f'{BADGE} · 30 дней — 600 ₽',
        '90 дней — 1500 ₽',
    ]
    assert [button.icon_custom_emoji_id for button in periods] == [USUAL_ICON, None]
    assert [button.style for button in periods] == ['primary', None]


@pytest.mark.parametrize('build', TARIFF_KEYBOARDS, ids=lambda build: build.__name__)
def test_tariff_keeps_discount_for_remaining_group_after_offer_expiry(build):
    periods = _period_buttons(build(_tariff(), _expired_offer_user(group_percent=10)))

    assert [_visible(button.text) for button in periods] == [
        '30 дней — 540 ₽ · Скидка 10%',
        '90 дней — 1350 ₽ · Скидка 10%',
    ]
    assert all(button.icon_custom_emoji_id == DISCOUNT_ICON for button in periods)


def test_extend_effective_percent_includes_device_costs():
    """База процента — реальная сумма (период + устройства), а не только период."""
    tariff = _tariff()
    tariff.device_price_kopeks = 10000

    keyboard = get_tariff_extend_keyboard(
        tariff,
        'ru',
        _active_user(group_percent=10, offer_percent=20),
        subscription_device_limit=2,
        subscription_id=1,
    )
    label = _visible(_period_buttons(keyboard)[0].text)

    # 60 000 (-10%) + 10 000 устройств = 64 000; -20% оффера = 51 200.
    # Эффективная скидка от 70 000: 27%, а не 28% по одному периоду.
    assert label == '30 дней — 512 ₽ · Скидка 27%'


def test_zero_base_price_does_not_invent_a_discount():
    """Нулевая база не должна давать деление на ноль или «Скидка 0%»."""
    tariff = _tariff()
    tariff.period_prices = {'30': 0}

    button = _period_buttons(get_tariff_periods_keyboard(tariff, 'ru', _active_user(10, 20)))[0]

    assert _visible(button.text) == '30 дней — 0 ₽'
    assert 'Скидка' not in button.text
    assert button.icon_custom_emoji_id == USUAL_ICON


# ==================== исключённые поверхности ====================


def test_tariff_switch_periods_keep_the_previous_appearance():
    """Смена тарифа в этот этап не входит: старый вид с 🔥−N% сохраняется."""
    periods = _period_buttons(get_tariff_switch_periods_keyboard(_tariff(), 'ru', _active_user(10, 20)))

    assert [_visible(button.text) for button in periods] == [
        '30 дней — 432 ₽ 🔥−28%',
        '90 дней — 1080 ₽ 🔥−28%',
    ]
    assert all(button.icon_custom_emoji_id == USUAL_ICON for button in periods)
