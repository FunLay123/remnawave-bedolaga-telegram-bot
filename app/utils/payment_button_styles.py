"""Единый реестр стилей кнопок экранов пополнения.

Ключ — действие, а не текст кнопки. Реестр намеренно пустой: пока в него не
добавили запись, кнопка выглядит как раньше. Так дизайн можно менять точечно,
не переписывая десятки веток сборки клавиатур.

Примеры ключей:

    method:stars
    method:support
    amount:*            # все быстрые суммы
    amount:50000        # конкретная сумма (переопределяет amount:*)
    invoice:pay
    invoice:status
    support:write
    success:return
    success:subscription
    success:gift
    submethod:platega:2
    submethod:platega:*
    submethod:cashera:sbp
    submethod:pal24:card
    submethod:overpay:int
    nav:back
    nav:cancel

`nav:back` и `nav:cancel` поддержаны, но по умолчанию пусты: иконки для них
по-прежнему ставит ButtonIconMiddleware.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog
from aiogram.types import InlineKeyboardButton


logger = structlog.get_logger(__name__)

_VALID_STYLES = frozenset({'primary', 'success', 'danger'})

# Ведущий юникод-эмодзи снимается только когда у кнопки задана кастомная
# иконка: иначе Telegram покажет две иконки подряд.
_LEADING_EMOJI_RE = re.compile(r'^[\s\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+')


@dataclass(frozen=True, slots=True)
class ButtonStyle:
    icon_custom_emoji_id: str | None = None
    style: str | None = None  # 'primary' | 'success' | 'danger'


# Заполняется вручную. Пустой словарь = текущий вид без изменений.
BUTTON_STYLES: dict[str, ButtonStyle] = {
    'method:stars': ButtonStyle(icon_custom_emoji_id='5460980668378931880'),
    'amount:15000': ButtonStyle(style='primary'),
    'success:main_menu': ButtonStyle(
        icon_custom_emoji_id='5314310000531766389',
        style='primary',
    ),
    # Примеры:
    # 'method:stars': ButtonStyle(icon_custom_emoji_id='1234567890', style='primary'),
    # 'method:yookassa': ButtonStyle(icon_custom_emoji_id='1234567890'),
    # 'amount:*': ButtonStyle(icon_custom_emoji_id='1234567890', style='primary'),
    # 'invoice:pay': ButtonStyle(icon_custom_emoji_id='1234567890', style='primary'),
    # 'support:write': ButtonStyle(icon_custom_emoji_id='1234567890', style='primary'),
    # 'submethod:platega:2': ButtonStyle(icon_custom_emoji_id='1234567890', style='primary'),
}


def _candidate_keys(action: str) -> list[str]:
    """От точного ключа к общим fallback-ключам."""
    candidates = [action]

    if action.startswith('submethod:'):
        parts = action.split(':')
        if len(parts) >= 3:
            candidates.append(f'{parts[0]}:{parts[1]}:*')
        candidates.append('submethod:*')
    elif action.startswith('amount:'):
        candidates.append('amount:*')
    elif action.startswith('method:'):
        candidates.append('method:*')
    elif action.startswith('invoice:pay:'):
        parts = action.split(':')
        if len(parts) >= 4:
            candidates.append(f'{parts[0]}:{parts[1]}:{parts[2]}:*')
        candidates.append('invoice:pay')

    return candidates


def resolve_button_style(action: str, fallback_action: str | None = None) -> ButtonStyle | None:
    for key in _candidate_keys(action):
        spec = BUTTON_STYLES.get(key)
        if spec is not None:
            return spec
    if fallback_action:
        return resolve_button_style(fallback_action)
    return None


def apply_button_style(
    button: InlineKeyboardButton,
    action: str,
    fallback_action: str | None = None,
) -> InlineKeyboardButton:
    """Применить стиль к уже созданной кнопке. Нет записи — кнопка не меняется."""
    spec = resolve_button_style(action, fallback_action)
    if spec is None:
        return button

    update: dict[str, object] = {}

    icon_id = (spec.icon_custom_emoji_id or '').strip()
    if icon_id:
        if not icon_id.isdigit():
            logger.warning(
                'Некорректный icon_custom_emoji_id, иконка не применена',
                action=action,
                icon_custom_emoji_id=icon_id,
            )
        else:
            update['icon_custom_emoji_id'] = icon_id
            cleaned_text = _LEADING_EMOJI_RE.sub('', button.text, count=1).strip()
            if cleaned_text:
                update['text'] = cleaned_text

    style = (spec.style or '').strip()
    if style:
        if style in _VALID_STYLES:
            update['style'] = style
        else:
            logger.warning('Некорректный style кнопки, стиль не применён', action=action, style=style)

    if not update:
        return button

    return button.model_copy(update=update)


def styled_button(
    action: str,
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    fallback_action: str | None = None,
    **kwargs: object,
) -> InlineKeyboardButton:
    """Создать кнопку и сразу применить к ней стиль из реестра."""
    button = InlineKeyboardButton(text=text, callback_data=callback_data, url=url, **kwargs)
    return apply_button_style(button, action, fallback_action)
