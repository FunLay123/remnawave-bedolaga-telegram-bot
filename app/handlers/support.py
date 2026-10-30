import structlog
from aiogram import Dispatcher, F, types

from app.database.models import User
from app.keyboards.inline import get_support_keyboard
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)

SUPPORT_TEXT = (
    '<tg-emoji emoji-id="5359415772844152891">🛠</tg-emoji> <b>Техподдержка</b>\n\n'
    '<blockquote>Документы и условия сервиса — ниже.</blockquote>'
)


async def show_support_info(callback: types.CallbackQuery, db_user: User):
    await edit_or_answer_photo(
        callback=callback,
        caption=SUPPORT_TEXT,
        keyboard=get_support_keyboard(db_user.language),
        parse_mode='HTML',
        force_text=True,
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_support_info, F.data == 'menu_support')
