"""Тосты без эмодзи: один фильтр на все AnswerCallbackQuery."""

from __future__ import annotations

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage

from app.middlewares.toast_emoji import ToastEmojiFilterMiddleware, strip_emoji


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('❌ Ошибка проверки статуса', 'Ошибка проверки статуса'),
        ('⚠️ Этот способ сейчас недоступен', 'Этот способ сейчас недоступен'),
        ('✅ Платёж уже зачислен', 'Платёж уже зачислен'),
        ('ℹ️ Статус отправлен в чат', 'Статус отправлен в чат'),
        ('🌐 Язык обновлен.', 'Язык обновлен.'),
        ('🙏 Спасибо за участие!', 'Спасибо за участие!'),
        ('1️⃣ Введите код', 'Введите код'),
        ('🇷🇺 Флаг', 'Флаг'),
        ('👨‍👩‍👧‍👦 Семья', 'Семья'),
        (
            '<tg-emoji emoji-id="1">📄</tg-emoji> Документ',
            '<tg-emoji emoji-id="1">📄</tg-emoji> Документ',
        ),
        (
            '❌ <tg-emoji emoji-id="1">📄</tg-emoji> Ошибка',
            '<tg-emoji emoji-id="1">📄</tg-emoji> Ошибка',
        ),
        ('❌ Ошибка: ❌', 'Ошибка:'),
        ('Текст без эмодзи', 'Текст без эмодзи'),
        ('Кнопка → далее', 'Кнопка → далее'),
        ('Цена 100 ₽ • скидка', 'Цена 100 ₽ • скидка'),
        ('👍', ''),
    ],
)
def test_strip_emoji(source, expected):
    assert strip_emoji(source) == expected


async def _make_request(bot, method):
    return method


@pytest.mark.anyio('asyncio')
async def test_middleware_rewrites_callback_text():
    method = AnswerCallbackQuery(callback_query_id='1', text='❌ Ошибка', show_alert=True)

    result = await ToastEmojiFilterMiddleware()(_make_request, None, method)

    assert result.text == 'Ошибка'


@pytest.mark.anyio('asyncio')
async def test_middleware_keeps_non_emoji_text():
    method = AnswerCallbackQuery(callback_query_id='1', text='Неверный формат', show_alert=True)

    result = await ToastEmojiFilterMiddleware()(_make_request, None, method)

    assert result.text == 'Неверный формат'


@pytest.mark.anyio('asyncio')
async def test_emoji_only_toast_becomes_silent():
    method = AnswerCallbackQuery(callback_query_id='1', text='✅')

    result = await ToastEmojiFilterMiddleware()(_make_request, None, method)

    assert result.text is None


@pytest.mark.anyio('asyncio')
async def test_custom_emoji_is_not_stripped():
    method = AnswerCallbackQuery(
        callback_query_id='1',
        text='<tg-emoji emoji-id="1">📄</tg-emoji>',
    )

    result = await ToastEmojiFilterMiddleware()(_make_request, None, method)

    assert result.text == '<tg-emoji emoji-id="1">📄</tg-emoji>'


@pytest.mark.anyio('asyncio')
async def test_emoji_only_alert_keeps_text():
    # Пустой alert Telegram не покажет, поэтому текст-эмодзи оставляем как был.
    method = AnswerCallbackQuery(callback_query_id='1', text='⚠️', show_alert=True)

    result = await ToastEmojiFilterMiddleware()(_make_request, None, method)

    assert result.text == '⚠️'


@pytest.mark.anyio('asyncio')
async def test_middleware_ignores_other_methods():
    method = SendMessage(chat_id=1, text='❌ Привет')

    result = await ToastEmojiFilterMiddleware()(_make_request, None, method)

    assert result.text == '❌ Привет'
