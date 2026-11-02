"""Патч Xray JSON для adblock-DNS.

Функция должна брать сгенерированный подпиской Xray JSON (объект или массив
конфигов), вставлять adblock-DNS первым сервером, оставлять остальные серверы
как fallback и добавлять два routing-правила: DNS-запрос через proxy и блок
0.0.0.0/8, ::/128 перед geoip:private. Всё остальное не трогать.
"""

from __future__ import annotations

import json

import pytest

from app.webserver.subscription_proxy import patch_adblock_dns


ADDRESS = '10.66.0.1'
PORT = 1153
TAG = 'dns_adblock'


def _config(**overrides):
    config = {
        'dns': {
            'tag': 'dns_out',
            'servers': [
                {'address': 'https://1.1.1.1/dns-query', 'skipFallback': False},
                {'address': 'tcp://8.8.8.8', 'skipFallback': False},
            ],
            'enableParallelQuery': True,
        },
        'routing': {
            'domainStrategy': 'AsIs',
            'rules': [
                {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'direct'},
                {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'direct'},
                {'type': 'field', 'domain': ['domain:ru'], 'outboundTag': 'direct'},
            ],
        },
        'outbounds': [
            {'tag': 'proxy', 'protocol': 'vless'},
            {'tag': 'direct', 'protocol': 'freedom'},
            {'tag': 'block', 'protocol': 'blackhole'},
        ],
        'remarks': 'node1',
    }
    config.update(overrides)
    return config


def _patched(configs, *, exclude_remarks=None):
    body = json.dumps(configs, separators=(',', ':')).encode()
    kwargs = {} if exclude_remarks is None else {'exclude_remarks': exclude_remarks}
    result = patch_adblock_dns(body, address=ADDRESS, port=PORT, timeout_ms=1000, tag=TAG, **kwargs)
    assert result is not None
    return json.loads(result)


def _rules(config):
    return config['routing']['rules']


def test_adds_adblock_dns_first_and_keeps_existing_servers():
    result = _patched([_config()])[0]

    servers = result['dns']['servers']
    assert servers[0] == {
        'tag': TAG,
        'address': ADDRESS,
        'port': PORT,
        'timeoutMs': 1000,
    }
    assert {'address': 'https://1.1.1.1/dns-query', 'skipFallback': False} in servers
    assert result['dns']['enableParallelQuery'] is True


def test_adds_dns_route_and_block_before_private_ip():
    result = _patched([_config()])[0]
    rules = _rules(result)

    assert rules[0] == {'type': 'field', 'inboundTag': [TAG], 'outboundTag': 'proxy'}

    block_index = next(i for i, rule in enumerate(rules) if rule.get('ip') == ['0.0.0.0/8', '::/128'])
    private_index = next(i for i, rule in enumerate(rules) if rule.get('ip') == ['geoip:private'])
    assert block_index < private_index
    assert rules[block_index]['outboundTag'] == 'block'


def test_does_not_add_catch_all_or_dns_ru_or_change_strategy():
    result = _patched([_config()])[0]

    assert result['routing']['domainStrategy'] == 'AsIs'
    assert not any(
        server.get('tag') == 'dns_ru' or 'geosite:category-ru' in server.get('domains', [])
        for server in result['dns']['servers']
    )
    # Ни одного «пустого» catch-all правила, добавленного патчем.
    assert not any(
        rule.get('type') == 'field' and rule.get('outboundTag') and len(rule) == 2 for rule in _rules(result)
    )


def test_patches_single_config_object():
    result = _patched(_config())

    assert isinstance(result, dict)
    assert result['dns']['servers'][0]['tag'] == TAG
    assert result['remarks'] == 'node1'


def test_creates_missing_dns_routing_and_block_outbound():
    config = {
        'outbounds': [
            {'tag': 'proxy', 'protocol': 'vless'},
            {'tag': 'direct', 'protocol': 'freedom'},
        ],
        'remarks': 'no-dns',
    }
    result = _patched([config])[0]

    assert result['dns']['servers'][0]['tag'] == TAG
    assert result['dns']['enableParallelQuery'] is True
    block_outbound = next(o for o in result['outbounds'] if o['protocol'] == 'blackhole')
    assert _rules(result)[0]['inboundTag'] == [TAG]
    assert any(
        rule.get('ip') == ['0.0.0.0/8', '::/128'] and rule['outboundTag'] == block_outbound['tag']
        for rule in _rules(result)
    )


def test_uses_existing_blackhole_tag():
    config = _config(
        outbounds=[
            {'tag': 'proxy', 'protocol': 'vless'},
            {'tag': 'direct', 'protocol': 'freedom'},
            {'tag': 'blackhole-existing', 'protocol': 'blackhole'},
        ]
    )
    result = _patched([config])[0]

    block_rule = next(rule for rule in _rules(result) if rule.get('ip') == ['0.0.0.0/8', '::/128'])
    assert block_rule['outboundTag'] == 'blackhole-existing'


def test_proxy_tag_is_detected_from_outbounds():
    config = _config(
        outbounds=[
            {'tag': 'direct', 'protocol': 'freedom'},
            {'tag': 'my-vless', 'protocol': 'vless'},
            {'tag': 'block', 'protocol': 'blackhole'},
        ]
    )
    result = _patched([config])[0]

    assert _rules(result)[0]['outboundTag'] == 'my-vless'


def test_is_idempotent():
    first = _patched([_config()])[0]
    body = json.dumps([first], separators=(',', ':')).encode()
    second = json.loads(patch_adblock_dns(body, address=ADDRESS, port=PORT, timeout_ms=1000, tag=TAG))

    assert [s for s in second[0]['dns']['servers'] if s.get('tag') == TAG] == [
        {'tag': TAG, 'address': ADDRESS, 'port': PORT, 'timeoutMs': 1000}
    ]
    assert [r for r in _rules(second[0]) if r.get('inboundTag') == [TAG]] == [
        {'type': 'field', 'inboundTag': [TAG], 'outboundTag': 'proxy'}
    ]
    assert [r for r in _rules(second[0]) if r.get('ip') == ['0.0.0.0/8', '::/128']] == [
        {'type': 'field', 'ip': ['0.0.0.0/8', '::/128'], 'outboundTag': 'block'}
    ]


def test_patches_every_config_in_array_and_preserves_remarks():
    result = _patched([_config(remarks='node1'), _config(remarks='node2')])

    assert [item['remarks'] for item in result] == ['node1', 'node2']
    assert all(item['dns']['servers'][0]['tag'] == TAG for item in result)


@pytest.mark.parametrize(
    'body',
    [
        b'vless://config',
        b'<html>subscription</html>',
        b'not-json',
        b'',
        b'["not-a-config"]',
    ],
)
def test_returns_none_for_non_xray_bodies(body):
    assert patch_adblock_dns(body, address=ADDRESS, port=PORT, timeout_ms=1000, tag=TAG) is None


def test_skips_unrelated_items_in_array_but_patches_configs():
    result = _patched([{'hello': 'world'}, _config()])

    assert result[0] == {'hello': 'world'}
    assert result[1]['dns']['servers'][0]['tag'] == TAG


def test_keeps_dns_tag_and_existing_servers_order_after_adblock():
    result = _patched([_config()])[0]

    assert result['dns']['tag'] == 'dns_out'
    assert result['dns']['servers'][1]['address'] == 'https://1.1.1.1/dns-query'


def test_malformed_config_is_not_partially_patched():
    malformed = _config(routing={'rules': 'not-a-list'})
    original = json.loads(json.dumps(malformed))

    result = _patched([malformed, _config()])

    assert result[0] == original
    assert result[1]['dns']['servers'][0]['tag'] == TAG


def test_excluded_remarks_are_skipped_case_insensitively():
    youtube = _config(remarks='YouTube-1080')
    youtube_alt = _config(remarks='youtube mobile')
    youtube_upper = _config(remarks='MY-YOUTUBE-PROFILE')
    netflix = _config(remarks='Netflix')

    result = _patched(
        [youtube, youtube_alt, youtube_upper, netflix],
        exclude_remarks=['youtube'],
    )

    for index in (0, 1, 2):
        assert 'dns_adblock' not in json.dumps(result[index])
    assert result[3]['dns']['servers'][0]['tag'] == TAG


def test_excluded_profile_stays_byte_identical():
    youtube = _config(remarks='YouTube-1080')
    original = json.loads(json.dumps(youtube))

    result = _patched([youtube, _config(remarks='Netflix')], exclude_remarks=['youtube'])

    assert result[0] == original


def test_all_profiles_excluded_returns_none():
    body = json.dumps([_config(remarks='YouTube-1080')], separators=(',', ':')).encode()

    result = patch_adblock_dns(
        body,
        address=ADDRESS,
        port=PORT,
        timeout_ms=1000,
        tag=TAG,
        exclude_remarks=['youtube'],
    )

    assert result is None


def test_missing_remarks_is_patched():
    config = _config()
    config.pop('remarks')

    result = _patched([config], exclude_remarks=['youtube'])

    assert result[0]['dns']['servers'][0]['tag'] == TAG


def test_multiple_exclude_tokens():
    result = _patched(
        [_config(remarks='YouTube-1080'), _config(remarks='YT music'), _config(remarks='Netflix')],
        exclude_remarks=['youtube', 'yt'],
    )

    assert 'dns_adblock' not in json.dumps(result[0])
    assert 'dns_adblock' not in json.dumps(result[1])
    assert result[2]['dns']['servers'][0]['tag'] == TAG


def test_empty_exclude_list_patches_youtube():
    result = _patched([_config(remarks='YouTube-1080')], exclude_remarks=[])

    assert result[0]['dns']['servers'][0]['tag'] == TAG


def test_excluded_remarks_handle_cyrillic_homoglyphs():
    # "☕Youtubе без рекламы": в конце "Youtubе" кириллическая е (U+0435).
    remark = '☕Youtubе без рекламы'

    result = _patched(
        [_config(remarks=remark), _config(remarks='Netflix')],
        exclude_remarks=['youtube'],
    )

    assert 'dns_adblock' not in json.dumps(result[0])
    assert result[1]['dns']['servers'][0]['tag'] == TAG


def test_cyrillic_token_matches_latin_remark():
    # Токен с кириллической е тоже должен находить латинский YouTube.
    result = _patched(
        [_config(remarks='YouTube-1080'), _config(remarks='Netflix')],
        exclude_remarks=['youtubе'],
    )

    assert 'dns_adblock' not in json.dumps(result[0])
    assert result[1]['dns']['servers'][0]['tag'] == TAG


def test_cyrillic_o_homoglyph_is_normalized():
    # "Yоutube" с кириллической о (U+043E).
    result = _patched(
        [_config(remarks='Yоutube'), _config(remarks='Netflix')],
        exclude_remarks=['youtube'],
    )

    assert 'dns_adblock' not in json.dumps(result[0])
    assert result[1]['dns']['servers'][0]['tag'] == TAG
