"""Status-aware single-tariff subscription screen.

Approved behaviour:
- one tariff: "Продлить" is the single purchase action; "Купить тариф" is
  hidden completely in single-tariff mode (the multi-tariff branch is kept);
- inactive keyboards don't offer Happ, autopay, revoke or traffic top-up;
  device management stays only on paid subscriptions when the device limit is
  exceeded — trials never get it (the handler rejects every trial).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.keyboards.inline import get_subscription_keyboard


# ── Keyboard matrix ────────────────────────────────────────────────────────


def _subscription(*, status: str = 'active', autopay: bool = False, tariff_id: int = 5):
    return SimpleNamespace(
        id=7,
        status=status,
        actual_status=status,
        is_trial=False,
        is_active=status == 'active',
        end_date=datetime.now(UTC) + timedelta(days=10),
        traffic_limit_gb=12,
        traffic_used_gb=0.0,
        device_limit=1,
        autopay_enabled=autopay,
        tariff_id=tariff_id,
        tariff=SimpleNamespace(
            id=tariff_id,
            name='Тест',
            is_daily=False,
            device_limit=1,
            can_topup_traffic=lambda: False,
        ),
        last_revoke_at=None,
    )


def _callback_data(keyboard) -> set[str]:
    return {button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data}


def _single_tariff_settings(monkeypatch, *, reset_on_payment: bool, happ: bool = False):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_subscription_revoke_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_happ_download_button_enabled', lambda self: happ)
    monkeypatch.setattr(type(settings), 'is_devices_selection_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_traffic_topup_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_traffic_topup_blocked', lambda self: True)


def test_active_keyboard_keeps_current_actions(monkeypatch):
    _single_tariff_settings(monkeypatch, reset_on_payment=True)
    callbacks = _callback_data(get_subscription_keyboard('ru', has_subscription=True, subscription=_subscription()))

    assert 'subscription_extend' in callbacks
    assert 'subscription_manage_devices' in callbacks
    assert 'subscription_autopay' in callbacks
    assert 'subscription_revoke' in callbacks
    assert 'menu_buy' not in callbacks


def test_inactive_keyboard_never_shows_buy_tariff_in_single_tariff(monkeypatch):
    for reset_on_payment in (True, False):
        _single_tariff_settings(monkeypatch, reset_on_payment=reset_on_payment)
        for status in ('expired', 'disabled'):
            callbacks = _callback_data(
                get_subscription_keyboard('ru', has_subscription=True, subscription=_subscription(status=status))
            )
            assert 'menu_buy' not in callbacks, (status, reset_on_payment)


def test_inactive_keyboard_hides_dead_actions(monkeypatch):
    _single_tariff_settings(monkeypatch, reset_on_payment=True, happ=True)
    callbacks = _callback_data(
        get_subscription_keyboard('ru', has_subscription=True, subscription=_subscription(status='expired'))
    )

    assert 'subscription_extend' in callbacks
    assert 'menu_promocode' in callbacks
    assert 'subscription_happ_download' not in callbacks
    assert 'subscription_autopay' not in callbacks
    assert 'subscription_revoke' not in callbacks
    assert 'subscription_manage_devices' not in callbacks
    assert 'buy_traffic' not in callbacks


def test_inactive_keyboard_keeps_devices_only_when_limit_exceeded(monkeypatch):
    _single_tariff_settings(monkeypatch, reset_on_payment=True)

    without_overrun = _callback_data(
        get_subscription_keyboard(
            'ru',
            has_subscription=True,
            subscription=_subscription(status='disabled'),
            devices_over_limit=False,
        )
    )
    with_overrun = _callback_data(
        get_subscription_keyboard(
            'ru',
            has_subscription=True,
            subscription=_subscription(status='disabled'),
            devices_over_limit=True,
        )
    )

    assert 'subscription_manage_devices' not in without_overrun
    assert 'subscription_manage_devices' in with_overrun


def test_inactive_keyboard_keeps_autopay_only_when_bound(monkeypatch):
    """Старую привязку автоплатежа на неактивной подписке нужно уметь выключить."""
    _single_tariff_settings(monkeypatch, reset_on_payment=True)

    bound = _callback_data(
        get_subscription_keyboard(
            'ru', has_subscription=True, subscription=_subscription(status='expired', autopay=True)
        )
    )
    unbound = _callback_data(
        get_subscription_keyboard(
            'ru', has_subscription=True, subscription=_subscription(status='expired', autopay=False)
        )
    )

    assert 'subscription_autopay' in bound
    assert 'subscription_autopay' not in unbound


def _daily_subscription(*, status: str = 'limited', topup: bool = True):
    return SimpleNamespace(
        id=8,
        status=status,
        actual_status=status,
        is_trial=False,
        is_active=status == 'active',
        end_date=datetime.now(UTC) + timedelta(days=1),
        traffic_limit_gb=10,
        traffic_used_gb=10.0,
        device_limit=1,
        autopay_enabled=False,
        is_daily_paused=False,
        tariff_id=6,
        tariff=SimpleNamespace(
            id=6,
            name='Суточный',
            is_daily=True,
            device_limit=1,
            can_topup_traffic=lambda: topup,
        ),
        last_revoke_at=None,
    )


def _trial_subscription(*, status: str = 'expired'):
    return SimpleNamespace(
        id=9,
        status=status,
        actual_status=status,
        is_trial=True,
        is_active=False,
        end_date=datetime.now(UTC) - timedelta(days=1),
        traffic_limit_gb=10,
        traffic_used_gb=0.0,
        device_limit=1,
        autopay_enabled=False,
        tariff_id=5,
        tariff=SimpleNamespace(
            id=5,
            name='Тест',
            is_daily=False,
            device_limit=1,
            can_topup_traffic=lambda: False,
        ),
        last_revoke_at=None,
    )


def test_daily_limited_keeps_devices_topup_and_revoke(monkeypatch):
    """Daily limited — это не expired/disabled: гейты не должны его прятать."""
    _single_tariff_settings(monkeypatch, reset_on_payment=False)
    callbacks = _callback_data(
        get_subscription_keyboard('ru', has_subscription=True, subscription=_daily_subscription(status='limited'))
    )

    assert 'toggle_daily_subscription_pause' in callbacks
    assert 'subscription_manage_devices' in callbacks
    assert 'buy_traffic' in callbacks
    assert 'subscription_revoke' in callbacks


def test_inactive_trial_hides_devices_without_overrun(monkeypatch):
    _single_tariff_settings(monkeypatch, reset_on_payment=False)
    callbacks = _callback_data(
        get_subscription_keyboard('ru', has_subscription=True, is_trial=True, subscription=_trial_subscription())
    )

    assert 'subscription_manage_devices' not in callbacks


def test_inactive_trial_hides_devices_even_when_limit_exceeded(monkeypatch):
    """Триалу управление устройствами недоступно: handler отклоняет любой trial."""
    _single_tariff_settings(monkeypatch, reset_on_payment=False)
    callbacks = _callback_data(
        get_subscription_keyboard(
            'ru',
            has_subscription=True,
            is_trial=True,
            subscription=_trial_subscription(),
            devices_over_limit=True,
        )
    )

    assert 'subscription_manage_devices' not in callbacks
