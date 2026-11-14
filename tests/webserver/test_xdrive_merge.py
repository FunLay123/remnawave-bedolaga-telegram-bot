"""Слияние xdrive-конфига PasarGuard в Xray JSON подписки.

Проверяем чистую функцию слияния и её место в прокси: тело не-Xray форматов
(базовая64, HTML, sing-box) уходит клиенту нетронутым, а любой отказ
PasarGuard оставляет исходную подписку рабочей.
"""

from __future__ import annotations

import base64
import gzip
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.xdrive_service import XdriveTarget
from app.webserver import subscription_proxy
from app.webserver.subscription_proxy import SubscriptionProxyMiddleware, merge_xdrive_configs


SUB_HOST = 'sub.example.com'
TARGET = XdriveTarget(subscription_id=7, pasarguard_user_id=42, cache_version='created|revoke')


def _xray_entry(tag: str) -> dict:
    return {
        'remarks': tag,
        'inbounds': [{'tag': 'socks', 'protocol': 'socks', 'port': 1080}],
        'outbounds': [
            {'tag': 'proxy', 'protocol': 'vless'},
            {'tag': 'direct', 'protocol': 'freedom'},
        ],
    }


def _body(*entries: dict) -> bytes:
    return json.dumps(list(entries), separators=(',', ':')).encode()


# ---- merge_xdrive_configs --------------------------------------------------


def test_appends_pasarguard_array_to_remnawave_array():
    merged = merge_xdrive_configs(_body(_xray_entry('remnawave')), [_xray_entry('pasarguard')])

    parsed = json.loads(merged)
    assert [entry['remarks'] for entry in parsed] == ['remnawave', 'pasarguard']


def test_single_remnawave_object_becomes_an_array():
    body = json.dumps(_xray_entry('remnawave'), separators=(',', ':')).encode()

    merged = merge_xdrive_configs(body, [_xray_entry('pasarguard')])

    parsed = json.loads(merged)
    assert isinstance(parsed, list)
    assert [entry['remarks'] for entry in parsed] == ['remnawave', 'pasarguard']


def test_sing_box_body_is_untouched():
    body = json.dumps({'outbounds': [{'type': 'vless'}]}).encode()

    assert merge_xdrive_configs(body, [_xray_entry('pasarguard')]) is None


def test_base64_and_html_bodies_are_untouched():
    assert merge_xdrive_configs(base64.b64encode(b'vless://config'), [_xray_entry('pasarguard')]) is None
    assert merge_xdrive_configs(b'<html><body>subscription</body></html>', [_xray_entry('pasarguard')]) is None


def test_gzip_body_is_decompressed_and_merged():
    body = gzip.compress(_body(_xray_entry('remnawave')))

    merged = merge_xdrive_configs(body, [_xray_entry('pasarguard')])

    parsed = json.loads(merged)
    assert [entry['remarks'] for entry in parsed] == ['remnawave', 'pasarguard']


def test_empty_pasarguard_config_returns_none():
    assert merge_xdrive_configs(_body(_xray_entry('remnawave')), []) is None


# ---- middleware ------------------------------------------------------------


class FakeProvider:
    def __init__(self, payload: bytes | None = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.targets: list[XdriveTarget] = []

    async def get(self, target: XdriveTarget) -> bytes | None:
        self.targets.append(target)
        if self.error is not None:
            raise self.error
        return self.payload

    async def aclose(self) -> None:
        pass


def _upstream(headers: dict, body: bytes) -> httpx.Response:
    return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


async def _no_usage(_short_uuids):
    return None


async def _no_adblock(_short_uuids):
    return False


def _client(upstream_handler, *, provider: FakeProvider, seen: list[httpx.Request] | None = None) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return upstream_handler(request)

    app = FastAPI()

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    async def lookup(_short_uuids):
        return TARGET

    app.add_middleware(
        SubscriptionProxyMiddleware,
        hosts={SUB_HOST},
        upstream='http://sub-page:3010/',
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        lookup=_no_usage,
        adblock_lookup=_no_adblock,
        xdrive_lookup=lookup,
        xdrive_config=provider,
    )
    return TestClient(app)


@pytest.fixture
def xdrive_env(monkeypatch):
    monkeypatch.setattr(subscription_proxy.settings, 'XDRIVE_ENABLED', True)
    monkeypatch.setattr(subscription_proxy.settings, 'XDRIVE_CLIENT_UA_REGEX', '')


async def test_middleware_returns_original_body_for_base64_upstream(xdrive_env):
    body = base64.b64encode(b'vless://config')
    provider = FakeProvider(payload=_body(_xray_entry('pasarguard')))

    def upstream(request):
        return _upstream({'content-type': 'text/plain'}, body)

    response = _client(upstream, provider=provider).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert response.status_code == 200
    assert response.content == body
    # Формат не Xray — панель ради него не дёргаем.
    assert provider.targets == []


async def test_middleware_returns_original_body_when_provider_fails(xdrive_env):
    body = _body(_xray_entry('remnawave'))
    provider = FakeProvider(error=RuntimeError('panel down'))

    def upstream(request):
        return _upstream({'content-type': 'application/json'}, body)

    response = _client(upstream, provider=provider).get('/Ab3dEf9hJk', headers={'Host': SUB_HOST})

    assert response.status_code == 200
    assert response.content == body
    assert provider.targets == [TARGET]


async def test_middleware_appends_config_and_drops_body_validators(xdrive_env):
    body = _body(_xray_entry('remnawave'))
    provider = FakeProvider(payload=_body(_xray_entry('pasarguard')))
    seen: list[httpx.Request] = []

    def upstream(request):
        return _upstream(
            {
                'content-type': 'application/json',
                'etag': '"etag-value"',
                'last-modified': 'Wed, 21 Oct 2026 07:28:00 GMT',
            },
            body,
        )

    response = _client(upstream, provider=provider, seen=seen).get(
        '/Ab3dEf9hJk',
        headers={'Host': SUB_HOST, 'User-Agent': 'xdrive-client/1.0'},
    )

    parsed = json.loads(response.content)
    assert [entry['remarks'] for entry in parsed] == ['remnawave', 'pasarguard']
    assert 'etag' not in response.headers
    assert 'last-modified' not in response.headers
    assert response.headers['content-length'] == str(len(response.content))
    [request] = seen
    assert request.headers['accept-encoding'] == 'identity'


async def test_middleware_respects_client_ua_regex(xdrive_env, monkeypatch):
    monkeypatch.setattr(subscription_proxy.settings, 'XDRIVE_CLIENT_UA_REGEX', r'xdrive-client/\d+')
    body = _body(_xray_entry('remnawave'))
    provider = FakeProvider(payload=_body(_xray_entry('pasarguard')))

    def upstream(request):
        return _upstream({'content-type': 'application/json'}, body)

    matching = _client(upstream, provider=provider).get(
        '/Ab3dEf9hJk',
        headers={'Host': SUB_HOST, 'User-Agent': 'xdrive-client/1.0'},
    )
    assert len(json.loads(matching.content)) == 2

    other = _client(upstream, provider=FakeProvider(payload=_body(_xray_entry('pasarguard')))).get(
        '/Ab3dEf9hJk',
        headers={'Host': SUB_HOST, 'User-Agent': 'Happ/3.0'},
    )
    assert json.loads(other.content) == [_xray_entry('remnawave')]
