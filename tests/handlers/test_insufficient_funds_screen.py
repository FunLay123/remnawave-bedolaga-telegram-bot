"""Экран «Недостаточно средств»: мельница, компактный текст и без корзины."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import settings
from app.keyboards.inline import get_insufficient_balance_keyboard, get_main_menu_keyboard
from app.localization.texts import get_texts
from app.services.subscription_checkout_service import should_offer_checkout_resume


MILL_EMOJI_ID = '5258382581375723416'


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def test_message_is_compact_and_uses_mill_emoji():
    text = (
        get_texts('ru')
        .t('ADDON_INSUFFICIENT_FUNDS_MESSAGE', '')
        .format(
            required='623 ₽',
            balance='500 ₽',
            missing='123 ₽',
        )
    )

    assert MILL_EMOJI_ID in text
    assert 'Не хватает 123 ₽' in text
    assert 'К оплате 623 ₽ · баланс 500 ₽' in text
    assert 'Стоимость услуги' not in text
    assert 'Выберите способ пополнения' not in text


def test_home_button_keeps_legacy_icon():
    keyboard = get_insufficient_balance_keyboard('ru', amount_kopeks=12300)
    home = next(button for button in _buttons(keyboard) if button.callback_data == 'back_to_menu')

    assert home.text == 'На главную'
    assert home.icon_custom_emoji_id == '5314310000531766389'


def test_resume_button_is_hidden_when_cart_resume_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'CART_RESUME_ENABLED', False)

    keyboard = get_insufficient_balance_keyboard(
        'ru',
        amount_kopeks=12300,
        has_saved_cart=True,
        resume_callback='subscription_resume_checkout',
    )

    callbacks = [button.callback_data for button in _buttons(keyboard)]
    assert 'return_to_saved_cart' not in callbacks
    assert 'subscription_resume_checkout' not in callbacks


def test_gift_return_button_survives_when_cart_resume_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'CART_RESUME_ENABLED', False)

    keyboard = get_insufficient_balance_keyboard(
        'ru',
        amount_kopeks=12300,
        has_saved_cart=True,
        resume_callback='return_to_gift_cart',
        resume_text='Вернуться к подарку',
    )

    callbacks = [button.callback_data for button in _buttons(keyboard)]
    assert 'return_to_gift_cart' in callbacks


def test_resume_button_still_available_when_flag_is_on(monkeypatch):
    monkeypatch.setattr(settings, 'CART_RESUME_ENABLED', True)

    keyboard = get_insufficient_balance_keyboard(
        'ru',
        amount_kopeks=12300,
        has_saved_cart=True,
    )

    callbacks = [button.callback_data for button in _buttons(keyboard)]
    assert 'return_to_saved_cart' in callbacks


def test_main_menu_resume_is_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'CART_RESUME_ENABLED', False)

    keyboard = get_main_menu_keyboard(
        'ru',
        has_active_subscription=True,
        subscription_is_active=True,
        show_resume_checkout=True,
        has_saved_cart=True,
    )

    callbacks = [button.callback_data for button in _buttons(keyboard)]
    assert 'return_to_saved_cart' not in callbacks
    assert 'subscription_resume_checkout' not in callbacks


def test_checkout_resume_respects_the_flag(monkeypatch):
    user = SimpleNamespace(id=1)

    monkeypatch.setattr(settings, 'CART_RESUME_ENABLED', False)
    assert should_offer_checkout_resume(user, True) is False

    monkeypatch.setattr(settings, 'CART_RESUME_ENABLED', True)
    assert should_offer_checkout_resume(user, True) is True
