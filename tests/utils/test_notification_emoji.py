"""Единый стиль уведомлений: ⚠️/🚫/❌/⏰ — своя иконка, остальные — мельница."""

from app.utils.notification_emoji import MILL_EMOJI_ID, WARNING_EMOJI_ID, style_notification_text


def test_warning_emoji_uses_warning_icon():
    styled = style_notification_text('⚠️ <b>Предупреждение</b>')

    assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">⚠️</tg-emoji>' in styled


def test_exclamation_uses_warning_icon():
    styled = style_notification_text('❗️ <b>Платёж не прошёл</b>')

    assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">❗️</tg-emoji>' in styled
    assert f'<tg-emoji emoji-id="{MILL_EMOJI_ID}">❗️</tg-emoji>' not in styled


def test_no_entry_emoji_uses_warning_icon():
    styled = style_notification_text('🚫 Доступ закрыт')

    assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">🚫</tg-emoji>' in styled


def test_cross_and_clock_use_warning_icon():
    for glyph in ('❌', '⏰'):
        styled = style_notification_text(f'\n{glyph} Автоплатёж')

        assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">{glyph}</tg-emoji>' in styled
        assert f'<tg-emoji emoji-id="{MILL_EMOJI_ID}">{glyph}</tg-emoji>' not in styled


def test_stop_sign_uses_warning_icon():
    """⛔ — «Подписка закончилась» — должен читаться как предупреждение."""
    styled = style_notification_text('⛔ Подписка закончилась')

    assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">⛔</tg-emoji>' in styled
    assert f'<tg-emoji emoji-id="{MILL_EMOJI_ID}">⛔</tg-emoji>' not in styled


def test_other_leading_emoji_keeps_mill():
    styled = style_notification_text('✅ Готово')

    assert f'<tg-emoji emoji-id="{MILL_EMOJI_ID}">✅</tg-emoji>' in styled


def test_existing_warning_custom_emoji_is_rewritten():
    styled = style_notification_text('<tg-emoji emoji-id="5210854838350403906">⚠️</tg-emoji> Трафик исчерпан')

    assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">⚠️</tg-emoji>' in styled
    assert '5210854838350403906' not in styled


def test_plain_text_is_unchanged():
    assert style_notification_text('Просто текст') == 'Просто текст'


def test_client_screen_warnings_use_custom_emoji():
    """Экраны, которые не проходят через стилизатор, уже содержат нужный ID."""
    from app.localization.texts import get_texts

    texts = get_texts('ru')
    for key in (
        'SAVED_CARDS_LAST_CARD_WARNING',
        'DAILY_SWITCH_WARNING',
        'TARIFF_PURCHASE_MISSING_LINE',
        'TARIFF_SWITCH_FULL_PRICE_NOTE',
        'TARIFF_SWITCH_FULL_PRICE_CHOOSE_PERIOD',
        'TARIFF_SWITCH_DISABLED',
        'REFERRAL_WITHDRAWAL_CONFIRM_WARNING',
        'TRIAL_PAYMENT_INSUFFICIENT_FUNDS',
        'TARIFF_PURCHASE_DAILY_INSUFFICIENT',
        'TARIFF_PURCHASE_STILL_INSUFFICIENT',
        'TARIFF_PURCHASE_STILL_INSUFFICIENT_DAILY',
        'TARIFF_SWITCH_UPGRADE_INSUFFICIENT',
    ):
        assert '5258382581375723416' in texts.t(key, ''), key


WAVE_CARD_TEXT = (
    '<tg-emoji emoji-id="5303040337059543238">⚠️</tg-emoji> '
    '<b>Скидка 20% на продление подписки</b>\n\n'
    'Активируйте и используйте до <b>02.01.2030 03:04</b>\n'
    'Цена со скидкой за 1 месяц: <b>800 ₽</b>\n\n'
    'Заберите скидку и продлите доступ.'
)


def test_both_discount_wave_types_opt_out_of_the_gate():
    """Only the two wave identities keep their own card emoji."""
    from app.utils.notification_emoji import EXCLUDED_NOTIFICATION_TYPES

    for notification_type in ('expired_discount_wave2', 'expired_discount_wave3'):
        assert notification_type in EXCLUDED_NOTIFICATION_TYPES
        assert style_notification_text(WAVE_CARD_TEXT, notification_type=notification_type) == WAVE_CARD_TEXT


def test_wave_gate_opt_out_does_not_change_expired_warning():
    """The initial SUBSCRIPTION_EXPIRED ⛔ keeps the common warning icon."""
    styled = style_notification_text('⛔ Подписка закончилась', notification_type='SUBSCRIPTION_EXPIRED')

    assert f'<tg-emoji emoji-id="{WARNING_EMOJI_ID}">⛔</tg-emoji>' in styled
    assert f'<tg-emoji emoji-id="{MILL_EMOJI_ID}">⛔</tg-emoji>' not in styled
    assert '5303040337059543238' not in styled
