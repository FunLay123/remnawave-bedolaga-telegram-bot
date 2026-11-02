"""Юридические документы бота: HTML-файлы без БД и админки.

В файлах лежат только placeholder'ы. Реквизиты оператора подставляются из
настроек окружения, чтобы ФИО, адрес и телефон не попадали в публичный git.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

from app.config import settings
from app.utils.telegram_html import stored_html_to_telegram_pages


EDITION = '30.10.2026'
LEGAL_EMOJI_ID = '5359415772844152891'


@dataclass(frozen=True)
class LegalDocument:
    key: str
    title: str
    filename: str
    required_at_entry: bool = False


DOCUMENTS: tuple[LegalDocument, ...] = (
    LegalDocument('offer', 'Публичная оферта', 'offer.html', True),
    LegalDocument('refund', 'Политика возвратов', 'refund.html'),
    LegalDocument('privacy', 'Политика конфиденциальности', 'privacy.html'),
    LegalDocument('rules', 'Правила сервиса', 'rules.html', True),
)
DOCUMENTS_BY_KEY = {document.key: document for document in DOCUMENTS}


def _value(name: str, fallback: str) -> str:
    """Значение настройки без пустых строк, экранированное для Telegram HTML."""
    raw = getattr(settings, name, '')
    return escape(str(raw).strip() if raw else fallback)


def _contact_values() -> dict[str, str]:
    return {
        'OPERATOR_NAME': _value('LEGAL_OPERATOR_NAME', 'Оператор сервиса'),
        'OPERATOR_INN': _value('LEGAL_OPERATOR_INN', 'не указан'),
        'OPERATOR_ADDRESS': _value('LEGAL_OPERATOR_ADDRESS', 'не указан'),
        'SUPPORT_EMAIL': _value('SUPPORT_EMAIL', 'не указан'),
        'SUPPORT_PHONE': _value('SUPPORT_PHONE', 'не указан'),
        'SUPPORT_TELEGRAM': _value('SUPPORT_USERNAME', ''),
    }


def load_document_text(key: str) -> str:
    document = DOCUMENTS_BY_KEY.get(key)
    if document is None:
        return ''
    return (Path(__file__).parent / document.filename).read_text(encoding='utf-8')


def render_document_text(key: str) -> str:
    """Подставляет реквизиты оператора в HTML-шаблон документа."""
    text = load_document_text(key)
    for name, value in _contact_values().items():
        text = text.replace(f'{{{{{name}}}}}', value)
    return text


def document_pages(key: str, max_length: int = 3500) -> list[str]:
    return stored_html_to_telegram_pages(render_document_text(key), max_length=max_length) or ['']


def contacts_text() -> str:
    values = _contact_values()
    lines = [
        '<b>Контакты</b>',
        '',
        f'Оператор: {values["OPERATOR_NAME"]}',
        f'ИНН: {values["OPERATOR_INN"]}',
        f'Адрес для корреспонденции: {values["OPERATOR_ADDRESS"]}',
        f'Email: {values["SUPPORT_EMAIL"]}',
        f'Телефон: {values["SUPPORT_PHONE"]}',
    ]
    if values['SUPPORT_TELEGRAM']:
        lines.append(f'Telegram: {values["SUPPORT_TELEGRAM"]}')
    return '\n'.join(lines)


def entry_gate_text() -> str:
    return (
        f'<tg-emoji emoji-id="{LEGAL_EMOJI_ID}">📄</tg-emoji> <b>Перед началом</b>\n\n'
        'Чтобы пользоваться сервисом, примите:\n'
        '• Публичную оферту\n'
        '• Правила сервиса'
    )
