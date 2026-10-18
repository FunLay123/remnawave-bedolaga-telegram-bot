"""Анимированная иконка на кнопках «Назад».

Кнопка «Назад» собирается в сотнях мест (`texts.BACK` и свои строки в обработчиках),
общего источника у неё нет. Поэтому иконку ставит не каждое место, а исходящий запрос:
middleware сессии находит в `reply_markup` кнопки «Назад» и задаёт им
`icon_custom_emoji_id`, а ведущую стрелку из текста убирает — иначе будет две иконки.
Кнопки, у которых иконка уже задана, не трогаются.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


BACK_ICON_CUSTOM_EMOJI_ID = '5877629862306385808'

_ARROWS = ('⬅️', '⬅', '◀️', '◀', '←')
# Слова «Назад» из локалей (ru, ua, en, zh, fa) — сравниваются после снятия стрелки.
_BACK_LABELS = frozenset({'назад', 'back', '返回', 'قبلی'})


def _strip_arrow(text: str) -> str:
    stripped = text.strip()
    for arrow in _ARROWS:
        if stripped.startswith(arrow):
            return stripped[len(arrow) :].strip()
    return stripped


def is_back_button(button: InlineKeyboardButton) -> bool:
    return _strip_arrow(button.text).casefold() in _BACK_LABELS


def apply_back_icon(markup: object) -> None:
    if not isinstance(markup, InlineKeyboardMarkup):
        return
    for row in markup.inline_keyboard:
        for button in row:
            if button.icon_custom_emoji_id or not is_back_button(button):
                continue
            button.text = _strip_arrow(button.text)
            button.icon_custom_emoji_id = BACK_ICON_CUSTOM_EMOJI_ID


class BackButtonIconMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        apply_back_icon(getattr(method, 'reply_markup', None))
        return await make_request(bot, method)
