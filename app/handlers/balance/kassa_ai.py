"""Handler for KassaAI balance top-up."""

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
from app.services.kassa_ai_service import KASSA_AI_SUB_METHODS
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


# --- Enabled check + display name lookup by payment method ---

_KASSA_AI_METHOD_CONFIG = {
    'kassa_ai': {
        'is_enabled': settings.is_kassa_ai_enabled,
        'display_name': settings.get_kassa_ai_display_name,
        'unavailable_text': 'KassaAI временно недоступен',
    },
    'kassa_ai_sbp': {
        'is_enabled': settings.is_kassa_ai_sbp_enabled,
        'display_name': settings.get_kassa_ai_sbp_display_name,
        'unavailable_text': 'KassaAI СБП временно недоступен',
    },
    'kassa_ai_card': {
        'is_enabled': settings.is_kassa_ai_card_enabled,
        'display_name': settings.get_kassa_ai_card_display_name,
        'unavailable_text': 'KassaAI Карта временно недоступна',
    },
    'kassa_ai_sberpay': {
        'is_enabled': settings.is_kassa_ai_sberpay_enabled,
        'display_name': settings.get_kassa_ai_sberpay_display_name,
        'unavailable_text': 'KassaAI SberPay временно недоступен',
    },
}


async def _check_topup_restriction(callback: types.CallbackQuery, db_user: User) -> bool:
    """Check if user has topup restriction. Returns True if restricted (handler should abort)."""
    if not getattr(db_user, 'restriction_topup', False):
        return False
    texts = get_texts(db_user.language)
    reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
    support_url = settings.get_support_contact_url()
    keyboard = []
    if support_url:
        keyboard.append([InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    await callback.message.edit_text(
        f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    return True


async def _create_kassa_ai_payment_and_respond(
    message_or_callback,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    edit_message: bool = False,
    payment_method: str = 'kassa_ai',
):
    """Common logic for creating KassaAI payment and sending response."""
    texts = get_texts(db_user.language)
    amount_rub = amount_kopeks / 100

    # Create payment
    payment_service = PaymentService()

    description = settings.PAYMENT_BALANCE_TEMPLATE.format(
        service_name=settings.PAYMENT_SERVICE_NAME,
        description='Пополнение баланса',
    )

    sub = KASSA_AI_SUB_METHODS.get(payment_method)
    payment_system_id = sub['payment_system_id'] if sub else settings.KASSA_AI_PAYMENT_SYSTEM_ID

    result = await payment_service.create_kassa_ai_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(db_user, 'email', None),
        language=db_user.language,
        payment_system_id=payment_system_id,
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
    cfg = _KASSA_AI_METHOD_CONFIG.get(payment_method, _KASSA_AI_METHOD_CONFIG['kassa_ai'])
    display_name = cfg['display_name']()

    keyboard = payment_invoice_keyboard(
        language=db_user.language,
        pay_url=payment_url,
        amount_kopeks=amount_kopeks,
    )

    response_text = payment_invoice_text(display_name, amount_kopeks)

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

    logger.info('KassaAI payment created', telegram_id=db_user.telegram_id, amount_rub=amount_rub)


@error_handler
async def process_kassa_ai_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
    payment_method: str = 'kassa_ai',
):
    """Process payment amount directly (called from custom_amount handlers)."""
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
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await state.clear()
        return

    # Validate amount
    min_amount = settings.KASSA_AI_MIN_AMOUNT_KOPEKS
    max_amount = settings.KASSA_AI_MAX_AMOUNT_KOPEKS

    if amount_kopeks < min_amount or amount_kopeks > max_amount:
        await message.answer(
            payment_amount_range_error(min_amount, max_amount),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            parse_mode='HTML',
        )
        return

    await state.clear()

    await _create_kassa_ai_payment_and_respond(
        message_or_callback=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        edit_message=False,
        payment_method=payment_method,
    )


# --- Generic start/quick-amount implementations ---


async def _start_kassa_ai_sub_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    payment_method: str,
):
    """Generic start topup handler for any KassaAI sub-method."""
    cfg = _KASSA_AI_METHOD_CONFIG[payment_method]
    texts = get_texts(db_user.language)

    if not cfg['is_enabled']():
        await callback.answer(texts.t('KASSA_AI_NOT_AVAILABLE', cfg['unavailable_text']), show_alert=True)
        return

    if await _check_topup_restriction(callback, db_user):
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method=payment_method)

    min_amount = settings.KASSA_AI_MIN_AMOUNT_KOPEKS // 100
    max_amount = settings.KASSA_AI_MAX_AMOUNT_KOPEKS // 100
    display_name = cfg['display_name']()

    keyboard = await get_topup_amount_keyboard(payment_method, db_user.language, db)

    await callback.message.edit_text(
        payment_amount_text(display_name, min_amount * 100, max_amount * 100),
        parse_mode='HTML',
        reply_markup=keyboard,
    )


# --- Public handler functions (registered in main.py) ---


@error_handler
async def start_kassa_ai_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start KassaAI top-up process - ask for amount."""
    await _start_kassa_ai_sub_topup(callback, db_user, db, state, 'kassa_ai')


@error_handler
async def process_kassa_ai_custom_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Process custom amount input for KassaAI payment."""
    data = await state.get_data()
    pm = data.get('payment_method', 'kassa_ai')
    if pm not in _KASSA_AI_METHOD_CONFIG:
        return

    try:
        amount_text = message.text.replace(',', '.').replace(' ', '').strip()
        amount_rubles = float(amount_text)
        amount_kopeks = int(amount_rubles * 100)
    except (ValueError, TypeError):
        await message.answer(
            payment_amount_error_text('Введите сумму числом, например 500'),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            parse_mode='HTML',
        )
        return

    await process_kassa_ai_payment_amount(
        message=message,
        db_user=db_user,
        db=db,
        amount_kopeks=amount_kopeks,
        state=state,
        payment_method=pm,
    )


@error_handler
async def start_kassa_ai_sbp_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start KassaAI SBP top-up process."""
    await _start_kassa_ai_sub_topup(callback, db_user, db, state, 'kassa_ai_sbp')


@error_handler
async def start_kassa_ai_card_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start KassaAI Card top-up process."""
    await _start_kassa_ai_sub_topup(callback, db_user, db, state, 'kassa_ai_card')


@error_handler
async def start_kassa_ai_sberpay_topup(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Start KassaAI SberPay top-up process."""
    await _start_kassa_ai_sub_topup(callback, db_user, db, state, 'kassa_ai_sberpay')
