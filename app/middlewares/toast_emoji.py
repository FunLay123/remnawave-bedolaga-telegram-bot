"""Убирает эмодзи из клиентских тостов (``AnswerCallbackQuery``).

Тосты в боте — это ``callback.answer(text)``: их тексты разбросаны по сотням
обработчиков, поэтому чистим не каждый вызов, а исходящий запрос целиком.
Так правило действует для всех тостов сразу и не разъезжается, когда новый
обработчик добавляют с эмодзи.

HTML-теги не трогаем (они остаются как были). Кастомные ``<tg-emoji>`` тоже
сохраняем: Telegram умеет рендерить их и в тостах, поэтому на время чистки
прячем теги за плейсхолдеры, чтобы не срезать эмодзи внутри них.
"""

from __future__ import annotations

import re
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import AnswerCallbackQuery, TelegramMethod


# Диапазоны, из которых Telegram собирает эмодзи: пиктограммы, флаги, тона кожи,
# ZWJ-последовательности, вариационные селекторы и keycap-хвосты. Обычные стрелки
# и типографские символы (→, —, •, ₽) сюда намеренно не попадают.
_EMOJI_RE = re.compile(
    '['
    '\U0001f1e6-\U0001f1ff'
    '\U0001f300-\U0001f5ff'
    '\U0001f600-\U0001f64f'
    '\U0001f680-\U0001f6ff'
    '\U0001f700-\U0001f77f'
    '\U0001f780-\U0001f7ff'
    '\U0001f800-\U0001f8ff'
    '\U0001f900-\U0001f9ff'
    '\U0001fa00-\U0001faff'
    '\U00002600-\U000027bf'
    '\U00002b00-\U00002bff'
    '\U0000fe00-\U0000fe0f'
    '\U0000200d'
    '\U000020e3'
    '\U00002139'
    '\U0000203c'
    '\U00002049'
    '\U00002194-\U00002199'
    '\U000021a9-\U000021aa'
    '\U00002934-\U00002935'
    '\U00003030'
    '\U0000303d'
    '\U00003297'
    '\U00003299'
    '\U000e0020-\U000e007f'
    ']+'
)

# «1️⃣»: цифра + вариационный селектор + enclosing keycap. Без отдельного правила
# от последовательности осталась бы голая цифра.
_KEYCAP_RE = re.compile('[0-9#*]\ufe0f?\u20e3')

_TG_EMOJI_RE = re.compile(r'<tg-emoji\b[^>]*>.*?</tg-emoji>', re.IGNORECASE | re.DOTALL)

_MULTISPACE_RE = re.compile(r'[ \t]{2,}')
_SPACE_BEFORE_PUNCT_RE = re.compile(r'[ \t]+([,.;:!?…)])')


def strip_emoji(text: str) -> str:
    """Убрать эмодзи и лишние пробелы, сохранив HTML и ``<tg-emoji>``."""
    custom_emojis: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        custom_emojis.append(match.group(0))
        return f'\x00{len(custom_emojis) - 1}\x00'

    cleaned = _TG_EMOJI_RE.sub(_protect, text)
    cleaned = _KEYCAP_RE.sub('', cleaned)
    cleaned = _EMOJI_RE.sub('', cleaned)
    cleaned = _MULTISPACE_RE.sub(' ', cleaned)
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r'\1', cleaned)
    for index, tag in enumerate(custom_emojis):
        cleaned = cleaned.replace(f'\x00{index}\x00', tag)
    return cleaned.strip()


class ToastEmojiFilterMiddleware(BaseRequestMiddleware):
    """Чистит ``AnswerCallbackQuery.text`` перед отправкой в Telegram."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        if isinstance(method, AnswerCallbackQuery) and method.text:
            cleaned = strip_emoji(method.text)
            if cleaned:
                method.text = cleaned
            elif not method.show_alert:
                # Тост был только из эмодзи — молча отвечаем на нажатие.
                method.text = None
            # Пустой alert Telegram не покажет, поэтому его текст не трогаем.
        return await make_request(bot, method)
