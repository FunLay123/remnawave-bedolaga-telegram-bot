import html
import secrets

import structlog
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.external.telegram_stars import TelegramStarsService
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_method_config_service import get_method_amount_limits
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.payment_utils import (
    DEFAULT_TOPUP_MAX_KOPEKS,
    DEFAULT_TOPUP_MIN_KOPEKS,
    payment_amount_range_error,
    payment_amount_text,
    payment_invoice_keyboard,
    payment_invoice_text,
    payment_success_text,
)


logger = structlog.get_logger(__name__)


async def _get_stars_limits() -> tuple[int, int]:
    """Лимиты Stars из настроек метода — те же, что фильтруют быстрые суммы."""
    try:
        async with AsyncSessionLocal() as session:
            return await get_method_amount_limits(session, 'telegram_stars')
    except Exception as error:
        logger.warning('Не удалось получить лимиты Stars, использую глобальные', error=error)
        return DEFAULT_TOPUP_MIN_KOPEKS, DEFAULT_TOPUP_MAX_KOPEKS


def _is_stars_test_allowed(db_user: User) -> bool:
    """Тестовый Stars — только при флаге и только администратору."""
    return bool(
        settings.TELEGRAM_STARS_TEST_MODE
        and settings.is_admin(telegram_id=getattr(db_user, 'telegram_id', None))
    )


@error_handler
async def start_stars_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await callback.answer('❌ Пополнение через Stars временно недоступно', show_alert=True)
        return

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

    stars_name = settings.get_telegram_stars_display_name()
    min_amount, max_amount = await _get_stars_limits()
    message_text = payment_amount_text(stars_name, min_amount, max_amount)

    keyboard = await get_topup_amount_keyboard('stars', db_user.language, back_callback='balance_topup')

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await state.update_data(
        stars_prompt_message_id=callback.message.message_id,
        stars_prompt_chat_id=callback.message.chat.id,
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='stars')
    await callback.answer()


@error_handler
async def process_stars_payment_amount(message: types.Message, db_user: User, amount_kopeks: int, state: FSMContext):
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

    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await message.answer('⚠️ Оплата Stars временно недоступна')
        return

    min_amount, max_amount = await _get_stars_limits()
    if amount_kopeks < min_amount or amount_kopeks > max_amount:
        await message.answer(
            payment_amount_range_error(min_amount, max_amount),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            parse_mode='HTML',
        )
        return

    try:
        amount_rubles = amount_kopeks / 100
        stars_amount = TelegramStarsService.calculate_stars_from_rubles(amount_rubles)
        stars_name = settings.get_telegram_stars_display_name()
        test_mode = _is_stars_test_allowed(db_user)
        test_token: str | None = None

        if test_mode:
            test_token = secrets.token_urlsafe(8)
            keyboard = payment_invoice_keyboard(
                language=db_user.language,
                pay_callback=f'stars_test_pay|{amount_kopeks}|{test_token}',
                amount_kopeks=amount_kopeks,
                pay_text=f'Оплатить {stars_amount} ★',
                pay_action='invoice:pay:stars',
            )
        else:
            payment_service = PaymentService(message.bot)
            invoice_link = await payment_service.create_stars_invoice(
                amount_kopeks=amount_kopeks,
                description=f'Пополнение баланса на {texts.format_price(amount_kopeks)}',
                payload=f'balance_{db_user.id}_{amount_kopeks}',
            )
            keyboard = payment_invoice_keyboard(
                language=db_user.language,
                pay_url=invoice_link,
                amount_kopeks=amount_kopeks,
                pay_text=f'Оплатить {stars_amount} ★',
                pay_action='invoice:pay:stars',
            )

        state_data = await state.get_data()

        prompt_message_id = state_data.get('stars_prompt_message_id')
        prompt_chat_id = state_data.get('stars_prompt_chat_id', message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - зависит от прав бота
            logger.warning('Не удалось удалить сообщение с суммой Stars', delete_error=delete_error)

        if prompt_message_id and prompt_message_id != message.message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - диагностический лог
                logger.warning('Не удалось удалить сообщение с запросом суммы Stars', delete_error=delete_error)

        invoice_message = await message.answer(
            payment_invoice_text(stars_name, amount_kopeks, stars_amount=stars_amount),
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        await state.update_data(
            stars_invoice_message_id=invoice_message.message_id,
            stars_invoice_chat_id=invoice_message.chat.id,
        )

        if test_mode:
            await state.update_data(
                stars_test_token=test_token,
                stars_test_amount_kopeks=amount_kopeks,
                stars_test_stars_amount=stars_amount,
            )

        await state.set_state(None)

    except Exception as e:
        logger.error('Ошибка создания Stars invoice', error=e)
        await message.answer('⚠️ Ошибка создания платежа')


@error_handler
async def confirm_stars_test_payment(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Тестовый «Оплатить N ★»: тот же путь зачисления, но без платежа Telegram."""
    if not _is_stars_test_allowed(db_user):
        await callback.answer('❌ Тестовый режим недоступен', show_alert=True)
        return

    try:
        _, amount_str, token = (callback.data or '').split('|', 2)
        amount_kopeks = int(amount_str)
    except (ValueError, AttributeError):
        await callback.answer('❌ Некорректный тестовый платёж', show_alert=True)
        return

    data = await state.get_data()
    if (
        not data.get('stars_test_token')
        or token != data.get('stars_test_token')
        or amount_kopeks != data.get('stars_test_amount_kopeks')
    ):
        await callback.answer('Тестовая сессия устарела. Начните заново.', show_alert=True)
        return

    stars_amount = data.get('stars_test_stars_amount') or settings.rubles_to_stars(amount_kopeks / 100)

    # Снимаем «часики» до похода в сервис: зачисление с побочками может идти
    # секунды, а Telegram ждёт ответ на нажатие недолго.
    await callback.answer()

    payment_service = PaymentService(callback.bot)
    result = await payment_service.process_stars_payment(
        db=db,
        user_id=db_user.id,
        stars_amount=stars_amount,
        payload=f'balance_{db_user.id}_{amount_kopeks}',
        telegram_payment_charge_id=f'stars_test_{token}',
    )

    if not result.success:
        await callback.message.answer('❌ Не удалось зачислить тестовый платёж')
        return

    await state.update_data(
        stars_test_token=None,
        stars_test_amount_kopeks=None,
        stars_test_stars_amount=None,
    )

    if result.already_processed:
        await callback.message.answer('ℹ️ Этот тестовый платёж уже зачислен')
        return

    try:
        await db.refresh(db_user)
    except Exception:  # pragma: no cover - защитный код
        pass

    amount_credited = result.amount_kopeks or amount_kopeks
    keyboard = await payment_service.build_topup_success_keyboard(db_user)
    text = payment_success_text(amount_credited, result.new_balance_kopeks, result.old_balance_kopeks)

    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    except TelegramBadRequest:  # pragma: no cover - сообщение могло быть удалено
        await callback.message.answer(text, reply_markup=keyboard, parse_mode='HTML')
