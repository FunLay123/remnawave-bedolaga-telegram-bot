"""CRUD состояний премиум-лимита: жизненный цикл периода и защита от гонки."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.crud.premium_traffic import (
    add_extra_bytes,
    delete_states_for_squads,
    delete_states_for_subscription,
    get_limited_squad_uuids,
    get_or_create_state,
    get_state,
    get_states_for_squad,
    get_states_for_subscription,
    record_usage,
    set_unlimited,
    start_new_period,
    sync_premium_state_limits,
)
from app.database.models import SubscriptionPremiumTraffic
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


# Таблица подписок не создаётся: sqlite не проверяет внешние ключи, если их не
# включить явно, а тесты здесь про состояния, а не про целостность связей.
TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER_SQUAD = '82a12389-14d6-40c6-b320-4674f6bbb344'

FIVE_GB = 5 * BYTES_IN_GB
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


async def _new_state(db, subscription_id=1, squad_uuid=SQUAD, limit_bytes=FIVE_GB, period_start_at=NOW):
    return await get_or_create_state(
        db,
        subscription_id,
        squad_uuid,
        limit_bytes=limit_bytes,
        period_start_at=period_start_at,
    )


async def test_state_is_created_once_and_read_back(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        created = await _new_state(db)
        await db.commit()

        assert created.limit_bytes == FIVE_GB
        assert created.used_bytes == 0
        assert created.is_limited is False

        again = await _new_state(db)
        assert again.id == created.id

        found = await get_state(db, 1, SQUAD)
        assert found is not None and found.id == created.id


async def test_lost_insert_race_returns_the_existing_row(monkeypatch):
    """Воркер и покупка трафика могут дойти до вставки одновременно.

    Имитируем проигранную гонку: первая проверка «есть ли строка» не видит
    чужую вставку, INSERT падает на уникальном ключе — и состояние должно
    вернуться перечитанным, а не всплыть исключением.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        winner = await _new_state(db)
        await db.commit()

        real_get_state = get_state
        calls = {'n': 0}

        async def _blind_once(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return None  # гонка: чужую строку ещё не видим
            return await real_get_state(*args, **kwargs)

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _blind_once)

        recovered = await _new_state(db)

        assert recovered.id == winner.id
        assert calls['n'] == 2  # первая проверка и перечитывание после конфликта
        monkeypatch.undo()
        assert len(await get_states_for_subscription(db, 1)) == 1


async def test_foreign_integrity_error_is_not_swallowed(monkeypatch):
    """Если упал не наш уникальный ключ — ошибку прятать нельзя."""
    async with memory_session(monkeypatch, TABLES) as db:

        async def _always_blind(*_args, **_kwargs):
            return None

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _always_blind)
        await _new_state(db)
        await db.commit()

        with pytest.raises(IntegrityError):
            await _new_state(db)


async def test_usage_never_goes_down_inside_a_period(monkeypatch):
    """Просадка выборки не должна вернуть доступ к исчерпанному скваду."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)

        record_usage(state, 4 * BYTES_IN_GB)
        assert state.used_bytes == 4 * BYTES_IN_GB

        record_usage(state, BYTES_IN_GB)
        assert state.used_bytes == 4 * BYTES_IN_GB

        record_usage(state, -1)
        assert state.used_bytes == 4 * BYTES_IN_GB


async def test_usage_records_check_time(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)

        record_usage(state, BYTES_IN_GB, checked_at=NOW)

        assert state.last_checked_at == NOW


async def test_exhaustion_and_topup_return_the_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)

        record_usage(state, FIVE_GB)
        assert state.is_exhausted is True

        state.is_limited = True
        state.notified_80 = True
        state.notified_90 = True

        add_extra_bytes(state, 2 * BYTES_IN_GB)

        assert state.extra_bytes == 2 * BYTES_IN_GB
        assert state.is_exhausted is False
        assert state.is_limited is False
        # Порог 80 % теперь считается от 7 ГБ — предупредить нужно заново.
        assert state.notified_80 is False
        assert state.notified_90 is False


async def test_unlimited_is_never_exhausted_even_above_the_limit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.used_bytes = 10 * FIVE_GB
        set_unlimited(state, True, now=NOW)
        await db.commit()
        reread = await get_state(db, 1, SQUAD)
        assert reread.unlimited_at == NOW
        assert reread.is_exhausted is False


async def test_closed_wins_over_unlimited(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.closed_at = NOW
        set_unlimited(state, True, now=NOW)
        assert state.is_exhausted is True


async def test_new_period_keeps_unlimited_and_zeroes_usage(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        set_unlimited(state, True, now=NOW)
        start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=FIVE_GB)
        assert state.unlimited_at == NOW
        assert state.used_bytes == 0
        assert state.is_limited is False


async def test_revoke_clears_unlimited_and_resets_warning_marks(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        set_unlimited(state, True, now=NOW)
        state.notified_80 = True
        state.notified_90 = True
        set_unlimited(state, False)
        assert state.unlimited_at is None
        assert state.notified_80 is False
        assert state.notified_90 is False


async def test_tariff_limit_sync_keeps_unlimited(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        set_unlimited(state, True, now=NOW)
        await db.commit()
        await sync_premium_state_limits(db, 1, SimpleNamespace(server_traffic_limits={SQUAD: {'traffic_limit_gb': 10}}))
        await db.commit()
        reread = await get_state(db, 1, SQUAD)
        assert reread.unlimited_at == NOW


async def test_topup_smaller_than_overspend_keeps_the_squad_limited(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        record_usage(state, 8 * BYTES_IN_GB)
        state.is_limited = True

        add_extra_bytes(state, BYTES_IN_GB)

        assert state.is_exhausted is True
        assert state.is_limited is True


async def test_non_positive_topup_changes_nothing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.is_limited = True

        add_extra_bytes(state, 0)
        add_extra_bytes(state, -BYTES_IN_GB)

        assert state.extra_bytes == 0
        assert state.is_limited is True


async def test_new_period_resets_everything_and_takes_fresh_limit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        record_usage(state, FIVE_GB)
        add_extra_bytes(state, BYTES_IN_GB)
        state.is_limited = True
        state.notified_80 = True
        state.notified_90 = True

        next_period = NOW + timedelta(days=30)
        start_new_period(state, period_start_at=next_period, limit_bytes=10 * BYTES_IN_GB)

        assert state.period_start_at == next_period
        # Лимит перечитан из тарифа: за прошедший период его могли поменять.
        assert state.limit_bytes == 10 * BYTES_IN_GB
        assert state.used_bytes == 0
        assert state.extra_bytes == 0
        assert state.notified_80 is False
        assert state.notified_90 is False
        assert state.is_limited is False


async def test_new_period_clears_the_first_day_correction(monkeypatch):
    """Период новый — поправку на его первые сутки надо снять заново."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.baseline_bytes = 3 * BYTES_IN_GB

        start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=FIVE_GB)

        assert state.baseline_bytes is None


async def test_new_period_keeps_ack_when_not_given(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.panel_reset_ack_at = NOW

        start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=FIVE_GB)

        assert state.panel_reset_ack_at == NOW


async def test_new_period_updates_closed_limit_but_keeps_access_blocked(monkeypatch):
    """Админское закрытие сильнее нового периода, но снимок лимита идёт за тарифом.

    Пользователь видит объём нового тарифа, однако `closed_at` и `is_limited`
    остаются, а расход подтянут к новому потолку — сам воркер сквад не откроет.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = state.total_limit_bytes
        state.is_limited = True
        state.closed_at = NOW

        start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=20 * BYTES_IN_GB)

        assert state.limit_bytes == 20 * BYTES_IN_GB
        assert state.used_bytes == 20 * BYTES_IN_GB
        assert state.is_limited is True
        assert state.closed_at == NOW


async def test_limited_squads_are_listed_for_the_subscription(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        limited = await _new_state(db, squad_uuid=SQUAD)
        await _new_state(db, squad_uuid=OTHER_SQUAD)
        limited.is_limited = True
        await db.commit()

        assert await get_limited_squad_uuids(db, 1) == {SQUAD}


async def test_limited_squads_of_other_subscriptions_do_not_leak(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        theirs = await _new_state(db, subscription_id=2)
        theirs.is_limited = True
        await db.commit()

        assert await get_limited_squad_uuids(db, 1) == set()


async def test_states_are_collected_per_squad_for_the_worker(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, subscription_id=1, squad_uuid=SQUAD)
        await _new_state(db, subscription_id=2, squad_uuid=SQUAD)
        await _new_state(db, subscription_id=3, squad_uuid=OTHER_SQUAD)
        await db.commit()

        by_squad = await get_states_for_squad(db, SQUAD)

        assert {s.subscription_id for s in by_squad} == {1, 2}


async def test_states_are_deleted_for_a_subscription(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, squad_uuid=SQUAD)
        await _new_state(db, squad_uuid=OTHER_SQUAD)
        await db.commit()

        deleted = await delete_states_for_subscription(db, 1)
        await db.commit()

        assert deleted == 2
        assert await get_states_for_subscription(db, 1) == []


async def test_only_named_squads_are_deleted(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, squad_uuid=SQUAD)
        await _new_state(db, squad_uuid=OTHER_SQUAD)
        await db.commit()

        deleted = await delete_states_for_squads(db, 1, {SQUAD})
        await db.commit()

        assert deleted == 1
        remaining = await get_states_for_subscription(db, 1)
        assert [s.squad_uuid for s in remaining] == [OTHER_SQUAD]


async def test_deleting_nothing_is_a_no_op(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db)
        await db.commit()

        assert await delete_states_for_squads(db, 1, set()) == 0
        assert len(await get_states_for_subscription(db, 1)) == 1


def _tariff(limits):
    return SimpleNamespace(server_traffic_limits=limits)


async def test_tariff_switch_refreshes_limit_and_keeps_extra(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.extra_bytes = 2 * BYTES_IN_GB
        await db.commit()

        changed = await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert changed == 1
        assert reread.limit_bytes == 20 * BYTES_IN_GB
        assert reread.extra_bytes == 2 * BYTES_IN_GB


async def test_tariff_switch_lowers_limit_of_an_open_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, limit_bytes=20 * BYTES_IN_GB)
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 5}}))
        await db.commit()

        assert (await get_state(db, 1, SQUAD)).limit_bytes == 5 * BYTES_IN_GB


async def test_tariff_switch_raises_limit_for_an_auto_limited_squad(monkeypatch):
    """Апгрейд должен вернуть воркеру право восстановить снятый сквад."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = 5 * BYTES_IN_GB
        state.is_limited = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 20 * BYTES_IN_GB
        assert reread.is_limited is True


async def test_tariff_switch_does_not_lower_limit_of_a_limited_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db, limit_bytes=20 * BYTES_IN_GB)
        state.used_bytes = 20 * BYTES_IN_GB
        state.is_limited = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 5}}))
        await db.commit()

        assert (await get_state(db, 1, SQUAD)).limit_bytes == 20 * BYTES_IN_GB


async def test_tariff_switch_leaves_admin_closed_squad_untouched(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = state.limit_bytes
        state.is_limited = True
        state.closed_at = NOW
        state.notified_80 = True
        state.notified_90 = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 5 * BYTES_IN_GB
        assert reread.closed_at == NOW
        assert reread.notified_80 is True
        assert reread.notified_90 is True


async def test_tariff_switch_ignores_squad_absent_from_new_tariff(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db)
        await db.commit()

        changed = await sync_premium_state_limits(db, 1, _tariff({}))
        await db.commit()

        assert changed == 0
        assert (await get_state(db, 1, SQUAD)).limit_bytes == 5 * BYTES_IN_GB


async def test_tariff_switch_rearms_warning_for_a_limited_squad(monkeypatch):
    """Новый, больший потолок должен снова предупредить о 90 %.

    Флаг `notified_90` остался с трёхгигабайтного периода: если его не снять,
    клиент не получит предупреждение на новом лимите и узнает об исчерпании
    только когда сквад уже отключат.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = state.total_limit_bytes
        state.is_limited = True
        state.notified_80 = True
        state.notified_90 = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 20 * BYTES_IN_GB
        assert reread.notified_80 is False
        assert reread.notified_90 is False


async def test_tariff_switch_keeps_warning_when_raised_limit_still_exhausted(monkeypatch):
    """Подняли 5 → 10 ГБ, но израсходовано 20: свободного места нет, флаг не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = 20 * BYTES_IN_GB
        state.is_limited = True
        state.notified_80 = True
        state.notified_90 = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 10}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 10 * BYTES_IN_GB
        assert reread.notified_80 is True
        assert reread.notified_90 is True


async def test_tariff_switch_rearms_warning_for_an_open_squad_on_raise(monkeypatch):
    """Как и докупка: больший потолок — новый порог 90 %, предупреждение заново."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = 4 * BYTES_IN_GB
        state.notified_80 = True
        state.notified_90 = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.notified_80 is False
        assert reread.notified_90 is False


async def test_tariff_switch_rearms_warning_with_extra_bytes(monkeypatch):
    """Свободное место считается от base + extra, а не только от нового base."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.extra_bytes = 2 * BYTES_IN_GB
        state.used_bytes = 7 * BYTES_IN_GB
        state.is_limited = True
        state.notified_80 = True
        state.notified_90 = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 10}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 10 * BYTES_IN_GB
        assert reread.notified_80 is False
        assert reread.notified_90 is False


async def test_tariff_switch_does_not_rearm_warning_when_limit_is_lowered(monkeypatch):
    """Понижение не создаёт новую квоту — старое предупреждение остаётся в силе."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db, limit_bytes=20 * BYTES_IN_GB)
        state.used_bytes = BYTES_IN_GB
        state.notified_80 = True
        state.notified_90 = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 5}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 5 * BYTES_IN_GB
        assert reread.notified_80 is True
        assert reread.notified_90 is True
