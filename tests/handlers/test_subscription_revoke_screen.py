"""Экран подтверждения перевыпуска подписки.

Вместо списка «Это действие: …» и вопроса «Продолжить?» — заголовок с
кастомной иконкой, суть в одном blockquote и primary-кнопка «Перевыпустить»
с иконкой пункта меню.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.subscription.revoke as revoke_mod
from app.handlers.subscription.revoke import _build_revoke_confirm_keyboard


REVOKE_ICON = '4911241630633165627'  # та же иконка, что у пункта «Перевыпустить»
TITLE_ICON = '5359415772844152891'


def test_confirm_button_is_primary_with_reissue_icon():
    keyboard = _build_revoke_confirm_keyboard('ru')

    button = keyboard.inline_keyboard[0][0]
    assert button.text == 'Подтвердить'
    assert button.style == 'primary'
    assert button.icon_custom_emoji_id == REVOKE_ICON


@pytest.mark.anyio('asyncio')
async def test_confirmation_screen_is_compact():
    callback = MagicMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()

    user = MagicMock()
    user.language = 'ru'
    user.subscription = MagicMock()
    user.subscription.is_active = True
    user.subscription.last_revoke_at = None

    with patch('app.config.Settings.is_subscription_revoke_enabled', lambda self: True):
        await revoke_mod.start_subscription_revoke(callback, user, AsyncMock(), None)

    text = callback.message.edit_text.await_args.args[0]
    assert f'<tg-emoji emoji-id="{TITLE_ICON}">' in text
    assert '<b>Перевыпуск подписки</b>' in text
    assert '<blockquote>' in text
    assert 'Старая ссылка перестанет работать' in text
    # Многословие старого экрана убрано.
    assert '⚠️' not in text
    assert 'Сгенерирует' not in text
    assert 'Продолжить?' not in text
