"""Кастомные иконки на кнопках «Назад» и «Отмена».

Кнопки собираются в сотнях мест (`texts.BACK`, `texts.CANCEL`, свои строки
в обработчиках), общего источника у них нет. Поэтому иконки ставит не каждое
место, а исходящий запрос: middleware сессии находит в `reply_markup` кнопки
«Назад» и «Отмена», задаёт им `icon_custom_emoji_id`, а ведущий юникод-символ
из текста убирает — иначе будет две иконки. Кнопки, у которых иконка уже
задана, не трогаются.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


BACK_ICON_CUSTOM_EMOJI_ID = '5877629862306385808'
CANCEL_ICON_CUSTOM_EMOJI_ID = '5276167070285835565'

_ARROWS = ('⬅️', '⬅', '◀️', '◀', '←')
_CROSSES = ('❌', '✖️', '✖', '✕', '❎', '×')
# Слова «Назад» из локалей (ru, ua, en, zh, fa) — сравниваются после снятия стрелки.
_BACK_LABELS = frozenset({'назад', 'back', '返回', 'قبلی'})
# Подписи «Отмена» из локалей (ru, ua, en, zh, fa).
_CANCEL_LABELS = frozenset({'отмена', 'отменить', 'cancel', 'скасувати', '取消', 'لغو'})


def _strip_prefixes(text: str, prefixes: tuple[str, ...]) -> str:
    stripped = text.strip()
    for prefix in prefixes:
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def _strip_arrow(text: str) -> str:
    return _strip_prefixes(text, _ARROWS)


def _strip_cross(text: str) -> str:
    # «◀️ Отмена» — тоже отмена: сначала снимаем стрелку, затем крестик.
    return _strip_prefixes(_strip_arrow(text), _CROSSES)


def is_back_button(button: InlineKeyboardButton) -> bool:
    return _strip_arrow(button.text).casefold() in _BACK_LABELS


def is_cancel_button(button: InlineKeyboardButton) -> bool:
    return _strip_cross(button.text).casefold() in _CANCEL_LABELS


def apply_button_icons(markup: object) -> None:
    if not isinstance(markup, InlineKeyboardMarkup):
        return
    for row in markup.inline_keyboard:
        for button in row:
            if button.icon_custom_emoji_id:
                continue
            if is_back_button(button):
                button.text = _strip_arrow(button.text)
                button.icon_custom_emoji_id = BACK_ICON_CUSTOM_EMOJI_ID
            elif is_cancel_button(button):
                button.text = _strip_cross(button.text)
                button.icon_custom_emoji_id = CANCEL_ICON_CUSTOM_EMOJI_ID


class ButtonIconMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        apply_button_icons(getattr(method, 'reply_markup', None))
        return await make_request(bot, method)
