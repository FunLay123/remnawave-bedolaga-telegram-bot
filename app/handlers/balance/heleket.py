import html
from datetime import UTC, datetime

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.payment_utils import (
    payment_amount_range_error,
    payment_amount_text,
    payment_invoice_keyboard,
    payment_invoice_text,
)


logger = structlog.get_logger(__name__)


@error_handler
async def start_heleket_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
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

    if not settings.is_heleket_enabled():
        await callback.answer('❌ Оплата через Heleket недоступна', show_alert=True)
        return

    message_text = payment_amount_text(settings.get_heleket_display_name(), 100_00, 100_000_00)

    keyboard = await get_topup_amount_keyboard('heleket', db_user.language, back_callback='balance_topup')

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='heleket',
        heleket_prompt_message_id=callback.message.message_id,
        heleket_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_heleket_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_heleket_enabled():
        await message.answer('❌ Оплата через Heleket недоступна')
        return

    amount_rubles = amount_kopeks / 100

    if amount_rubles < 100 or amount_rubles > 100000:
        await message.answer(
            payment_amount_range_error(100_00, 100_000_00),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            parse_mode='HTML',
        )
        return

    payment_service = PaymentService(message.bot)

    result = await payment_service.create_heleket_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=f'Пополнение баланса на {amount_rubles:.0f} ₽',
        language=db_user.language,
    )

    if not result:
        await message.answer('❌ Не удалось создать счёт в Heleket. Попробуйте позже или обратитесь в поддержку.')
        await state.clear()
        return

    payment_url = result.get('payment_url')
    if not payment_url:
        await message.answer('❌ Не удалось получить ссылку для оплаты Heleket')
        await state.clear()
        return

    keyboard = payment_invoice_keyboard(
        language=db_user.language,
        pay_url=payment_url,
        amount_kopeks=amount_kopeks,
        status_callback=f'check_heleket_{result["local_payment_id"]}',
    )

    state_data = await state.get_data()
    prompt_message_id = state_data.get('heleket_prompt_message_id')
    prompt_chat_id = state_data.get('heleket_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - depends on bot rights
        logger.warning('Не удалось удалить сообщение с суммой Heleket', delete_error=delete_error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - diagnostic
            logger.warning('Не удалось удалить сообщение с запросом суммы Heleket', delete_error=delete_error)

    payer_amount = result.get('payer_amount')
    payer_currency = result.get('payer_currency')
    amount_note = f'≈ {payer_amount} {payer_currency}' if payer_amount and payer_currency else None

    invoice_message = await message.answer(
        payment_invoice_text(settings.get_heleket_display_name(), amount_kopeks, amount_note=amount_note),
        parse_mode='HTML',
        reply_markup=keyboard,
    )

    try:
        from app.services import payment_service as payment_module

        payment = await payment_module.get_heleket_payment_by_id(db, result['local_payment_id'])
        if payment:
            metadata = dict(getattr(payment, 'metadata_json', {}) or {})
            metadata['invoice_message'] = {
                'chat_id': invoice_message.chat.id,
                'message_id': invoice_message.message_id,
            }
            await db.execute(
                update(payment.__class__)
                .where(payment.__class__.id == payment.id)
                .values(metadata_json=metadata, updated_at=datetime.now(UTC))
            )
            await db.commit()
    except Exception as error:  # pragma: no cover - diagnostics
        logger.warning('Не удалось сохранить сообщение Heleket', error=error)

    await state.update_data(
        heleket_invoice_message_id=invoice_message.message_id,
        heleket_invoice_chat_id=invoice_message.chat.id,
    )

    await state.clear()


@error_handler
async def check_heleket_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
) -> None:
    try:
        local_payment_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('Некорректный идентификатор платежа', show_alert=True)
        return

    from app.database.crud.heleket import get_heleket_payment_by_id

    payment = await get_heleket_payment_by_id(db, local_payment_id)
    if not payment:
        await callback.answer('Платёж не найден', show_alert=True)
        return

    language = getattr(payment.user, 'language', None) or settings.DEFAULT_LANGUAGE
    texts = get_texts(language)

    if payment.is_paid:
        message = texts.t('HELEKET_PAYMENT_ALREADY_PAID', '✅ Платёж уже зачислен')
        await callback.answer(message, show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    updated_payment = await payment_service.sync_heleket_payment_status(
        db,
        local_payment_id=local_payment_id,
    )

    if updated_payment:
        payment = updated_payment

    if payment.is_paid:
        message = texts.t('HELEKET_PAYMENT_SUCCESS', '✅ Платёж зачислен на баланс')
        await callback.answer(message, show_alert=True)
        return

    status_normalized = (payment.status or '').lower()
    status_messages = {
        'check': texts.t('HELEKET_STATUS_CHECK', '⏳ Ожидание оплаты'),
        'process': texts.t('HELEKET_STATUS_PROCESS', '⚙️ Платёж обрабатывается'),
        'confirm_check': texts.t('HELEKET_STATUS_CONFIRM_CHECK', '⛓ Ожидание подтверждений сети'),
        'wrong_amount': texts.t('HELEKET_STATUS_WRONG_AMOUNT', '❗️ Оплачена неверная сумма'),
        'wrong_amount_waiting': texts.t(
            'HELEKET_STATUS_WRONG_AMOUNT_WAITING',
            '❗️ Недостаточная сумма, ожидаем доплату',
        ),
        'paid_over': texts.t('HELEKET_STATUS_PAID_OVER', '✅ Платёж зачислен (с переплатой)'),
        'paid': texts.t('HELEKET_STATUS_PAID', '✅ Платёж зачислен'),
        'cancel': texts.t('HELEKET_STATUS_CANCEL', '🚫 Платёж отменён'),
        'fail': texts.t('HELEKET_STATUS_FAIL', '❌ Ошибка при оплате'),
        'system_fail': texts.t('HELEKET_STATUS_SYSTEM_FAIL', '❌ Системная ошибка Heleket'),
        'refund_process': texts.t('HELEKET_STATUS_REFUND_PROCESS', '↩️ Возврат обрабатывается'),
        'refund_fail': texts.t('HELEKET_STATUS_REFUND_FAIL', '⚠️ Ошибка возврата'),
        'refund_paid': texts.t('HELEKET_STATUS_REFUND_PAID', '✅ Возврат выполнен'),
        'locked': texts.t('HELEKET_STATUS_LOCKED', '🔒 Средства заблокированы'),
    }

    message = status_messages.get(status_normalized)
    if message is None:
        template = texts.t('HELEKET_STATUS_UNKNOWN', 'ℹ️ Статус платежа: {status}')
        status_value = payment.status or status_normalized or '—'
        try:
            message = template.format(status=status_value)
        except Exception:  # pragma: no cover - defensive formatting
            message = f'ℹ️ Статус платежа: {status_value}'

    await callback.answer(message, show_alert=True)
