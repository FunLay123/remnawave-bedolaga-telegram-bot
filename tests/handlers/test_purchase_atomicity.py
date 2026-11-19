"""Обычная покупка/продление тарифа — одна транзакция.

Списание с баланса, изменение подписки и запись об оплате обязаны сохраняться
вместе. Сбой между списанием и коммитом не оставляет ни долга, ни «половины»
покупки, а успешная покупка пишет ровно одну строку ``SUBSCRIPTION_PAYMENT``.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.handlers.admin.users as admin_users
import app.handlers.subscription.tariff_purchase as tariff_mod
from app.config import settings
from app.database.models import Base, Subscription, Tariff, Transaction, TransactionType, User
from app.services.pricing_engine import pricing_engine
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


TABLES = list(Base.metadata.sorted_tables)

USER_ID = 7
SUB_ID = 10
TARIFF_ID = 42
INITIAL_BALANCE = 200_000
PRICE = 10_000


@contextlib.asynccontextmanager
async def _db(monkeypatch, tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    ensure_real_aiosqlite(monkeypatch)
    db_path = (tmp_path / 'purchase.db').as_posix()
    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}', connect_args={'timeout': 15})
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield maker
    finally:
        await engine.dispose()


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
        bot=None,
    )


def _state() -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.clear = AsyncMock()
    return state


async def _seed(maker: async_sessionmaker[AsyncSession]) -> tuple[datetime, datetime]:
    """Юзер с балансом и промо-скидкой, живая платная подписка на тариф."""
    now = datetime.now(UTC)
    sub_end = now + timedelta(days=10)
    promo_expires = now + timedelta(days=3)
    async with maker() as seed:
        seed.add(
            User(
                id=USER_ID,
                telegram_id=1007,
                first_name='Buyer',
                language='ru',
                balance_kopeks=INITIAL_BALANCE,
                promo_offer_discount_percent=25,
                promo_offer_discount_source='expired_discount_wave2',
                promo_offer_discount_expires_at=promo_expires,
            )
        )
        seed.add(
            Tariff(
                id=TARIFF_ID,
                name='Базовый',
                description='',
                is_active=True,
                traffic_limit_gb=50,
                device_limit=1,
                allowed_squads=['squad-1'],
                period_prices={'30': PRICE},
                display_order=1,
            )
        )
        seed.add(
            Subscription(
                id=SUB_ID,
                user_id=USER_ID,
                remnawave_short_id='sub10',
                status='active',
                is_trial=False,
                start_date=now - timedelta(days=20),
                end_date=sub_end,
                traffic_limit_gb=50,
                device_limit=1,
                tariff_id=TARIFF_ID,
                connected_squads=['squad-1'],
                autopay_enabled=False,
            )
        )
        await seed.commit()
    return sub_end, promo_expires


def _patch_extend_side_effects(monkeypatch) -> None:
    panel_service = SimpleNamespace(
        create_remnawave_user=AsyncMock(),
        update_remnawave_user=AsyncMock(),
    )
    monkeypatch.setattr(tariff_mod, 'SubscriptionService', MagicMock(return_value=panel_service), raising=False)
    monkeypatch.setattr(tariff_mod, 'should_create_panel_account', AsyncMock(return_value=False), raising=False)
    monkeypatch.setattr(tariff_mod, 'AdminNotificationService', MagicMock(), raising=False)
    monkeypatch.setattr(tariff_mod, 'emit_transaction_side_effects', AsyncMock(), raising=False)
    monkeypatch.setattr(
        'app.services.referral_purchase_service.register_first_purchase_after_purchase',
        AsyncMock(),
        raising=False,
    )
    monkeypatch.setattr(
        'app.services.user_cart_service.user_cart_service.delete_user_cart',
        AsyncMock(),
        raising=False,
    )
    monkeypatch.setattr(
        'app.services.user_cart_service.user_cart_service.delete_subscription_cart',
        AsyncMock(),
        raising=False,
    )
    monkeypatch.setattr(
        'app.handlers.subscription.purchase.show_subscription_info',
        AsyncMock(),
        raising=False,
    )


async def _payment_count(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as check:
        result = await check.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value)
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_extend_failure_between_debit_and_commit_leaves_no_trace(monkeypatch, tmp_path):
    """Сбой продления после списания не оставляет списанных денег и сожжённого промо."""
    async with _db(monkeypatch, tmp_path) as maker:
        original_end, promo_expires = await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            _patch_extend_side_effects(monkeypatch)
            monkeypatch.setattr(
                pricing_engine,
                'calculate_tariff_purchase_price',
                AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=1000)),
            )
            monkeypatch.setattr(
                tariff_mod,
                'extend_subscription',
                AsyncMock(side_effect=RuntimeError('db down')),
                raising=False,
            )
            failed_refund = AsyncMock()
            monkeypatch.setattr(tariff_mod, '_persist_failed_refund', failed_refund, raising=False)

            await inspect.unwrap(tariff_mod.confirm_tariff_extend)(
                _callback(f'tariff_ext_confirm:{SUB_ID}:{TARIFF_ID}:30'),
                user,
                db,
                _state(),
            )

        async with maker() as check:
            fresh_user = await check.get(User, USER_ID)
            assert fresh_user is not None
            assert fresh_user.balance_kopeks == INITIAL_BALANCE
            # Промо-скидка не сгорела: списание откатилось вместе с покупкой.
            assert fresh_user.promo_offer_discount_percent == 25
            assert fresh_user.promo_offer_discount_source == 'expired_discount_wave2'
            assert fresh_user.promo_offer_discount_expires_at == promo_expires

            sub = await check.get(Subscription, SUB_ID)
            assert sub is not None
            assert sub.end_date == original_end

        assert await _payment_count(maker) == 0
        failed_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_extend_journal_failure_rolls_back_whole_purchase(monkeypatch, tmp_path):
    """Неудачная запись об оплате откатывает и списание, и продление."""
    async with _db(monkeypatch, tmp_path) as maker:
        original_end, _ = await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            _patch_extend_side_effects(monkeypatch)
            monkeypatch.setattr(
                pricing_engine,
                'calculate_tariff_purchase_price',
                AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=0)),
            )
            monkeypatch.setattr(
                tariff_mod,
                'create_transaction',
                AsyncMock(side_effect=RuntimeError('journal down')),
                raising=False,
            )

            await inspect.unwrap(tariff_mod.confirm_tariff_extend)(
                _callback(f'tariff_ext_confirm:{SUB_ID}:{TARIFF_ID}:30'),
                user,
                db,
                _state(),
            )

        async with maker() as check:
            fresh_user = await check.get(User, USER_ID)
            assert fresh_user is not None
            assert fresh_user.balance_kopeks == INITIAL_BALANCE
            sub = await check.get(Subscription, SUB_ID)
            assert sub is not None
            assert sub.end_date == original_end

        assert await _payment_count(maker) == 0


@pytest.mark.asyncio
async def test_successful_extend_commits_debit_term_and_single_payment(monkeypatch, tmp_path):
    """Успешное продление: одно списание, новый срок и ровно одна запись об оплате."""
    async with _db(monkeypatch, tmp_path) as maker:
        original_end, _ = await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            _patch_extend_side_effects(monkeypatch)
            monkeypatch.setattr(
                pricing_engine,
                'calculate_tariff_purchase_price',
                AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=0)),
            )
            monkeypatch.setattr(
                tariff_mod,
                'shift_lava_next_charge_after_manual_extension',
                AsyncMock(),
                raising=False,
            )

            await inspect.unwrap(tariff_mod.confirm_tariff_extend)(
                _callback(f'tariff_ext_confirm:{SUB_ID}:{TARIFF_ID}:30'),
                user,
                db,
                _state(),
            )

        async with maker() as check:
            fresh_user = await check.get(User, USER_ID)
            assert fresh_user is not None
            assert fresh_user.balance_kopeks == INITIAL_BALANCE - PRICE

            sub = await check.get(Subscription, SUB_ID)
            assert sub is not None
            # Продление того же тарифа добавляет купленные дни к прежнему сроку.
            assert original_end + timedelta(days=30) - timedelta(seconds=1) <= sub.end_date
            assert sub.end_date <= original_end + timedelta(days=30) + timedelta(seconds=1)

            result = await check.execute(
                select(Transaction).where(Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value)
            )
            payments = list(result.scalars().all())

        assert len(payments) == 1
        assert payments[0].amount_kopeks == -PRICE


@pytest.mark.asyncio
async def test_tariff_purchase_failure_between_debit_and_commit_leaves_no_trace(monkeypatch, tmp_path):
    """Сбой покупки тарифа после списания тоже не оставляет следов."""
    async with _db(monkeypatch, tmp_path) as maker:
        original_end, promo_expires = await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
            _patch_extend_side_effects(monkeypatch)
            monkeypatch.setattr(
                pricing_engine,
                'calculate_tariff_purchase_price',
                AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=1000)),
            )
            monkeypatch.setattr(
                tariff_mod,
                'extend_subscription',
                AsyncMock(side_effect=RuntimeError('db down')),
                raising=False,
            )
            monkeypatch.setattr(tariff_mod, '_persist_failed_refund', AsyncMock(), raising=False)

            await inspect.unwrap(tariff_mod.confirm_tariff_purchase)(
                _callback(f'tariff_confirm:{TARIFF_ID}:30'),
                user,
                db,
                _state(),
            )

        async with maker() as check:
            fresh_user = await check.get(User, USER_ID)
            assert fresh_user is not None
            assert fresh_user.balance_kopeks == INITIAL_BALANCE
            assert fresh_user.promo_offer_discount_percent == 25
            assert fresh_user.promo_offer_discount_source == 'expired_discount_wave2'
            assert fresh_user.promo_offer_discount_expires_at == promo_expires

            sub = await check.get(Subscription, SUB_ID)
            assert sub is not None
            assert sub.end_date == original_end

        assert await _payment_count(maker) == 0


@pytest.mark.asyncio
async def test_tariff_purchase_renewal_keeps_post_commit_lava_shift(monkeypatch, tmp_path):
    """Покупка тарифа с продлением сохраняет сдвиг автосписания Lava после коммита."""
    async with _db(monkeypatch, tmp_path) as maker:
        await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
            _patch_extend_side_effects(monkeypatch)
            monkeypatch.setattr(
                pricing_engine,
                'calculate_tariff_purchase_price',
                AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=0)),
            )
            shift = AsyncMock()
            monkeypatch.setattr(
                'app.services.payment.lava.shift_lava_next_charge_after_manual_extension',
                shift,
            )

            await inspect.unwrap(tariff_mod.confirm_tariff_purchase)(
                _callback(f'tariff_confirm:{TARIFF_ID}:30'),
                user,
                db,
                _state(),
            )

            shift.assert_awaited_once()
            assert shift.await_args.args[1] == SUB_ID
            assert shift.await_args.args[2] == 30


@pytest.mark.asyncio
async def test_emitter_rollback_does_not_block_panel_or_referral(monkeypatch, tmp_path):
    """Сбой/rollback post-commit эмиттера не срывает панель и реферальный хук."""
    async with _db(monkeypatch, tmp_path) as maker:
        original_end, _ = await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            _patch_extend_side_effects(monkeypatch)
            monkeypatch.setattr(tariff_mod, 'AsyncSessionLocal', maker)
            seen: dict[str, bool] = {}

            async def failing_emit(side_db, transaction, **kwargs):
                seen['independent_session'] = side_db is not db
                await side_db.rollback()
                raise RuntimeError('promo group assignment failed')

            monkeypatch.setattr(tariff_mod, 'emit_transaction_side_effects', failing_emit)
            referral = AsyncMock()
            monkeypatch.setattr(
                'app.services.referral_purchase_service.register_first_purchase_after_purchase',
                referral,
            )
            monkeypatch.setattr(
                pricing_engine,
                'calculate_tariff_purchase_price',
                AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=0)),
            )

            await inspect.unwrap(tariff_mod.confirm_tariff_extend)(
                _callback(f'tariff_ext_confirm:{SUB_ID}:{TARIFF_ID}:30'),
                user,
                db,
                _state(),
            )

        panel_service = tariff_mod.SubscriptionService.return_value
        panel_service.update_remnawave_user.assert_awaited_once()
        referral.assert_awaited_once()
        assert seen['independent_session'] is True

        async with maker() as check:
            fresh_user = await check.get(User, USER_ID)
            assert fresh_user is not None
            assert fresh_user.balance_kopeks == INITIAL_BALANCE - PRICE
            sub = await check.get(Subscription, SUB_ID)
            assert sub is not None
            assert original_end + timedelta(days=30) - timedelta(seconds=1) <= sub.end_date
            assert sub.end_date <= original_end + timedelta(days=30) + timedelta(seconds=1)

        assert await _payment_count(maker) == 1


@pytest.mark.asyncio
async def test_classic_refund_after_rollback_restores_balance_and_promo(monkeypatch, tmp_path):
    """Классический возврат после rollback: пользователь по id, промо восстанавливается."""
    from app.database.crud.user import subtract_user_balance
    from app.handlers.subscription.purchase import _refund_failed_purchase

    async with _db(monkeypatch, tmp_path) as maker:
        await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            user_id = user.id
            assert await subtract_user_balance(db, user, PRICE, 'test charge', consume_promo_offer=True) is True

            # Rollback истекает ORM-объект — прежняя компенсация здесь молча падала.
            await db.rollback()

            refunded = await _refund_failed_purchase(
                db,
                user,
                PRICE,
                'Возврат: ошибка продления подписки',
                promo_state=(25, 'expired_discount_wave2', None),
                user_id=user_id,
            )
            assert refunded is True

        async with maker() as check:
            fresh_user = await check.get(User, USER_ID)
            assert fresh_user is not None
            assert fresh_user.balance_kopeks == INITIAL_BALANCE
            assert fresh_user.promo_offer_discount_percent == 25
            assert fresh_user.promo_offer_discount_source == 'expired_discount_wave2'


@pytest.mark.asyncio
async def test_admin_refund_after_rollback_restores_balance(monkeypatch, tmp_path):
    """Компенсация админской покупки после rollback: пользователь по id, не ORM-объект."""
    from app.database.crud.user import subtract_user_balance

    async with _db(monkeypatch, tmp_path) as maker:
        await _seed(maker)
        async with maker() as db:
            user = await db.get(User, USER_ID)
            user_id = user.id
            assert await subtract_user_balance(db, user, PRICE, 'test charge') is True

            # Rollback истекает ORM-объект — ровно так ломается прежняя компенсация.
            await db.rollback()

            refunded = await admin_users._refund_admin_purchase(  # type: ignore[arg-type]
                db,
                user_id,
                PRICE,
                'Возврат: ошибка покупки подписки (администратор)',
            )
            assert refunded is True

        async with maker() as check:
            fresh_user = await check.get(User, USER_ID)
            assert fresh_user is not None
            assert fresh_user.balance_kopeks == INITIAL_BALANCE

            result = await check.execute(
                select(Transaction).where(Transaction.type == TransactionType.REFUND.value)
            )
            refunds = list(result.scalars().all())

        assert len(refunds) == 1
        assert refunds[0].amount_kopeks == PRICE
