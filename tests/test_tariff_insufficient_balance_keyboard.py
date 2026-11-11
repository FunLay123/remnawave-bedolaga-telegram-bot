from app.config import settings
from app.handlers.subscription.tariff_purchase import (
    get_tariff_extend_insufficient_balance_keyboard,
    get_tariff_insufficient_balance_keyboard,
)


def _callbacks(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def test_purchase_offers_single_topup_button(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)

    keyboard = get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)
    topup = next(button for button in _buttons(keyboard) if button.text.startswith('Пополнить на '))

    assert topup.text == f'Пополнить на {settings.format_price(50000)}'
    assert topup.callback_data == 'topup_sum|50000'
    assert topup.icon_custom_emoji_id == '5332588449335628124'
    assert topup.style == 'primary'
    assert not any((c or '').startswith('topup_amount|') for c in callbacks)
    assert 'tariff_select:7' in callbacks


def test_purchase_topup_does_not_depend_on_autopurchase_flag(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    keyboard = get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)

    assert 'topup_sum|50000' in callbacks
    assert not any((c or '').startswith('topup_amount|') for c in callbacks)


def test_non_positive_missing_amount_keeps_plain_balance_topup(monkeypatch):
    """Нехватка <= 0: явной суммы нет, кнопка открывает экран выбора суммы."""
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)

    for missing in (0, -100):
        keyboards = [
            get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=missing),
            get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=missing),
        ]
        for keyboard in keyboards:
            callbacks = _callbacks(keyboard)
            assert 'balance_topup' in callbacks
            assert not any((c or '').startswith('topup_sum|') for c in callbacks)


def test_purchase_message_is_compact_and_has_no_cart_hint():
    from app.localization.texts import get_texts

    text = (
        get_texts('ru')
        .t('TARIFF_PURCHASE_INSUFFICIENT', '')
        .format(
            price='150 ₽',
            period='30 дней',
            missing='150 ₽',
            balance='0 ₽',
        )
    )

    assert '5269616102698163129' in text
    assert '5116444615301399317' in text
    assert '5449444442056441600' in text
    assert 'К оплате: 150 ₽ (30 дней)' in text
    assert 'Не хватает: 150 ₽ (баланс: 0 ₽)' in text
    assert 'Тариф' not in text
    assert 'Корзина' not in text
    assert 'автоматически' not in text


def test_sbp_purchase_offered_on_insufficient_balance(monkeypatch):
    """СБП-оформлению баланс не нужен — кнопка обязана быть на экране нехватки."""
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: True)

    for autopurchase in (False, True):
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', autopurchase)
        callbacks = _callbacks(get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000))

        assert 'tariff_sbp:7' in callbacks
        assert 'topup_sum|50000' in callbacks


def test_no_sbp_purchase_when_recurrent_disabled(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)

    callbacks = _callbacks(get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000))

    assert not any((c or '').startswith('tariff_sbp:') for c in callbacks)


def test_daily_insufficient_balance_offers_sbp_purchase(monkeypatch):
    from app.handlers.subscription.tariff_purchase import get_daily_tariff_insufficient_balance_keyboard

    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: True)
    assert 'tariff_sbp:7' in _callbacks(get_daily_tariff_insufficient_balance_keyboard(7, 'ru'))

    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: False)
    assert 'tariff_sbp:7' not in _callbacks(get_daily_tariff_insufficient_balance_keyboard(7, 'ru'))


def test_extend_offers_single_topup_button_when_autopurchase_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)
    topup = next(button for button in _buttons(keyboard) if button.callback_data == 'topup_sum|50000')

    # отдельные topup_amount|… больше не нужны: сумма недостачи идёт в topup_sum|…
    assert topup.text == f'Пополнить на {settings.format_price(50000)}'
    assert 'topup_sum|50000' in callbacks
    assert not any((c or '').startswith('topup_amount|') for c in callbacks)
    assert 'subscription_extend' in callbacks
    assert not any((c or '').startswith('tariff_select:') for c in callbacks)
    assert not any((c or '').startswith('tariff_sbp:') for c in callbacks)


def test_extend_classic_topup_when_autopurchase_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)

    assert 'topup_sum|50000' in callbacks
    assert 'subscription_extend' in callbacks
    assert not any((c or '').startswith('topup_amount|') for c in callbacks)


def test_extend_topup_amount_is_missing_not_full_price(monkeypatch):
    """Частичная нехватка: на кнопке missing (260), не цена (630) и не баланс (370)."""
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    price = 63000
    balance = 37000
    missing = price - balance  # 26000

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=missing)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    texts = [button.text for button in buttons]

    assert f'Пополнить на {settings.format_price(missing)}' in texts
    assert f'Пополнить на {settings.format_price(price)}' not in texts
    assert f'Пополнить на {settings.format_price(balance)}' not in texts
    assert 'subscription_extend' in [button.callback_data for button in buttons]


def test_extend_back_points_at_the_subscription_in_multi_tariff(monkeypatch):
    """«Назад» обязан адресовать конкретную подписку, иначе он никуда не ведёт.

    На экране возврата после пополнения активная подписка в FSM не закрепляется,
    а само состояние к тому моменту могло быть очищено. Голый
    `subscription_extend` при двух и более подписках упирается в «Выберите
    подписку» и НИЧЕГО не перерисовывает — кнопка выглядит сломанной. Идиома
    `se:{id}` уже используется в monitoring_service и recurrent_payment_service.
    """
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)

    assert 'se:42' in _callbacks(keyboard)


def test_extend_back_stays_generic_without_multi_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)

    assert 'subscription_extend' in _callbacks(keyboard)
    assert not any((c or '').startswith('se:') for c in _callbacks(keyboard))


def test_extend_falls_back_to_topup_without_direct_payment_methods(monkeypatch):
    """Фильтр строк оплаты — единственное, что делает зеркалирование безопасным.

    `get_payment_methods_keyboard` кроме способов оплаты отдаёт и свою навигацию:
    строку поддержки и собственное «Назад» на `menu_balance`. Без фильтра экран
    получил бы ДВЕ кнопки «Назад», ведущие в разные места, а запасной переход
    «Пополнить баланс» перестал бы появляться там, где прямых способов нет.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.keyboards import inline

    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    # Прямых способов нет — только служебные строки.
    monkeypatch.setattr(
        inline,
        'get_payment_methods_keyboard',
        lambda *a, **kw: InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='Поддержка', callback_data='topup_support')],
                [InlineKeyboardButton(text='Назад', callback_data='menu_balance')],
            ]
        ),
    )

    callbacks = _callbacks(get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000))

    assert 'topup_sum|50000' in callbacks, 'кнопка пополнения должна передавать сумму явно'
    assert 'menu_balance' not in callbacks, 'чужая навигация не должна попадать на экран продления'
    assert 'topup_support' not in callbacks
