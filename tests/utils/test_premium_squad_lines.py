"""Общий лимит исчерпан: в меню остаётся одна причина, без цифр трафика."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.utils import rich_menu
from app.utils.rich_menu import general_traffic_exhausted_line, is_general_traffic_exhausted


@pytest.mark.anyio('asyncio')
async def test_unlimited_squad_line_shows_infinity(monkeypatch):
    subscription = SimpleNamespace(id=1, connected_squads=['wl'])
    tariff = SimpleNamespace(
        server_traffic_limits={'wl': {'traffic_limit_gb': 5}},
    )
    state = SimpleNamespace(
        squad_uuid='wl',
        used_bytes=2 * 1024**3,
        total_limit_bytes=5 * 1024**3,
        unlimited_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    monkeypatch.setattr(
        'app.database.crud.premium_traffic.get_states_for_subscription', AsyncMock(return_value=[state])
    )
    monkeypatch.setattr('app.database.crud.server_squad.get_squad_display_names', AsyncMock(return_value={'wl': 'WL'}))

    lines = await rich_menu.premium_squad_lines(subscription, tariff, get_texts('ru'), AsyncMock())

    assert len(lines) == 1
    assert 'WL' in lines[0]
    assert '♾️' in lines[0]


@pytest.mark.anyio('asyncio')
async def test_legacy_squad_without_unlimited_at_shows_numeric_total(monkeypatch):
    subscription = SimpleNamespace(id=1, connected_squads=['wl'])
    tariff = SimpleNamespace(server_traffic_limits={'wl': {'traffic_limit_gb': 5}})
    state = SimpleNamespace(
        squad_uuid='wl',
        used_bytes=2 * 1024**3,
        total_limit_bytes=5 * 1024**3,
    )
    monkeypatch.setattr(
        'app.database.crud.premium_traffic.get_states_for_subscription', AsyncMock(return_value=[state])
    )
    monkeypatch.setattr('app.database.crud.server_squad.get_squad_display_names', AsyncMock(return_value={'wl': 'WL'}))

    lines = await rich_menu.premium_squad_lines(subscription, tariff, get_texts('ru'), AsyncMock())

    assert len(lines) == 1
    assert 'WL' in lines[0]
    assert '♾️' not in lines[0]
    assert '5 ГБ' in lines[0]


def test_general_limit_is_exhausted_by_numbers():
    assert is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=12, traffic_used_gb=12))
    assert is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=12, traffic_used_gb=32.7))
    assert not is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=12, traffic_used_gb=5))
    assert not is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=0, traffic_used_gb=100))


def test_exhausted_line_uses_custom_emoji():
    line = general_traffic_exhausted_line(get_texts('ru'))

    assert '5258382581375723416' in line
    assert 'Лимит трафика исчерпан — белые списки тоже недоступны' in line


@pytest.mark.anyio('asyncio')
async def test_rich_block_shows_only_the_reason_when_general_limit_is_exhausted(monkeypatch):
    subscription = SimpleNamespace(
        id=1,
        actual_status='limited',
        status='limited',
        is_trial=True,
        tariff_id=5,
        tariff=SimpleNamespace(is_daily=False),
        traffic_limit_gb=12,
        traffic_used_gb=32.7,
        device_limit=3,
        end_date=None,
    )
    user = SimpleNamespace(subscription=subscription, balance_kopeks=0, full_name='Test', language='ru')

    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(rich_menu, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(is_daily=False)))
    monkeypatch.setattr(
        rich_menu,
        'premium_squad_lines',
        AsyncMock(return_value=['<tg-emoji emoji-id="5116444615301399317">💠</tg-emoji> Белые списки: 1 ГБ / 3 ГБ']),
    )

    html = await rich_menu._build_single_subscription_block(user, get_texts('ru'), AsyncMock())

    assert 'Лимит трафика исчерпан — белые списки тоже недоступны' in html
    assert '32.7' not in html
    assert 'Белые списки: 1 ГБ / 3 ГБ' not in html
