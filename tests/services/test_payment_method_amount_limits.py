"""Эффективные min/max метода: конфиг кабинета важнее env-дефолта.

Регрессия из ревью: экран суммы Stars и ручная валидация должны брать один
и тот же диапазон, иначе бот рекламирует 1–10 000 ₽, а принимает 1–50 000 ₽
(или наоборот).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.payment_method_config_service import get_method_amount_limits


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, config):
        self._config = config
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _Result(self._config)


@pytest.mark.asyncio
async def test_missing_config_row_uses_method_defaults():
    assert await get_method_amount_limits(_Session(None), 'telegram_stars') == (100, 1_000_000)
    assert await get_method_amount_limits(_Session(None), 'cryptobot') == (10_000, 10_000_000)
    assert await get_method_amount_limits(_Session(None), 'heleket') == (10_000, 10_000_000)


@pytest.mark.asyncio
async def test_cabinet_override_wins_over_defaults():
    config = SimpleNamespace(min_amount_kopeks=500_00, max_amount_kopeks=5_000_00)
    assert await get_method_amount_limits(_Session(config), 'telegram_stars') == (50_000, 500_000)


@pytest.mark.asyncio
async def test_none_fields_fall_back_individually():
    config = SimpleNamespace(min_amount_kopeks=None, max_amount_kopeks=200_000)
    assert await get_method_amount_limits(_Session(config), 'telegram_stars') == (100, 200_000)


@pytest.mark.asyncio
async def test_unknown_method_uses_base_defaults():
    assert await get_method_amount_limits(_Session(None), 'unknown_method') == (1_000, 10_000_000)
