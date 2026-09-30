"""Прокси страницы подписок: подмена subscription-userinfo суммой премиум-сквадов,
нетронутый ответ, когда подменять нечего или расчёт упал, проброс запроса как есть,
502 при недоступной странице подписок и чужие домены — мимо прокси."""

from __future__ import annotations

import gzip
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.utils.premium_traffic import BYTES_IN_GB
from app.webserver import subscription_proxy
from app.webserver.subscription_proxy import (
    SubscriptionProxyMiddleware,
    premium_usage,
    rewrite_userinfo,
    short_uuid_candidates,
)


SUB_HOST = 'sub.example.com'
PANEL_USERINFO = 'upload=100; download=200; total=0; expire=1790000000'


def _client(upstream_handler, lookup, *, seen: list | None = None) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return upstream_handler(request)

    app = FastAPI()

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    app.add_middleware(
        SubscriptionProxyMiddleware,
        hosts={SUB_HOST},
        upstream='http://sub-page:3010/',
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        lookup=lookup,
    )
    return TestClient(app)


def _upstream(headers: dict, body: bytes) -> httpx.Response:
    # Потоком, как отвечает настоящий сервер: ответ с content= уже прочитан,
    # и aiter_raw() прокси на нём падает.
    return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


def _subscription_response(request: httpx.Request) -> httpx.Response:
    return _upstream(
        {'subscription-userinfo': PANEL_USERINFO, 'content-type': 'text/plain', 'profile-title': 'VPN'},
        b'vless://config',
    )


def _usage(result):
    calls: list[list[str]] = []

    async def lookup(short_uuids):
        calls.append(short_uuids)
        if isinstance(result, Exception):
            raise result
        return result

    return lookup, calls


def test_rewrite_userinfo_keeps_expire_and_unknown_fields():
    assert rewrite_userinfo(PANEL_USERINFO + '; extra=1', 5, 10) == (
        'upload=0; download=5; total=10; expire=1790000000; extra=1'
    )


def test_short_uuid_candidates_skip_prefixes_that_do_not_look_like_ids():
    assert short_uuid_candidates('/sub/Ab3dEf9hJk/json') == ['Ab3dEf9hJk']


def test_userinfo_is_replaced_with_premium_usage():
    lookup, calls = _usage((3 * BYTES_IN_GB, 10 * BYTES_IN_GB))
    response = _client(_subscription_response, lookup).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert response.status_code == 200
    assert response.headers['subscription-userinfo'] == (
        f'upload=0; download={3 * BYTES_IN_GB}; total={10 * BYTES_IN_GB}; expire=1790000000'
    )
    assert response.headers['profile-title'] == 'VPN'
    assert response.content == b'vless://config'
    assert calls == [['Ab3dEf9hJk']]


@pytest.mark.parametrize('result', [None, RuntimeError('db down')])
def test_userinfo_untouched_without_premium_or_on_error(result):
    lookup, _ = _usage(result)
    response = _client(_subscription_response, lookup).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert response.status_code == 200
    assert response.headers['subscription-userinfo'] == PANEL_USERINFO


def test_html_page_is_not_rewritten():
    def html_page(request):
        return _upstream({'subscription-userinfo': PANEL_USERINFO, 'content-type': 'text/html'}, b'<html>')

    lookup, calls = _usage((1, 2))
    response = _client(html_page, lookup).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert response.headers['subscription-userinfo'] == PANEL_USERINFO
    assert calls == []


def test_request_is_forwarded_as_is():
    seen: list[httpx.Request] = []
    lookup, _ = _usage(None)
    _client(_subscription_response, lookup, seen=seen).get(
        '/Ab3dEf9hJk/json?x=1',
        headers={'Host': SUB_HOST, 'User-Agent': 'Happ/3.0', 'X-Forwarded-For': '203.0.113.7'},
    )

    [request] = seen
    assert str(request.url) == 'http://sub-page:3010/Ab3dEf9hJk/json?x=1'
    assert request.headers['host'] == SUB_HOST
    assert request.headers['user-agent'] == 'Happ/3.0'
    assert request.headers['x-forwarded-for'] == '203.0.113.7'


def test_compressed_body_passes_through_undecoded():
    compressed = gzip.compress(b'vless://config')

    def gzipped(request):
        return _upstream({'subscription-userinfo': PANEL_USERINFO, 'content-encoding': 'gzip'}, compressed)

    lookup, _ = _usage(None)
    response = _client(gzipped, lookup).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    # Распакуй прокси тело, оставив Content-Encoding, клиент споткнулся бы на распаковке.
    assert response.headers['content-length'] == str(len(compressed))
    assert response.content == b'vless://config'


def test_unreachable_subscription_page_gives_502():
    def down(request):
        raise httpx.ConnectError('refused', request=request)

    lookup, _ = _usage(None)
    response = _client(down, lookup).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})
    assert response.status_code == 502


def test_other_hosts_reach_bot_routes():
    seen: list[httpx.Request] = []
    lookup, _ = _usage(None)
    response = _client(_subscription_response, lookup, seen=seen).get('/health', headers={'Host': 'bot.example.com'})

    assert response.json() == {'status': 'ok'}
    assert seen == []


# ---- premium_usage -------------------------------------------------------------------------


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        rows = self._rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _state(squad_uuid, used_gb, limit_gb, extra_gb=0):
    return SimpleNamespace(
        squad_uuid=squad_uuid,
        used_bytes=used_gb * BYTES_IN_GB,
        total_limit_bytes=(limit_gb + extra_gb) * BYTES_IN_GB,
    )


@pytest.fixture
def premium_env(monkeypatch):
    tariff = SimpleNamespace(
        server_traffic_limits={
            'wl-1': {'traffic_limit_gb': 10},
            'wl-2': {'traffic_limit_gb': 5},
            'not-connected': {'traffic_limit_gb': 7},
        }
    )
    states: list = []

    async def get_tariff(_db, _tariff_id):
        return tariff

    async def get_states(_db, _subscription_id):
        return states

    monkeypatch.setattr(subscription_proxy, 'get_tariff_by_id', get_tariff)
    monkeypatch.setattr(subscription_proxy, 'get_states_for_subscription', get_states)
    monkeypatch.setattr(subscription_proxy.settings, 'PREMIUM_TRAFFIC_ENABLED', True)
    monkeypatch.setattr(type(subscription_proxy.settings), 'is_tariffs_mode', lambda self: True)
    return states


def _subscription(**overrides):
    values = {'id': 1, 'tariff_id': 7, 'connected_squads': ['wl-1', 'wl-2', 'regular']}
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_premium_usage_sums_connected_premium_squads(premium_env):
    premium_env.extend([_state('wl-1', 3, 10, extra_gb=2)])

    used, total = await premium_usage(_FakeDb([_subscription()]), ['Ab3dEf9hJk'])

    # wl-1: замерен, с докупкой; wl-2: ещё не замерен — лимит тарифа.
    assert used == 3 * BYTES_IN_GB
    assert total == (12 + 5) * BYTES_IN_GB


async def test_premium_usage_none_without_connected_premium_squads(premium_env):
    assert await premium_usage(_FakeDb([_subscription(connected_squads=['regular'])]), ['Ab3dEf9hJk']) is None


async def test_premium_usage_none_when_path_is_ambiguous(premium_env):
    assert await premium_usage(_FakeDb([_subscription(), _subscription(id=2)]), ['a1b2c3', 'd4e5f6']) is None


async def test_premium_usage_none_when_premium_disabled(premium_env, monkeypatch):
    monkeypatch.setattr(subscription_proxy.settings, 'PREMIUM_TRAFFIC_ENABLED', False)
    assert await premium_usage(_FakeDb([_subscription()]), ['Ab3dEf9hJk']) is None
