"""Юридические документы бота: HTML-файлы без БД и админки."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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


def load_document_text(key: str) -> str:
    document = DOCUMENTS_BY_KEY.get(key)
    if document is None:
        return ''
    return (Path(__file__).parent / document.filename).read_text(encoding='utf-8')


def document_pages(key: str, max_length: int = 3500) -> list[str]:
    return stored_html_to_telegram_pages(load_document_text(key), max_length=max_length) or ['']


def entry_gate_text() -> str:
    return (
        f'<tg-emoji emoji-id="{LEGAL_EMOJI_ID}">📄</tg-emoji> <b>Перед началом</b>\n\n'
        'Чтобы пользоваться сервисом, примите:\n'
        '• Публичную оферту\n'
        '• Правила сервиса'
    )
