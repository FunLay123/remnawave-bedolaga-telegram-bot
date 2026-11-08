"""Админские превью уведомлений должны переживать смену шаблонов.

Превью — второй потребитель тех же ключей, что и мониторинг. Новые шаблоны
expired_1d и скидочных волн добавили {balance}/{discounted_price}; превью
обязано передавать их, иначе кнопка «Тест» падает с KeyError.
"""

from __future__ import annotations

import pytest

from app.handlers.admin.monitoring import _build_notification_preview_message


@pytest.mark.anyio('asyncio')
@pytest.mark.parametrize(
    ('notification_type', 'needle'),
    [
        ('expired_1d', 'Баланс'),
        ('expired_2d', 'Цена со скидкой'),
        ('expired_nd', 'Цена со скидкой'),
    ],
)
async def test_preview_formats_current_templates(notification_type: str, needle: str):
    message, keyboard = await _build_notification_preview_message('ru', notification_type)

    assert needle in message
    assert keyboard is not None
