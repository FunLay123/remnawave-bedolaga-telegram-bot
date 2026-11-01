"""Строки белых списков: пометка, когда общий лимит уже исчерпан."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.localization.texts import get_texts
from app.utils.premium_traffic import BYTES_IN_GB
from app.utils.rich_menu import premium_squad_lines


SQUAD = '11111111-1111-4111-8111-111111111111'


def _tariff():
    return SimpleNamespace(server_traffic_limits={SQUAD: {'traffic_limit_gb': 3}})


def _subscription(*, traffic_limit_gb: float, traffic_used_gb: float):
    return SimpleNamespace(
        id=1,
        connected_squads=[SQUAD],
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=traffic_used_gb,
    )


def _patch_states(monkeypatch):
    state = SimpleNamespace(
        squad_uuid=SQUAD,
        total_limit_bytes=3 * BYTES_IN_GB,
        used_bytes=1 * BYTES_IN_GB,
    )
    monkeypatch.setattr(
        'app.database.crud.premium_traffic.get_states_for_subscription',
        AsyncMock(return_value=[state]),
    )
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_squad_display_names',
        AsyncMock(return_value={SQUAD: 'EU'}),
    )


@pytest.mark.anyio('asyncio')
async def test_general_limit_exhausted_note_is_added(monkeypatch):
    _patch_states(monkeypatch)

    lines = await premium_squad_lines(
        _subscription(traffic_limit_gb=12, traffic_used_gb=12),
        _tariff(),
        get_texts('ru'),
        None,
    )

    text = '\n'.join(lines)
    assert 'Общий лимит трафика исчерпан — белые списки тоже недоступны' in text
    assert '5361907081509098425' in text


@pytest.mark.anyio
async def test_no_note_when_general_traffic_is_still_available(monkeypatch):
    _patch_states(monkeypatch)

    lines = await premium_squad_lines(
        _subscription(traffic_limit_gb=12, traffic_used_gb=5),
        _tariff(),
        get_texts('ru'),
        None,
    )

    assert 'Общий лимит трафика исчерпан' not in '\n'.join(lines)


@pytest.mark.anyio
async def test_no_note_for_unlimited_general_traffic(monkeypatch):
    _patch_states(monkeypatch)

    lines = await premium_squad_lines(
        _subscription(traffic_limit_gb=0, traffic_used_gb=100),
        _tariff(),
        get_texts('ru'),
        None,
    )

    assert 'Общий лимит трафика исчерпан' not in '\n'.join(lines)
