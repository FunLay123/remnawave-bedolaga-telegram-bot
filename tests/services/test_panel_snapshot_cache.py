"""TTL-кэш снимка панели — минимальная единица оптимизации экрана подписки."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import panel_snapshot_cache
from tests.external.test_remnawave_3_0_0 import _api_with_session, _FakeResponse


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _reset_cache():
    panel_snapshot_cache.invalidate_all()
    yield
    panel_snapshot_cache.invalidate_all()


def _use_clock(monkeypatch, clock: _Clock) -> None:
    monkeypatch.setattr(panel_snapshot_cache, 'time', SimpleNamespace(monotonic=clock.monotonic))


def test_put_then_get_returns_user_and_devices():
    devices = {'total': 2, 'devices': []}
    panel_snapshot_cache.put(42, 'panel-user', devices)

    assert panel_snapshot_cache.get(42) == ('panel-user', devices)


def test_get_unknown_key_returns_none():
    assert panel_snapshot_cache.get(999) is None


def test_entry_is_fresh_within_ttl(monkeypatch):
    clock = _Clock()
    _use_clock(monkeypatch, clock)

    panel_snapshot_cache.put(42, 'panel-user', None)
    clock.now += panel_snapshot_cache.PANEL_SNAPSHOT_TTL_SECONDS - 0.5

    assert panel_snapshot_cache.get(42) == ('panel-user', None)


def test_entry_expires_after_ttl_and_is_removed(monkeypatch):
    clock = _Clock()
    _use_clock(monkeypatch, clock)

    panel_snapshot_cache.put(42, 'panel-user', None)
    clock.now += panel_snapshot_cache.PANEL_SNAPSHOT_TTL_SECONDS + 0.5

    assert panel_snapshot_cache.get(42) is None
    assert 42 not in panel_snapshot_cache._cache


def test_put_refreshes_fetched_at(monkeypatch):
    clock = _Clock()
    _use_clock(monkeypatch, clock)

    panel_snapshot_cache.put(42, 'old', None)
    clock.now += panel_snapshot_cache.PANEL_SNAPSHOT_TTL_SECONDS - 1
    panel_snapshot_cache.put(42, 'new', None)
    clock.now += panel_snapshot_cache.PANEL_SNAPSHOT_TTL_SECONDS - 1

    assert panel_snapshot_cache.get(42) == ('new', None)


def test_invalidate_drops_only_that_user():
    panel_snapshot_cache.put(1, 'one', None)
    panel_snapshot_cache.put(2, 'two', None)

    panel_snapshot_cache.invalidate(1)

    assert panel_snapshot_cache.get(1) is None
    assert panel_snapshot_cache.get(2) == ('two', None)


def test_invalidate_unknown_key_is_noop():
    panel_snapshot_cache.put(1, 'one', None)

    panel_snapshot_cache.invalidate(999)

    assert panel_snapshot_cache.get(1) == ('one', None)


def test_invalidate_all_clears_everything():
    panel_snapshot_cache.put(1, 'one', None)
    panel_snapshot_cache.put(2, 'two', None)

    panel_snapshot_cache.invalidate_all()

    assert panel_snapshot_cache.get(1) is None
    assert panel_snapshot_cache.get(2) is None
    assert not panel_snapshot_cache._cache


def test_put_prunes_expired_entries_first(monkeypatch):
    clock = _Clock()
    _use_clock(monkeypatch, clock)

    panel_snapshot_cache.put(1, 'one', None)
    clock.now += panel_snapshot_cache.PANEL_SNAPSHOT_TTL_SECONDS + 1
    panel_snapshot_cache.put(2, 'two', None)

    assert 1 not in panel_snapshot_cache._cache
    assert panel_snapshot_cache.get(2) == ('two', None)


def test_put_drops_oldest_insertion_when_full(monkeypatch):
    monkeypatch.setattr(panel_snapshot_cache, 'MAX_ENTRIES', 3)

    for panel_user_id in (1, 2, 3):
        panel_snapshot_cache.put(panel_user_id, panel_user_id, None)

    panel_snapshot_cache.put(4, 4, None)

    assert 1 not in panel_snapshot_cache._cache
    assert {2, 3, 4} <= set(panel_snapshot_cache._cache)


def test_repeated_put_moves_entry_out_of_eviction_order(monkeypatch):
    monkeypatch.setattr(panel_snapshot_cache, 'MAX_ENTRIES', 3)

    for panel_user_id in (1, 2, 3):
        panel_snapshot_cache.put(panel_user_id, panel_user_id, None)
    panel_snapshot_cache.put(2, 'two-refreshed', None)
    panel_snapshot_cache.put(4, 4, None)

    assert 1 not in panel_snapshot_cache._cache
    assert panel_snapshot_cache.get(2) == ('two-refreshed', None)
    assert 3 in panel_snapshot_cache._cache


def test_cache_is_bounded_by_max_entries(monkeypatch):
    monkeypatch.setattr(panel_snapshot_cache, 'MAX_ENTRIES', 2)

    panel_snapshot_cache.put(1, 1, None)
    panel_snapshot_cache.put(2, 2, None)
    panel_snapshot_cache.put(3, 3, None)

    assert len(panel_snapshot_cache._cache) == 2


@pytest.mark.asyncio
async def test_non_get_panel_request_invalidates_cache():
    """Любая запись в панель сбрасывает снимок: продление/сброс/удаление."""
    panel_snapshot_cache.put(42, 'panel-user', {'total': 1})
    api, _session = _api_with_session(_FakeResponse(200, '{"response":{}}'))

    await api._make_request('PATCH', '/api/users', {'id': 42})

    assert panel_snapshot_cache.get(42) is None


@pytest.mark.asyncio
async def test_get_panel_request_keeps_cache():
    panel_snapshot_cache.put(42, 'panel-user', {'total': 1})
    api, _session = _api_with_session(_FakeResponse(200, '{"response":{}}'))

    await api._make_request('GET', '/api/users/42')

    assert panel_snapshot_cache.get(42) == ('panel-user', {'total': 1})
