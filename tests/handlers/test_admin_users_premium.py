"""Тесты премиум-трафика в карточке подписки (бот, Task 11).

Контракт задачи — не спутать четыре состояния премиум-сквада:
1) нет отдельного лимита (`traffic_limit_gb == 0` в тарифе) — не безлимит и не
   закрытый доступ;
2) снят автоматически за перерасход (`is_limited`, `closed_at is None`,
   лимит исчерпан) — сам вернётся с новым периодом;
3) закрыт администратором (`closed_at` стоит) — переживает смену периода,
   докупку и уход сквада из тарифа;
4) открыт администратором, но ещё не возвращён в панель воркером
   (`closed_at is None`, `is_limited=True`, лимит уже не исчерпан) — то самое
   переходное состояние, которое кабинет верно показывает как «ещё
   ограничено»; здесь его нельзя называть «закрыто администратором».
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.admin.users as users_mod
from app.database.crud.premium_traffic import get_or_create_state, get_state
from app.database.models import SubscriptionPremiumTraffic
from app.utils.premium_traffic import BYTES_IN_GB, PremiumSquadConfig
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER = '82a12389-14d6-40c6-b320-4674f6bbb344'
UNMANAGED = 'b7f6a2b7-2222-4a3f-9c11-8e6b2a2f9911'
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _config(limit_gb=5, squad_uuid=SQUAD, name=None):
    return PremiumSquadConfig(squad_uuid=squad_uuid, limit_gb=limit_gb, name=name)


def _bare_state(*, closed_at=None, is_limited=False, limit_bytes=5 * BYTES_IN_GB, extra_bytes=0, used_bytes=0):
    """Несохранённый ORM-объект — для тестов классификации без БД."""
    return SubscriptionPremiumTraffic(
        subscription_id=1,
        squad_uuid=SQUAD,
        limit_bytes=limit_bytes,
        extra_bytes=extra_bytes,
        used_bytes=used_bytes,
        is_limited=is_limited,
        closed_at=closed_at,
        period_start_at=NOW,
    )


def _subscription(limits=None, subscription_id=1, connected=None):
    return SimpleNamespace(
        id=subscription_id,
        connected_squads=connected if connected is not None else [SQUAD],
        tariff=SimpleNamespace(
            server_traffic_limits=limits if limits is not None else {SQUAD: {'traffic_limit_gb': 5}}
        ),
    )


def _callback(data: str):
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=1)
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _db_user():
    return SimpleNamespace(id=999, language='ru')


# ============================= Классификация состояний =============================


def test_no_limit_is_not_active_and_not_closed():
    kind = users_mod._classify_premium_traffic_state(_config(limit_gb=0), None)

    assert kind is users_mod.PremiumSquadCardState.NO_LIMIT


def test_active_state_without_history():
    kind = users_mod._classify_premium_traffic_state(_config(limit_gb=5), None)

    assert kind is users_mod.PremiumSquadCardState.ACTIVE


def test_exhausted_by_usage_is_not_closed():
    state = _bare_state(is_limited=True, closed_at=None, used_bytes=5 * BYTES_IN_GB)

    kind = users_mod._classify_premium_traffic_state(_config(limit_gb=5), state)

    assert kind is users_mod.PremiumSquadCardState.EXHAUSTED


def test_admin_closed_wins_over_exhaustion():
    state = _bare_state(is_limited=True, closed_at=NOW, used_bytes=5 * BYTES_IN_GB)

    kind = users_mod._classify_premium_traffic_state(_config(limit_gb=5), state)

    assert kind is users_mod.PremiumSquadCardState.CLOSED


def test_admin_closed_wins_even_when_usage_alone_would_not_be_exhausted():
    """closed_at — durable-флаг, проверяется первым независимо от used_bytes."""
    state = _bare_state(is_limited=True, closed_at=NOW, used_bytes=0)

    kind = users_mod._classify_premium_traffic_state(_config(limit_gb=5), state)

    assert kind is users_mod.PremiumSquadCardState.CLOSED


def test_reopened_but_not_yet_restored_is_a_distinct_state_from_closed():
    """Открыт, но воркер ещё не вернул сквад в панель — НЕ 'закрыто администратором'."""
    state = _bare_state(is_limited=True, closed_at=None, used_bytes=0)

    kind = users_mod._classify_premium_traffic_state(_config(limit_gb=5), state)

    assert kind is users_mod.PremiumSquadCardState.REOPENED_PENDING
    assert kind is not users_mod.PremiumSquadCardState.CLOSED


def test_row_rendering_never_calls_reopened_pending_state_closed_by_admin():
    state = _bare_state(is_limited=True, closed_at=None, used_bytes=0)
    row = {'squad_uuid': SQUAD, 'config': _config(limit_gb=5), 'state': state, 'connected': True, 'name': 'DE-1'}

    rendered = users_mod._format_premium_traffic_row(row, users_mod.PremiumSquadCardState.REOPENED_PENDING)

    assert 'закрыт администратором' not in rendered.lower()
    assert 'ожидает возврата' in rendered.lower()


def test_row_rendering_closed_by_admin_says_so_explicitly():
    state = _bare_state(is_limited=True, closed_at=NOW, used_bytes=5 * BYTES_IN_GB)
    row = {'squad_uuid': SQUAD, 'config': _config(limit_gb=5), 'state': state, 'connected': True, 'name': 'DE-1'}

    rendered = users_mod._format_premium_traffic_row(row, users_mod.PremiumSquadCardState.CLOSED)

    assert 'закрыт администратором' in rendered.lower()


def test_row_rendering_no_limit_never_says_closed():
    row = {'squad_uuid': SQUAD, 'config': _config(limit_gb=0), 'state': None, 'connected': True, 'name': 'DE-1'}

    rendered = users_mod._format_premium_traffic_row(row, users_mod.PremiumSquadCardState.NO_LIMIT)

    # Текст явно проговаривает «не закрыт» — это то самое смешение состояний
    # 1 и 3, которого требует избежать задача, — но не должен утверждать
    # обратное или упоминать закрытие администратором.
    assert 'закрыт администратором' not in rendered.lower()
    assert 'без отдельного лимита' in rendered.lower()


# ============================= Сбор "относящихся" сквадов =============================


class TestCollectPremiumTrafficRows:
    async def test_connected_and_configured_squad_is_a_managed_row(self, monkeypatch):
        monkeypatch.setattr(users_mod, 'get_squad_display_names', AsyncMock(return_value={SQUAD: 'DE-1'}))
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription(limits={SQUAD: {'traffic_limit_gb': 5}}, connected=[SQUAD])

            rows, unmanaged = await users_mod._collect_premium_traffic_rows(db, subscription)

            assert len(rows) == 1
            assert rows[0]['squad_uuid'] == SQUAD
            assert unmanaged == []

    async def test_orphaned_closed_state_survives_squad_leaving_tariff(self, monkeypatch):
        """closed_at переживает уход сквада из премиум-конфига тарифа — должен остаться видимым."""
        monkeypatch.setattr(users_mod, 'get_squad_display_names', AsyncMock(return_value={}))
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            state.is_limited = True
            await db.commit()

            # Сквад убрали из тарифа целиком и отключили от подписки.
            subscription = _subscription(limits={}, connected=[])

            rows, unmanaged = await users_mod._collect_premium_traffic_rows(db, subscription)

            assert len(rows) == 1
            assert rows[0]['squad_uuid'] == SQUAD
            kind = users_mod._classify_premium_traffic_state(rows[0]['config'], rows[0]['state'])
            assert kind is users_mod.PremiumSquadCardState.CLOSED

    async def test_unmanaged_squad_is_footer_only_not_a_managed_row(self, monkeypatch):
        """Подключённый сквад без отдельного лимита и без истории — не строка, а сноска."""
        monkeypatch.setattr(users_mod, 'get_squad_display_names', AsyncMock(return_value={UNMANAGED: 'FR-1'}))
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription(limits={}, connected=[UNMANAGED])

            rows, unmanaged = await users_mod._collect_premium_traffic_rows(db, subscription)

            assert rows == []
            assert unmanaged == ['FR-1']

    async def test_no_relevant_squads_returns_empty_without_display_names_query(self, monkeypatch):
        display_names = AsyncMock(return_value={})
        monkeypatch.setattr(users_mod, 'get_squad_display_names', display_names)
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription(limits={}, connected=[])

            rows, unmanaged = await users_mod._collect_premium_traffic_rows(db, subscription)

            assert rows == []
            assert unmanaged == []
            display_names.assert_not_awaited()


# ============================= Рубильник PREMIUM_TRAFFIC_ENABLED =============================


class TestKillSwitch:
    async def test_disabled_kill_switch_is_shown_but_does_not_hide_management_controls(self, monkeypatch):
        monkeypatch.setattr(users_mod.settings, 'PREMIUM_TRAFFIC_ENABLED', False, raising=False)
        monkeypatch.setattr(users_mod, '_resolve_admin_subscription', AsyncMock(return_value=_subscription()))
        monkeypatch.setattr(users_mod, 'get_squad_display_names', AsyncMock(return_value={SQUAD: 'DE-1'}))
        async with memory_session(monkeypatch, TABLES) as db:
            callback = _callback('admin_user_premium_1')

            ok = await users_mod._render_premium_traffic_screen(callback, db, 1, None)

            assert ok is True
            text = callback.message.edit_text.await_args.args[0]
            assert 'глобально отключена' in text.lower()
            keyboard = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
            callbacks = [button.callback_data for row in keyboard for button in row]
            assert any(cb.startswith('admin_user_premium_sq_') for cb in callbacks)


# ============================= Ручная выдача трафика =============================


class TestGrantPremiumTraffic:
    async def test_grant_adds_extra_bytes_to_current_period(self, monkeypatch):
        monkeypatch.setattr(users_mod, '_resolve_admin_subscription', AsyncMock(return_value=_subscription()))
        async with memory_session(monkeypatch, TABLES) as db:
            ok, message = await users_mod._grant_premium_traffic(db, 1, None, SQUAD, 10, admin_id=999)

            assert ok is True
            assert '10 ГБ' in message
            state = await get_state(db, 1, SQUAD)
            assert state.extra_bytes == 10 * BYTES_IN_GB

    async def test_grant_is_rejected_when_squad_has_no_positive_limit_in_current_tariff(self, monkeypatch):
        """Как и ручная выдача в кабинете: докупать лимит, которого сейчас нет, нельзя."""
        subscription = _subscription(limits={SQUAD: {'traffic_limit_gb': 0}})
        monkeypatch.setattr(users_mod, '_resolve_admin_subscription', AsyncMock(return_value=subscription))
        async with memory_session(monkeypatch, TABLES) as db:
            ok, message = await users_mod._grant_premium_traffic(db, 1, None, SQUAD, 10, admin_id=999)

            assert ok is False
            state = await get_state(db, 1, SQUAD)
            assert state is None

    async def test_grant_does_not_clear_admin_closed_flag(self, monkeypatch):
        """add_extra_bytes сам не снимает closed_at — докупка не равна открытию доступа."""
        monkeypatch.setattr(users_mod, '_resolve_admin_subscription', AsyncMock(return_value=_subscription()))
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            state.is_limited = True
            state.used_bytes = 5 * BYTES_IN_GB
            await db.commit()

            ok, _message = await users_mod._grant_premium_traffic(db, 1, None, SQUAD, 10, admin_id=999)

            assert ok is True
            reread = await get_state(db, 1, SQUAD)
            assert reread.closed_at is not None


# ============================= Закрытие / открытие доступа из карточки =============================


class TestCloseAndReopenFromCard:
    async def test_confirmation_step_does_not_mutate_state(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            await db.commit()

            callback = _callback('admin_user_premium_close_1_0')
            await _unwrap(users_mod.ask_close_premium_access)(callback, _db_user())

            state = await get_state(db, 1, SQUAD)
            assert state.closed_at is None
            callback.message.edit_text.assert_awaited_once()

    async def test_confirm_close_sets_closed_at_and_exhausts_state(self, monkeypatch):
        subscription = _subscription()
        monkeypatch.setattr(users_mod, '_resolve_admin_subscription', AsyncMock(return_value=subscription))
        monkeypatch.setattr(users_mod, 'get_squad_display_names', AsyncMock(return_value={}))
        async with memory_session(monkeypatch, TABLES) as db:
            callback = _callback('admin_user_premium_close_confirm_1_0')

            await _unwrap(users_mod.confirm_close_premium_access)(callback, _db_user(), db)

            state = await get_state(db, 1, SQUAD)
            assert state.closed_at is not None
            assert state.is_limited is True
            assert state.used_bytes >= state.total_limit_bytes

    async def test_reopen_clears_closed_at_and_reports_pending_restore(self, monkeypatch):
        subscription = _subscription(connected=[])  # сквад больше не подключён -> is_limited должен снимаемым
        monkeypatch.setattr(users_mod, '_resolve_admin_subscription', AsyncMock(return_value=subscription))
        monkeypatch.setattr(users_mod, 'get_squad_display_names', AsyncMock(return_value={}))
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            state.is_limited = True
            state.used_bytes = 5 * BYTES_IN_GB
            await db.commit()

            callback = _callback('admin_user_premium_reopen_1_0')
            await _unwrap(users_mod.reopen_premium_access_handler)(callback, _db_user(), db)

            reread = await get_state(db, 1, SQUAD)
            assert reread.closed_at is None
            assert reread.used_bytes == 0

    async def test_reopen_while_still_connected_reports_pending_not_closed_wording(self, monkeypatch):
        """Сквад ещё подключён -> is_limited остаётся True: тот самый переходный сигнал."""
        subscription = _subscription(connected=[SQUAD])
        monkeypatch.setattr(users_mod, '_resolve_admin_subscription', AsyncMock(return_value=subscription))
        monkeypatch.setattr(users_mod, 'get_squad_display_names', AsyncMock(return_value={}))
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.closed_at = NOW
            state.is_limited = True
            state.used_bytes = 5 * BYTES_IN_GB
            await db.commit()

            callback = _callback('admin_user_premium_reopen_1_0')
            await _unwrap(users_mod.reopen_premium_access_handler)(callback, _db_user(), db)

            reread = await get_state(db, 1, SQUAD)
            assert reread.closed_at is None
            assert reread.is_limited is True
            text = callback.message.edit_text.await_args.args[0]
            assert 'закрыт администратором' not in text.lower()
            assert 'ожидает возврата' in text.lower()


# ============================= Разбор callback_data =============================


def test_extract_premium_squad_context_without_subscription_id():
    assert users_mod._extract_premium_squad_context('admin_user_premium_sq_123_4') == (123, None, 4)


def test_extract_premium_squad_context_with_subscription_id():
    assert users_mod._extract_premium_squad_context('admin_user_premium_sq_123_s456_4') == (123, 456, 4)


def test_extract_premium_squad_value_context_without_subscription_id():
    assert users_mod._extract_premium_squad_value_context('admin_user_premium_grant_set_123_4_10') == (
        123,
        None,
        4,
        10,
    )


def test_extract_premium_squad_value_context_with_subscription_id():
    assert users_mod._extract_premium_squad_value_context('admin_user_premium_grant_set_123_s456_4_10') == (
        123,
        456,
        4,
        10,
    )


def test_callback_data_stays_within_telegram_limit_for_worst_case_ids():
    user_id = 9_999_999_999
    subscription_id = 999_999
    idx = 42
    gb = 100_000
    candidates = [
        f'admin_user_premium_grant_set_{user_id}_s{subscription_id}_{idx}_{gb}',
        f'admin_user_premium_close_confirm_{user_id}_s{subscription_id}_{idx}',
        f'admin_user_premium_reopen_{user_id}_s{subscription_id}_{idx}',
        f'admin_user_premium_sq_{user_id}_s{subscription_id}_{idx}',
    ]
    for callback_data in candidates:
        assert len(callback_data.encode('utf-8')) <= 64, callback_data
