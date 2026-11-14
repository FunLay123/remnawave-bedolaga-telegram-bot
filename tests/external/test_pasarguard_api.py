"""Тонкий клиент PasarGuard API на записанных ответах: точные маршруты, заголовок
X-Api-Key, тело создания, разбор ``lifetime_used_traffic``, сырые байты конфига и
разделение ошибок (404 / 5xx+таймаут / прочие 4xx).

Клиент — раб панели: только он ходит в PasarGuard. Повторяются лишь идемпотентные
GET; запись повторять нельзя — панель не знает Idempotency-Key, и второй POST
создал бы второго пользователя.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.external.pasarguard_api import (
    PasarguardAPI,
    PasarguardAPIError,
    PasarguardNotFoundError,
    PasarguardTransientError,
)


PANEL_URL = 'https://panel.example'
API_KEY = 'secret'


def _user_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'id': 42,
        'username': 'bx7',
        'status': 'active',
        'expire': '2030-01-01T00:00:00Z',
        'data_limit': 1073741824,
        'used_traffic': 5,
        'lifetime_used_traffic': 10,
        'group_ids': [3],
        'note': 'xdrive-bot:test:sub:7',
    }
    payload.update(overrides)
    return payload


def _api(handler: Callable[[httpx.Request], httpx.Response]) -> PasarguardAPI:
    return PasarguardAPI(PANEL_URL, API_KEY, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_create_user_posts_to_api_user():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_user_payload())

    api = _api(handler)
    try:
        user = await api.create_user(
            'bx7',
            [3],
            datetime(2030, 1, 1, tzinfo=UTC),
            1073741824,
            note='xdrive-bot:test:sub:7',
        )
    finally:
        await api.close()

    request = seen[0]
    body = json.loads(request.content)
    assert request.method == 'POST'
    assert request.url.path == '/api/user'
    assert request.headers['x-api-key'] == API_KEY
    assert body['username'] == 'bx7'
    assert body['group_ids'] == [3]
    assert body['expire'] == '2030-01-01T00:00:00+00:00'
    assert body['data_limit'] == 1073741824
    assert body['data_limit_reset_strategy'] == 'no_reset'
    assert body['note'] == 'xdrive-bot:test:sub:7'
    assert user.id == 42


@pytest.mark.asyncio
async def test_get_user_by_id_uses_by_id_path():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_user_payload())

    api = _api(handler)
    try:
        user = await api.get_user_by_id(42)
    finally:
        await api.close()

    assert seen[0].method == 'GET'
    assert seen[0].url.path == '/api/user/by-id/42'
    assert user.username == 'bx7'
    assert user.expire == datetime(2030, 1, 1, tzinfo=UTC)
    assert user.group_ids == [3]


@pytest.mark.asyncio
async def test_get_user_by_username_uses_by_username_path():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_user_payload())

    api = _api(handler)
    try:
        await api.get_user_by_username('bx7')
    finally:
        await api.close()

    assert seen[0].method == 'GET'
    assert seen[0].url.path == '/api/user/by-username/bx7'


@pytest.mark.asyncio
async def test_disable_uses_by_id_disabled_and_toggle_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_user_payload(status='disabled'))

    api = _api(handler)
    try:
        await api.set_disabled(42, True)
    finally:
        await api.close()

    request = seen[0]
    assert request.method == 'PUT'
    assert request.url.path == '/api/user/by-id/42/disabled'
    assert json.loads(request.content) == {'disabled': True}


@pytest.mark.asyncio
async def test_revoke_uses_by_id_revoke_sub():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_user_payload())

    api = _api(handler)
    try:
        await api.revoke_sub(42)
    finally:
        await api.close()

    assert seen[0].method == 'POST'
    assert seen[0].url.path == '/api/user/by-id/42/revoke_sub'


@pytest.mark.asyncio
async def test_delete_uses_delete_by_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    api = _api(handler)
    try:
        await api.delete_user(42)
    finally:
        await api.close()

    assert seen[0].method == 'DELETE'
    assert seen[0].url.path == '/api/user/by-id/42'


@pytest.mark.asyncio
async def test_list_users_paginates_and_parses_lifetime():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                'users': [
                    _user_payload(
                        id=1,
                        username='bx1',
                        used_traffic=4,
                        lifetime_used_traffic=10,
                        group_ids=[],
                        note=None,
                    )
                ],
                'total': 1,
            },
        )

    api = _api(handler)
    try:
        users = await api.list_users()
    finally:
        await api.close()

    request = seen[0]
    assert request.method == 'GET'
    assert request.url.path == '/api/users'
    assert request.url.params['offset'] == '0'
    assert request.url.params['limit'] == '200'
    assert len(users) == 1
    assert users[0].lifetime_used_traffic == 10
    assert users[0].used_traffic == 4


@pytest.mark.asyncio
async def test_list_users_forwards_pagination():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={'users': [], 'total': 0})

    api = _api(handler)
    try:
        await api.list_users(offset=400, limit=50)
    finally:
        await api.close()

    assert seen[0].url.params['offset'] == '400'
    assert seen[0].url.params['limit'] == '50'


@pytest.mark.asyncio
async def test_subscription_config_returns_raw_bytes():
    body = b'[{"outbounds":[{"protocol":"vless"}],"xdriveSettings":{}}]'
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body, headers={'content-type': 'application/json'})

    api = _api(handler)
    try:
        config = await api.get_subscription_config(42)
    finally:
        await api.close()

    assert seen[0].method == 'GET'
    assert seen[0].url.path == '/api/user/42/subscription/xray'
    assert config == body


@pytest.mark.asyncio
async def test_404_raises_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={'detail': 'User not found'})

    api = _api(handler)
    try:
        with pytest.raises(PasarguardNotFoundError) as info:
            await api.get_user_by_id(42)
    finally:
        await api.close()

    assert info.value.status_code == 404
    assert not isinstance(info.value, PasarguardTransientError)


@pytest.mark.asyncio
async def test_other_4xx_raises_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={'detail': 'Forbidden'})

    api = _api(handler)
    try:
        with pytest.raises(PasarguardAPIError) as info:
            await api.create_user('bx7', [3], datetime(2030, 1, 1, tzinfo=UTC), 1024)
    finally:
        await api.close()

    assert info.value.status_code == 403
    assert not isinstance(info.value, (PasarguardNotFoundError, PasarguardTransientError))


@pytest.mark.asyncio
async def test_5xx_and_timeout_raise_transient():
    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text='upstream unavailable')

    api = _api(server_error)
    try:
        with pytest.raises(PasarguardTransientError) as info:
            await api.get_user_by_id(42)
    finally:
        await api.close()
    assert info.value.status_code == 503

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException('slow panel', request=request)

    timed_api = _api(timeout)
    try:
        with pytest.raises(PasarguardTransientError):
            await timed_api.get_user_by_id(42)
    finally:
        await timed_api.close()


@pytest.mark.asyncio
async def test_get_retries_are_capped_by_get_attempts():
    """Прокси подписки задаёт одну попытку: зависшая панель не должна
    задерживать выдачу на три таймаута с backoff."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    api = PasarguardAPI(PANEL_URL, API_KEY, transport=httpx.MockTransport(handler), get_attempts=1)
    try:
        with pytest.raises(PasarguardTransientError):
            await api.get_user_by_id(42)
    finally:
        await api.close()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_get_retries_three_times_by_default():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    api = _api(handler)
    try:
        with pytest.raises(PasarguardTransientError):
            await api.get_user_by_id(42)
    finally:
        await api.close()

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_writes_are_not_retried():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    api = _api(handler)
    try:
        with pytest.raises(PasarguardTransientError):
            await api.create_user('bx7', [3], datetime(2030, 1, 1, tzinfo=UTC), 1024)
    finally:
        await api.close()

    assert len(calls) == 1
