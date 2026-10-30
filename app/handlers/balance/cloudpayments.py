"""Handler for CloudPayments balance top-up."""

import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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
    payment_amount_error_text,
    payment_amount_range_error,
    payment_amount_text,
    payment_invoice_keyboard,
    payment_invoice_text,
)


logger = structlog.get_logger(__name__)


async def _create_cloudpayments_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
):
    """
    Common logic for creating CloudPayments payment and sending response.

    Args:
        message_or_callback: Either a Message or CallbackQuery object
        db_user: User object
        db: Database session
        amount_kopeks: Amount in kopeks
        edit_message: Whether to edit existing message or send new one
    """
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description=settings.CLOUDPAYMENTS_DESCRIPTION,
    )

    result = await payment_service.create_cloudpayments_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        telegram_id=db_user.telegram_id,
        language=db_user.language,
    )

    if not result:
        error_text = texts.t(
            'PAYMENT_CREATE_ERROR',
            'Не удалось создать платёж. Попробуйте позже.',
        )
        if edit_message:
            await message_or_callback.edit_text(
                error_text,
                reply_markup=get_back_keyboard(db_user.language),
                parse_mode='HTML',
            )
        else:
            await message_or_callback.answer(
                error_text,
                parse_mode='HTML',
            )
        return

    payment_url = result.get('payment_url')

    keyboard = payment_invoice_keyboard(
        language=db_user.language,
        pay_url=payment_url,
        amount_kopeks=amount_kopeks,
    )

    response_text = payment_invoice_text(settings.get_cloudpayments_display_name(), amount_kopeks)

    if edit_message:
        await message_or_callback.edit_text(
            response_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    else:
        await message_or_callback.answer(
            response_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

    logger.info('CloudPayments payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_cloudpayments_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    """
    Process payment amount directly.

    Similar to other payment amount handlers.
    """
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_cloudpayments_enabled():
        await message.answer(
            texts.t('CLOUDPAYMENTS_NOT_AVAILABLE', 'CloudPayments временно недоступен'),
        )
        return

    # Validate amount
    if (
        amount_kopeks < settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS
        or amount_kopeks > settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS
    ):
        await message.answer(
            payment_amount_range_error(
                settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS, settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS
            ),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            parse_mode='HTML',
        )
        return

    await state.clear()

    await _create_cloudpayments_payment_and_respond(message, db_user, db, amount_kopeks, edit_message=False)


@error_handler
async def start_cloudpayments_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    """
    Start CloudPayments payment flow.

    Shows amount input prompt.
    """
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_cloudpayments_enabled():
        await callback.answer(
            texts.t('CLOUDPAYMENTS_NOT_AVAILABLE', 'CloudPayments временно недоступен'),
            show_alert=True,
        )
        return

    message_text = payment_amount_text(
        settings.get_cloudpayments_display_name(),
        settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS,
        settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS,
    )

    keyboard = await get_topup_amount_keyboard('cloudpayments', db_user.language, back_callback='balance_topup')

    await callback.message.edit_text(
        message_text,
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='cloudpayments')
    await state.update_data(
        cloudpayments_prompt_message_id=callback.message.message_id,
        cloudpayments_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_cloudpayments_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """
    Process entered amount and create CloudPayments payment.

    Generates payment link and sends it to user.
    """
    texts = get_texts(db_user.language)

    # Get state data
    state_data = await state.get_data()
    payment_method = state_data.get('payment_method')

    if payment_method != 'cloudpayments':
        return  # Not our payment method

    # Parse amount
    try:
        amount_text = message.text.strip().replace(',', '.').replace(' ', '')
        amount_rub = float(amount_text)
        amount_kopeks = int(amount_rub * 100)
    except (ValueError, TypeError):
        await message.answer(
            payment_amount_error_text('Введите сумму числом, например 500'),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            parse_mode='HTML',
        )
        return

    # Validate amount
    if (
        amount_kopeks < settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS
        or amount_kopeks > settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS
    ):
        await message.answer(
            payment_amount_range_error(
                settings.CLOUDPAYMENTS_MIN_AMOUNT_KOPEKS, settings.CLOUDPAYMENTS_MAX_AMOUNT_KOPEKS
            ),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            parse_mode='HTML',
        )
        return

    await state.clear()

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description=settings.CLOUDPAYMENTS_DESCRIPTION,
    )

    result = await payment_service.create_cloudpayments_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        telegram_id=db_user.telegram_id,
        language=db_user.language,
    )

    if not result:
        await message.answer(
            texts.t(
                'PAYMENT_CREATE_ERROR',
                'Не удалось создать платёж. Попробуйте позже.',
            ),
            parse_mode='HTML',
        )
        return

    payment_url = result.get('payment_url')

    keyboard = payment_invoice_keyboard(
        language=db_user.language,
        pay_url=payment_url,
        amount_kopeks=amount_kopeks,
    )

    await message.answer(
        payment_invoice_text(settings.get_cloudpayments_display_name(), amount_kopeks),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    logger.info('CloudPayments payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)
