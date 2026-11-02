"""Прокси страницы подписок: подмена subscription-userinfo суммой премиум-сквадов,
нетронутый ответ, когда подменять нечего или расчёт упал, проброс запроса как есть,
502 при недоступной странице подписок и чужие домены — мимо прокси."""

from __future__ import annotations

import gzip
import json
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


def _client(upstream_handler, lookup, *, seen: list | None = None, adblock_lookup=None) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return upstream_handler(request)

    app = FastAPI()

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    middleware_kwargs = {}
    if adblock_lookup is not None:
        middleware_kwargs['adblock_lookup'] = adblock_lookup

    app.add_middleware(
        SubscriptionProxyMiddleware,
        hosts={SUB_HOST},
        upstream='http://sub-page:3010/',
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        lookup=lookup,
        **middleware_kwargs,
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


# ---- adblock DNS body patch ----------------------------------------------------------------


def _xray_config() -> dict:
    return {
        'dns': {'tag': 'dns_out', 'servers': [{'address': 'https://1.1.1.1/dns-query'}], 'enableParallelQuery': True},
        'routing': {
            'domainStrategy': 'AsIs',
            'rules': [{'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'direct'}],
        },
        'outbounds': [
            {'tag': 'proxy', 'protocol': 'vless'},
            {'tag': 'direct', 'protocol': 'freedom'},
            {'tag': 'block', 'protocol': 'blackhole'},
        ],
        'remarks': 'node1',
    }


def _xray_response(request: httpx.Request) -> httpx.Response:
    body = json.dumps([_xray_config()], separators=(',', ':')).encode()
    return _upstream(
        {'subscription-userinfo': PANEL_USERINFO, 'content-type': 'application/json'},
        body,
    )


def _adblock(result):
    calls: list[list[str]] = []

    async def lookup(short_uuids):
        calls.append(short_uuids)
        if isinstance(result, Exception):
            raise result
        return result

    return lookup, calls


@pytest.fixture
def adblock_env(monkeypatch):
    monkeypatch.setattr(subscription_proxy.settings, 'ADBLOCK_DNS_ENABLED', True)
    monkeypatch.setattr(subscription_proxy.settings, 'ADBLOCK_DNS_ADDRESS', '10.66.0.1')
    monkeypatch.setattr(subscription_proxy.settings, 'ADBLOCK_DNS_PORT', 1153)
    monkeypatch.setattr(subscription_proxy.settings, 'ADBLOCK_DNS_TIMEOUT_MS', 1000)
    monkeypatch.setattr(subscription_proxy.settings, 'ADBLOCK_DNS_TAG', 'dns_adblock')


def test_adblock_body_and_userinfo_are_rewritten_together(adblock_env):
    usage, _ = _usage((3 * BYTES_IN_GB, 10 * BYTES_IN_GB))
    adblock, adblock_calls = _adblock(True)

    response = _client(_xray_response, usage, adblock_lookup=adblock).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    config = json.loads(response.content)[0]
    assert config['dns']['servers'][0]['tag'] == 'dns_adblock'
    assert config['routing']['rules'][0] == {
        'type': 'field',
        'inboundTag': ['dns_adblock'],
        'outboundTag': 'proxy',
    }
    assert response.headers['subscription-userinfo'].startswith(
        f'upload=0; download={3 * BYTES_IN_GB}; total={10 * BYTES_IN_GB};'
    )
    assert response.headers['content-length'] == str(len(response.content))
    assert adblock_calls == [['Ab3dEf9hJk']]


def test_adblock_off_keeps_xray_body(adblock_env):
    usage, _ = _usage(None)
    adblock, adblock_calls = _adblock(False)
    original = json.dumps([_xray_config()], separators=(',', ':')).encode()

    response = _client(_xray_response, usage, adblock_lookup=adblock).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert response.content == original
    assert adblock_calls == [['Ab3dEf9hJk']]


def test_adblock_lookup_error_keeps_body(adblock_env):
    usage, _ = _usage(None)
    adblock, _ = _adblock(RuntimeError('db down'))
    original = json.dumps([_xray_config()], separators=(',', ':')).encode()

    response = _client(_xray_response, usage, adblock_lookup=adblock).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert response.status_code == 200
    assert response.content == original


def test_adblock_on_non_json_body_passes_through(adblock_env):
    usage, _ = _usage(None)
    adblock, _ = _adblock(True)

    response = _client(_subscription_response, usage, adblock_lookup=adblock).get(
        '/Ab3dEf9hJk', headers={'Host': SUB_HOST}
    )

    assert response.content == b'vless://config'


def test_adblock_on_requests_identity_encoding(adblock_env):
    seen: list[httpx.Request] = []
    usage, _ = _usage(None)
    adblock, _ = _adblock(True)

    _client(_xray_response, usage, seen=seen, adblock_lookup=adblock).get(
        '/Ab3dEf9hJk', headers={'Host': SUB_HOST, 'Accept-Encoding': 'gzip, br'}
    )

    [request] = seen
    assert request.headers['accept-encoding'] == 'identity'


def test_adblock_excludes_profiles_with_youtube_remarks(adblock_env, monkeypatch):
    monkeypatch.setattr(subscription_proxy.settings, 'ADBLOCK_DNS_EXCLUDE_REMARKS', 'youtube')
    youtube = _xray_config()
    youtube['remarks'] = 'YouTube-1080'
    netflix = _xray_config()
    netflix['remarks'] = 'Netflix'

    def upstream(request):
        body = json.dumps([youtube, netflix], separators=(',', ':')).encode()
        return _upstream(
            {'subscription-userinfo': PANEL_USERINFO, 'content-type': 'application/json'},
            body,
        )

    usage, _ = _usage(None)
    adblock, _ = _adblock(True)

    response = _client(upstream, usage, adblock_lookup=adblock).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    configs = json.loads(response.content)
    assert 'dns_adblock' not in json.dumps(configs[0])
    assert configs[1]['dns']['servers'][0]['tag'] == 'dns_adblock'


def test_adblock_on_gzip_body_is_patched_and_encoding_dropped(adblock_env):
    compressed = gzip.compress(json.dumps([_xray_config()]).encode())

    def gzipped(request):
        return _upstream(
            {
                'subscription-userinfo': PANEL_USERINFO,
                'content-type': 'application/json',
                'content-encoding': 'gzip',
            },
            compressed,
        )

    usage, _ = _usage(None)
    adblock, _ = _adblock(True)

    response = _client(gzipped, usage, adblock_lookup=adblock).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert 'content-encoding' not in response.headers
    config = json.loads(response.content)[0]
    assert config['dns']['servers'][0]['tag'] == 'dns_adblock'
    assert response.headers['content-length'] == str(len(response.content))


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


# ---- lookup_adblock_enabled ----------------------------------------------------------------


class _FakeAsyncSession:
    def __init__(self, rows):
        self.rows = rows
        self.executed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        self.executed = True
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))


def _adblock_session(monkeypatch, rows):
    session = _FakeAsyncSession(rows)
    monkeypatch.setattr(subscription_proxy, 'AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(type(subscription_proxy.settings), 'is_adblock_dns_enabled', lambda self: True)
    return session


def test_adblock_exclude_remarks_are_parsed_case_insensitively(monkeypatch):
    monkeypatch.setattr(subscription_proxy.settings, 'ADBLOCK_DNS_EXCLUDE_REMARKS', ' YouTube, YT ,, ')

    assert subscription_proxy.settings.get_adblock_dns_exclude_remarks() == ['youtube', 'yt']


async def test_lookup_adblock_enabled_true_for_single_enabled_subscription(monkeypatch):
    _adblock_session(monkeypatch, [True])

    assert await subscription_proxy.lookup_adblock_enabled(['Ab3dEf9hJk']) is True


async def test_lookup_adblock_enabled_false_for_disabled_subscription(monkeypatch):
    _adblock_session(monkeypatch, [False])

    assert await subscription_proxy.lookup_adblock_enabled(['Ab3dEf9hJk']) is False


async def test_lookup_adblock_enabled_false_when_path_is_ambiguous(monkeypatch):
    _adblock_session(monkeypatch, [True, True])

    assert await subscription_proxy.lookup_adblock_enabled(['a1b2c3', 'd4e5f6']) is False


async def test_lookup_adblock_enabled_false_without_short_uuid(monkeypatch):
    def explode():
        raise AssertionError('DB must not be touched without a short uuid')

    monkeypatch.setattr(subscription_proxy, 'AsyncSessionLocal', explode)
    monkeypatch.setattr(type(subscription_proxy.settings), 'is_adblock_dns_enabled', lambda self: True)

    assert await subscription_proxy.lookup_adblock_enabled([]) is False


async def test_lookup_adblock_enabled_false_when_globally_disabled(monkeypatch):
    def explode():
        raise AssertionError('DB must not be touched when the feature is off')

    monkeypatch.setattr(subscription_proxy, 'AsyncSessionLocal', explode)
    monkeypatch.setattr(type(subscription_proxy.settings), 'is_adblock_dns_enabled', lambda self: False)

    assert await subscription_proxy.lookup_adblock_enabled(['Ab3dEf9hJk']) is False
