"""Временный единый стиль уведомлений: мельница вместо ведущего эмодзи.

Если текст уведомления начинается с эмодзи, он заменяется на кастомный эмодзи
мельницы, а прежний эмодзи остаётся фоллбэком внутри ``<tg-emoji>``. Если текст
начинается не с эмодзи, он не меняется.

Модуль изолирован намеренно: чтобы вернуть прежний вид, достаточно выключить
``STYLE_ENABLED`` или перестать вызывать ``style_notification_text``. Партнёрка
и ответы поддержки исключены — они уже оформлены отдельно.
"""

from __future__ import annotations

import re
import unicodedata


STYLE_ENABLED = True
MILL_EMOJI_ID = '5359415772844152891'
MILL_EMOJI_FALLBACK = '📋'

# Эти уведомления уже оформлены вручную и не должны перекрашиваться.
EXCLUDED_NOTIFICATION_TYPES = frozenset(
    {
        'referral_bonus',
        'referral_registered',
        'referral_welcome',
        'ticket_reply',
    }
)

_CUSTOM_EMOJI_RE = re.compile(r'<tg-emoji\b[^>]*>(?P<glyph>.*?)</tg-emoji>', re.DOTALL)


def _leading_emoji_span(text: str) -> tuple[int, int, bool] | None:
    """Найти ведущий эмодзи: (начало, конец, это_готовый_tg-emoji)."""
    start = len(text) - len(text.lstrip())
    rest = text[start:]
    if not rest:
        return None
    if rest.startswith('<tg-emoji'):
        match = _CUSTOM_EMOJI_RE.match(rest)
        if match is None:
            return None
        return start, start + match.end(), True

    # Эмодзи в Unicode — категория So. Некоторые (ℹ\ufe0f, ©\ufe0f) приходят как буквы/
    # символы с вариационным селектором — их тоже считаем эмодзи.
    if unicodedata.category(rest[0]) != 'So' and not (len(rest) > 1 and rest[1] == '\ufe0f'):
        return None
    end = 1
    if len(rest) > 1 and rest[1] == '\ufe0f':
        end = 2
    while len(rest) > end + 1 and rest[end] == '\u200d':
        end += 2
        if len(rest) > end and rest[end] == '\ufe0f':
            end += 1
    return start, start + end, False


def style_notification_text(text: str, *, notification_type: object | None = None) -> str:
    """Заменить ведущий эмодзи на мельницу, сохранив его как фоллбэк."""
    if not STYLE_ENABLED or not text:
        return text

    type_value = getattr(notification_type, 'value', notification_type)
    if isinstance(type_value, str) and type_value in EXCLUDED_NOTIFICATION_TYPES:
        return text

    span = _leading_emoji_span(text)
    if span is None:
        return text

    start, end, is_custom = span
    leading = text[start:end]
    if is_custom:
        match = _CUSTOM_EMOJI_RE.match(leading)
        glyph = match.group('glyph') if match else ''
    else:
        glyph = leading
    if not glyph:
        return text

    replacement = f'<tg-emoji emoji-id="{MILL_EMOJI_ID}">{glyph}</tg-emoji>'
    return text[:start] + replacement + text[end:]
