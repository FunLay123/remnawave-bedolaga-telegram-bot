"""Trial connection nudges from the Remnawave webhook must be durable and deduped.

The ``SentNotification`` table stores two markers:
``webhook_first_connected`` (no ``days_before``) and ``webhook_not_connected``
(``days_before`` = normalized ``notConnectedAfterHours``). A failed delivery
must not create a delivered marker, and a connection must suppress later
"not connected" nudges.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.services import remnawave_webhook_service
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


FIRST_CONNECTED = 'webhook_first_connected'
NOT_CONNECTED = 'webhook_not_connected'


def _service(notify_result: bool = True) -> RemnaWaveWebhookService:
    svc = RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock(return_value=notify_result)
    svc._get_connect_keyboard = MagicMock(return_value=None)
    svc._get_subscription_keyboard = MagicMock(return_value=None)
    return svc


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    return user


def _sub() -> MagicMock:
    sub = MagicMock()
    sub.id = 42
    return sub


class _MarkerStore:
    """In-memory stand-in for notification_sent/record_notification."""

    def __init__(self) -> None:
        self.rows: set[tuple[int, int, str, int | None]] = set()

    async def sent(self, db, user_id, subscription_id, notification_type, days_before=None) -> bool:
        return (user_id, subscription_id, notification_type, days_before) in self.rows

    async def record(self, db, user_id, subscription_id, notification_type, days_before=None, *, commit=True) -> None:
        self.rows.add((user_id, subscription_id, notification_type, days_before))


def _install_markers(monkeypatch) -> _MarkerStore:
    store = _MarkerStore()
    monkeypatch.setattr(remnawave_webhook_service, 'notification_sent', store.sent)
    monkeypatch.setattr(remnawave_webhook_service, 'record_notification', store.record)
    return store


def _not_connected_data(hours: int | None) -> dict:
    return {'_meta': {'notConnectedAfterHours': hours}}


async def test_duplicate_not_connected_notifies_once_and_records_hour(monkeypatch):
    store = _install_markers(monkeypatch)
    svc = _service()
    db, user, sub = AsyncMock(), _user(), _sub()

    await svc._handle_user_not_connected(db, user, sub, _not_connected_data(1))
    await svc._handle_user_not_connected(db, user, sub, _not_connected_data(1))

    assert svc._notify_user.await_count == 1
    assert (user.id, sub.id, NOT_CONNECTED, 1) in store.rows


async def test_failed_not_connected_delivery_allows_retry(monkeypatch):
    store = _install_markers(monkeypatch)
    svc = _service(notify_result=False)
    db, user, sub = AsyncMock(), _user(), _sub()

    await svc._handle_user_not_connected(db, user, sub, _not_connected_data(1))

    assert svc._notify_user.await_count == 1
    assert not store.rows  # no false delivered marker

    svc._notify_user.return_value = True
    await svc._handle_user_not_connected(db, user, sub, _not_connected_data(1))

    assert svc._notify_user.await_count == 2
    assert (user.id, sub.id, NOT_CONNECTED, 1) in store.rows


async def test_not_connected_after_first_connected_sends_nothing(monkeypatch):
    store = _install_markers(monkeypatch)
    svc = _service()
    db, user, sub = AsyncMock(), _user(), _sub()

    await svc._handle_first_connected(db, user, sub, {})
    assert (user.id, sub.id, FIRST_CONNECTED, None) in store.rows

    await svc._handle_user_not_connected(db, user, sub, _not_connected_data(1))

    # Only the first-connected notification went out.
    assert svc._notify_user.await_count == 1


async def test_duplicate_first_connected_notifies_once(monkeypatch):
    store = _install_markers(monkeypatch)
    svc = _service()
    db, user, sub = AsyncMock(), _user(), _sub()

    await svc._handle_first_connected(db, user, sub, {})
    await svc._handle_first_connected(db, user, sub, {})

    assert svc._notify_user.await_count == 1
    assert (user.id, sub.id, FIRST_CONNECTED, None) in store.rows


async def test_invalid_not_connected_hours_normalize_to_zero(monkeypatch):
    store = _install_markers(monkeypatch)
    svc = _service()
    db, user, sub = AsyncMock(), _user(), _sub()

    await svc._handle_user_not_connected(db, user, sub, _not_connected_data('oops'))

    assert (user.id, sub.id, NOT_CONNECTED, 0) in store.rows


def test_main_starts_web_app_for_remnawave_webhook_only():
    main_source = (Path(__file__).resolve().parents[2] / 'main.py').read_text(encoding='utf-8')
    match = re.search(r'should_start_web_app\s*=\s*\((.*?)\n\s*\)', main_source, re.DOTALL)
    assert match is not None, 'should_start_web_app predicate not found in main.py'
    expression = match.group(1)
    assert re.search(r'\bor\s+settings\.is_remnawave_webhook_enabled\(\)', expression), (
        'Remnawave webhook must independently satisfy the unified-server startup predicate'
    )
