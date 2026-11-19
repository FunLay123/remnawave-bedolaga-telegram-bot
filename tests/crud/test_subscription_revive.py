"""
Регрессия: в мульти-тарифе повторная покупка тарифа, чья подписка уже истекла,
должна РЕАНИМИРОВАТЬ ту же запись, а не плодить дубликат.

Partial unique index ``uq_subscriptions_user_tariff_active`` сторожит только
живые статусы (active/trial/limited), поэтому истёкшие дубли одного тарифа
копились пачкой у пользователей (отчёт not2clean, топик «Баги»). Фикс:
``create_paid_subscription`` в мульти-тарифе сначала ищет существующую подписку
тарифа ВКЛЮЧАЯ истёкшие и реанимирует её через ``_revive_paid_subscription``.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import subscription as sub_crud
from app.database.models import SubscriptionStatus


def _sub(**kw) -> MagicMock:
    s = MagicMock()
    # Эти тесты моделируют legacy-строки: явного учёта реферального остатка нет.
    s.trial_referral_seconds = None
    s.base_trial_end_date = None
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def _no_grace_history(monkeypatch):
    """Реанимация сначала возвращает затёртое оверлеем грейса; у этих подписок истории нет."""
    monkeypatch.setattr('app.services.grace_access_echo.undo_grace_overlay_echo', AsyncMock(return_value=set()))


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


async def test_revive_expired_starts_fresh_period(monkeypatch):
    """Истёкшую реанимируем: статус active, период с «сейчас», трафик обнулён."""
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    past = datetime.now(UTC) - timedelta(days=5)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=past,
        start_date=past - timedelta(days=30),
        is_trial=False,
        traffic_used_gb=44.0,
        connected_squads=['sq'],
        device_limit=2,
        traffic_limit_gb=100,
    )

    result = await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )

    assert result is s
    assert s.status == SubscriptionStatus.ACTIVE.value
    assert s.is_trial is False
    assert s.traffic_used_gb == 0.0
    assert s.end_date > datetime.now(UTC) + timedelta(days=29)  # свежий период ~ now+30
    db.commit.assert_awaited_once()
    db.add.assert_not_called()  # никакой новой записи


async def test_revive_alive_extends_from_end_date(monkeypatch):
    """Ещё живую продлеваем от её end_date, накопленный трафик не сбрасываем."""
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    future = datetime.now(UTC) + timedelta(days=10)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=future,
        start_date=datetime.now(UTC),
        is_trial=False,
        traffic_used_gb=20.0,
        connected_squads=['sq'],
        device_limit=2,
        traffic_limit_gb=100,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=None,
        update_server_counters=False,
        commit=True,
    )

    assert s.end_date > future + timedelta(days=29)  # продлено от end_date, не от now
    assert s.traffic_used_gb == 20.0  # живой трафик не трогаем


async def test_create_paid_subscription_revives_existing_in_multitariff(monkeypatch):
    """Мульти-тариф + есть ИСТЁКШАЯ запись тарифа → revive, без вставки дубля."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    existing = _sub(id=5, user_id=7, tariff_id=3, is_trial=False, status=SubscriptionStatus.EXPIRED.value)
    lookup = AsyncMock(return_value=existing)
    revive = AsyncMock(return_value=existing)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)
    monkeypatch.setattr(sub_crud, '_revive_paid_subscription', revive)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=3)

    assert result is existing
    lookup.assert_awaited_once()
    assert lookup.await_args.kwargs.get('include_inactive') is True
    revive.assert_awaited_once()
    db.add.assert_not_called()  # новую подписку НЕ создавали


async def test_create_paid_subscription_revives_expired_trial(monkeypatch):
    """#3004 (централизовано): ИСТЁКШИЙ ТРИАЛ того же тарифа при платной покупке
    (бот/гость) теперь реанимируется/конвертируется на месте, а не плодит дубль —
    та же Remnawave-ссылка. Раньше guard исключал триалы (``not _existing.is_trial``).
    """
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    expired_trial = _sub(id=9, user_id=7, tariff_id=3, is_trial=True, status=SubscriptionStatus.EXPIRED.value)
    lookup = AsyncMock(return_value=expired_trial)
    revive = AsyncMock(return_value=expired_trial)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)
    monkeypatch.setattr(sub_crud, '_revive_paid_subscription', revive)
    db = _db()

    result = await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=3)

    assert result is expired_trial
    revive.assert_awaited_once()  # истёкший триал теперь уходит в revive
    db.add.assert_not_called()  # новую подписку/ссылку НЕ создавали


async def test_revive_expired_trial_converts_to_paid(monkeypatch):
    """Реанимация истёкшего ТРИАЛА: снимаем триальный флаг, обнуляем трафик,
    стартуем свежий платный период — конвертация trial→paid в той же записи."""
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    past = datetime.now(UTC) - timedelta(days=2)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=past,
        start_date=past - timedelta(days=3),
        is_trial=True,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )

    assert s.is_trial is False
    assert s.status == SubscriptionStatus.ACTIVE.value
    assert s.traffic_used_gb == 0.0
    assert s.end_date > datetime.now(UTC) + timedelta(days=29)
    db.add.assert_not_called()


async def test_create_paid_subscription_does_not_revive_active(monkeypatch):
    """Активную (не истёкшую) НЕ реанимируем — падаем в обычное создание/IntegrityError."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    active = _sub(id=5, user_id=7, tariff_id=3, is_trial=False, status=SubscriptionStatus.ACTIVE.value)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=active))
    # Живого триала нет — иначе сработала бы конверсия триала на месте
    monkeypatch.setattr(sub_crud, 'get_alive_trial_subscription', AsyncMock(return_value=None))
    revive = AsyncMock(return_value=active)
    monkeypatch.setattr(sub_crud, '_revive_paid_subscription', revive)
    # short-circuit тяжёлый путь создания сразу после guard
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(side_effect=RuntimeError('reached create')))
    db = _db()

    try:
        await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, traffic_limit_gb=100, tariff_id=3)
    except RuntimeError as e:
        assert str(e) == 'reached create'  # дошли до создания, не реанимировали

    revive.assert_not_awaited()  # active не реанимируется


async def test_revive_expired_trial_with_living_tail_starts_from_now(monkeypatch):
    """EXPIRED-триал с живым реферальным хвостом: платный период — now + только хвост.

    Базовая дата в прошлом, общий ``end_date`` на 10 дней впереди. Покупка 30
    дней даёт ``now+40``: платный срок стартует от покупки, а не от старого
    общего ``end_date`` (иначе базовый остаток учитывался бы второй раз).
    """
    monkeypatch.setattr(sub_crud.settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    before = datetime.now(UTC)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=before + timedelta(days=10),
        base_trial_end_date=before - timedelta(days=5),
        start_date=before - timedelta(days=8),
        is_trial=True,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )
    after = datetime.now(UTC)

    assert s.is_trial is False
    assert s.status == SubscriptionStatus.ACTIVE.value
    assert before + timedelta(days=40) <= s.end_date <= after + timedelta(days=40)


async def test_revive_fully_expired_trial_gets_purchase_only(monkeypatch):
    """Полностью истёкший хвост не восстанавливается: ровно купленные 30 дней."""
    monkeypatch.setattr(sub_crud.settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    before = datetime.now(UTC)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=before - timedelta(days=10),
        base_trial_end_date=before - timedelta(days=30),
        start_date=before - timedelta(days=33),
        is_trial=True,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )
    after = datetime.now(UTC)

    assert s.is_trial is False
    assert s.traffic_used_gb == 0.0
    assert before + timedelta(days=30) <= s.end_date <= after + timedelta(days=30)


async def test_revive_living_trial_with_future_base_carries_only_referral_tail(monkeypatch):
    """Если базовая дата ещё впереди, переносится только реферальный хвост.

    База ``now+5``, общий ``end_date`` ``now+15`` (10 реферальных дней). Покупка
    30 дней даёт ``now+40``, а не ``end_date+30 = now+45``: остаток базового
    бесплатного триала не переносится.
    """
    monkeypatch.setattr(sub_crud.settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    before = datetime.now(UTC)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=before + timedelta(days=15),
        base_trial_end_date=before + timedelta(days=5),
        start_date=before,
        is_trial=True,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )
    after = datetime.now(UTC)

    assert s.is_trial is False
    assert before + timedelta(days=40) <= s.end_date <= after + timedelta(days=40)


async def test_revive_legacy_null_base_alive_trial_extends_from_end_date(monkeypatch):
    """Старый триал без базовой даты (carry off) реанимируется по-прежнему от end_date."""
    monkeypatch.setattr(sub_crud.settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(sub_crud.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    before = datetime.now(UTC)
    old_end = before + timedelta(days=10)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=old_end,
        base_trial_end_date=None,
        start_date=before - timedelta(days=8),
        is_trial=True,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )

    assert s.is_trial is False
    assert s.end_date == old_end + timedelta(days=30)


async def test_revive_levels_alive_trial_extends_from_end_date(monkeypatch):
    """Схема levels: живой триал реанимируется от старого end_date, не от now."""
    monkeypatch.setattr(sub_crud.settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(sub_crud.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    before = datetime.now(UTC)
    old_end = before + timedelta(days=10)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=old_end,
        base_trial_end_date=before - timedelta(days=5),
        start_date=before - timedelta(days=8),
        is_trial=True,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )

    assert s.is_trial is False
    assert s.end_date == old_end + timedelta(days=30)


async def test_revive_alive_days_only_trial_restarts_paid_period(monkeypatch):
    """Живой days_only-триал: платный период стартует от now, а не от старого end_date.

    База ``now+5``, общий ``end_date`` ``now+15`` (10 реферальных дней). Покупка
    30 дней даёт ``now+40``; ``start_date`` переставляется на now даже без
    истечения, а накопленный трафик живой строки не сбрасывается.
    """
    monkeypatch.setattr(sub_crud.settings, 'REFERRAL_REWARD_SCHEME', 'days_only')
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(sub_crud.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    before = datetime.now(UTC)
    old_start = before - timedelta(days=10)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=before + timedelta(days=15),
        base_trial_end_date=before + timedelta(days=5),
        start_date=old_start,
        is_trial=True,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )
    after = datetime.now(UTC)

    assert s.is_trial is False
    assert before <= s.start_date <= after
    assert s.start_date != old_start
    assert before + timedelta(days=40) <= s.end_date <= after + timedelta(days=40)
    assert s.traffic_used_gb == 12.0  # живой трафик не сбрасываем


async def test_revive_trial_conversion_applies_autopay_default(monkeypatch):
    """Конверсия триала в revive получает дефолт автоплатежа."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_autopay_enabled_by_default', lambda self: True)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    past = datetime.now(UTC) - timedelta(days=2)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=past,
        start_date=past - timedelta(days=3),
        is_trial=True,
        autopay_enabled=False,
        traffic_used_gb=12.0,
        connected_squads=['sq'],
        device_limit=1,
        traffic_limit_gb=50,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=True,
    )

    assert s.autopay_enabled is True


async def test_revive_paid_extension_preserves_autopay(monkeypatch):
    """Продление уже платной подписки не трогает осознанный выбор автоплатежа."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_autopay_enabled_by_default', lambda self: True)
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = _db()
    future = datetime.now(UTC) + timedelta(days=10)
    s = _sub(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=future,
        start_date=datetime.now(UTC),
        is_trial=False,
        autopay_enabled=False,
        traffic_used_gb=20.0,
        connected_squads=['sq'],
        device_limit=2,
        traffic_limit_gb=100,
    )

    await sub_crud._revive_paid_subscription(
        db,
        s,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=None,
        update_server_counters=False,
        commit=True,
    )

    assert s.autopay_enabled is False


async def test_create_paid_subscription_skips_revive_without_tariff(monkeypatch):
    """Классический режим (tariff_id=None) — lookup тарифа не дёргаем, создаём как раньше."""
    monkeypatch.setattr(type(sub_crud.settings), 'is_multi_tariff_enabled', lambda self: True)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_and_tariff', lookup)
    # short-circuit тяжёлый путь создания сразу после guard
    monkeypatch.setattr(sub_crud, 'generate_unique_short_id', AsyncMock(side_effect=RuntimeError('reached create')))
    db = _db()

    try:
        await sub_crud.create_paid_subscription(db, user_id=7, duration_days=30, tariff_id=None)
    except RuntimeError as e:
        assert str(e) == 'reached create'  # дошли до создания, не до revive

    lookup.assert_not_awaited()  # без tariff_id поиск тарифа не запускается
