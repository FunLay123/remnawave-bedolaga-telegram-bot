from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.models import (
    Base,
    ReferralEarning,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
)
from app.services.referral_purchase_service import (
    backfill_first_purchase_markers,
    ensure_marker_for_already_paid_referral,
    has_first_purchase_marker,
    has_paid_subscription_before_attach,
)
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, referred_by_id: int | None = None, *, has_had_paid_subscription: bool = False) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referred_by_id=referred_by_id,
        has_had_paid_subscription=has_had_paid_subscription,
    )


def _payment(
    transaction_id: int,
    user_id: int,
    *,
    amount_kopeks: int = -10000,
    description: str | None = 'Оплата подписки',
    is_completed: bool = True,
    tx_type: TransactionType = TransactionType.SUBSCRIPTION_PAYMENT,
) -> Transaction:
    return Transaction(
        id=transaction_id,
        user_id=user_id,
        type=tx_type.value,
        amount_kopeks=amount_kopeks,
        description=description,
        is_completed=is_completed,
    )


def _subscription(
    sub_id: int,
    user_id: int,
    *,
    status: str = SubscriptionStatus.ACTIVE.value,
    is_trial: bool = False,
    start_offset_days: int = -1,
    end_offset_days: int = 30,
) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sub{sub_id}',
        status=status,
        is_trial=is_trial,
        start_date=now + timedelta(days=start_offset_days),
        end_date=now + timedelta(days=end_offset_days),
        traffic_limit_gb=100,
        device_limit=1,
    )


def _paid_sub(sub_id: int, user_id: int) -> Subscription:
    return _subscription(sub_id, user_id)


@pytest.mark.asyncio
async def test_marker_is_written_once_for_a_paid_referral(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _paid_sub(10, 2),
                _payment(100, 2, amount_kopeks=-10000),
            ]
        )
        await db.commit()

        assert await has_first_purchase_marker(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await has_first_purchase_marker(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is False

        rows = (
            (
                await db.execute(
                    select(ReferralEarning).where(
                        ReferralEarning.user_id == 2,
                        ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].days_granted == 0
        assert rows[0].amount_kopeks == 0


@pytest.mark.asyncio
async def test_backfill_is_idempotent(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3, referred_by_id=1),
                _paid_sub(10, 2),
                _payment(100, 2, amount_kopeks=-10000),
            ]
        )
        await db.commit()

        assert await backfill_first_purchase_markers(db) == 1
        assert await backfill_first_purchase_markers(db) == 0


@pytest.mark.asyncio
async def test_pending_subscription_does_not_write_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _subscription(10, 2, status=SubscriptionStatus.PENDING.value),
            ]
        )
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_expired_zero_duration_placeholder_does_not_write_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _subscription(
                    10,
                    2,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=0,
                    end_offset_days=0,
                ),
            ]
        )
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_expired_subscription_with_real_payment_writes_marker(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _payment(100, 2, amount_kopeks=-10000),
                _subscription(
                    10,
                    2,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=-60,
                    end_offset_days=-30,
                ),
            ]
        )
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
@pytest.mark.parametrize('scheme', ['legacy', 'levels'])
async def test_non_days_only_scheme_writes_no_marker(monkeypatch, scheme):
    """Маркер принадлежит только схеме «только дни».

    В legacy/levels оплата уже учитывается деньгами и флагами пополнения;
    маркер там исказил бы счётчик оплативших после переключения схемы.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', scheme)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _paid_sub(10, 2),
                _payment(100, 2, amount_kopeks=-10000),
            ]
        )
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False
        assert (await db.execute(select(ReferralEarning))).scalars().all() == []


@pytest.mark.asyncio
async def test_backfill_marks_paid_flags_and_payments_not_subscriptions(monkeypatch):
    """Бэкфилл смотрит на реальную транзакцию оплаты, а не на подписку.

    Подписки без оплаты (подарок, черновик, СБП-заготовка) кандидатами не
    считаются: иначе они лишили бы человека настоящей награды за первую покупку.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3, referred_by_id=1),
                _user(4, referred_by_id=1),
                _user(5, referred_by_id=1),
                _user(6, referred_by_id=1),
                _subscription(10, 2, status=SubscriptionStatus.PENDING.value),
                _subscription(
                    11,
                    3,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=0,
                    end_offset_days=0,
                ),
                _subscription(
                    12,
                    4,
                    status=SubscriptionStatus.EXPIRED.value,
                    start_offset_days=-60,
                    end_offset_days=-30,
                ),
                _subscription(13, 5, status=SubscriptionStatus.ACTIVE.value),
                _payment(100, 4, amount_kopeks=-10000),
                _payment(101, 5, amount_kopeks=-5000),
                _payment(102, 6, amount_kopeks=10000),
            ]
        )
        await db.commit()

        assert await backfill_first_purchase_markers(db) == 3

        marked_user_ids = set((await db.execute(select(ReferralEarning.user_id))).scalars().all())
        assert marked_user_ids == {4, 5, 6}
        assert await backfill_first_purchase_markers(db) == 0


@pytest.mark.asyncio
async def test_non_trial_gift_subscription_without_payment_is_not_paid(monkeypatch):
    """Нетриальная подписка без флага и транзакции — подарок, а не оплата."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_sub(10, 2)])
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_paid_flag_without_transaction_is_not_paid(monkeypatch):
    """Флаг сам по себе не доказательство оплаты.

    Его выставляют купон, админская конверсия trial→paid и удаление любой
    нетриальной подписки, поэтому оплатой считается только реальная
    завершённая транзакция ``SUBSCRIPTION_PAYMENT``.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1, has_had_paid_subscription=True)])
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_completed_subscription_payment_marks_referral(monkeypatch):
    """Провайдерная оплата могла не выставить флаг — её видно по транзакции."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _payment(100, 2, amount_kopeks=-10000)])
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_legacy_positive_subscription_payment_marks_referral(monkeypatch):
    """Старые/провайдерные строки могли хранить сумму положительной."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _payment(100, 2, amount_kopeks=10000)])
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is True


@pytest.mark.asyncio
async def test_incomplete_subscription_payment_is_not_paid(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _payment(100, 2, is_completed=False)])
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'description',
    ['Докупить трафик на 10 ГБ', 'Добавить устройство в подписку'],
)
async def test_addon_subscription_payment_is_not_a_subscription_payment(monkeypatch, description):
    """Допы переиспользуют SUBSCRIPTION_PAYMENT, но оплатой подписки не являются."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [_user(1), _user(2, referred_by_id=1), _payment(100, 2, amount_kopeks=-5000, description=description)]
        )
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'description',
    [
        'Суточная оплата тарифа «Базовый»',
        'Покупка суточного тарифа Базовый (первый день)',
        'Смена на суточный тариф Базовый (первый день)',
        'Переключение на суточный тариф Базовый (первый день)',
        # Проротные переключения на daily из бота и мини-аппа.
        'Переключение на суточный тариф Базовый (доплата за 10 дней)',
        'Переход на суточный тариф Базовый (доплата за 10 дней)',
        "Переход на суточный тариф 'Базовый'",
    ],
)
async def test_daily_charge_is_not_a_subscription_payment(monkeypatch, description):
    """Daily-списание переиспользует SUBSCRIPTION_PAYMENT, но покупкой не является.

    Иначе первый день суточного тарифа навсегда записал бы маркер и сжёг бонус
    за настоящую первую покупку. Проверяются все daily-пути: списание,
    возобновление, покупка, смена и переключение тарифа.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [_user(1), _user(2, referred_by_id=1), _payment(100, 2, amount_kopeks=-1000, description=description)]
        )
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_miniapp_daily_activation_is_not_a_subscription_payment(monkeypatch):
    """Литерал мини-аппа «Активация суточного тарифа '…' (первый день)».

    Отдельный тест фиксирует конкретный путь ``app/webapi/routes/miniapp.py``:
    он попадает под ``%суточного тарифа%``, но покрыт явно, чтобы новый
    daily-путь не остался незамеченным.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _payment(
                    100,
                    2,
                    amount_kopeks=-1000,
                    description="Активация суточного тарифа 'Базовый' (первый день)",
                ),
            ]
        )
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is False
        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_regular_subscription_payment_with_description_marks_referral(monkeypatch):
    """Обычное описание оплаты по-прежнему считается покупкой."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _payment(100, 2, description='Оплата подписки')])
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_subscription_payment_without_description_marks_referral(monkeypatch):
    """NULL-описание не должно отсекаться отрицанием addon-условия."""
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _payment(100, 2, description=None)])
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'description',
    [
        'Переключение на тариф Базовый',
        "Переход на тариф 'Базовый' (доплата за 10 дней)",
    ],
)
async def test_non_daily_tariff_switch_is_still_a_subscription_payment(monkeypatch, description):
    """Переключение на НЕ-суточный тариф остаётся покупкой.

    Новые шаблоны отсекают только переключения на суточный тариф; широкие
    ``%Переход на тариф%``/``%Переключение на тариф%`` сюда не добавлялись,
    иначе настоящая первая покупка через смену тарифа перестала бы считаться.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [_user(1), _user(2, referred_by_id=1), _payment(100, 2, amount_kopeks=-1000, description=description)]
        )
        await db.commit()

        assert await has_paid_subscription_before_attach(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is True


@pytest.mark.asyncio
@pytest.mark.parametrize('scheme', ['legacy', 'levels'])
async def test_backfill_refuses_without_queries_on_non_days_only_scheme(monkeypatch, scheme):
    """Маркеры принадлежат только схеме days_only: вне неё бэкфилл молча не работает.

    Запросов быть не должно вовсе: иначе на чужой схеме в ledger попали бы
    строки, которые ни одна ветка наград не читает.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', scheme)

    async def explode(*_args, **_kwargs):
        raise AssertionError('бэкфилл не должен ходить в базу вне схемы days_only')

    db = SimpleNamespace(execute=explode, commit=explode)

    assert await backfill_first_purchase_markers(db) == 0


@pytest.mark.asyncio
async def test_backfill_prefetches_markers_in_one_query(monkeypatch):
    """Число SELECT'ов не зависит от числа кандидатов: маркеры читаются одним запросом."""
    from sqlalchemy import event

    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3, referred_by_id=1),
                _user(4, referred_by_id=1),
                _payment(100, 2, amount_kopeks=-10000),
                _payment(101, 3, amount_kopeks=-10000),
                _payment(102, 4, amount_kopeks=-10000),
            ]
        )
        await db.commit()

        selects = []

        def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                selects.append(statement)

        event.listen(db.bind.sync_engine, 'before_cursor_execute', before_cursor_execute)
        try:
            assert await backfill_first_purchase_markers(db) == 3
        finally:
            event.remove(db.bind.sync_engine, 'before_cursor_execute', before_cursor_execute)

        assert len(selects) == 2, selects


@pytest.mark.asyncio
async def test_backfill_skips_conflicting_candidate_and_creates_rest(monkeypatch):
    """Конкурентный маркер после bulk-проверки не отменяет остальные строки.

    Живая покупка могла записать маркер между prefetch-запросом и INSERT.
    SAVEPOINT откатывает только конфликтовавшего кандидата: остальные маркеры
    коммитятся, а ``created`` не считает откаченную строку.
    """
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3, referred_by_id=1),
                _user(4, referred_by_id=1),
                _payment(100, 2, amount_kopeks=-10000),
                _payment(101, 3, amount_kopeks=-10000),
                _payment(102, 4, amount_kopeks=-10000),
            ]
        )
        await db.commit()

        real_add = db.add

        def add_with_conflict(obj):
            # Имитируем вставку, упёршуюся в партиальный UNIQUE-индекс маркера.
            if getattr(obj, 'user_id', None) == 3:
                raise IntegrityError('INSERT referral_earnings', None, Exception('duplicate key value'))
            return real_add(obj)

        monkeypatch.setattr(db, 'add', add_with_conflict)

        assert await backfill_first_purchase_markers(db) == 2

        marked_user_ids = set((await db.execute(select(ReferralEarning.user_id))).scalars().all())
        assert marked_user_ids == {2, 4}
