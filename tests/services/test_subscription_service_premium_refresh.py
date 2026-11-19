"""Пробуждение воркера премиум-трафика после покупки."""

from types import SimpleNamespace

from app.services import subscription_service


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'


def _subscription(limits):
    return SimpleNamespace(tariff=SimpleNamespace(server_traffic_limits=limits))


def _fake_service(monkeypatch, *, enabled=True, calls=None):
    calls = [] if calls is None else calls
    fake = SimpleNamespace(
        is_enabled=lambda: enabled,
        request_pass=lambda *, panel_user_id: calls.append(panel_user_id),
    )
    monkeypatch.setattr('app.services.premium_traffic_service.premium_traffic_service', fake)
    return calls


def test_premium_tariff_wakes_the_worker_with_the_panel_id(monkeypatch):
    calls = _fake_service(monkeypatch)

    subscription_service._request_premium_traffic_refresh(
        _subscription({SQUAD: {'traffic_limit_gb': 20}}),
        42,
        reset_traffic=True,
    )

    assert calls == [42]


def test_public_refresh_wakes_without_a_reset_flag(monkeypatch):
    """Delivered purchase may not reset panel traffic — the wake is still needed."""
    calls = _fake_service(monkeypatch)

    subscription_service.request_premium_traffic_refresh(
        _subscription({SQUAD: {'traffic_limit_gb': 20}}),
        42,
    )

    assert calls == [42]


def test_sync_without_traffic_reset_does_not_wake_the_worker(monkeypatch):
    calls = _fake_service(monkeypatch)

    subscription_service._request_premium_traffic_refresh(
        _subscription({SQUAD: {'traffic_limit_gb': 20}}),
        42,
        reset_traffic=False,
    )

    assert calls == []


def test_tariff_without_premium_squads_does_not_wake_the_worker(monkeypatch):
    calls = _fake_service(monkeypatch)

    subscription_service._request_premium_traffic_refresh(_subscription({}), 42, reset_traffic=True)

    assert calls == []


def test_disabled_feature_does_not_wake_the_worker(monkeypatch):
    calls = _fake_service(monkeypatch, enabled=False)

    subscription_service._request_premium_traffic_refresh(
        _subscription({SQUAD: {'traffic_limit_gb': 20}}),
        42,
        reset_traffic=True,
    )

    assert calls == []
