"""Иконки «Назад» и «Отмена» на исходящих inline-клавиатурах.

Кнопки «Отмена» собираются в десятках мест (`texts.CANCEL` и свои строки),
поэтому иконку ставит middleware, а не каждый обработчик. Он же убирает
ведущий «❌», иначе получаются две иконки.
"""

import pytest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.keyboards.inline import get_confirm_change_devices_keyboard
from app.middlewares.back_button_icon import (
    BACK_ICON_CUSTOM_EMOJI_ID,
    CANCEL_ICON_CUSTOM_EMOJI_ID,
    apply_button_icons,
)


def _markup(*labels: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f'cb_{index}')] for index, label in enumerate(labels)
        ]
    )


def test_cancel_button_gets_custom_icon_and_loses_cross():
    markup = _markup('❌ Отмена')

    apply_button_icons(markup)

    button = markup.inline_keyboard[0][0]
    assert button.text == 'Отмена'
    assert button.icon_custom_emoji_id == CANCEL_ICON_CUSTOM_EMOJI_ID


@pytest.mark.parametrize('label', ['❌ Cancel', '❌ Скасувати', '❌取消', '❌ لغو'])
def test_cancel_icon_works_for_all_locales(label):
    markup = _markup(label)

    apply_button_icons(markup)

    button = markup.inline_keyboard[0][0]
    assert button.icon_custom_emoji_id == CANCEL_ICON_CUSTOM_EMOJI_ID
    assert '❌' not in button.text


def test_back_button_still_gets_its_icon():
    markup = _markup('⬅️ Назад')

    apply_button_icons(markup)

    button = markup.inline_keyboard[0][0]
    assert button.text == 'Назад'
    assert button.icon_custom_emoji_id == BACK_ICON_CUSTOM_EMOJI_ID


def test_arrow_prefixed_cancel_is_recognized():
    """«◀️ Отмена» встречается в админке — это тоже отмена."""
    markup = _markup('◀️ Отмена')

    apply_button_icons(markup)

    button = markup.inline_keyboard[0][0]
    assert button.text == 'Отмена'
    assert button.icon_custom_emoji_id == CANCEL_ICON_CUSTOM_EMOJI_ID


def test_existing_icon_is_not_overridden():
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='x', icon_custom_emoji_id='999')]]
    )

    apply_button_icons(markup)

    assert markup.inline_keyboard[0][0].icon_custom_emoji_id == '999'


def test_longer_labels_are_not_hijacked():
    """«Отменить подписку» — не кнопка отмены, иконку не ставим."""
    markup = _markup('❌ Отменить подписку')

    apply_button_icons(markup)

    button = markup.inline_keyboard[0][0]
    assert button.icon_custom_emoji_id is None
    assert button.text == '❌ Отменить подписку'


def test_reply_keyboard_is_untouched():
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='❌ Отмена')]], resize_keyboard=True)

    apply_button_icons(markup)

    assert markup.keyboard[0][0].text == '❌ Отмена'


def test_devices_confirmation_cancel_gets_icon():
    keyboard = get_confirm_change_devices_keyboard(5, 1000, 'ru')

    apply_button_icons(keyboard)

    cancel = keyboard.inline_keyboard[-1][0]
    assert cancel.text == 'Отмена'
    assert cancel.icon_custom_emoji_id == CANCEL_ICON_CUSTOM_EMOJI_ID
