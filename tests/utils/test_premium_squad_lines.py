"""Общий лимит исчерпан: в меню остаётся одна причина, без цифр трафика."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.utils import rich_menu
from app.utils.rich_menu import general_traffic_exhausted_line, is_general_traffic_exhausted


def test_general_limit_is_exhausted_by_numbers():
    assert is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=12, traffic_used_gb=12))
    assert is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=12, traffic_used_gb=32.7))
    assert not is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=12, traffic_used_gb=5))
    assert not is_general_traffic_exhausted(SimpleNamespace(traffic_limit_gb=0, traffic_used_gb=100))


def test_exhausted_line_uses_custom_emoji():
    line = general_traffic_exhausted_line(get_texts('ru'))

    assert '5361907081509098425' in line
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
