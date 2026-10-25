"""Единые тексты экранов пополнения: целые рубли, шапка и буллиты."""

from app.utils.payment_utils import (
    PAYMENT_PAY_ICON_ID,
    PAYMENT_SCREEN_HEADER,
    format_max_rubles,
    format_min_rubles,
    format_whole_rubles,
    get_payment_methods_text,
    payment_amount_error_text,
    payment_amount_range_error,
    payment_amount_text,
    payment_invoice_keyboard,
    payment_invoice_text,
    payment_success_text,
    payment_support_text,
)


def test_whole_rubles_drops_kopecks_without_rounding_up() -> None:
    assert format_whole_rubles(18_699) == '186 ₽'
    assert format_whole_rubles(100) == '1 ₽'
    assert format_whole_rubles(0) == '0 ₽'


def test_methods_screen_has_balance_and_no_method_descriptions() -> None:
    text = get_payment_methods_text('ru', 15_000, has_direct_methods=True)

    assert text.startswith(PAYMENT_SCREEN_HEADER)
    assert '<b>Пополнение баланса</b>' in text
    assert '⚪️ Баланс: <b>150 ₽</b>' in text
    assert '▫️ Выберите способ оплаты:' in text
    assert 'быстро и удобно' not in text
    assert 'Выберите способ пополнения' not in text


def test_only_support_and_no_methods_use_a_short_blockquote(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, 'SUPPORT_TOPUP_ENABLED', True)
    only_support = get_payment_methods_text('ru', 0, has_direct_methods=False)
    assert '<blockquote>' in only_support
    assert 'Напишите в поддержку' in only_support
    assert '⚪️ Баланс' not in only_support

    monkeypatch.setattr(settings, 'SUPPORT_TOPUP_ENABLED', False)
    none = get_payment_methods_text('ru', 0, has_direct_methods=False)
    assert '<blockquote>Способы оплаты временно недоступны' in none


def test_amount_screen_uses_provider_and_whole_ruble_range() -> None:
    text = payment_amount_text('Telegram Stars', 5_000, 5_000_000)

    assert '<b>Сумма пополнения</b>' in text
    assert '⚪️ Способ: Telegram Stars' in text
    assert 'от 50 ₽ до 50000 ₽' in text


def test_range_rounds_min_up_and_max_down() -> None:
    assert format_min_rubles(5_055) == '51 ₽'
    assert format_max_rubles(5_099) == '50 ₽'

    text = payment_amount_range_error(5_055, 10_000)
    assert 'от 51 ₽ до 100 ₽' in text


def test_range_without_whole_ruble_values_shows_exact_bounds() -> None:
    text = payment_amount_range_error(5_055, 5_099)
    assert 'от 50.55 ₽ до 50.99 ₽' in text


def test_range_clamped_to_global_manual_input_limits() -> None:
    # Провайдер требует минимум выше глобального потолка ручного ввода:
    # обещать «от 50 001 до 60 000 ₽» нельзя — бот отвергнет такие суммы.
    text = payment_amount_range_error(5_000_001, 6_000_000)
    assert 'недоступно' in text.lower()
    assert '50 001' not in text


def test_invoice_screen_has_amount_and_optional_stars() -> None:
    text = payment_invoice_text('Telegram Stars', 15_000, stars_amount=150)
    assert '<b>Счёт на оплату</b>' in text
    assert '⚪️ Способ: Telegram Stars' in text
    assert '▫️ К оплате: <b>150 ₽ (150 ★)</b>' in text


def test_invoice_screen_can_show_secondary_currency() -> None:
    text = payment_invoice_text('OverPay · EUR', 100_00, amount_note='≈ 1.00 €')
    assert '100 ₽ (≈ 1.00 €)' in text


def test_invoice_screen_without_known_amount_points_to_provider_page() -> None:
    text = payment_invoice_text('Tribute', None)
    assert 'Сумму укажете на странице оплаты' in text


def test_success_screen_shows_credit_and_balance_transition() -> None:
    text = payment_success_text(15_000, 30_000, 15_000)
    assert '<b>Баланс пополнен</b>' in text
    assert '⚪️ Зачислено: <b>+150 ₽</b>' in text
    assert '▫️ Баланс: 150 ₽ → 300 ₽' in text


def test_success_screen_without_old_balance_shows_only_new_balance() -> None:
    text = payment_success_text(15_000, 30_000)
    assert '▫️ Баланс: 300 ₽' in text
    assert '→' not in text


def test_success_screen_without_any_balance_omits_balance_line() -> None:
    text = payment_success_text(15_000, None)
    assert 'Зачислено: <b>+150 ₽</b>' in text
    assert '▫️ Баланс' not in text
    assert '→' not in text


def test_support_screen_is_compact_and_escapes_user_id() -> None:
    text = payment_support_text('<42>')
    assert '<b>Через поддержку</b>' in text
    assert '<code>&lt;42&gt;</code>' in text
    assert '1-24 часа' not in text


def test_amount_error_is_a_compact_blockquote() -> None:
    text = payment_amount_error_text('Допустимая сумма: от 50 до 5000 ₽')
    assert '<blockquote>Допустимая сумма: от 50 до 5000 ₽</blockquote>' in text


def test_invoice_keyboard_can_use_callback_pay_button() -> None:
    keyboard = payment_invoice_keyboard(
        pay_callback='stars_test_pay|50000|token',
        amount_kopeks=50_000,
        pay_text='Оплатить 500 ★',
    )
    pay_button = keyboard.inline_keyboard[0][0]
    assert pay_button.text == 'Оплатить 500 ★'
    assert pay_button.callback_data == 'stars_test_pay|50000|token'
    assert pay_button.url is None
    assert pay_button.style == 'primary'
    assert pay_button.icon_custom_emoji_id == PAYMENT_PAY_ICON_ID


def test_invoice_keyboard_has_primary_pay_button_and_cancel() -> None:
    keyboard = payment_invoice_keyboard(
        pay_url='https://example.com/pay',
        amount_kopeks=50_000,
        status_callback='check_demo_1',
    )
    pay_button = keyboard.inline_keyboard[0][0]
    assert pay_button.text == 'Оплатить 500 ₽'
    assert pay_button.style == 'primary'
    assert pay_button.icon_custom_emoji_id == PAYMENT_PAY_ICON_ID
    assert keyboard.inline_keyboard[-1][0].callback_data == 'balance_topup'
