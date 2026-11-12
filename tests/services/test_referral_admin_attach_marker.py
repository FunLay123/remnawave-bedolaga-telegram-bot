"""Админские поздние привязки: маркер первой покупки для уже плативших.

``UserService.update_user_referrals`` и
``ReferralDiagnosticsService.fix_lost_referrals`` проставляют ``referred_by_id``
в обход обычного регистрационного пути. Если у человека к этому моменту уже
была оплата, маркер обязан появиться сразу — иначе следующая покупка принесёт
награду за «первую».
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import Base, ReferralEarning, Transaction, TransactionType, User
from app.services.referral_diagnostics_service import LostReferral, ReferralDiagnosticsService
from app.services.referral_purchase_service import (
    ensure_marker_for_already_paid_referral,
    ensure_marker_for_already_paid_referral_in_new_session,
    has_first_purchase_marker,
)
from app.services.referral_reward_service import REASON_FIRST_PURCHASE_REGISTERED
from app.services.referral_service import process_referral_registration
from app.services.user_service import UserService
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


def _paid_transaction(transaction_id: int, user_id: int, *, description: str = 'Оплата подписки') -> Transaction:
    return Transaction(
        id=transaction_id,
        user_id=user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT.value,
        amount_kopeks=-10000,
        description=description,
        is_completed=True,
    )


def _daily_transaction(transaction_id: int, user_id: int) -> Transaction:
    return _paid_transaction(
        transaction_id,
        user_id,
        description='Суточная оплата тарифа «Базовый»',
    )


def _lost(user: User) -> LostReferral:
    return LostReferral(
        telegram_id=user.telegram_id,
        username=user.username,
        full_name=user.first_name,
        referral_code='refABC12345',
        expected_referrer_code='refREF00000',
        expected_referrer_id=1,
        expected_referrer_name='Referrer',
        click_time=datetime.now(UTC),
        registered=True,
        has_referrer=False,
        current_referrer_id=None,
    )


def _enable_days_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'days_only')


def _use_test_session_factory(monkeypatch, db) -> None:
    """Marker-helper в отдельной сессии должен смотреть в ту же in-memory SQLite."""
    monkeypatch.setattr(
        'app.database.database.AsyncSessionLocal',
        async_sessionmaker(db.bind, expire_on_commit=False),
    )


async def _reload_user(db, user_id: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    return result.scalar_one()


async def _marker_rows(db) -> list[ReferralEarning]:
    result = await db.execute(select(ReferralEarning).where(ReferralEarning.reason == REASON_FIRST_PURCHASE_REGISTERED))
    return list(result.scalars().all())


def _marker_row(user_id: int, referrer_id: int) -> ReferralEarning:
    return ReferralEarning(
        user_id=user_id,
        referral_id=referrer_id,
        amount_kopeks=0,
        reason=REASON_FIRST_PURCHASE_REGISTERED,
        reward_type='days',
        level=1,
        days_granted=0,
    )


@pytest.mark.asyncio
async def test_admin_update_marks_paid_referral_and_skips_unpaid(monkeypatch):
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2), _user(3), _paid_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        ok, stats = await UserService().update_user_referrals(db, 1, [2, 3], admin_id=1)

        assert ok is True
        assert stats['added'] == 2

        assert (await _reload_user(db, 2)).referred_by_id == 1
        assert (await _reload_user(db, 3)).referred_by_id == 1
        assert await has_first_purchase_marker(db, 2) is True
        assert await has_first_purchase_marker(db, 3) is False


@pytest.mark.asyncio
async def test_admin_update_skips_marker_helper_for_already_marked_users(monkeypatch):
    """Уже помеченный реферал не должен открывать отдельную сессию.

    Прежний цикл вызывал helper для каждого ``to_assign``: N уже помеченных
    рефералов давали N лишних подключений и SELECT'ов, каждый из которых всё
    равно возвращал «маркер уже есть». Существующие маркеры читаются одним
    запросом в текущей сессии, и helper зовётся только для непомеченных.
    """
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2, referred_by_id=1),
                _user(3),
                _marker_row(2, 1),
                _paid_transaction(100, 3),
            ]
        )
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        import app.services.referral_purchase_service as purchase_module

        batch_calls: list[list[int]] = []

        async def batch_spy(user_ids):
            batch_calls.append(list(user_ids))
            return len(batch_calls[-1])

        monkeypatch.setattr(
            purchase_module,
            'ensure_markers_for_already_paid_referrals_in_new_session',
            batch_spy,
        )

        ok, stats = await UserService().update_user_referrals(db, 1, [2, 3], admin_id=1)

        assert ok is True
        assert stats['added'] == 1
        assert batch_calls == [[3]], 'одна batch-сессия, только непомеченные'
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_admin_update_does_not_mark_removed_referral(monkeypatch):
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        ok, stats = await UserService().update_user_referrals(db, 1, [], admin_id=1)

        assert ok is True
        assert stats['removed'] == 1
        assert (await _reload_user(db, 2)).referred_by_id is None
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_fix_lost_referrals_preview_writes_no_markers(monkeypatch):
    _enable_days_only(monkeypatch)
    service = ReferralDiagnosticsService(log_path='/nonexistent/referral-diagnostics.log')
    async with memory_session(monkeypatch, TABLES) as db:
        paid = _user(2)
        unpaid = _user(3)
        db.add_all([_user(1), paid, unpaid, _paid_transaction(100, 2)])
        await db.commit()

        report = await service.fix_lost_referrals(db, [_lost(paid), _lost(unpaid)], apply=False)

        assert report.users_fixed == 2
        assert (await _reload_user(db, 2)).referred_by_id is None
        assert (await _reload_user(db, 3)).referred_by_id is None
        assert await has_first_purchase_marker(db, 2) is False
        assert await has_first_purchase_marker(db, 3) is False


@pytest.mark.asyncio
async def test_fix_lost_referrals_apply_marks_paid_users_only(monkeypatch):
    _enable_days_only(monkeypatch)
    service = ReferralDiagnosticsService(log_path='/nonexistent/referral-diagnostics.log')
    async with memory_session(monkeypatch, TABLES) as db:
        paid = _user(2)
        unpaid = _user(3)
        db.add_all([_user(1), paid, unpaid, _paid_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        report = await service.fix_lost_referrals(db, [_lost(paid), _lost(unpaid)], apply=True)

        assert report.users_fixed == 2
        assert (await _reload_user(db, 2)).referred_by_id == 1
        assert (await _reload_user(db, 3)).referred_by_id == 1
        assert await has_first_purchase_marker(db, 2) is True
        assert await has_first_purchase_marker(db, 3) is False


@pytest.mark.asyncio
async def test_marker_failure_keeps_committed_attach_and_caller_session_usable(monkeypatch):
    """Сбой маркера не откатывает привязку и не экспайрит объекты caller'а.

    ``admin/users.py`` после вызова читает ``db_user.language``; полный
    rollback внутри сервиса экспайрил объект админа и давал MissingGreenlet.
    """
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        admin = _user(1)
        db.add_all([admin, _user(2), _paid_transaction(100, 2)])
        await db.commit()

        async def _boom(user_ids):
            raise RuntimeError(f'marker boom for {list(user_ids)}')

        monkeypatch.setattr(
            'app.services.referral_purchase_service.ensure_markers_for_already_paid_referrals_in_new_session',
            _boom,
        )

        ok, stats = await UserService().update_user_referrals(db, 1, [2], admin_id=1)

        assert ok is True
        assert stats['added'] == 1
        # Объект caller'а должен остаться загруженным — без MissingGreenlet.
        assert admin.language == 'ru'
        # Привязка уже закоммичена и не откатывается сбоем маркера.
        assert (await _reload_user(db, 2)).referred_by_id == 1
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_fix_lost_referrals_retries_marker_for_unchanged_attachment(monkeypatch):
    """Повторный apply дописывает ранее упавший маркер уже привязанному пользователю.

    Привязка не меняется (``users_fixed == 0``), но маркер-хелпер вызывается для
    всех обработанных пользователей с ``referred_by_id``: первый прогон мог
    упасть уже после commit привязки, и без ретрая покупка осталась бы без
    отметки, а следующая покупка получила бы награду за «первую».
    """
    _enable_days_only(monkeypatch)
    service = ReferralDiagnosticsService(log_path='/nonexistent/referral-diagnostics.log')
    async with memory_session(monkeypatch, TABLES) as db:
        paid = _user(2, referred_by_id=1)
        db.add_all([_user(1), paid, _paid_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        import app.services.referral_purchase_service as purchase_module

        real_marker = purchase_module.ensure_marker_for_already_paid_referral_in_new_session

        async def failing_marker(user_id):
            raise RuntimeError(f'marker boom for {user_id}')

        monkeypatch.setattr(
            purchase_module,
            'ensure_marker_for_already_paid_referral_in_new_session',
            failing_marker,
        )
        first = await service.fix_lost_referrals(db, [_lost(paid)], apply=True)

        assert first.users_fixed == 0
        assert await has_first_purchase_marker(db, 2) is False

        monkeypatch.setattr(
            purchase_module,
            'ensure_marker_for_already_paid_referral_in_new_session',
            real_marker,
        )
        second = await service.fix_lost_referrals(db, [_lost(paid)], apply=True)

        assert second.users_fixed == 0
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_new_session_helper_writes_marker_in_separate_session(monkeypatch):
    """Marker-helper пишет через AsyncSessionLocal, не через request-сессию."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        assert await ensure_marker_for_already_paid_referral_in_new_session(2) is True

        # Результат, записанный отдельной сессией, виден request-сессии.
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_process_registration_marks_paid_late_attach(monkeypatch):
    """Общая точка поздних привязок пишет маркер даже без bot.

    ``process_referral_registration`` вызывают кампания, /start и кабинет.
    Проверка «уже платил» раньше жила только в ветке уведомлений (``if bot``),
    поэтому привязка из кампании/кабинета оставляла оплату без маркера, и
    следующая покупка могла получить награду за «первую».
    """
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        result = await process_referral_registration(db, 2, 1)

        assert result is True
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_process_registration_does_not_mark_unpaid_attach(monkeypatch):
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(3, referred_by_id=1)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        result = await process_referral_registration(db, 3, 1)

        assert result is True
        assert await has_first_purchase_marker(db, 3) is False


@pytest.mark.asyncio
async def test_admin_update_does_not_mark_daily_only_payer(monkeypatch):
    """Daily-списания — не покупка: поздняя привязка маркер не пишет."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2), _daily_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        ok, stats = await UserService().update_user_referrals(db, 1, [2], admin_id=1)

        assert ok is True
        assert stats['added'] == 1
        assert (await _reload_user(db, 2)).referred_by_id == 1
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_fix_lost_referrals_apply_does_not_mark_daily_only_payer(monkeypatch):
    _enable_days_only(monkeypatch)
    service = ReferralDiagnosticsService(log_path='/nonexistent/referral-diagnostics.log')
    async with memory_session(monkeypatch, TABLES) as db:
        daily_payer = _user(2)
        db.add_all([_user(1), daily_payer, _daily_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        report = await service.fix_lost_referrals(db, [_lost(daily_payer)], apply=True)

        assert report.users_fixed == 1
        assert (await _reload_user(db, 2)).referred_by_id == 1
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_process_registration_does_not_mark_daily_only_payer(monkeypatch):
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _daily_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        result = await process_referral_registration(db, 2, 1)

        assert result is True
        assert await has_first_purchase_marker(db, 2) is False


@pytest.mark.asyncio
async def test_ensure_marker_returns_false_when_marker_exists(monkeypatch):
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        assert await ensure_marker_for_already_paid_referral(db, 2) is True
        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert len(await _marker_rows(db)) == 1


@pytest.mark.asyncio
async def test_unique_index_rejects_second_marker_row(monkeypatch):
    """Структурная защита: вторая строка маркера не проходит на уровне БД."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()
        assert await ensure_marker_for_already_paid_referral(db, 2) is True

        db.add(_marker_row(2, 1))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        assert len(await _marker_rows(db)) == 1


@pytest.mark.asyncio
async def test_ensure_marker_swallows_unique_conflict(monkeypatch):
    """Гонка после SELECT: вставка упирается в индекс, наружу не падает."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()
        assert await ensure_marker_for_already_paid_referral(db, 2) is True

        import app.services.referral_purchase_service as purchase_module

        # Имитируем конкурентную сессию: проверка не увидела уже закоммиченную
        # строку, и INSERT доходит до уникального индекса.
        monkeypatch.setattr(purchase_module, 'has_first_purchase_marker', AsyncMock(return_value=False))

        assert await ensure_marker_for_already_paid_referral(db, 2) is False
        assert len(await _marker_rows(db)) == 1
        # После rollback сессия остаётся рабочей.
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_ensure_marker_unique_conflict_emits_stable_debug_event(monkeypatch):
    """Проигранная гонка вставки маркера логируется стабильным событием."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()
        assert await ensure_marker_for_already_paid_referral(db, 2) is True

        import app.services.referral_purchase_service as purchase_module

        debug_calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            purchase_module,
            'logger',
            SimpleNamespace(debug=lambda message, **kwargs: debug_calls.append((message, kwargs))),
        )
        # Имитируем конкурентную сессию: проверка не увидела уже закоммиченную
        # строку, и INSERT доходит до уникального индекса.
        monkeypatch.setattr(purchase_module, 'has_first_purchase_marker', AsyncMock(return_value=False))

        assert await ensure_marker_for_already_paid_referral(db, 2) is False

    assert debug_calls == [
        ('Referral first-purchase marker insert raced, marker already exists', {'user_id': 2}),
    ]


@pytest.mark.asyncio
async def test_process_registration_retries_marker_after_failure(monkeypatch):
    """Повторная обработка после сбоя маркера дописывает его без дублей уведомлений."""
    _enable_days_only(monkeypatch)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)
    bot = SimpleNamespace(send_message=AsyncMock())

    import app.services.referral_purchase_service as purchase_module

    real_marker = purchase_module.ensure_marker_for_already_paid_referral_in_new_session

    async def failing_marker(user_id):
        raise RuntimeError(f'marker boom for {user_id}')

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        monkeypatch.setattr(
            purchase_module,
            'ensure_marker_for_already_paid_referral_in_new_session',
            failing_marker,
        )
        assert await process_referral_registration(db, 2, 1, bot) is True
        assert await has_first_purchase_marker(db, 2) is False
        sent_after_first = bot.send_message.await_count
        assert sent_after_first == 2

        # Ретрай: якорь уже есть, уведомления не повторяются, маркер дописывается.
        monkeypatch.setattr(
            purchase_module,
            'ensure_marker_for_already_paid_referral_in_new_session',
            real_marker,
        )
        assert await process_referral_registration(db, 2, 1, bot) is True

        assert await has_first_purchase_marker(db, 2) is True
        assert bot.send_message.await_count == sent_after_first
        assert len(await _marker_rows(db)) == 1


@pytest.mark.asyncio
async def test_admin_update_retries_marker_for_already_attached(monkeypatch):
    """Повторный запуск админской привязки ретраит ранее упавший маркер."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()
        _use_test_session_factory(monkeypatch, db)

        import app.services.referral_purchase_service as purchase_module

        real_batch_marker = purchase_module.ensure_markers_for_already_paid_referrals_in_new_session

        async def failing_marker(user_ids):
            raise RuntimeError(f'marker boom for {list(user_ids)}')

        monkeypatch.setattr(
            purchase_module,
            'ensure_markers_for_already_paid_referrals_in_new_session',
            failing_marker,
        )
        ok, stats = await UserService().update_user_referrals(db, 1, [2], admin_id=1)

        assert ok is True
        assert stats['added'] == 0
        assert await has_first_purchase_marker(db, 2) is False

        monkeypatch.setattr(
            purchase_module,
            'ensure_markers_for_already_paid_referrals_in_new_session',
            real_batch_marker,
        )
        ok, stats = await UserService().update_user_referrals(db, 1, [2], admin_id=1)

        assert ok is True
        assert stats['added'] == 0
        assert await has_first_purchase_marker(db, 2) is True


@pytest.mark.asyncio
async def test_ensure_marker_nowait_lock_conflict_returns_false(monkeypatch):
    """Занятая строка пользователя при NOWAIT — не ошибка: маркер не пишется."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        real_execute = db.execute
        attempts = 0

        async def flaky_execute(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # asyncpg отдаёт lock_not_available как OperationalError с
                # SQLSTATE 55P03 — только этот код и означает «строка занята».
                orig = SimpleNamespace(pgcode='55P03', sqlstate='55P03')
                raise OperationalError('SELECT ... FOR UPDATE NOWAIT', None, orig)
            return await real_execute(*args, **kwargs)

        monkeypatch.setattr(db, 'execute', flaky_execute)

        assert await ensure_marker_for_already_paid_referral(db, 2, lock_nowait=True) is False
        # rollback вернул сессию в рабочее состояние: ретрай позже сработает.
        assert await ensure_marker_for_already_paid_referral(db, 2, lock_nowait=True) is True


@pytest.mark.asyncio
async def test_ensure_marker_nowait_lock_conflict_as_asyncpg_dbapi_error(monkeypatch):
    """asyncpg отдаёт lock_not_available как общий DBAPIError с SQLSTATE 55P03."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        real_execute = db.execute
        attempts = 0

        async def flaky_execute(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                orig = SimpleNamespace(pgcode='55P03', sqlstate='55P03')
                raise DBAPIError('SELECT ... FOR UPDATE NOWAIT', None, orig)
            return await real_execute(*args, **kwargs)

        monkeypatch.setattr(db, 'execute', flaky_execute)

        assert await ensure_marker_for_already_paid_referral(db, 2, lock_nowait=True) is False
        assert await ensure_marker_for_already_paid_referral(db, 2, lock_nowait=True) is True


@pytest.mark.asyncio
async def test_ensure_marker_nowait_propagates_non_lock_db_error(monkeypatch):
    """NOWAIT не должен прятать ошибки БД, не связанные с занятым локом."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        async def failing_execute(*_args, **_kwargs):
            orig = SimpleNamespace(pgcode='08006', sqlstate='08006')
            raise DBAPIError('SELECT ... FOR UPDATE NOWAIT', None, orig)

        monkeypatch.setattr(db, 'execute', failing_execute)

        with pytest.raises(DBAPIError):
            await ensure_marker_for_already_paid_referral(db, 2, lock_nowait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('pgcode', [None, '40P01', '57014'])
async def test_ensure_marker_nowait_propagates_operational_error_without_lock_code(monkeypatch, pgcode):
    """NOWAIT глотает только 55P03, а не любой OperationalError.

    None (нет кода), deadlock_detected 40P01, query_canceled 57014 и прочие
    коды — настоящие сбои БД: их нельзя выдавать за «маркер уже пишется».
    """
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        async def failing_execute(*_args, **_kwargs):
            orig = SimpleNamespace(pgcode=pgcode, sqlstate=pgcode)
            raise OperationalError('SELECT ... FOR UPDATE NOWAIT', None, orig)

        monkeypatch.setattr(db, 'execute', failing_execute)

        with pytest.raises(OperationalError):
            await ensure_marker_for_already_paid_referral(db, 2, lock_nowait=True)


@pytest.mark.asyncio
async def test_ensure_marker_blocking_lock_error_propagates(monkeypatch):
    """Обычная блокирующая запись маркера не глотает ошибки БД."""
    _enable_days_only(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        async def failing_execute(*_args, **_kwargs):
            raise OperationalError('SELECT ... FOR UPDATE', None, Exception('connection lost'))

        monkeypatch.setattr(db, 'execute', failing_execute)

        with pytest.raises(OperationalError):
            await ensure_marker_for_already_paid_referral(db, 2)


@pytest.mark.asyncio
async def test_process_registration_retries_marker_nowait_and_survives_lock_conflict(monkeypatch):
    """Ретрай маркера при существующем якоре не ждёт чужой лок.

    Caller (кампания/кабинет) может держать строку пользователя в своей
    незакоммиченной транзакции. Вторая сессия с блокирующим ``FOR UPDATE``
    встала бы в ожидание этого лока; существующий якорь уже подтверждает
    регистрацию, поэтому ретрай обязан быть best-effort и не ронять её.
    """
    _enable_days_only(monkeypatch)

    import app.services.referral_purchase_service as purchase_module

    calls: list[dict] = []

    async def locked_marker(user_id, *, lock_nowait=False):
        calls.append({'user_id': user_id, 'lock_nowait': lock_nowait})
        raise OperationalError('SELECT ... FOR UPDATE NOWAIT', None, Exception('lock not available'))

    monkeypatch.setattr(
        purchase_module,
        'ensure_marker_for_already_paid_referral_in_new_session',
        locked_marker,
    )

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by_id=1), _paid_transaction(100, 2)])
        await db.commit()

        # Первый вызов создаёт якорь и ретраит маркер блокирующей веткой.
        assert await process_referral_registration(db, 2, 1) is True
        # Повтор видит якорь и ретраит маркер без ожидания чужого лока.
        assert await process_referral_registration(db, 2, 1) is True

    assert [call['lock_nowait'] for call in calls] == [False, True]
