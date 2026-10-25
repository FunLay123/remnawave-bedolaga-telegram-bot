import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.localization.texts import get_texts


# Единый визуальный язык экранов пополнения. Шапка — тот же кастомный эмодзи,
# что и на экранах подписки/продления; буллиты — ⚪️ и ▫️.
PAYMENT_SCREEN_HEADER = '<tg-emoji emoji-id="5359415772844152891">📋</tg-emoji>'
PAYMENT_BULLET = '⚪️'
PAYMENT_SUB_BULLET = '▫️'
PAYMENT_PAY_ICON_ID = '5334552404736104314'
PAYMENT_SUPPORT_ICON_ID = '5988023995125993550'

DEFAULT_TOPUP_MIN_KOPEKS = 1_00
DEFAULT_TOPUP_MAX_KOPEKS = 50_000_00


def format_whole_rubles(amount_kopeks: int | None) -> str:
    """Сумма строго в целых рублях: 18 699 копеек → «186 ₽»."""
    return f'{int(amount_kopeks or 0) // 100} ₽'


def format_min_rubles(amount_kopeks: int | None) -> str:
    """Минимум округляется вверх: показанная граница должна проходить валидацию."""
    return f'{(int(amount_kopeks or 0) + 99) // 100} ₽'


def format_max_rubles(amount_kopeks: int | None) -> str:
    """Максимум округляется вниз по той же причине."""
    return f'{int(amount_kopeks or 0) // 100} ₽'


def _format_exact_rubles(amount_kopeks: int) -> str:
    """Точная сумма без округления — для диапазонов уже одного рубля."""
    rubles, kopeks = divmod(max(int(amount_kopeks), 0), 100)
    if kopeks == 0:
        return f'{rubles} ₽'
    return f'{rubles}.{kopeks:02d}'.rstrip('0').rstrip('.') + ' ₽'


def payment_methods_text(balance_kopeks: int) -> str:
    return (
        f'{PAYMENT_SCREEN_HEADER} <b>Пополнение баланса</b>\n\n'
        f'{PAYMENT_BULLET} Баланс: <b>{format_whole_rubles(balance_kopeks)}</b>\n'
        f'{PAYMENT_SUB_BULLET} Выберите способ оплаты:'
    )


def payment_methods_unavailable_text(reason: str) -> str:
    return f'{PAYMENT_SCREEN_HEADER} <b>Пополнение баланса</b>\n\n<blockquote>{html.escape(reason)}</blockquote>'


def payment_amount_text(provider_name: str, min_amount_kopeks: int, max_amount_kopeks: int) -> str:
    return (
        f'{PAYMENT_SCREEN_HEADER} <b>Сумма пополнения</b>\n\n'
        f'{PAYMENT_BULLET} Способ: {html.escape(provider_name)}\n'
        f'{PAYMENT_SUB_BULLET} Выберите сумму или отправьте число в чат '
        f'(от {format_min_rubles(min_amount_kopeks)} до {format_max_rubles(max_amount_kopeks)}):'
    )


def payment_invoice_text(
    provider_name: str,
    amount_kopeks: int | None,
    stars_amount: int | None = None,
    amount_note: str | None = None,
) -> str:
    if amount_kopeks is None:
        amount_line = 'Сумму укажете на странице оплаты'
    else:
        amount = format_whole_rubles(amount_kopeks)
        if stars_amount is not None:
            amount = f'{amount} ({stars_amount} ★)'
        elif amount_note:
            amount = f'{amount} ({html.escape(amount_note)})'
        amount_line = f'К оплате: <b>{amount}</b>'
    return (
        f'{PAYMENT_SCREEN_HEADER} <b>Счёт на оплату</b>\n\n'
        f'{PAYMENT_BULLET} Способ: {html.escape(provider_name)}\n'
        f'{PAYMENT_SUB_BULLET} {amount_line}'
    )


def payment_success_text(
    amount_kopeks: int,
    new_balance_kopeks: int | None,
    old_balance_kopeks: int | None = None,
) -> str:
    lines = [
        f'{PAYMENT_SCREEN_HEADER} <b>Баланс пополнен</b>',
        '',
        f'{PAYMENT_BULLET} Зачислено: <b>+{format_whole_rubles(amount_kopeks)}</b>',
    ]

    # Если баланс не удалось получить, не показываем выдуманное «0 → сумма».
    if new_balance_kopeks is not None:
        if old_balance_kopeks is None:
            balance_line = f'Баланс: {format_whole_rubles(new_balance_kopeks)}'
        else:
            balance_line = (
                f'Баланс: {format_whole_rubles(old_balance_kopeks)} → {format_whole_rubles(new_balance_kopeks)}'
            )
        lines.append(f'{PAYMENT_SUB_BULLET} {balance_line}')

    return '\n'.join(lines)


def payment_support_text(user_id: str) -> str:
    return (
        f'{PAYMENT_SCREEN_HEADER} <b>Через поддержку</b>\n\n'
        f'{PAYMENT_BULLET} Ваш ID: <code>{html.escape(str(user_id))}</code>\n'
        f'{PAYMENT_SUB_BULLET} Напишите в поддержку сумму пополнения — выдадим реквизиты и зачислим баланс.'
    )


def payment_amount_error_text(reason: str) -> str:
    return f'{PAYMENT_SCREEN_HEADER} <b>Сумма пополнения</b>\n\n<blockquote>{html.escape(reason)}</blockquote>'


def payment_amount_range_error(min_amount_kopeks: int, max_amount_kopeks: int) -> str:
    """Компактная ошибка с границами, которые действительно пройдут валидацию.

    Показываем пересечение лимитов провайдера с глобальным диапазоном ручного
    ввода: иначе сообщение может обещать суммы, которые бот отвергнет раньше.
    """
    lower = max(int(min_amount_kopeks), DEFAULT_TOPUP_MIN_KOPEKS)
    upper = min(int(max_amount_kopeks), DEFAULT_TOPUP_MAX_KOPEKS)

    if lower > upper:
        return payment_amount_error_text(
            'Пополнение этим способом сейчас недоступно: лимиты провайдера не пересекаются '
            'с допустимым диапазоном'
        )

    whole_min = (lower + 99) // 100
    whole_max = upper // 100
    if whole_min <= whole_max:
        return payment_amount_error_text(f'Допустимая сумма: от {whole_min} ₽ до {whole_max} ₽')

    return payment_amount_error_text(
        f'Допустимая сумма: от {_format_exact_rubles(lower)} до {_format_exact_rubles(upper)}'
    )


def payment_submethod_text(provider_name: str) -> str:
    return (
        f'{PAYMENT_SCREEN_HEADER} <b>Способ оплаты</b>\n\n'
        f'{PAYMENT_BULLET} {html.escape(provider_name)}\n'
        f'{PAYMENT_SUB_BULLET} Выберите вариант:'
    )


def payment_invoice_keyboard(
    *,
    language: str = 'ru',
    pay_url: str | None = None,
    pay_callback: str | None = None,
    amount_kopeks: int = 0,
    pay_text: str | None = None,
    status_callback: str | None = None,
    back_callback: str = 'balance_topup',
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows: list[list[InlineKeyboardButton]] = []

    if pay_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text=pay_text or f'Оплатить {format_whole_rubles(amount_kopeks)}',
                    url=pay_url,
                    style='primary',
                    icon_custom_emoji_id=PAYMENT_PAY_ICON_ID,
                )
            ]
        )
    elif pay_callback:
        rows.append(
            [
                InlineKeyboardButton(
                    text=pay_text or f'Оплатить {format_whole_rubles(amount_kopeks)}',
                    callback_data=pay_callback,
                    style='primary',
                    icon_custom_emoji_id=PAYMENT_PAY_ICON_ID,
                )
            ]
        )

    if extra_rows:
        rows.extend(extra_rows)

    if status_callback:
        rows.append([InlineKeyboardButton(text='Проверить статус', callback_data=status_callback)])

    rows.append([InlineKeyboardButton(text=texts.CANCEL, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_support_keyboard(language: str, support_url: str | None) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows: list[list[InlineKeyboardButton]] = []
    if support_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text='Написать в поддержку',
                    url=support_url,
                    style='primary',
                    icon_custom_emoji_id=PAYMENT_SUPPORT_ICON_ID,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def verify_payment_amount(
    received_kopeks: int,
    expected_kopeks: int,
    tolerance_kopeks: int = 1,
) -> bool:
    """Check that the received amount matches the expected amount within tolerance."""
    return abs(received_kopeks - expected_kopeks) <= tolerance_kopeks


def get_available_payment_methods() -> list[dict[str, str]]:
    """
    Возвращает список доступных способов оплаты с их настройками
    """
    methods = []

    if settings.TELEGRAM_STARS_ENABLED:
        methods.append(
            {
                'id': 'stars',
                'name': 'Telegram Stars',
                'icon': '⭐',
                'description': 'быстро и удобно',
                'callback': 'topup_stars',
            }
        )

    if settings.is_yookassa_enabled():
        if getattr(settings, 'YOOKASSA_SBP_ENABLED', False):
            methods.append(
                {
                    'id': 'yookassa_sbp',
                    'name': 'СБП (YooKassa)',
                    'icon': '🏦',
                    'description': 'моментальная оплата по QR',
                    'callback': 'topup_yookassa_sbp',
                }
            )

        methods.append(
            {
                'id': 'yookassa',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': 'через YooKassa',
                'callback': 'topup_yookassa',
            }
        )

    if settings.TRIBUTE_ENABLED:
        methods.append(
            {
                'id': 'tribute',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': 'через Tribute',
                'callback': 'topup_tribute',
            }
        )

    if settings.is_mulenpay_enabled():
        mulenpay_name = settings.get_mulenpay_display_name()
        methods.append(
            {
                'id': 'mulenpay',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': f'через {mulenpay_name}',
                'callback': 'topup_mulenpay',
            }
        )

    if settings.is_wata_enabled():
        methods.append(
            {
                'id': 'wata',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': 'через WATA',
                'callback': 'topup_wata',
            }
        )

    if settings.is_pal24_enabled():
        methods.append(
            {'id': 'pal24', 'name': 'СБП', 'icon': '🏦', 'description': 'через PayPalych', 'callback': 'topup_pal24'}
        )

    if settings.is_cryptobot_enabled():
        methods.append(
            {
                'id': 'cryptobot',
                'name': 'Криптовалюта',
                'icon': '🪙',
                'description': 'через CryptoBot',
                'callback': 'topup_cryptobot',
            }
        )

    if settings.is_heleket_enabled():
        methods.append(
            {
                'id': 'heleket',
                'name': 'Криптовалюта',
                'icon': '🪙',
                'description': 'через Heleket',
                'callback': 'topup_heleket',
            }
        )

    if settings.is_platega_enabled() and settings.get_platega_active_methods():
        platega_name = settings.get_platega_display_name()
        if settings.PLATEGA_INLINE_METHODS:
            for method_code in settings.get_platega_active_methods():
                info = settings.get_platega_method_definitions().get(method_code, {})
                methods.append(
                    {
                        'id': f'platega_m{method_code}',
                        'name': info.get('name', f'Метод {method_code}'),
                        'icon': info.get('title', '💳').split(' ', 1)[0] if info.get('title') else '💳',
                        'description': f'через {platega_name}',
                        'callback': f'topup_platega_m{method_code}',
                    }
                )
        else:
            methods.append(
                {
                    'id': 'platega',
                    'name': 'Банковская карта',
                    'icon': '💳',
                    'description': f'через {platega_name} (карты + СБП)',
                    'callback': 'topup_platega',
                }
            )

    if settings.is_cloudpayments_enabled():
        cloudpayments_name = settings.get_cloudpayments_display_name()
        methods.append(
            {
                'id': 'cloudpayments',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': f'через {cloudpayments_name}',
                'callback': 'topup_cloudpayments',
            }
        )

    if settings.is_freekassa_enabled():
        freekassa_name = settings.get_freekassa_display_name()
        methods.append(
            {
                'id': 'freekassa',
                'name': freekassa_name,
                'icon': '💳',
                'description': f'через {freekassa_name}',
                'callback': 'topup_freekassa',
            }
        )

    if settings.is_kassa_ai_enabled():
        kassa_ai_name = settings.get_kassa_ai_display_name()
        methods.append(
            {
                'id': 'kassa_ai',
                'name': kassa_ai_name,
                'icon': '💳',
                'description': f'через {kassa_ai_name}',
                'callback': 'topup_kassa_ai',
            }
        )

    if settings.is_riopay_enabled():
        riopay_name = settings.get_riopay_display_name()
        methods.append(
            {
                'id': 'riopay',
                'name': f'Банковская карта ({riopay_name})',
                'icon': '💳',
                'description': f'через {riopay_name}',
                'callback': 'topup_riopay',
            }
        )

    if settings.is_severpay_enabled():
        severpay_name = settings.get_severpay_display_name()
        methods.append(
            {
                'id': 'severpay',
                'name': f'Банковская карта ({severpay_name})',
                'icon': '💳',
                'description': f'через {severpay_name}',
                'callback': 'topup_severpay',
            }
        )

    if settings.is_paypear_enabled():
        paypear_name = settings.get_paypear_display_name()
        methods.append(
            {
                'id': 'paypear',
                'name': paypear_name,
                'icon': '💳',
                'description': f'через {paypear_name}',
                'callback': 'topup_paypear',
            }
        )

    if settings.is_rollypay_enabled():
        rollypay_name = settings.get_rollypay_display_name()
        methods.append(
            {
                'id': 'rollypay',
                'name': rollypay_name,
                'icon': '💳',
                'description': f'через {rollypay_name}',
                'callback': 'topup_rollypay',
            }
        )

    if settings.is_overpay_enabled():
        overpay_name = settings.get_overpay_display_name()
        methods.append(
            {
                'id': 'overpay',
                'name': overpay_name,
                'icon': '💳',
                'description': f'через {overpay_name}',
                'callback': 'topup_overpay',
            }
        )

    if settings.is_aurapay_sbp_enabled():
        sbp_name = settings.get_aurapay_sbp_display_name()
        methods.append(
            {
                'id': 'aurapay_sbp',
                'name': sbp_name,
                'icon': '📱',
                'description': f'через {sbp_name}',
                'callback': 'topup_aurapay_sbp',
            }
        )

    if settings.is_aurapay_card_enabled():
        card_name = settings.get_aurapay_card_display_name()
        methods.append(
            {
                'id': 'aurapay_card',
                'name': card_name,
                'icon': '💳',
                'description': f'через {card_name}',
                'callback': 'topup_aurapay_card',
            }
        )

    if (
        settings.is_aurapay_enabled()
        and not settings.is_aurapay_sbp_enabled()
        and not settings.is_aurapay_card_enabled()
    ):
        aurapay_name = settings.get_aurapay_display_name()
        methods.append(
            {
                'id': 'aurapay',
                'name': aurapay_name,
                'icon': '💳',
                'description': f'через {aurapay_name}',
                'callback': 'topup_aurapay',
            }
        )

    # Левая часть — имя метода (LAVA_SBP/CARD_DISPLAY_NAME), правая «через …» —
    # имя провайдера (LAVA_DISPLAY_NAME): одна переменная в обеих частях давала
    # дубль вида «СБП (QR) - через СБП (QR)».
    if settings.is_lava_sbp_enabled():
        sbp_name = settings.get_lava_sbp_display_name()
        lava_name = settings.get_lava_display_name()
        methods.append(
            {
                'id': 'lava_sbp',
                'name': sbp_name,
                'icon': '📱',
                'description': f'через {lava_name}',
                'callback': 'topup_lava_sbp',
            }
        )

    if settings.is_lava_card_enabled():
        card_name = settings.get_lava_card_display_name()
        lava_name = settings.get_lava_display_name()
        methods.append(
            {
                'id': 'lava_card',
                'name': card_name,
                'icon': '💳',
                'description': f'через {lava_name}',
                'callback': 'topup_lava_card',
            }
        )

    if settings.is_lava_enabled() and not settings.is_lava_sbp_enabled() and not settings.is_lava_card_enabled():
        lava_name = settings.get_lava_display_name()
        methods.append(
            {
                'id': 'lava',
                'name': lava_name,
                'icon': '💳',
                'description': f'через {lava_name}',
                'callback': 'topup_lava',
            }
        )

    if settings.is_cispay_sbp_enabled():
        sbp_name = settings.get_cispay_sbp_display_name()
        methods.append(
            {
                'id': 'cispay_sbp',
                'name': sbp_name,
                'icon': '📱',
                'description': f'через {sbp_name}',
                'callback': 'topup_cispay_sbp',
            }
        )

    if settings.is_cispay_card_enabled():
        card_name = settings.get_cispay_card_display_name()
        methods.append(
            {
                'id': 'cispay_card',
                'name': card_name,
                'icon': '💳',
                'description': f'через {card_name}',
                'callback': 'topup_cispay_card',
            }
        )

    if settings.is_cispay_enabled() and not settings.is_cispay_sbp_enabled() and not settings.is_cispay_card_enabled():
        cispay_name = settings.get_cispay_display_name()
        methods.append(
            {
                'id': 'cispay',
                'name': cispay_name,
                'icon': '💳',
                'description': f'через {cispay_name}',
                'callback': 'topup_cispay',
            }
        )

    if settings.is_cashera_enabled():
        cashera_name = settings.get_cashera_display_name()
        if settings.CASHERA_INLINE_METHODS:
            for method_code in settings.get_cashera_active_methods():
                info = settings.get_cashera_method_definitions().get(method_code, {})
                methods.append(
                    {
                        'id': f'cashera_m_{method_code}',
                        'name': info.get('name', method_code),
                        'icon': info.get('title', '💳').split(' ', 1)[0] if info.get('title') else '💳',
                        'description': f'через {cashera_name}',
                        'callback': f'topup_cashera_m_{method_code}',
                    }
                )
        else:
            methods.append(
                {
                    'id': 'cashera',
                    'name': cashera_name,
                    'icon': '💳',
                    'description': f'через {cashera_name}',
                    'callback': 'topup_cashera',
                }
            )

    if settings.is_tabpay_sbp_enabled():
        sbp_name = settings.get_tabpay_sbp_display_name()
        methods.append(
            {
                'id': 'tabpay_sbp',
                'name': sbp_name,
                'icon': '📱',
                'description': f'через {sbp_name}',
                'callback': 'topup_tabpay_sbp',
            }
        )

    if settings.is_tabpay_card_enabled():
        card_name = settings.get_tabpay_card_display_name()
        methods.append(
            {
                'id': 'tabpay_card',
                'name': card_name,
                'icon': '💳',
                'description': f'через {card_name}',
                'callback': 'topup_tabpay_card',
            }
        )

    if settings.is_tabpay_enabled() and not settings.is_tabpay_sbp_enabled() and not settings.is_tabpay_card_enabled():
        tabpay_name = settings.get_tabpay_display_name()
        methods.append(
            {
                'id': 'tabpay',
                'name': tabpay_name,
                'icon': '💳',
                'description': f'через {tabpay_name}',
                'callback': 'topup_tabpay',
            }
        )

    if settings.is_paritypay_sbp_enabled():
        sbp_name = settings.get_paritypay_sbp_display_name()
        methods.append(
            {
                'id': 'paritypay_sbp',
                'name': sbp_name,
                'icon': '📱',
                'description': f'через {sbp_name}',
                'callback': 'topup_paritypay_sbp',
            }
        )

    if settings.is_paritypay_card_enabled():
        card_name = settings.get_paritypay_card_display_name()
        methods.append(
            {
                'id': 'paritypay_card',
                'name': card_name,
                'icon': '💳',
                'description': f'через {card_name}',
                'callback': 'topup_paritypay_card',
            }
        )

    if (
        settings.is_paritypay_enabled()
        and not settings.is_paritypay_sbp_enabled()
        and not settings.is_paritypay_card_enabled()
    ):
        paritypay_name = settings.get_paritypay_display_name()
        methods.append(
            {
                'id': 'paritypay',
                'name': paritypay_name,
                'icon': '💳',
                'description': f'через {paritypay_name}',
                'callback': 'topup_paritypay',
            }
        )

    if settings.is_etoplatezhi_sbp_enabled():
        sbp_name = settings.get_etoplatezhi_sbp_display_name()
        methods.append(
            {
                'id': 'etoplatezhi_sbp',
                'name': sbp_name,
                'icon': '📱',
                'description': f'через {sbp_name}',
                'callback': 'topup_etoplatezhi_sbp',
            }
        )

    if settings.is_etoplatezhi_card_enabled():
        card_name = settings.get_etoplatezhi_card_display_name()
        methods.append(
            {
                'id': 'etoplatezhi_card',
                'name': card_name,
                'icon': '💳',
                'description': f'через {card_name}',
                'callback': 'topup_etoplatezhi_card',
            }
        )

    if (
        settings.is_etoplatezhi_enabled()
        and not settings.is_etoplatezhi_sbp_enabled()
        and not settings.is_etoplatezhi_card_enabled()
    ):
        etoplatezhi_name = settings.get_etoplatezhi_display_name()
        methods.append(
            {
                'id': 'etoplatezhi',
                'name': etoplatezhi_name,
                'icon': '💳',
                'description': f'через {etoplatezhi_name}',
                'callback': 'topup_etoplatezhi',
            }
        )

    if settings.is_antilopay_sbp_enabled():
        sbp_name = settings.get_antilopay_sbp_display_name()
        methods.append(
            {
                'id': 'antilopay_sbp',
                'name': sbp_name,
                'icon': '📱',
                'description': f'через {sbp_name}',
                'callback': 'topup_antilopay_sbp',
            }
        )

    if settings.is_antilopay_card_enabled():
        card_name = settings.get_antilopay_card_display_name()
        methods.append(
            {
                'id': 'antilopay_card',
                'name': card_name,
                'icon': '💳',
                'description': f'через {card_name}',
                'callback': 'topup_antilopay_card',
            }
        )

    if settings.is_antilopay_sberpay_enabled():
        sberpay_name = settings.get_antilopay_sberpay_display_name()
        methods.append(
            {
                'id': 'antilopay_sberpay',
                'name': sberpay_name,
                'icon': '💳',
                'description': f'через {sberpay_name}',
                'callback': 'topup_antilopay_sberpay',
            }
        )

    if (
        settings.is_antilopay_enabled()
        and not settings.is_antilopay_sbp_enabled()
        and not settings.is_antilopay_card_enabled()
        and not settings.is_antilopay_sberpay_enabled()
    ):
        antilopay_name = settings.get_antilopay_display_name()
        methods.append(
            {
                'id': 'antilopay',
                'name': antilopay_name,
                'icon': '💳',
                'description': f'через {antilopay_name}',
                'callback': 'topup_antilopay',
            }
        )

    if settings.is_support_topup_enabled():
        methods.append(
            {
                'id': 'support',
                'name': 'Через поддержку',
                'icon': '🛠️',
                'description': 'другие способы',
                'callback': 'topup_support',
            }
        )

    return methods


def get_payment_methods_text(
    language: str,
    balance_kopeks: int = 0,
    *,
    has_direct_methods: bool = True,
) -> str:
    """Текст экрана «Пополнение баланса».

    ``has_direct_methods`` считает вызывающий хэндлер по фактической клавиатуре:
    список способов в тексте больше не дублируется, а от наличия автоматических
    методов зависит только короткая подсказка.
    """
    if has_direct_methods:
        return payment_methods_text(balance_kopeks)

    if settings.is_support_topup_enabled():
        reason = 'Автоматические способы оплаты недоступны. Напишите в поддержку.'
    else:
        reason = 'Способы оплаты временно недоступны.'

    return payment_methods_unavailable_text(reason)


def is_payment_method_available(method_id: str) -> bool:
    """
    Проверяет, доступен ли конкретный способ оплаты
    """
    if method_id == 'stars':
        return settings.TELEGRAM_STARS_ENABLED
    if method_id == 'yookassa':
        return settings.is_yookassa_enabled()
    if method_id == 'tribute':
        return settings.TRIBUTE_ENABLED
    if method_id == 'mulenpay':
        return settings.is_mulenpay_enabled()
    if method_id == 'wata':
        return settings.is_wata_enabled()
    if method_id == 'pal24':
        return settings.is_pal24_enabled()
    if method_id == 'cryptobot':
        return settings.is_cryptobot_enabled()
    if method_id == 'heleket':
        return settings.is_heleket_enabled()
    if method_id == 'platega':
        return settings.is_platega_enabled() and bool(settings.get_platega_active_methods())
    if method_id.startswith('platega_m'):
        if not settings.is_platega_enabled():
            return False
        try:
            code = int(method_id[len('platega_m') :])
        except ValueError:
            return False
        return code in settings.get_platega_active_methods()
    if method_id == 'cloudpayments':
        return settings.is_cloudpayments_enabled()
    if method_id == 'freekassa':
        return settings.is_freekassa_enabled()
    if method_id == 'kassa_ai':
        return settings.is_kassa_ai_enabled()
    if method_id == 'riopay':
        return settings.is_riopay_enabled()
    if method_id == 'severpay':
        return settings.is_severpay_enabled()
    if method_id == 'paypear':
        return settings.is_paypear_enabled()
    if method_id == 'rollypay':
        return settings.is_rollypay_enabled()
    if method_id == 'overpay':
        return settings.is_overpay_enabled()
    if method_id == 'aurapay':
        return settings.is_aurapay_enabled()
    if method_id == 'aurapay_sbp':
        return settings.is_aurapay_sbp_enabled()
    if method_id == 'aurapay_card':
        return settings.is_aurapay_card_enabled()
    if method_id == 'lava':
        return settings.is_lava_enabled()
    if method_id == 'lava_sbp':
        return settings.is_lava_sbp_enabled()
    if method_id == 'lava_card':
        return settings.is_lava_card_enabled()
    if method_id == 'cispay':
        return settings.is_cispay_enabled()
    if method_id == 'cashera':
        return settings.is_cashera_enabled()
    if method_id.startswith('cashera_m_'):
        return settings.is_cashera_enabled() and method_id.removeprefix('cashera_m_') in (
            settings.get_cashera_active_methods()
        )
    if method_id == 'cispay_sbp':
        return settings.is_cispay_sbp_enabled()
    if method_id == 'cispay_card':
        return settings.is_cispay_card_enabled()
    if method_id == 'tabpay':
        return settings.is_tabpay_enabled()
    if method_id == 'tabpay_sbp':
        return settings.is_tabpay_sbp_enabled()
    if method_id == 'tabpay_card':
        return settings.is_tabpay_card_enabled()
    if method_id == 'paritypay':
        return settings.is_paritypay_enabled()
    if method_id == 'paritypay_sbp':
        return settings.is_paritypay_sbp_enabled()
    if method_id == 'paritypay_card':
        return settings.is_paritypay_card_enabled()
    if method_id == 'etoplatezhi':
        return settings.is_etoplatezhi_enabled()
    if method_id == 'etoplatezhi_sbp':
        return settings.is_etoplatezhi_sbp_enabled()
    if method_id == 'etoplatezhi_card':
        return settings.is_etoplatezhi_card_enabled()
    if method_id == 'antilopay':
        return settings.is_antilopay_enabled()
    if method_id == 'antilopay_sbp':
        return settings.is_antilopay_sbp_enabled()
    if method_id == 'antilopay_card':
        return settings.is_antilopay_card_enabled()
    if method_id == 'antilopay_sberpay':
        return settings.is_antilopay_sberpay_enabled()
    if method_id == 'support':
        return settings.is_support_topup_enabled()
    return False


def get_payment_method_status() -> dict[str, bool]:
    """
    Возвращает статус всех способов оплаты
    """
    return {
        'stars': settings.TELEGRAM_STARS_ENABLED,
        'yookassa': settings.is_yookassa_enabled(),
        'tribute': settings.TRIBUTE_ENABLED,
        'mulenpay': settings.is_mulenpay_enabled(),
        'wata': settings.is_wata_enabled(),
        'pal24': settings.is_pal24_enabled(),
        'cryptobot': settings.is_cryptobot_enabled(),
        'heleket': settings.is_heleket_enabled(),
        'platega': settings.is_platega_enabled() and bool(settings.get_platega_active_methods()),
        'cloudpayments': settings.is_cloudpayments_enabled(),
        'freekassa': settings.is_freekassa_enabled(),
        'kassa_ai': settings.is_kassa_ai_enabled(),
        'riopay': settings.is_riopay_enabled(),
        'severpay': settings.is_severpay_enabled(),
        'paypear': settings.is_paypear_enabled(),
        'rollypay': settings.is_rollypay_enabled(),
        'overpay': settings.is_overpay_enabled(),
        'aurapay': settings.is_aurapay_enabled(),
        'aurapay_sbp': settings.is_aurapay_sbp_enabled(),
        'aurapay_card': settings.is_aurapay_card_enabled(),
        'lava': settings.is_lava_enabled(),
        'lava_sbp': settings.is_lava_sbp_enabled(),
        'lava_card': settings.is_lava_card_enabled(),
        'etoplatezhi': settings.is_etoplatezhi_enabled(),
        'etoplatezhi_sbp': settings.is_etoplatezhi_sbp_enabled(),
        'etoplatezhi_card': settings.is_etoplatezhi_card_enabled(),
        'antilopay': settings.is_antilopay_enabled(),
        'antilopay_sbp': settings.is_antilopay_sbp_enabled(),
        'antilopay_card': settings.is_antilopay_card_enabled(),
        'antilopay_sberpay': settings.is_antilopay_sberpay_enabled(),
        'support': settings.is_support_topup_enabled(),
    }


def get_enabled_payment_methods_count() -> int:
    """
    Возвращает количество включенных способов оплаты (не считая поддержку)
    """
    count = 0
    if settings.TELEGRAM_STARS_ENABLED:
        count += 1
    if settings.is_yookassa_enabled():
        count += 1
    if settings.TRIBUTE_ENABLED:
        count += 1
    if settings.is_mulenpay_enabled():
        count += 1
    if settings.is_wata_enabled():
        count += 1
    if settings.is_pal24_enabled():
        count += 1
    if settings.is_cryptobot_enabled():
        count += 1
    if settings.is_heleket_enabled():
        count += 1
    if settings.is_platega_enabled() and settings.get_platega_active_methods():
        count += 1
    if settings.is_cloudpayments_enabled():
        count += 1
    if settings.is_freekassa_enabled():
        count += 1
    if settings.is_kassa_ai_enabled():
        count += 1
    if settings.is_riopay_enabled():
        count += 1
    if settings.is_severpay_enabled():
        count += 1
    if settings.is_paypear_enabled():
        count += 1
    if settings.is_rollypay_enabled():
        count += 1
    if settings.is_overpay_enabled():
        count += 1
    if settings.is_aurapay_enabled():
        count += 1
    if settings.is_lava_enabled():
        count += 1
    if settings.is_etoplatezhi_enabled():
        count += 1
    if settings.is_antilopay_enabled():
        count += 1
    return count
