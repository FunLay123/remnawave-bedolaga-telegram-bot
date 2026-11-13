"""Wave-only claim path: original deadline, eligibility guards and exact UI.

The two expired-discount waves (`expired_discount_wave2` / `expired_discount_wave3`)
must not restart a fresh active-discount window at claim time, must reject a claim
after renewal, and must not overwrite a better personal discount that is already
active. Non-wave offers keep the pre-existing claim behaviour untouched.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from app.database.crud.discount_offer import lock_discount_offer_user_for_update
from app.database.models import DiscountOffer, PromoOfferLog, Subscription, User
from app.handlers.subscription import promo as promo_handler
from app.utils.timezone import get_local_timezone
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    DiscountOffer.__table__,
    PromoOfferLog.__table__,
)

WAVE2 = 'expired_discount_wave2'
WAVE3 = 'expired_discount_wave3'

WAVE_SUCCESS_TEMPLATE = (
    '<tg-emoji emoji-id="5303040337059543238">🎉</tg-emoji> <b>Скидка {percent}% активирована!</b>\n\n'
    'Она суммируется с другими скидками.\n'
    'Примените её при оплате до <b>{expires_at}</b>.'
)

WAVE_POPUP = 'Скидка активирована!'
WAVE_BETTER_ACTIVE = 'ℹ️ У вас уже действует скидка не меньше этой.'
WAVE_NOT_ELIGIBLE = 'ℹ️ Скидка недоступна: подписка уже не истекла.'
DISCOUNT_CLAIM_ERROR = '❌ Не удалось активировать скидку. Попробуйте позже.'


def _deadline(minutes: int = 1) -> datetime:
    return datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=minutes)


def _local_deadline(value: datetime) -> str:
    return value.astimezone(get_local_timezone()).strftime('%d.%m.%Y %H:%M')


def _callback(offer_id: int):
    return SimpleNamespace(
        data=f'claim_discount_{offer_id}',
        message=AsyncMock(),
        answer=AsyncMock(),
    )


async def _seed_claim(
    db,
    *,
    notification_type: str = WAVE2,
    offer_percent: int = 20,
    offer_expires_at: datetime | None = None,
    offer_is_active: bool = True,
    offer_claimed_at: datetime | None = None,
    subscription_status: str = 'expired',
    subscription_end: datetime | None = None,
    personal_percent: int = 0,
    personal_expires_at: datetime | None = None,
    extra_data: dict | None = None,
) -> tuple[User, DiscountOffer]:
    if subscription_end is None:
        subscription_end = datetime.now(UTC) - timedelta(days=1)
    if offer_expires_at is None:
        offer_expires_at = _deadline()

    user = User(
        id=1,
        telegram_id=100,
        username='wave-user',
        first_name='Волна',
        last_name='Тест',
        language='ru',
        promo_offer_discount_percent=personal_percent,
        promo_offer_discount_source='previous_offer' if personal_percent else None,
        promo_offer_discount_expires_at=personal_expires_at,
    )
    subscription = Subscription(
        id=1,
        user_id=1,
        status=subscription_status,
        is_trial=False,
        start_date=datetime.now(UTC) - timedelta(days=30),
        end_date=subscription_end,
        remnawave_short_id='short1',
    )
    offer = DiscountOffer(
        id=1,
        user_id=1,
        subscription_id=1,
        notification_type=notification_type,
        discount_percent=offer_percent,
        bonus_amount_kopeks=0,
        expires_at=offer_expires_at,
        claimed_at=offer_claimed_at,
        is_active=offer_is_active,
        effect_type='percent_discount',
        # A valid offer always carries the legacy template field; the wave claim
        # must ignore it instead of starting a fresh window from claim time.
        extra_data=extra_data
        if extra_data is not None
        else {'offer_type': 'extend_discount', 'active_discount_hours': 24},
    )
    db.add_all([user, subscription, offer])
    await db.commit()
    return user, offer


async def _reload_user(db) -> User:
    user = await db.get(User, 1)
    await db.refresh(user)
    return user


async def _reload_offer(db) -> DiscountOffer:
    return await db.get(DiscountOffer, 1)


class TestWaveClaimSuccess:
    @pytest.mark.asyncio
    async def test_one_minute_before_deadline_keeps_original_offer_deadline(self, monkeypatch):
        deadline = _deadline(minutes=1)

        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(
                db,
                notification_type=WAVE3,
                offer_percent=20,
                offer_expires_at=deadline,
            )
            callback = _callback(offer.id)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == 20
        assert user.promo_offer_discount_source == WAVE3
        assert user.promo_offer_discount_expires_at == deadline
        assert offer.claimed_at is not None
        assert offer.is_active is False

        callback.answer.assert_awaited_once_with(WAVE_POPUP, show_alert=True)

        callback.message.answer.assert_awaited_once()
        message_text = callback.message.answer.await_args.args[0]
        keyboard = callback.message.answer.await_args.kwargs['reply_markup']
        assert message_text == WAVE_SUCCESS_TEMPLATE.format(
            percent=20,
            expires_at=_local_deadline(deadline),
        )

        rows = keyboard.inline_keyboard
        assert [len(row) for row in rows] == [1, 1]

        purchase = rows[0][0]
        assert purchase.text == 'Купить подписку'
        assert purchase.style == 'primary'
        assert purchase.icon_custom_emoji_id == '5334552404736104314'
        assert purchase.callback_data == 'subscription_upgrade'

        main_menu = rows[1][0]
        assert main_menu.text == 'В главное меню'
        assert main_menu.style == 'primary'
        assert main_menu.icon_custom_emoji_id == '5314310000531766389'
        assert main_menu.callback_data == 'back_to_menu'


class TestWaveClaimRejections:
    @pytest.mark.asyncio
    async def test_after_offer_deadline_rejects_without_user_changes(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db, offer_expires_at=_deadline(minutes=-1))
            callback = _callback(offer.id)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_source is None
        assert user.promo_offer_discount_expires_at is None
        assert offer.claimed_at is None
        assert offer.is_active is False
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('active_percent', [50, 20])
    async def test_better_or_equal_active_personal_discount_is_not_overwritten(self, monkeypatch, active_percent):
        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(
                db,
                offer_percent=20,
                personal_percent=active_percent,
                personal_expires_at=_deadline(minutes=30),
            )
            callback = _callback(offer.id)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == active_percent
        assert user.promo_offer_discount_source == 'previous_offer'
        assert offer.claimed_at is None
        assert offer.is_active is True
        callback.answer.assert_awaited_once_with(WAVE_BETTER_ACTIVE, show_alert=True)
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_renewed_subscription_rejects_claim_without_user_changes(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(
                db,
                subscription_status='active',
                subscription_end=datetime.now(UTC) + timedelta(days=30),
            )
            callback = _callback(offer.id)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_expires_at is None
        assert offer.claimed_at is None
        assert offer.is_active is True
        callback.answer.assert_awaited_once_with(WAVE_NOT_ELIGIBLE, show_alert=True)
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deactivated_offer_rejects_claim(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db, offer_is_active=False)
            callback = _callback(offer.id)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == 0
        assert offer.claimed_at is None
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_claimed_offer_rejects_second_claim(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(
                db,
                offer_claimed_at=datetime.now(UTC) - timedelta(minutes=5),
                offer_is_active=False,
            )
            callback = _callback(offer.id)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)

        assert user.promo_offer_discount_percent == 0
        callback.message.answer.assert_not_awaited()


class TestRaceBoundaryReloads:
    @pytest.mark.asyncio
    async def test_locked_offer_reread_wins_over_stale_callback_offer(self, monkeypatch):
        """A renewal may deactivate the offer between the first read and the claim."""

        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)
            callback = _callback(offer.id)
            calls: list[int] = []

            stale = SimpleNamespace(
                id=offer.id,
                user_id=user.id,
                subscription_id=1,
                notification_type=WAVE2,
                discount_percent=20,
                claimed_at=None,
                is_active=False,
                expires_at=offer.expires_at,
                effect_type='percent_discount',
                extra_data={},
            )

            async def fake_get_offer_for_update(session, offer_id):
                calls.append(offer_id)
                return stale

            monkeypatch.setattr(promo_handler, 'get_offer_by_id_for_update', fake_get_offer_for_update)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)

        assert calls == [offer.id]
        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_expires_at is None
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_locked_subscription_reread_wins_over_stale_relationship(self, monkeypatch):
        """A renewal may activate the subscription after the offer's first read."""

        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)
            callback = _callback(offer.id)

            async def fake_get_subscription_for_update(session, subscription_id):
                return SimpleNamespace(
                    id=subscription_id,
                    user_id=user.id,
                    status='active',
                    actual_status='active',
                    is_active=True,
                )

            monkeypatch.setattr(promo_handler, 'get_offer_subscription_for_update', fake_get_subscription_for_update)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_expires_at is None
        assert offer.claimed_at is None
        callback.message.answer.assert_not_awaited()


class TestLockOrder:
    @pytest.mark.asyncio
    async def test_wave_claim_locks_subscription_before_user_and_offer(self, monkeypatch):
        """Other flows hold Subscription then User; the claim must use the same order."""

        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)
            callback = _callback(offer.id)
            order: list[str] = []

            async def fake_get_subscription_for_update(session, subscription_id):
                order.append('subscription')
                return SimpleNamespace(
                    id=subscription_id,
                    user_id=user.id,
                    actual_status='expired',
                    is_active=False,
                )

            async def fake_lock_user(session, user_id):
                order.append('user')
                return SimpleNamespace(
                    id=user_id,
                    promo_offer_discount_percent=0,
                    promo_offer_discount_source=None,
                    promo_offer_discount_expires_at=None,
                )

            async def fake_get_offer_for_update(session, offer_id):
                order.append('offer')
                return SimpleNamespace(
                    id=offer_id,
                    user_id=user.id,
                    subscription_id=1,
                    notification_type=WAVE2,
                    discount_percent=20,
                    claimed_at=None,
                    is_active=False,
                    expires_at=_deadline(),
                    effect_type='percent_discount',
                    extra_data={},
                )

            monkeypatch.setattr(promo_handler, 'get_offer_subscription_for_update', fake_get_subscription_for_update)
            monkeypatch.setattr(promo_handler, 'lock_discount_offer_user_for_update', fake_lock_user)
            monkeypatch.setattr(promo_handler, 'get_offer_by_id_for_update', fake_get_offer_for_update)

            await promo_handler.claim_discount_offer(callback, user, db)

        assert order == ['subscription', 'user', 'offer']
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_offer_repointed_to_another_subscription_is_rejected(self, monkeypatch):
        """A locked offer must still point at the exact subscription that was locked."""

        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)
            callback = _callback(offer.id)

            repointed = SimpleNamespace(
                id=offer.id,
                user_id=user.id,
                subscription_id=999,
                notification_type=WAVE2,
                discount_percent=20,
                claimed_at=None,
                is_active=True,
                expires_at=_deadline(),
                effect_type='percent_discount',
                extra_data={},
            )

            async def fake_get_offer_for_update(session, offer_id):
                return repointed

            monkeypatch.setattr(promo_handler, 'get_offer_by_id_for_update', fake_get_offer_for_update)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_expires_at is None
        assert offer.claimed_at is None
        assert offer.is_active is True
        callback.answer.assert_awaited_once_with(WAVE_NOT_ELIGIBLE, show_alert=True)
        callback.message.answer.assert_not_awaited()


class _CapturingSession:
    def __init__(self):
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: None)


class TestUserLockNowait:
    @pytest.mark.asyncio
    async def test_user_lock_sql_uses_for_update_nowait(self):
        session = _CapturingSession()

        await lock_discount_offer_user_for_update(session, 42)

        sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
        assert 'FOR UPDATE NOWAIT' in sql

    @pytest.mark.asyncio
    async def test_lock_not_available_rolls_back_and_shows_existing_error(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)
            callback = _callback(offer.id)
            order: list[str] = []
            original_rollback = db.rollback

            async def fake_get_subscription_for_update(session, subscription_id):
                order.append('subscription')
                return SimpleNamespace(
                    id=subscription_id,
                    user_id=user.id,
                    actual_status='expired',
                    is_active=False,
                )

            async def fake_lock_user(session, user_id):
                order.append('user-lock')
                # Real asyncpg 55P03 is wrapped by SQLAlchemy as the
                # DBAPIError base class, not OperationalError.
                raise DBAPIError(
                    'SELECT ... FOR UPDATE NOWAIT',
                    {},
                    SimpleNamespace(sqlstate='55P03', pgcode='55P03'),
                )

            async def tracked_rollback():
                order.append('rollback')
                await original_rollback()

            monkeypatch.setattr(promo_handler, 'get_offer_subscription_for_update', fake_get_subscription_for_update)
            monkeypatch.setattr(promo_handler, 'lock_discount_offer_user_for_update', fake_lock_user)
            monkeypatch.setattr(db, 'rollback', tracked_rollback)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert order == ['subscription', 'user-lock', 'rollback']
        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_expires_at is None
        assert offer.claimed_at is None
        assert offer.is_active is True
        callback.answer.assert_awaited_once_with(DISCOUNT_CLAIM_ERROR, show_alert=True)
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_operational_errors_are_reraised(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)
            callback = _callback(offer.id)

            async def fake_lock_user(session, user_id):
                raise DBAPIError(
                    'connection reset',
                    {},
                    SimpleNamespace(sqlstate='08006', pgcode='08006'),
                )

            monkeypatch.setattr(promo_handler, 'lock_discount_offer_user_for_update', fake_lock_user)

            with pytest.raises(DBAPIError):
                await promo_handler.claim_discount_offer(callback, user, db)

        callback.answer.assert_not_awaited()
        callback.message.answer.assert_not_awaited()


class TestAtomicNoDowngradeGuard:
    @pytest.mark.asyncio
    async def test_wave_claim_locks_user_row_before_comparing_discounts(self, monkeypatch):
        """The guard must use a locked fresh user, not the middleware object."""

        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)
            callback = _callback(offer.id)
            lock_calls: list[int] = []

            locked_user = SimpleNamespace(
                id=user.id,
                promo_offer_discount_percent=50,
                promo_offer_discount_source='other_claim',
                promo_offer_discount_expires_at=_deadline(minutes=30),
            )

            async def fake_lock_user(session, user_id):
                lock_calls.append(user_id)
                return locked_user

            monkeypatch.setattr(promo_handler, 'lock_discount_offer_user_for_update', fake_lock_user)

            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert lock_calls == [1]
        assert user.promo_offer_discount_percent == 0
        assert user.promo_offer_discount_expires_at is None
        assert offer.claimed_at is None
        callback.answer.assert_awaited_once_with(WAVE_BETTER_ACTIVE, show_alert=True)
        callback.message.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_identity_map_user_sees_better_discount_committed_elsewhere(self, monkeypatch):
        """A discount committed through another write must win over a stale session object."""

        better_deadline = _deadline(minutes=30)

        async with memory_session(monkeypatch, TABLES) as db:
            user, offer = await _seed_claim(db)

            # Commit through a bypassing UPDATE so the identity-map object passed
            # to the handler stays stale, exactly like a user loaded earlier.
            await db.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    promo_offer_discount_percent=50,
                    promo_offer_discount_source='other_claim',
                    promo_offer_discount_expires_at=better_deadline,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            assert user.promo_offer_discount_percent == 0

            callback = _callback(offer.id)
            await promo_handler.claim_discount_offer(callback, user, db)

            user = await _reload_user(db)
            offer = await _reload_offer(db)

        assert user.promo_offer_discount_percent == 50
        assert user.promo_offer_discount_source == 'other_claim'
        assert user.promo_offer_discount_expires_at == better_deadline
        assert offer.claimed_at is None
        callback.answer.assert_awaited_once_with(WAVE_BETTER_ACTIVE, show_alert=True)
        callback.message.answer.assert_not_awaited()


class TestNonWaveClaimUnchanged:
    @pytest.mark.asyncio
    async def test_non_wave_offer_on_active_subscription_uses_existing_branch(self, monkeypatch):
        user = SimpleNamespace(
            id=1,
            language='ru',
            promo_offer_discount_percent=0,
            promo_offer_discount_source=None,
            promo_offer_discount_expires_at=None,
            updated_at=None,
            subscriptions=[SimpleNamespace(is_active=True, is_trial=False)],
        )
        offer = SimpleNamespace(
            id=7,
            user_id=1,
            subscription_id=1,
            notification_type='winback_discount',
            discount_percent=15,
            bonus_amount_kopeks=0,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            claimed_at=None,
            is_active=True,
            effect_type='percent_discount',
            extra_data={'active_discount_hours': 5},
        )
        claims: list[dict | None] = []

        async def fake_get_offer(db, offer_id):
            return offer

        async def fake_mark_claimed(db, target_offer, *, details=None):
            claims.append(details)
            target_offer.claimed_at = datetime.now(UTC)
            target_offer.is_active = False
            return target_offer

        monkeypatch.setattr(promo_handler, 'get_offer_by_id', fake_get_offer)
        monkeypatch.setattr(promo_handler, 'mark_offer_claimed', fake_mark_claimed)

        callback = _callback(offer.id)
        db = AsyncMock()

        await promo_handler.claim_discount_offer(callback, user, db)

        assert user.promo_offer_discount_percent == 15
        assert user.promo_offer_discount_source == 'winback_discount'
        assert user.promo_offer_discount_expires_at is not None
        assert offer.claimed_at is not None
        assert claims and claims[0]['context'] == 'discount_claim'
        db.refresh.assert_awaited_once_with(user)
        callback.answer.assert_awaited_once_with('✅ Скидка активирована!', show_alert=True)
        callback.message.answer.assert_awaited_once()
