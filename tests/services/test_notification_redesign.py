"""Approved notification redesign: pure-text bodies, state-aware templates, merge policy.

The spec lives in `notification-design-brief-2026-11-06.md`:
- no emoji/custom-emoji inside the message body (only the leading header emoji);
- no blockquote/table, so rich messages keep <h4> + <hr/>;
- autopay failure + planned expiry warning are merged (B2/I3) with a ~3h dedupe;
- primary button depends on balance/required/autopay state.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.localization.texts import get_texts
from app.services.monitoring_service import (
    AutopayFailState,
    MonitoringService,
    decide_expiring_notification_state,
    expiring_template_key,
    should_merge_autopay_failure_with_expiring,
    should_suppress_expiring_notification,
)


def test_autopay_off_state():
    assert (
        decide_expiring_notification_state(
            autopay_enabled=False,
            balance_kopeks=0,
            required_kopeks=15000,
        )
        == 'autopay_off'
    )


def test_autopay_on_enough_money_state():
    assert (
        decide_expiring_notification_state(
            autopay_enabled=True,
            balance_kopeks=15000,
            required_kopeks=15000,
        )
        == 'autopay_ok'
    )


def test_autopay_on_not_enough_money_state():
    assert (
        decide_expiring_notification_state(
            autopay_enabled=True,
            balance_kopeks=0,
            required_kopeks=15000,
        )
        == 'autopay_short'
    )


def test_template_keys_match_states():
    assert expiring_template_key('autopay_off') == 'SUBSCRIPTION_EXPIRING_AUTOPAY_OFF'
    assert expiring_template_key('autopay_ok') == 'SUBSCRIPTION_EXPIRING_AUTOPAY_OK'
    assert expiring_template_key('autopay_short') == 'SUBSCRIPTION_EXPIRING_AUTOPAY_SHORT'


def test_suppress_within_three_hours():
    now = 1_000_000.0
    assert should_suppress_expiring_notification(now - 60, now) is True
    assert should_suppress_expiring_notification(now - 3600, now) is True
    assert should_suppress_expiring_notification(now - 3599, now) is True
    assert should_suppress_expiring_notification(now - 10799, now) is True


def test_do_not_suppress_after_window_or_without_marker():
    now = 1_000_000.0
    assert should_suppress_expiring_notification(now - 10800, now) is False
    assert should_suppress_expiring_notification(0.0, now) is False


def test_merge_only_for_non_final_with_enabled_expiry_and_due_warning():
    assert (
        should_merge_autopay_failure_with_expiring(
            is_final=False,
            expiry_notifications_enabled=True,
            days_left=3,
            warning_days=[3, 1],
        )
        is True
    )
    assert (
        should_merge_autopay_failure_with_expiring(
            is_final=True,
            expiry_notifications_enabled=True,
            days_left=3,
            warning_days=[3, 1],
        )
        is False
    )
    assert (
        should_merge_autopay_failure_with_expiring(
            is_final=False,
            expiry_notifications_enabled=False,
            days_left=3,
            warning_days=[3, 1],
        )
        is False
    )
    assert (
        should_merge_autopay_failure_with_expiring(
            is_final=False,
            expiry_notifications_enabled=True,
            days_left=10,
            warning_days=[3, 1],
        )
        is False
    )


def test_autopay_fail_state_roundtrip_keeps_merged_marker():
    state = AutopayFailState(count=1, last_sent_ts=123.5, final_sent=False, merged_expiring_ts=456.0)
    assert AutopayFailState.from_dict(state.to_dict()) == state


# ── Expiring sender: template by state + primary button ─────────────────────


class _RecordingTexts:
    """Records requested keys and returns marker templates for the new keys."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.templates = {
            'SUBSCRIPTION_EXPIRING_AUTOPAY_OFF': 'OFF {days} {days_text} {end_date} {balance}',
            'SUBSCRIPTION_EXPIRING_AUTOPAY_OK': 'OK {days} {days_text} {end_date} {balance}',
            'SUBSCRIPTION_EXPIRING_AUTOPAY_SHORT': 'SHORT {days} {days_text} {end_date} {balance} {required}',
        }

    def t(self, key: str, default: str | None = None) -> str:
        self.keys.append(key)
        if key in self.templates:
            return self.templates[key]
        return default if default is not None else key


def _expiring_user(balance_kopeks: int) -> SimpleNamespace:
    return SimpleNamespace(telegram_id=12345, language='ru', balance_kopeks=balance_kopeks)


def _expiring_subscription(*, autopay_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        autopay_enabled=autopay_enabled,
        end_date=datetime.now(UTC) + timedelta(days=3),
        tariff=None,
    )


async def _send_expiring(monkeypatch, *, user, subscription, texts_stub):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', False)
    monkeypatch.setattr(settings, 'PRICE_30_DAYS', 15000)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr('app.services.monitoring_service.get_texts', lambda _lang: texts_stub)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    service = MonitoringService(bot=bot)
    result = await service._send_subscription_expiring_notification(user, subscription, days=3)
    return result, bot


def _buttons(bot) -> dict[str, object]:
    keyboard = bot.send_message.await_args.kwargs['reply_markup']
    return {button.callback_data: button for row in keyboard.inline_keyboard for button in row}


async def test_expiring_autopay_off_uses_off_template_and_renew_primary(monkeypatch):
    texts = _RecordingTexts()
    result, bot = await _send_expiring(
        monkeypatch,
        user=_expiring_user(0),
        subscription=_expiring_subscription(autopay_enabled=False),
        texts_stub=texts,
    )

    assert result is True
    assert 'SUBSCRIPTION_EXPIRING_AUTOPAY_OFF' in texts.keys
    buttons = _buttons(bot)
    assert buttons['subscription_extend'].style == 'primary'
    assert buttons['balance_topup'].style != 'primary'
    assert 'menu_subscription' in buttons


async def test_expiring_autopay_ok_uses_ok_template_and_hides_topup(monkeypatch):
    texts = _RecordingTexts()
    result, bot = await _send_expiring(
        monkeypatch,
        user=_expiring_user(15000),
        subscription=_expiring_subscription(autopay_enabled=True),
        texts_stub=texts,
    )

    assert result is True
    assert 'SUBSCRIPTION_EXPIRING_AUTOPAY_OK' in texts.keys
    buttons = _buttons(bot)
    assert buttons['subscription_extend'].style == 'primary'
    assert 'balance_topup' not in buttons
    assert 'menu_subscription' in buttons


async def test_expiring_autopay_short_uses_short_template_and_topup_primary(monkeypatch):
    texts = _RecordingTexts()
    result, bot = await _send_expiring(
        monkeypatch,
        user=_expiring_user(0),
        subscription=_expiring_subscription(autopay_enabled=True),
        texts_stub=texts,
    )

    assert result is True
    assert 'SUBSCRIPTION_EXPIRING_AUTOPAY_SHORT' in texts.keys
    buttons = _buttons(bot)
    assert buttons['balance_topup'].style == 'primary'
    assert buttons['subscription_extend'].style != 'primary'
    assert 'menu_subscription' in buttons


_EMOJI_IN_BODY_RE = re.compile(r'<tg-emoji|[📅💳💰⚠️⏰✅⛔📊📱]')


async def test_expiring_real_locale_body_has_no_emoji_or_block_markup(monkeypatch):
    # Real ru texts: the only custom emoji may be the leading header icon.
    result, bot = await _send_expiring(
        monkeypatch,
        user=_expiring_user(0),
        subscription=_expiring_subscription(autopay_enabled=True),
        texts_stub=get_texts('ru'),
    )

    assert result is True
    sent_text = bot.send_message.await_args.kwargs['text']
    lines = sent_text.splitlines()
    header_index = next(i for i, line in enumerate(lines) if line.strip())
    body = '\n'.join(lines[header_index + 1 :])
    assert _EMOJI_IN_BODY_RE.search(body) is None
    assert '<blockquote' not in sent_text
    assert '<table' not in sent_text


# ── Autopay failure: merged card, plain card, final reminder ────────────────


def _autopay_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, telegram_id=12345, language='ru', balance_kopeks=0)


def _autopay_subscription(hours: int = 72) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        end_date=datetime.now(UTC) + timedelta(hours=hours),
        tariff=None,
    )


async def _send_autopay_failed(
    monkeypatch,
    *,
    merge: bool,
    is_final: bool,
    subscription=None,
    texts_stub=None,
):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    if texts_stub is not None:
        monkeypatch.setattr('app.services.monitoring_service.get_texts', lambda _lang: texts_stub)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    service = MonitoringService(bot=bot)
    await service._send_autopay_failed_notification(
        _autopay_user(),
        0,
        15000,
        subscription=subscription,
        is_final=is_final,
        merge_with_expiry=merge,
    )
    return bot


async def test_autopay_failure_merged_card_has_date_and_renew_button(monkeypatch):
    bot = await _send_autopay_failed(
        monkeypatch,
        merge=True,
        is_final=False,
        subscription=_autopay_subscription(),
    )

    text = bot.send_message.await_args.kwargs['text']
    assert 'истекает через' in text
    assert 'автоплатёж не прошёл' in text.lower()

    buttons = _buttons(bot)
    assert buttons['balance_topup'].style == 'primary'
    assert buttons['subscription_extend'].style != 'primary'
    assert 'menu_subscription' in buttons


async def test_autopay_failure_plain_card_has_no_renew_button(monkeypatch):
    bot = await _send_autopay_failed(monkeypatch, merge=False, is_final=False)

    text = bot.send_message.await_args.kwargs['text']
    assert 'Автоплатёж не прошёл' in text
    buttons = _buttons(bot)
    assert buttons['balance_topup'].style == 'primary'
    assert 'subscription_extend' not in buttons
    assert 'menu_subscription' in buttons


async def test_autopay_failure_final_card_has_no_renew_button(monkeypatch):
    bot = await _send_autopay_failed(monkeypatch, merge=False, is_final=True)

    text = bot.send_message.await_args.kwargs['text']
    assert 'Подписка скоро отключится' in text
    buttons = _buttons(bot)
    assert buttons['balance_topup'].style == 'primary'
    assert 'subscription_extend' not in buttons
    assert 'menu_subscription' in buttons


async def test_autopay_failure_real_locale_body_has_no_emoji_or_block_markup(monkeypatch):
    bot = await _send_autopay_failed(
        monkeypatch,
        merge=True,
        is_final=False,
        subscription=_autopay_subscription(),
        texts_stub=get_texts('ru'),
    )

    sent_text = bot.send_message.await_args.kwargs['text']
    lines = sent_text.splitlines()
    header_index = next(i for i, line in enumerate(lines) if line.strip())
    body = '\n'.join(lines[header_index + 1 :])
    assert _EMOJI_IN_BODY_RE.search(body) is None
    assert '<blockquote' not in sent_text
    assert '<table' not in sent_text


def _install_cycle_markers(monkeypatch) -> set[tuple]:
    from app.services import monitoring_service as ms

    markers: set[tuple] = set()

    async def fake_sent(db, user_id, subscription_id, notification_type, days_before=None):
        return (user_id, subscription_id, notification_type, days_before) in markers

    async def fake_record(db, user_id, subscription_id, notification_type, days_before=None, *, commit=True):
        markers.add((user_id, subscription_id, notification_type, days_before))

    monkeypatch.setattr(settings, 'AUTOPAY_WARNING_DAYS', '3,1')
    monkeypatch.setattr(
        ms.NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(ms, 'notification_sent', fake_sent)
    monkeypatch.setattr(ms, 'record_notification', fake_record)
    return markers


async def test_maybe_notify_autopay_failure_records_confirmed_attention(monkeypatch):
    from app.services import monitoring_service as ms

    markers = _install_cycle_markers(monkeypatch)
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.SENT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda _user: True)

    now = datetime.now(UTC)
    user = SimpleNamespace(id=1, telegram_id=555, balance_kopeks=0, notification_settings={})
    sub = SimpleNamespace(id=7, end_date=now + timedelta(hours=72), autopay_enabled=True)

    await service._maybe_notify_autopay_failure(user, 15000, sub, now, db=object())

    send.assert_awaited_once()
    assert send.await_args.kwargs['merge_with_expiry'] is True
    assert markers == {(1, 7, 'autopay_attention', 3), (1, 7, 'expiring', 3)}


async def test_maybe_notify_inside_final_window_sends_merged_not_final(monkeypatch):
    from app.services import monitoring_service as ms

    markers = _install_cycle_markers(monkeypatch)
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.SENT)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda _user: True)

    now = datetime.now(UTC)
    user = SimpleNamespace(id=1, telegram_id=555, balance_kopeks=0, notification_settings={})
    sub = SimpleNamespace(id=7, end_date=now + timedelta(hours=2), autopay_enabled=True)

    await service._maybe_notify_autopay_failure(user, 15000, sub, now, db=object())

    send.assert_awaited_once()
    assert send.await_args.kwargs.get('is_final', False) is False
    assert markers == {(1, 7, 'autopay_attention', 1), (1, 7, 'expiring', 1)}


# ── Planned warning: suppression after merge + required price passed through ─


def _planned_warning_fixtures():
    end_date = datetime.now(UTC) + timedelta(hours=20)
    subscription = SimpleNamespace(
        id=7,
        user_id=42,
        autopay_enabled=True,
        end_date=end_date,
        status='active',
        tariff=None,
    )
    user = SimpleNamespace(
        id=42,
        telegram_id=555,
        language='ru',
        balance_kopeks=0,
        notification_settings={},
    )
    return subscription, user


async def _run_expiring_check(monkeypatch, service, subscription, user):
    from app.services import monitoring_service as ms
    from app.services.notification_settings_service import NotificationSettingsService

    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())
    service._log_monitoring_event = AsyncMock()
    db = SimpleNamespace(execute=AsyncMock())
    await service._check_expiring_subscriptions(db)


async def test_expiring_warning_is_skipped_for_autopay_enabled(monkeypatch):
    service = MonitoringService(bot=object())
    subscription, user = _planned_warning_fixtures()
    service._send_subscription_expiring_notification = AsyncMock()

    await _run_expiring_check(monkeypatch, service, subscription, user)

    service._send_subscription_expiring_notification.assert_not_awaited()


async def test_expiring_warning_passes_required_price(monkeypatch):
    service = MonitoringService(bot=object())
    subscription, user = _planned_warning_fixtures()
    subscription.autopay_enabled = False
    monkeypatch.setattr(service, '_resolve_renewal_cost_kopeks', AsyncMock(return_value=15000))
    service._send_subscription_expiring_notification = AsyncMock(return_value=True)

    await _run_expiring_check(monkeypatch, service, subscription, user)

    service._send_subscription_expiring_notification.assert_awaited_once()
    assert service._send_subscription_expiring_notification.await_args.kwargs['required_kopeks'] == 15000


# ── Remaining notification set: same pure-text grammar ──────────────────────


def _sender_user(balance: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        telegram_id=12345,
        language='ru',
        balance_kopeks=balance,
        status='active',
    )


def _sender_subscription(hours: int = 24, tariff=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        end_date=datetime.now(UTC) + timedelta(hours=hours),
        tariff=tariff,
    )


def _sender_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _set_sender_settings(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)


async def test_expired_notification_uses_new_text_and_price(monkeypatch):
    _set_sender_settings(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    result = await service._send_subscription_expired_notification(
        _sender_user(),
        _sender_subscription(),
        price_kopeks=15000,
    )

    assert result is True
    text = bot.send_message.await_args.kwargs['text']
    assert 'Подписка закончилась' in text
    assert 'продление' in text
    assert '150 ₽' in text
    assert _buttons(bot)['subscription_extend'].style == 'primary'


async def test_expired_day1_has_balance_and_button_icons(monkeypatch):
    _set_sender_settings(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)
    monkeypatch.setattr(service, '_resolve_renewal_cost_kopeks', AsyncMock(return_value=15000))

    result = await service._send_expired_day1_notification(
        db=SimpleNamespace(),
        user=_sender_user(),
        subscription=_sender_subscription(hours=-24),
    )

    assert result is True
    text = bot.send_message.await_args.kwargs['text']
    assert 'Отключена' in text
    assert 'Баланс' in text
    buttons = _buttons(bot)
    assert buttons['subscription_extend'].icon_custom_emoji_id == '5334552404736104314'
    assert buttons['balance_topup'].icon_custom_emoji_id == '5258500422393415126'
    assert buttons['menu_support'].icon_custom_emoji_id == '5988023995125993550'


async def test_discount_wave_has_discounted_price_and_two_buttons(monkeypatch):
    _set_sender_settings(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)
    from app.services import pricing_engine as pricing_engine_module

    calculate = AsyncMock(return_value=SimpleNamespace(final_total=13500))
    monkeypatch.setattr(pricing_engine_module.pricing_engine, 'calculate_renewal_price', calculate)

    result = await service._send_expired_discount_notification(
        db=SimpleNamespace(),
        user=_sender_user(),
        subscription=_sender_subscription(hours=-48),
        percent=10,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        offer_id=1,
        wave='second',
    )

    assert result is True
    text = bot.send_message.await_args.kwargs['text']
    assert 'Скидка 10%' in text
    assert 'Цена со скидкой за 1 месяц' in text
    assert '135 ₽' in text
    buttons = _buttons(bot)
    assert buttons['claim_discount_1'].style == 'primary'
    assert buttons['claim_discount_1'].icon_custom_emoji_id == '5235695112419303615'
    assert buttons['balance_topup'].icon_custom_emoji_id == '5258500422393415126'
    assert 'subscription_extend' not in buttons


async def test_autopay_success_uses_new_text(monkeypatch):
    _set_sender_settings(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)
    subscription = _sender_subscription()

    await service._send_autopay_success_notification(
        _sender_user(balance=50000),
        15000,
        30,
        subscription=subscription,
    )

    text = bot.send_message.await_args.kwargs['text']
    assert 'Подписка продлена' in text
    assert 'Списано' in text
    assert 'баланс' in text
    assert '150 ₽' in text


async def test_autopay_success_has_main_menu_keyboard(monkeypatch):
    _set_sender_settings(monkeypatch)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    await service._send_autopay_success_notification(
        _sender_user(balance=50000),
        15000,
        30,
        subscription=_sender_subscription(),
    )

    buttons = _buttons(bot)
    assert set(buttons) == {'back_to_menu'}
    button = buttons['back_to_menu']
    assert button.text == 'Главное меню'
    assert button.style == 'primary'
    assert button.icon_custom_emoji_id == '5314310000531766389'


async def test_trial_ending_uses_new_text_and_button_icons(monkeypatch):
    _set_sender_settings(monkeypatch)
    monkeypatch.setattr(settings, 'PRICE_30_DAYS', 15000)
    from app.services import pricing_engine as pricing_engine_module

    calculate_renewal_price = AsyncMock(return_value=SimpleNamespace(final_total=20000))
    monkeypatch.setattr(pricing_engine_module.pricing_engine, 'calculate_renewal_price', calculate_renewal_price)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    result = await service._send_trial_ending_notification(
        SimpleNamespace(),
        _sender_user(),
        _sender_subscription(hours=2),
    )

    assert result is True
    calculate_renewal_price.assert_awaited_once()
    assert calculate_renewal_price.await_args.args[2] == 30
    text = bot.send_message.await_args.kwargs['text']
    assert 'Тестовый период закончится через 2 часа' in text
    assert '200 ₽ за 30 дней' in text, 'цена — из pricing engine, а не константа PRICE_30_DAYS'
    buttons = _buttons(bot)
    assert buttons['menu_buy'].style == 'primary'
    assert buttons['menu_buy'].icon_custom_emoji_id == '5334552404736104314'
    assert buttons['balance_topup'].icon_custom_emoji_id == '5258500422393415126'


async def test_trial_ending_falls_back_to_price_30_days_when_pricing_fails(monkeypatch):
    _set_sender_settings(monkeypatch)
    monkeypatch.setattr(settings, 'PRICE_30_DAYS', 15000)
    from app.services import pricing_engine as pricing_engine_module

    monkeypatch.setattr(
        pricing_engine_module.pricing_engine,
        'calculate_renewal_price',
        AsyncMock(side_effect=RuntimeError('pricing down')),
    )
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    result = await service._send_trial_ending_notification(
        SimpleNamespace(),
        _sender_user(),
        _sender_subscription(hours=2),
    )

    assert result is True
    text = bot.send_message.await_args.kwargs['text']
    assert '150 ₽ за 30 дней' in text


def test_traffic_warning_keyboard_has_icons():
    from app.services.monitoring_service import build_traffic_warning_keyboard

    keyboard = build_traffic_warning_keyboard(get_texts('ru'))
    buttons = {button.callback_data: button for row in keyboard.inline_keyboard for button in row}
    assert buttons['buy_traffic'].style == 'primary'
    assert buttons['buy_traffic'].icon_custom_emoji_id == '5303396278179210513'
    assert buttons['menu_subscription'].icon_custom_emoji_id == '5431374840332302296'


# ── Screens from audit item 5: trial + ordinary traffic top-up ─────────────


def test_trial_keyboard_activate_has_custom_icon():
    from app.keyboards.inline import get_trial_keyboard

    keyboard = get_trial_keyboard('ru')
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == 'trial_activate'
    assert button.icon_custom_emoji_id == '5334552404736104314'
    assert button.style == 'primary'
    assert '🎁' not in button.text


def test_tariff_traffic_keyboard_premium_button_has_icon():
    from app.keyboards.inline import get_add_traffic_keyboard_from_tariff

    keyboard = get_add_traffic_keyboard_from_tariff(
        'ru',
        {10: 10000, 50: 40000},
        subscription_end_date=datetime.now(UTC) + timedelta(days=30),
        has_premium_topup=True,
    )
    buttons = {
        button.callback_data: button for row in keyboard.inline_keyboard for button in row if button.callback_data
    }
    assert buttons['premium_traffic_topup'].icon_custom_emoji_id == '5335020345717973061'
    assert '💠' not in buttons['premium_traffic_topup'].text


async def test_maybe_notify_does_not_send_outside_user_window(monkeypatch):
    """Пользователь сузил окно до 1 дня — карточка «через 3 дня» не должна уходить."""
    from app.services import monitoring_service as ms

    _install_cycle_markers(monkeypatch)
    service = MonitoringService(bot=object())
    sent: list[bool] = []

    async def fake_send(user, balance, required, *, subscription=None, is_final=False, merge_with_expiry=False):
        sent.append(merge_with_expiry)
        return ms.NotificationDeliveryStatus.SENT

    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', fake_send)
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda _user: True)

    now = datetime.now(UTC)
    user = SimpleNamespace(
        id=1,
        telegram_id=555,
        balance_kopeks=0,
        notification_settings={'subscription_expiry_days': 1},
    )
    sub = SimpleNamespace(id=7, end_date=now + timedelta(hours=72), autopay_enabled=True)

    await service._maybe_notify_autopay_failure(user, 15000, sub, now, db=object())

    assert sent == []


async def test_maybe_notify_failed_send_records_no_attention(monkeypatch):
    """Явная ошибка отправки не покрывает день — более поздняя попытка может повторить."""
    from app.services import monitoring_service as ms

    markers = _install_cycle_markers(monkeypatch)
    service = MonitoringService(bot=object())
    send = AsyncMock(return_value=ms.NotificationDeliveryStatus.FAILED)
    monkeypatch.setattr(service, '_send_autopay_failed_notification_result', send)
    monkeypatch.setattr(ms, 'is_subscription_expiry_enabled', lambda _user: True)

    now = datetime.now(UTC)
    user = SimpleNamespace(id=1, telegram_id=555, balance_kopeks=0, notification_settings={})
    sub = SimpleNamespace(id=7, end_date=now + timedelta(hours=72), autopay_enabled=True)

    await service._maybe_notify_autopay_failure(user, 15000, sub, now, db=object())

    send.assert_awaited_once()
    assert markers == set()


# ── Multi-tariff labels: one subscription must be identifiable ──────────────


async def test_autopay_success_keeps_tariff_label_in_multi_tariff(monkeypatch):
    _set_sender_settings(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    await service._send_autopay_success_notification(
        _sender_user(balance=50000),
        15000,
        30,
        subscription=_sender_subscription(tariff=SimpleNamespace(name='Тест')),
    )

    text = bot.send_message.await_args.kwargs['text']
    assert 'Тариф: «Тест»' in text


async def test_expired_notification_keeps_tariff_label_in_multi_tariff(monkeypatch):
    _set_sender_settings(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    result = await service._send_subscription_expired_notification(
        _sender_user(),
        _sender_subscription(),
        tariff_name='Тест',
        price_kopeks=15000,
    )

    assert result is True
    text = bot.send_message.await_args.kwargs['text']
    assert 'Подписка «Тест» закончилась' in text


async def test_discount_wave_keeps_tariff_label_in_multi_tariff(monkeypatch):
    _set_sender_settings(monkeypatch)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    bot = _sender_bot()
    service = MonitoringService(bot=bot)

    result = await service._send_expired_discount_notification(
        db=None,
        user=_sender_user(),
        subscription=_sender_subscription(hours=-48, tariff=SimpleNamespace(name='Тест')),
        percent=10,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        offer_id=1,
        wave='second',
    )

    assert result is True
    text = bot.send_message.await_args.kwargs['text']
    assert '«Тест»' in text
