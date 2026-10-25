import html

import structlog
from aiogram import types

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import error_handler
from app.utils.payment_utils import payment_invoice_keyboard, payment_invoice_text


logger = structlog.get_logger(__name__)


@error_handler
async def start_tribute_payment(
    callback: types.CallbackQuery,
    db_user: User,
):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.TRIBUTE_ENABLED:
        await callback.answer('❌ Оплата картой временно недоступна', show_alert=True)
        return

    try:
        from app.services.tribute_service import TributeService

        tribute_service = TributeService(callback.bot)
        payment_url = await tribute_service.create_payment_link(
            user_id=db_user.telegram_id,
            amount_kopeks=0,
            description='Пополнение баланса VPN',
        )

        if not payment_url:
            await callback.answer('❌ Ошибка создания платежа', show_alert=True)
            return

        keyboard = payment_invoice_keyboard(
            language=db_user.language,
            pay_url=payment_url,
            amount_kopeks=0,
            pay_text='Оплатить',
        )

        message_text = (
            payment_invoice_text('Tribute', None)
            + '\n\n<blockquote>Не отправляйте платёж анонимно — иначе не сможем зачислить его на баланс</blockquote>'
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        TributeService.remember_invoice_message(
            db_user.telegram_id,
            callback.message.chat.id,
            callback.message.message_id,
        )

    except Exception as e:
        logger.error('Ошибка создания Tribute платежа', error=e)
        await callback.answer('❌ Ошибка создания платежа', show_alert=True)

    await callback.answer()
