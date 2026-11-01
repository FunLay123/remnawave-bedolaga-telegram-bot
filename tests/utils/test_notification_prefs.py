"""Порог предупреждения об обычном трафике — глобальный, не пользовательский."""

from types import SimpleNamespace

from app.config import settings
from app.utils.notification_prefs import get_traffic_warning_percent


def test_user_setting_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, 'TRAFFIC_WARNING_PERCENT', 90)
    user = SimpleNamespace(notification_settings={'traffic_warning_percent': 70})

    assert get_traffic_warning_percent(user) == 90


def test_global_percent_is_clamped(monkeypatch):
    user = SimpleNamespace(notification_settings={})

    monkeypatch.setattr(settings, 'TRAFFIC_WARNING_PERCENT', 150)
    assert get_traffic_warning_percent(user) == 99

    monkeypatch.setattr(settings, 'TRAFFIC_WARNING_PERCENT', 10)
    assert get_traffic_warning_percent(user) == 50
