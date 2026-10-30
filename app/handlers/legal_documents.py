from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext

from app.database.models import User
from app.keyboards.inline import (
    get_legal_document_keyboard,
    get_legal_documents_keyboard,
    get_legal_gate_keyboard,
)
from app.legal_documents import (
    DOCUMENTS_BY_KEY,
    EDITION,
    LEGAL_EMOJI_ID,
    document_pages,
    entry_gate_text,
)
from app.states import RegistrationStates
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


async def show_documents(callback: types.CallbackQuery, db_user: User):
    text = (
        f'<tg-emoji emoji-id="{LEGAL_EMOJI_ID}">📄</tg-emoji> <b>Документы</b>\n\n'
        '<blockquote>Оферта, возвраты, конфиденциальность и правила сервиса.</blockquote>'
    )
    await edit_or_answer_photo(
        callback=callback,
        caption=text,
        keyboard=get_legal_documents_keyboard(db_user.language),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()


async def show_legal_gate(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(RegistrationStates.waiting_for_legal_accept)
    await edit_or_answer_photo(
        callback=callback,
        caption=entry_gate_text(),
        keyboard=get_legal_gate_keyboard(),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()


async def show_document(callback: types.CallbackQuery):
    parts = (callback.data or '').split(':')
    key = parts[1] if len(parts) > 1 else ''
    document = DOCUMENTS_BY_KEY.get(key)
    if document is None:
        await callback.answer('Документ не найден', show_alert=True)
        return
    try:
        page = max(1, int(parts[2]))
    except (IndexError, ValueError):
        page = 1
    origin = parts[3] if len(parts) > 3 and parts[3] in {'gate', 'hub'} else 'hub'
    try:
        pages = document_pages(key)
    except Exception as error:
        logger.error('Не удалось загрузить юридический документ', document=key, error=str(error))
        await callback.answer('Документ временно недоступен', show_alert=True)
        return
    if not pages or not pages[0]:
        logger.error('Юридический документ пуст', document=key)
        await callback.answer('Документ временно недоступен', show_alert=True)
        return
    page = min(page, len(pages))
    text = f'<b>{document.title}</b>\nРедакция от {EDITION}\n\n{pages[page - 1]}'
    await edit_or_answer_photo(
        callback=callback,
        caption=text,
        keyboard=get_legal_document_keyboard(key, page, len(pages), origin),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_documents, F.data == 'legal_docs')
    dp.callback_query.register(show_legal_gate, F.data == 'legal_gate')
    dp.callback_query.register(show_document, F.data.startswith('legal_doc:'))
