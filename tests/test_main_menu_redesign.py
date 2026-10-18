from aiogram.types import InlineKeyboardButton

from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard, get_subscription_keyboard
from app.services.support_settings_service import SupportSettingsService


def _prepare_menu(monkeypatch):
    for name, value in (
        ('MENU_LAYOUT_ENABLED', False),
        ('MAIN_MENU_MODE', 'default'),
        ('TRIAL_DURATION_DAYS', 0),
        ('SIMPLE_SUBSCRIPTION_ENABLED', False),
        ('CONTESTS_ENABLED', False),
        ('CONTESTS_BUTTON_VISIBLE', False),
        ('ACTIVATE_BUTTON_VISIBLE', False),
        ('REFERRAL_PROGRAM_ENABLED', False),
        ('LANGUAGE_SELECTION_ENABLED', False),
        ('PREMIUM_TRAFFIC_ENABLED', False),
        ('TRAFFIC_SELECTION_MODE', 'selectable'),
        ('TRAFFIC_TOPUP_ENABLED', False),
        ('BUY_TRAFFIC_BUTTON_VISIBLE', False),
    ):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(SupportSettingsService, 'is_support_menu_enabled', lambda: True)


def _callbacks(keyboard):
    return [[button.callback_data for button in row] for row in keyboard.inline_keyboard]


def test_main_menu_has_connect_subscription_topup_and_support_rows(monkeypatch):
    """The customer menu keeps its four primary actions in the agreed row layout."""
    _prepare_menu(monkeypatch)

    keyboard = get_main_menu_keyboard(language='ru', has_active_subscription=True, subscription_is_active=True)

    assert _callbacks(keyboard)[:3] == [
        ['subscription_connect'],
        ['menu_subscription', 'balance_topup'],
        ['menu_support'],
    ]
    assert [[button.text for button in row] for row in keyboard.inline_keyboard[:3]] == [
        ['Подключиться'],
        ['Подписка', 'Пополнить'],
        ['Техподдержка'],
    ]
    assert all('menu_balance' not in row for row in _callbacks(keyboard))


def test_main_menu_keeps_admin_panel_separate(monkeypatch):
    """The redesigned customer rows must not remove or relocate the admin entry."""
    _prepare_menu(monkeypatch)

    keyboard = get_main_menu_keyboard(
        language='ru', is_admin=True, has_active_subscription=True, subscription_is_active=True
    )

    assert _callbacks(keyboard)[-1] == ['admin_panel']


def test_main_menu_does_not_offer_purchase_or_trial(monkeypatch):
    """Purchase and trial moved off the main screen into the subscription screen."""
    _prepare_menu(monkeypatch)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 7)
    keyboard = get_main_menu_keyboard(language='ru')

    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'menu_trial' not in callbacks
    assert 'menu_buy' not in callbacks


def test_subscription_screen_without_subscription_offers_trial_and_purchase():
    """A new customer must still reach purchase and trial, now through the subscription screen."""
    keyboard = get_subscription_keyboard('ru', has_subscription=False, show_trial=True)

    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'menu_trial' in callbacks
    assert 'menu_buy' in callbacks


def test_subscription_screen_hides_trial_when_not_eligible():
    keyboard = get_subscription_keyboard('ru', has_subscription=False, show_trial=False)

    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'menu_trial' not in callbacks
    assert 'menu_buy' in callbacks


def test_topup_entry_is_a_callback_not_a_balance_screen(monkeypatch):
    """The main-menu top-up action opens payment methods directly, not balance/history."""
    _prepare_menu(monkeypatch)
    keyboard = get_main_menu_keyboard(language='ru', has_active_subscription=True, subscription_is_active=True)

    topup = next(
        button for row in keyboard.inline_keyboard for button in row if button.callback_data == 'balance_topup'
    )

    assert isinstance(topup, InlineKeyboardButton)
    assert topup.url is None
    assert topup.web_app is None


def test_subscription_screen_hides_gift_and_tariff_tab_and_exposes_extracted_items(monkeypatch):
    """Подарок и «Тариф» скрыты; устройства, перевыпуск и промокод — отдельные пункты."""
    from types import SimpleNamespace

    monkeypatch.setattr(settings, 'SUBSCRIPTION_REVOKE_ENABLED', True, raising=False)
    monkeypatch.setattr(type(settings), 'is_subscription_revoke_enabled', lambda self: True)
    sub = SimpleNamespace(
        id=1,
        tariff=None,
        tariff_id=None,
        status='active',
        actual_status='active',
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
    )

    keyboard = get_subscription_keyboard('ru', has_subscription=True, subscription=sub, gift_enabled=True)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert 'subscription_gift' not in callbacks
    assert 'instant_switch' not in callbacks
    assert 'subscription_connect' not in callbacks
    for expected in ('subscription_manage_devices', 'subscription_revoke', 'menu_promocode'):
        assert expected in callbacks


def test_connect_without_subscription_leads_to_subscription_screen(monkeypatch):
    """Без подписки подключаться не к чему: кнопка ведёт туда, где можно купить или взять пробный период."""
    _prepare_menu(monkeypatch)

    keyboard = get_main_menu_keyboard(language='ru', has_active_subscription=False)

    assert _callbacks(keyboard)[0] == ['menu_subscription']


def test_subscription_screen_uses_short_labels_and_row_layout(monkeypatch):
    """Минималистичная клавиатура: Продлить / Устройства+Автоплатёж / Перевыпустить+Промокод / Назад."""
    from types import SimpleNamespace

    monkeypatch.setattr(type(settings), 'is_subscription_revoke_enabled', lambda self: True)
    # Без режима тарифов подписка без тарифа не считается «старой» и получает «Продлить».
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)
    sub = SimpleNamespace(
        id=1,
        tariff=None,
        tariff_id=None,
        status='active',
        actual_status='active',
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
    )

    keyboard = get_subscription_keyboard('ru', has_subscription=True, subscription=sub)
    rows = [[(button.text, button.callback_data) for button in row] for row in keyboard.inline_keyboard]

    assert rows[0] == [('Продлить подписку', 'subscription_extend')]
    assert rows[1] == [('Устройства', 'subscription_manage_devices'), ('Автоплатёж', 'subscription_autopay')]
    assert rows[-2] == [('Перевыпустить', 'subscription_revoke'), ('Промокод', 'menu_promocode')]
    assert rows[-1][0][1] == 'back_to_menu'
