"""Реестр стилей кнопок пополнения: точечные иконки/primary без правки веток."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from app.config import settings
from app.keyboards.inline import get_payment_methods_keyboard
from app.utils.payment_button_styles import (
    BUTTON_STYLES,
    ButtonStyle,
    apply_button_style,
    resolve_button_style,
    styled_button,
)
from app.utils.payment_utils import payment_invoice_keyboard, payment_support_keyboard


def test_unconfigured_action_is_a_noop() -> None:
    button = InlineKeyboardButton(text='⭐ Telegram Stars', callback_data='topup_stars')
    assert apply_button_style(button, 'method:not_configured') is button


def test_icon_is_applied_and_leading_emoji_is_stripped(monkeypatch) -> None:
    monkeypatch.setitem(
        BUTTON_STYLES,
        'method:stars',
        ButtonStyle(icon_custom_emoji_id='1234567890', style='primary'),
    )
    button = InlineKeyboardButton(text='⭐ Telegram Stars', callback_data='topup_stars')

    styled = apply_button_style(button, 'method:stars')

    assert styled.text == 'Telegram Stars'
    assert styled.icon_custom_emoji_id == '1234567890'
    assert styled.style == 'primary'
    assert styled.callback_data == 'topup_stars'


def test_invalid_icon_id_is_ignored_but_style_still_applies(monkeypatch) -> None:
    monkeypatch.setitem(
        BUTTON_STYLES,
        'method:stars',
        ButtonStyle(icon_custom_emoji_id='not-a-number', style='primary'),
    )
    styled = apply_button_style(
        InlineKeyboardButton(text='⭐ Telegram Stars', callback_data='topup_stars'),
        'method:stars',
    )

    assert styled.icon_custom_emoji_id is None
    assert styled.text == '⭐ Telegram Stars'
    assert styled.style == 'primary'


def test_invalid_style_is_ignored(monkeypatch) -> None:
    monkeypatch.setitem(BUTTON_STYLES, 'method:stars', ButtonStyle(style='rainbow'))
    styled = apply_button_style(
        InlineKeyboardButton(text='Telegram Stars', callback_data='topup_stars'),
        'method:stars',
    )

    assert styled.style is None


def test_amount_and_submethod_fallbacks(monkeypatch) -> None:
    monkeypatch.setitem(BUTTON_STYLES, 'amount:*', ButtonStyle(icon_custom_emoji_id='11'))
    monkeypatch.setitem(BUTTON_STYLES, 'submethod:platega:*', ButtonStyle(icon_custom_emoji_id='22'))

    assert resolve_button_style('amount:50000').icon_custom_emoji_id == '11'
    assert resolve_button_style('submethod:platega:2').icon_custom_emoji_id == '22'


def test_invoice_provider_fallback(monkeypatch) -> None:
    monkeypatch.setitem(BUTTON_STYLES, 'invoice:pay', ButtonStyle(icon_custom_emoji_id='44'))
    monkeypatch.setitem(BUTTON_STYLES, 'invoice:pay:pal24:*', ButtonStyle(icon_custom_emoji_id='55'))

    assert resolve_button_style('invoice:pay:pal24:sbp').icon_custom_emoji_id == '55'
    assert resolve_button_style('invoice:pay:stars').icon_custom_emoji_id == '44'


def test_exact_key_beats_fallback(monkeypatch) -> None:
    monkeypatch.setitem(BUTTON_STYLES, 'amount:*', ButtonStyle(icon_custom_emoji_id='11'))
    monkeypatch.setitem(BUTTON_STYLES, 'amount:50000', ButtonStyle(icon_custom_emoji_id='33'))

    assert resolve_button_style('amount:50000').icon_custom_emoji_id == '33'


def test_nav_keys_are_supported_but_empty_by_default(monkeypatch) -> None:
    assert resolve_button_style('nav:back') is None
    assert resolve_button_style('nav:cancel') is None

    monkeypatch.setitem(BUTTON_STYLES, 'nav:cancel', ButtonStyle(style='danger'))
    styled = apply_button_style(
        InlineKeyboardButton(text='❌ Отмена', callback_data='balance_topup'),
        'nav:cancel',
    )
    assert styled.style == 'danger'


def test_styled_button_preserves_url_and_callback() -> None:
    callback_button = styled_button('support:write', 'Поддержка', callback_data='menu_support')
    url_button = styled_button('invoice:pay', 'Оплатить', url='https://example.com')

    assert callback_button.callback_data == 'menu_support'
    assert callback_button.url is None
    assert url_button.url == 'https://example.com'
    assert url_button.callback_data is None


def test_methods_keyboard_uses_registry(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setitem(
        BUTTON_STYLES,
        'method:stars',
        ButtonStyle(icon_custom_emoji_id='1234567890', style='primary'),
    )

    keyboard = get_payment_methods_keyboard(0, 'ru')
    button = next(
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data == 'topup_stars'
    )

    assert button.text == 'Telegram Stars'
    assert button.icon_custom_emoji_id == '1234567890'
    assert button.style == 'primary'


def test_invoice_and_support_keyboards_use_registry(monkeypatch) -> None:
    monkeypatch.setitem(
        BUTTON_STYLES,
        'invoice:pay',
        ButtonStyle(icon_custom_emoji_id='444', style='success'),
    )
    monkeypatch.setitem(BUTTON_STYLES, 'nav:cancel', ButtonStyle(style='danger'))
    monkeypatch.setitem(BUTTON_STYLES, 'support:write', ButtonStyle(icon_custom_emoji_id='555'))

    invoice = payment_invoice_keyboard(pay_url='https://example.com', amount_kopeks=50_000)
    pay = invoice.inline_keyboard[0][0]
    cancel = invoice.inline_keyboard[-1][0]
    assert pay.icon_custom_emoji_id == '444'
    assert pay.style == 'success'
    assert cancel.style == 'danger'

    support = payment_support_keyboard('ru', 'https://t.me/support')
    write = support.inline_keyboard[0][0]
    assert write.icon_custom_emoji_id == '555'
