"""Стартовое назначение единственного тарифа старым подпискам (F05).

Функция меняет только метку тарифа, поэтому её ошибки тихие: неоплаченный
черновик получает тариф, которого не покупал, или пачка строк одного человека
нарушает частичный уникальный индекс и откатывает весь коммит. Тесты держат
границы: режим продаж, статус строки и одна метка на пользователя.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.crud.tariff import assign_single_tariff_to_legacy_subscriptions
from app.database.models import PromoGroup, Subscription, Tariff, tariff_promo_groups
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    Tariff.__table__,
    # Тариф тянет промогруппы жадно — без связки запрос к тарифам падает.
    tariff_promo_groups,
    PromoGroup.__table__,
    Subscription.__table__,
)
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
USER_ID = 7


def _tariff(tariff_id=1, *, is_active=True, is_daily=False):
    return Tariff(
        id=tariff_id,
        name=f'Тариф {tariff_id}',
        is_active=is_active,
        is_daily=is_daily,
        allowed_squads=[],
        server_traffic_limits={},
        period_prices={'30': 10000},
    )


def _subscription(subscription_id, *, status='active', is_trial=False, tariff_id=None, user_id=USER_ID):
    return Subscription(
        id=subscription_id,
        user_id=user_id,
        status=status,
        is_trial=is_trial,
        tariff_id=tariff_id,
        end_date=NOW + timedelta(days=30),
        connected_squads=[],
        # Уникальный короткий id иначе достаётся всем строкам одинаковым.
        remnawave_short_id=f'sub{subscription_id}',
    )


@pytest.fixture(autouse=True)
def _tariffs_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)


async def test_imported_paid_subscription_gets_the_single_tariff(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        subscription = _subscription(1, status='active')
        db.add(subscription)
        await db.commit()

        assigned = await assign_single_tariff_to_legacy_subscriptions(db)

        await db.refresh(subscription)
        assert assigned == 1
        assert subscription.tariff_id == 1


async def test_unpaid_pending_draft_is_not_labelled(monkeypatch):
    """``is_trial=false`` не доказывает оплату: брошенный черновик остаётся без тарифа."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        draft = _subscription(1, status='pending', is_trial=False)
        db.add(draft)
        await db.commit()

        assigned = await assign_single_tariff_to_legacy_subscriptions(db)

        await db.refresh(draft)
        assert assigned == 0
        assert draft.tariff_id is None


async def test_trial_is_not_labelled(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        trial = _subscription(1, status='trial', is_trial=True)
        db.add(trial)
        await db.commit()

        assert await assign_single_tariff_to_legacy_subscriptions(db) == 0

        await db.refresh(trial)
        assert trial.tariff_id is None


async def test_classic_mode_is_left_alone(monkeypatch):
    """В classic тариф не участвует в продаже — метка там не значит ничего."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        subscription = _subscription(1, status='active')
        db.add(subscription)
        await db.commit()

        assert await assign_single_tariff_to_legacy_subscriptions(db) == 0

        await db.refresh(subscription)
        assert subscription.tariff_id is None


async def test_multi_tariff_mode_is_left_alone(monkeypatch):
    """В мультитарифе выбор есть всегда — автоматически ничего не назначаем."""
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        subscription = _subscription(1, status='active')
        db.add(subscription)
        await db.commit()

        assert await assign_single_tariff_to_legacy_subscriptions(db) == 0

        await db.refresh(subscription)
        assert subscription.tariff_id is None


async def test_several_tariffs_leave_the_choice_to_the_user(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_tariff(1), _tariff(2)])
        subscription = _subscription(1, status='active')
        db.add(subscription)
        await db.commit()

        assert await assign_single_tariff_to_legacy_subscriptions(db) == 0

        await db.refresh(subscription)
        assert subscription.tariff_id is None


async def test_daily_tariff_is_not_applied_to_periodic_subscriptions(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff(is_daily=True))
        subscription = _subscription(1, status='active')
        db.add(subscription)
        await db.commit()

        assert await assign_single_tariff_to_legacy_subscriptions(db) == 0

        await db.refresh(subscription)
        assert subscription.tariff_id is None


async def test_only_one_row_per_user_is_labelled(monkeypatch):
    """Частичный уникальный индекс не даёт двум активным строкам один тариф.

    На PostgreSQL пачка таких строк откатила бы весь коммит вместе с уже
    назначенными, поэтому метку получает одна строка — активная.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        expired = _subscription(1, status='expired', user_id=USER_ID)
        active = _subscription(2, status='active', user_id=USER_ID)
        disabled = _subscription(3, status='disabled', user_id=USER_ID)
        db.add_all([expired, active, disabled])
        await db.commit()

        assigned = await assign_single_tariff_to_legacy_subscriptions(db)

        await db.refresh(expired)
        await db.refresh(active)
        await db.refresh(disabled)
        assert assigned == 1
        assert active.tariff_id == 1, 'метка нужна строке, которую человек видит в меню'
        assert expired.tariff_id is None
        assert disabled.tariff_id is None


async def test_different_users_each_get_the_tariff(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        tariff = _tariff()
        db.add(tariff)
        first = _subscription(1, status='active', user_id=1)
        second = _subscription(2, status='active', user_id=2)
        db.add_all([first, second])
        await db.commit()

        assigned = await assign_single_tariff_to_legacy_subscriptions(db)

        await db.refresh(first)
        await db.refresh(second)
        assert assigned == 2
        assert first.tariff_id == 1
        assert second.tariff_id == 1


async def test_rows_with_a_tariff_are_not_touched(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_tariff(1), _tariff(2, is_active=False)])
        already = _subscription(1, status='active', tariff_id=2)
        db.add(already)
        await db.commit()

        assert await assign_single_tariff_to_legacy_subscriptions(db) == 0

        await db.refresh(already)
        assert already.tariff_id == 2


async def test_assignment_does_not_change_dates_squads_or_limits(monkeypatch):
    """Метка тарифа — только метка: оплаченный доступ не пересобирается."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        subscription = _subscription(1, status='active')
        subscription.end_date = NOW + timedelta(days=17)
        subscription.connected_squads = ['squad-a', 'squad-b']
        subscription.traffic_limit_gb = 250
        subscription.device_limit = 4
        db.add(subscription)
        await db.commit()

        await assign_single_tariff_to_legacy_subscriptions(db)

        await db.refresh(subscription)
        assert subscription.tariff_id == 1
        assert subscription.end_date == NOW + timedelta(days=17)
        assert subscription.connected_squads == ['squad-a', 'squad-b']
        assert subscription.traffic_limit_gb == 250
        assert subscription.device_limit == 4


async def test_repeat_run_does_not_label_another_row_of_the_same_user(monkeypatch):
    """Повторный прогон не должен «доéхать» до следующей строки того же человека.

    Первый прогон помечает активную строку. Второй видит оставшуюся строку без
    метки и, если не знать про уже подписанного пользователя, пометил бы и её:
    с каждым запуском меток становилось бы больше, а на PostgreSQL две активные
    строки на один тариф откатили бы весь коммит.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_tariff())
        active = _subscription(1, status='active', user_id=USER_ID)
        expired = _subscription(2, status='expired', user_id=USER_ID)
        db.add_all([active, expired])
        await db.commit()

        assert await assign_single_tariff_to_legacy_subscriptions(db) == 1
        assert await assign_single_tariff_to_legacy_subscriptions(db) == 0

        await db.refresh(active)
        await db.refresh(expired)
        assert active.tariff_id == 1
        assert expired.tariff_id is None


async def test_labelled_users_are_skipped_while_others_are_labelled(monkeypatch):
    """Смешанный прогон: подписанный и занятый пользователи не трогаются.

    Пользователь 1 уже имеет строку с этим тарифом (повторный прогон), у
    пользователя 2 живая подписка на другой тариф — вторая метка дала бы ему
    две активные подписки в одноботарном режиме. Помечается только третий.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_tariff(1), _tariff(2, is_active=False)])
        already_on_this = _subscription(1, status='expired', user_id=1, tariff_id=1)
        extra_for_first = _subscription(2, status='active', user_id=1)
        busy_on_another = _subscription(3, status='active', user_id=2, tariff_id=2)
        extra_for_second = _subscription(4, status='expired', user_id=2)
        fresh = _subscription(5, status='active', user_id=3)
        db.add_all([already_on_this, extra_for_first, busy_on_another, extra_for_second, fresh])
        await db.commit()

        assigned = await assign_single_tariff_to_legacy_subscriptions(db)

        for subscription in (already_on_this, extra_for_first, busy_on_another, extra_for_second, fresh):
            await db.refresh(subscription)
        assert assigned == 1
        assert fresh.tariff_id == 1
        assert extra_for_first.tariff_id is None, 'пользователь уже подписан этим тарифом'
        assert extra_for_second.tariff_id is None, 'у пользователя уже есть живая подписка'
        assert busy_on_another.tariff_id == 2, 'чужая метка не перезаписывается'


async def test_uniqueness_conflict_rolls_back_without_touching_expired_objects(monkeypatch):
    """Сбой коммита по уникальности не должен падать на истёкшем `tariff`.

    После `rollback()` ORM-объекты сессии истекают: обращение к `tariff.id` в
    логе запустило бы ленивую загрузку вне greenlet (MissingGreenlet). Поэтому
    скаляры для лога снимаются до коммита, а сессия остаётся рабочей.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        tariff = _tariff()
        db.add(tariff)
        first = _subscription(1, status='active', user_id=1)
        second = _subscription(2, status='active', user_id=2)
        db.add_all([first, second])
        await db.commit()

        real_commit = db.commit

        async def _conflicting_commit():
            # Изменения уходят во flush, коммит падает — как при настоящем
            # конфликте частичного уникального индекса.
            await db.flush()
            raise IntegrityError('COMMIT', {}, Exception('uq_subscriptions_user_tariff_active'))

        monkeypatch.setattr(db, 'commit', _conflicting_commit)
        assigned = await assign_single_tariff_to_legacy_subscriptions(db)
        monkeypatch.setattr(db, 'commit', real_commit)

        assert assigned == 0
        # Прямая проверка условия: объект действительно истёк, и лог не имеет
        # права читать из него `id`/`name`.
        assert sqlalchemy_inspect(tariff).expired
        await db.refresh(first)
        await db.refresh(second)
        assert first.tariff_id is None
        assert second.tariff_id is None
