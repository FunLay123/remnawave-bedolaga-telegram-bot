"""Воркер премиум-трафика: подсчёт, снятие, возврат, устойчивость к сбоям."""

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud.premium_traffic import get_or_create_state, get_state
from app.database.models import (
    PromoGroup,
    ServerSquad,
    Subscription,
    SubscriptionPremiumTraffic,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services.premium_traffic_service import PremiumTrafficService, _Target
from app.utils.premium_traffic import BYTES_IN_GB, PremiumSquadConfig
from tests.fixtures.sqlite_memory import memory_session


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER_SQUAD = '82a12389-14d6-40c6-b320-4674f6bbb344'
NODE_A = '3ca79b63-1b0d-49ec-b2d7-6eb264a560c5'
NODE_B = '7f2c1a90-0000-4000-8000-000000000002'
PANEL_USER_ID = 42
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

# Уборке осиротевших состояний нужны настоящие подписка и тариф: она сверяет
# строку состояния с премиальным списком ТЕКУЩЕГО тарифа подписки.
ORPHAN_TABLES = (
    User.__table__,
    PromoGroup.__table__,
    Tariff.__table__,
    # Тариф тянет промогруппы жадно — без связки запрос к подпискам падает.
    tariff_promo_groups,
    Subscription.__table__,
    SubscriptionPremiumTraffic.__table__,
    # Имена премиум-серверов воркер подтягивает из справочника.
    ServerSquad.__table__,
)


class FakeRemnawaveApi:
    """Заглушка панели: отдаёт заранее заданный расход по нодам."""

    def __init__(self, usage_by_node=None, nodes=(NODE_A,), panel_user=None):
        self.usage_by_node = usage_by_node or {}
        self.nodes = list(nodes)
        self.panel_user = panel_user
        self.usage_calls: list[tuple[list[str], str, str]] = []
        self.node_calls = 0

    async def get_internal_squad_accessible_nodes(self, squad_uuid):
        self.node_calls += 1
        return [SimpleNamespace(uuid=uuid) for uuid in self.nodes]

    async def get_bandwidth_stats_nodes_usage(self, node_uuids, start_date, end_date, min_total_bytes=0):
        self.usage_calls.append((list(node_uuids), start_date, end_date))
        return {'nodes': [{'uuid': uuid, 'users': self.usage_by_node.get(uuid, [])} for uuid in node_uuids]}

    async def get_user_by_id(self, user_id):
        return self.panel_user


def _state(limit_gb=5, used_bytes=0, extra_bytes=0, is_limited=False, notified_80=False, baseline_bytes=0):
    """Лёгкий двойник состояния: воркер обращается только к этим полям."""
    limit_bytes = limit_gb * BYTES_IN_GB

    class _State:
        def __init__(self):
            self.limit_bytes = limit_bytes
            self.extra_bytes = extra_bytes
            self.used_bytes = used_bytes
            self.is_limited = is_limited
            self.notified_80 = notified_80
            # По умолчанию поправка на первые сутки уже снята: тесты решений
            # про пороги, а не про неё — у неё свой набор.
            self.baseline_bytes = baseline_bytes
            self.last_checked_at = None
            self.period_start_at = NOW
            self.panel_reset_ack_at = None

        @property
        def total_limit_bytes(self):
            return self.limit_bytes + self.extra_bytes

        @property
        def is_exhausted(self):
            return self.total_limit_bytes > 0 and self.used_bytes >= self.total_limit_bytes

    return _State()


def _target(subscription_id=1, limit_gb=5, connected=(SQUAD,), language='ru', telegram_id=555, display_name='LTE'):
    user = SimpleNamespace(telegram_id=telegram_id, language=language, remnawave_id=PANEL_USER_ID)
    subscription = SimpleNamespace(
        id=subscription_id,
        user=user,
        connected_squads=list(connected),
        start_date=NOW - timedelta(days=10),
        tariff=SimpleNamespace(traffic_reset_mode='MONTH'),
        remnawave_id=PANEL_USER_ID,
    )
    return _Target(subscription, PremiumSquadConfig(squad_uuid=SQUAD, limit_gb=limit_gb), PANEL_USER_ID, display_name)


class _Db:
    """Сессия-заглушка: воркеру от неё нужен commit и вложенная транзакция."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    def begin_nested(self):
        return _NestedTransaction()


class _NestedTransaction:
    """Заглушка SAVEPOINT: реальный откат в этих тестах не проверяется."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestUsageCollection:
    async def test_bytes_are_summed_across_all_nodes_of_the_squad(self):
        api = FakeRemnawaveApi(
            usage_by_node={
                NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 1000}, {'id': 7, 'totalBytes': 50}],
                NODE_B: [{'id': PANEL_USER_ID, 'totalBytes': 2000}],
            },
            nodes=(NODE_A, NODE_B),
        )
        service = PremiumTrafficService()

        usage = await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)

        assert usage[PANEL_USER_ID] == 3000
        assert usage[7] == 50

    async def test_one_request_covers_all_nodes_and_users(self):
        """Стоимость прохода растёт от числа сквадов, а не пользователей."""
        api = FakeRemnawaveApi(nodes=(NODE_A, NODE_B))
        service = PremiumTrafficService()

        await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)

        assert len(api.usage_calls) == 1
        assert api.usage_calls[0][0] == [NODE_A, NODE_B]

    async def test_end_date_covers_today(self):
        """Панель включает конечную дату непредсказуемо; лишние сутки не занижают."""
        api = FakeRemnawaveApi()
        service = PremiumTrafficService()

        await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)

        _nodes, start, end = api.usage_calls[0]
        assert start == '2026-09-01'
        assert end == '2026-09-07'

    async def test_squad_without_nodes_reports_no_usage(self):
        api = FakeRemnawaveApi(nodes=())
        service = PremiumTrafficService()

        assert await service._fetch_usage(api, SQUAD, '2026-09-01', NOW) == {}
        assert api.usage_calls == []

    async def test_nodes_are_cached_between_passes(self):
        """Ноды сквада меняются редко, а спрашивают их на каждом проходе."""
        api = FakeRemnawaveApi()
        service = PremiumTrafficService()

        await service._fetch_usage(api, SQUAD, '2026-09-01', NOW)
        await service._fetch_usage(api, SQUAD, '2026-09-02', NOW)

        assert api.node_calls == 1

    async def test_entries_without_user_id_are_skipped(self):
        api = FakeRemnawaveApi(usage_by_node={NODE_A: [{'totalBytes': 999}, {'id': None, 'totalBytes': 5}]})
        service = PremiumTrafficService()

        assert await service._fetch_usage(api, SQUAD, '2026-09-01', NOW) == {}


class TestDecisions:
    async def _apply(self, service, target, state, used_bytes, monkeypatch, api=None, panel_user=None):
        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.subscription.id)

        monkeypatch.setattr(service, '_push_squads', _push)
        outcome = await service._apply_usage(
            _Db(),
            api or FakeRemnawaveApi(),
            target,
            used_bytes=used_bytes,
            period_start=NOW,
            now=NOW,
            panel_user=panel_user,
        )
        return outcome, pushed

    async def test_exhausted_squad_is_limited_and_pushed(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5)

        outcome, pushed = await self._apply(service, _target(), state, 5 * BYTES_IN_GB, monkeypatch)

        assert outcome == 'limited'
        assert state.is_limited is True
        assert pushed == [1]

    async def test_usage_below_limit_changes_nothing(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5)

        outcome, pushed = await self._apply(service, _target(), state, BYTES_IN_GB, monkeypatch)

        assert outcome is None
        assert state.is_limited is False
        assert pushed == []

    async def test_warning_is_sent_once_at_eighty_percent(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5)

        outcome, _ = await self._apply(service, _target(), state, 4 * BYTES_IN_GB, monkeypatch)
        assert outcome == 'warned'
        assert state.notified_80 is True

        second, _ = await self._apply(service, _target(), state, 4 * BYTES_IN_GB, monkeypatch)
        assert second is None

    async def test_topup_restores_a_limited_squad(self, monkeypatch):
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)
        state.extra_bytes = 3 * BYTES_IN_GB

        outcome, pushed = await self._apply(service, _target(), state, 5 * BYTES_IN_GB, monkeypatch)

        assert outcome == 'restored'
        assert state.is_limited is False
        assert pushed == [1]

    async def test_recorded_usage_never_drops(self, monkeypatch):
        """Просадка выборки не должна вернуть доступ к исчерпанному скваду."""
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)

        outcome, _ = await self._apply(service, _target(), state, 0, monkeypatch)

        assert state.used_bytes == 5 * BYTES_IN_GB
        assert state.is_limited is True
        assert outcome is None

    async def test_squad_missing_in_panel_is_resynced(self, monkeypatch):
        """Докупка снимает `is_limited` сама и сама возвращает сквад в панель.

        Если та отправка не дошла, ветки «снять»/«вернуть» сюда уже не попадут —
        флаг-то снят. Сверка с фактическим состоянием панели это добирает.
        """
        service = PremiumTrafficService()
        state = _state(limit_gb=5, extra_bytes=5 * BYTES_IN_GB, used_bytes=5 * BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.config.squad_uuid)

        monkeypatch.setattr(service, '_push_squads', _push)

        outcome = await service._apply_usage(
            _Db(),
            FakeRemnawaveApi(),
            _target(),
            used_bytes=5 * BYTES_IN_GB,
            period_start=NOW,
            now=NOW,
            panel_user=SimpleNamespace(active_internal_squads=[]),
        )

        assert outcome == 'restored'
        assert pushed == [SQUAD]

    async def test_squad_missing_in_panel_push_failure_does_not_raise(self, monkeypatch):
        """Досылка упала — сверка должна проглотить отказ, а не уронить проход."""
        service = PremiumTrafficService()
        state = _state(limit_gb=5, extra_bytes=5 * BYTES_IN_GB, used_bytes=5 * BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)

        async def _push(_db, _api, _tgt):
            raise RuntimeError('панель недоступна')

        monkeypatch.setattr(service, '_push_squads', _push)

        from structlog.testing import capture_logs

        with capture_logs() as logs:
            outcome = await service._apply_usage(
                _Db(),
                FakeRemnawaveApi(),
                _target(),
                used_bytes=5 * BYTES_IN_GB,
                period_start=NOW,
                now=NOW,
                panel_user=SimpleNamespace(active_internal_squads=[]),
            )

        assert outcome is None
        assert any(
            entry['event'] == 'Не удалось досинхронизировать премиум-сквад с панелью'
            and entry['log_level'] == 'warning'
            for entry in logs
        )

    async def test_squad_missing_in_panel_deferred_push_is_not_applied(self, monkeypatch):
        """Досылка отложена оверлеем — не считать себя применённой, повторить."""
        from app.services.grace_access_runtime import DeferredPanelUpdate

        service = PremiumTrafficService()
        state = _state(limit_gb=5, extra_bytes=5 * BYTES_IN_GB, used_bytes=5 * BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)

        async def _push(_db, _api, tgt):
            return DeferredPanelUpdate(SimpleNamespace(id=tgt.panel_user_id))

        monkeypatch.setattr(service, '_push_squads', _push)

        from structlog.testing import capture_logs

        with capture_logs() as logs:
            outcome = await service._apply_usage(
                _Db(),
                FakeRemnawaveApi(),
                _target(),
                used_bytes=5 * BYTES_IN_GB,
                period_start=NOW,
                now=NOW,
                panel_user=SimpleNamespace(active_internal_squads=[]),
            )

        assert outcome is None
        assert any(
            entry['event'] == 'Досинхронизация премиум-сквада отложена: открыт grace-оверлей'
            and entry['log_level'] == 'info'
            for entry in logs
        )

    async def test_squad_present_in_panel_is_left_alone(self, monkeypatch):
        """Панель отдаёт сквады объектами {uuid, name}, а не строками.

        Сравнение по строкам не находило бы совпадений никогда, и бот дёргал бы
        панель на каждом проходе.
        """
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.config.squad_uuid)

        monkeypatch.setattr(service, '_push_squads', _push)

        outcome = await service._apply_usage(
            _Db(),
            FakeRemnawaveApi(),
            _target(),
            used_bytes=BYTES_IN_GB,
            period_start=NOW,
            now=NOW,
            panel_user=SimpleNamespace(active_internal_squads=[{'uuid': SQUAD, 'name': 'LTE'}]),
        )

        assert outcome is None
        assert pushed == []

    async def test_limited_squad_still_served_by_the_panel_is_resent(self, monkeypatch):
        """Отправка снятия упала после коммита — база и панель разошлись.

        Ветки «снять»/«вернуть» сюда уже не попадут: флаг стоит, лимит исчерпан.
        Досылает только сверка — иначе клиент пользуется премиумом бессрочно.
        """
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)

        outcome, pushed = await self._apply(
            service,
            _target(),
            state,
            5 * BYTES_IN_GB,
            monkeypatch,
            panel_user=SimpleNamespace(active_internal_squads=[{'uuid': SQUAD, 'name': 'LTE'}]),
        )

        assert outcome == 'limited'
        assert state.is_limited is True
        assert pushed == [1]

    async def test_limited_squad_still_served_push_failure_does_not_raise(self, monkeypatch):
        """Повторное снятие упало — сверка не должна ронять весь проход."""
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)

        async def _push(_db, _api, _tgt):
            raise RuntimeError('панель недоступна')

        monkeypatch.setattr(service, '_push_squads', _push)

        from structlog.testing import capture_logs

        with capture_logs() as logs:
            outcome = await service._apply_usage(
                _Db(),
                FakeRemnawaveApi(),
                _target(),
                used_bytes=5 * BYTES_IN_GB,
                period_start=NOW,
                now=NOW,
                panel_user=SimpleNamespace(active_internal_squads=[{'uuid': SQUAD, 'name': 'LTE'}]),
            )

        assert outcome is None
        assert any(
            entry['event'] == 'Не удалось доснять премиум-сквад в панели' and entry['log_level'] == 'warning'
            for entry in logs
        )

    async def test_limited_squad_still_served_deferred_push_is_not_applied(self, monkeypatch):
        """Повторное снятие отложено оверлеем — не считать себя применённым."""
        from app.services.grace_access_runtime import DeferredPanelUpdate

        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)

        async def _push(_db, _api, tgt):
            return DeferredPanelUpdate(SimpleNamespace(id=tgt.panel_user_id))

        monkeypatch.setattr(service, '_push_squads', _push)

        from structlog.testing import capture_logs

        with capture_logs() as logs:
            outcome = await service._apply_usage(
                _Db(),
                FakeRemnawaveApi(),
                _target(),
                used_bytes=5 * BYTES_IN_GB,
                period_start=NOW,
                now=NOW,
                panel_user=SimpleNamespace(active_internal_squads=[{'uuid': SQUAD, 'name': 'LTE'}]),
            )

        assert outcome is None
        assert any(
            entry['event'] == 'Повторное снятие премиум-сквада отложено: открыт grace-оверлей'
            and entry['log_level'] == 'info'
            for entry in logs
        )

    async def test_limited_squad_absent_from_the_panel_is_left_alone(self, monkeypatch):
        """Снятие доехало — досылать нечего, иначе панель дёргалась бы каждый проход."""
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)

        outcome, pushed = await self._apply(
            service,
            _target(),
            state,
            5 * BYTES_IN_GB,
            monkeypatch,
            panel_user=SimpleNamespace(active_internal_squads=[{'uuid': OTHER_SQUAD, 'name': 'Базовый'}]),
        )

        assert outcome is None
        assert pushed == []

    async def test_unknown_panel_state_does_not_resend_a_limited_squad(self, monkeypatch):
        """Неполный ответ панели — не повод считать сквад неснятым."""
        service = PremiumTrafficService()

        for panel_user in (None, SimpleNamespace(active_internal_squads=None)):
            state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB, is_limited=True)
            outcome, pushed = await self._apply(
                service,
                _target(),
                state,
                5 * BYTES_IN_GB,
                monkeypatch,
                panel_user=panel_user,
            )
            assert outcome is None
            assert pushed == []

    async def test_unknown_panel_state_does_not_trigger_a_push(self, monkeypatch):
        """Панель не ответила — сверять не с чем, трогать ничего нельзя."""
        service = PremiumTrafficService()
        state = _state(limit_gb=5, used_bytes=BYTES_IN_GB)

        async def _get_state(_db, _sub_id, _squad):
            return state

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _get_state)
        pushed = []

        async def _push(_db, _api, tgt):
            pushed.append(tgt.config.squad_uuid)

        monkeypatch.setattr(service, '_push_squads', _push)

        for panel_user in (None, SimpleNamespace(active_internal_squads=None)):
            outcome = await service._apply_usage(
                _Db(),
                FakeRemnawaveApi(),
                _target(),
                used_bytes=BYTES_IN_GB,
                period_start=NOW,
                now=NOW,
                panel_user=panel_user,
            )
            assert outcome is None

        assert pushed == []

    async def test_missing_state_is_not_an_error(self, monkeypatch):
        service = PremiumTrafficService()

        async def _none(_db, _sub_id, _squad):
            return None

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _none)

        outcome = await service._apply_usage(
            _Db(), FakeRemnawaveApi(), _target(), used_bytes=0, period_start=NOW, now=NOW
        )

        assert outcome is None


class TestPanelUserCache:
    """Карточка панельного пользователя — самая дорогая часть прохода.

    Запрашивать её на каждого подписчика каждые пять минут значит держать на
    панели больше десятка запросов в секунду при пяти тысячах подписок. Обе
    нужные величины (дата сброса и набор сквадов) меняются куда реже.
    """

    async def test_card_is_requested_once_and_reused(self):
        api = FakeRemnawaveApi(panel_user=SimpleNamespace(last_traffic_reset_at=None))
        service = PremiumTrafficService()

        await service._panel_user(api, PANEL_USER_ID)
        await service._panel_user(api, PANEL_USER_ID)

        assert service._cached_panel_user(PANEL_USER_ID) is not None

    async def test_our_own_change_drops_the_cache(self):
        """Иначе сверка увидела бы протухший снимок и отправила бы всё заново."""
        api = FakeRemnawaveApi(panel_user=SimpleNamespace(last_traffic_reset_at=None))
        service = PremiumTrafficService()
        await service._panel_user(api, PANEL_USER_ID)

        service.invalidate_panel_user(PANEL_USER_ID)

        assert service._cached_panel_user(PANEL_USER_ID) is None

    async def test_expired_entry_is_refetched(self, monkeypatch):
        api = FakeRemnawaveApi(panel_user=SimpleNamespace(last_traffic_reset_at=None))
        service = PremiumTrafficService()
        await service._panel_user(api, PANEL_USER_ID)

        # Протухание: подменяем срок годности на прошлое.
        _expires, panel_user = service._panel_users[PANEL_USER_ID]
        service._panel_users[PANEL_USER_ID] = (-1.0, panel_user)

        assert service._cached_panel_user(PANEL_USER_ID) is None

    async def test_unknown_user_is_not_cached(self):
        api = FakeRemnawaveApi(panel_user=SimpleNamespace(last_traffic_reset_at=None))
        service = PremiumTrafficService()

        assert service._cached_panel_user(999) is None
        void = await service._panel_user(api, 999)
        assert void is not None


class TestFirstDayCorrection:
    """Статистика панели задаётся датами без времени.

    Из-за этого запрос за день начала периода приносит и трафик, потраченный до
    сброса. Если период начался не в полночь, первый замер целиком относится к
    прошлому периоду — его и вычитаем.
    """

    def test_midday_start_subtracts_the_first_measurement(self):
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)

        # Первый замер: 8 ГБ, но всё это — вчерашний период.
        assert PremiumTrafficService._net_usage(state, 8 * BYTES_IN_GB, start, start) == 0
        assert state.baseline_bytes == 8 * BYTES_IN_GB

        # Дальше считается только прирост.
        later = start + timedelta(hours=2)
        assert PremiumTrafficService._net_usage(state, 11 * BYTES_IN_GB, start, later) == 3 * BYTES_IN_GB

    def test_midnight_start_needs_no_correction(self):
        """Календарные режимы начинаются в полночь — запрос точен."""
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

        assert PremiumTrafficService._net_usage(state, 4 * BYTES_IN_GB, start, NOW) == 4 * BYTES_IN_GB
        assert state.baseline_bytes == 0

    def test_late_first_check_does_not_swallow_real_usage(self):
        """Воркер простоял сутки — вычитать уже нечего.

        Первый замер включал бы законный расход нового периода, и поправка
        подарила бы пользователю лимит.
        """
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)
        much_later = start + timedelta(days=2)

        assert PremiumTrafficService._net_usage(state, 9 * BYTES_IN_GB, start, much_later) == 9 * BYTES_IN_GB
        assert state.baseline_bytes == 0

    def test_correction_is_measured_once_per_period(self):
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)

        PremiumTrafficService._net_usage(state, 8 * BYTES_IN_GB, start, start)
        # Повторный замер поправку не переопределяет.
        PremiumTrafficService._net_usage(state, 12 * BYTES_IN_GB, start, start)

        assert state.baseline_bytes == 8 * BYTES_IN_GB

    def test_usage_never_goes_negative(self):
        """Панель отдала меньше, чем на момент замера поправки."""
        state = _state(limit_gb=5, baseline_bytes=None)
        start = datetime(2026, 9, 6, 17, 9, tzinfo=UTC)
        PremiumTrafficService._net_usage(state, 8 * BYTES_IN_GB, start, start)

        assert PremiumTrafficService._net_usage(state, BYTES_IN_GB, start, start) == 0


class TestIntervalSettings:
    @pytest.mark.parametrize('raw,expected', [(300, 300), (600, 600), (10, 60), ('900', 900), ('abc', 300)])
    def test_interval_is_clamped_and_coerced(self, monkeypatch, raw, expected):
        """Чаще минуты смысла нет: панель агрегирует статистику с задержкой."""
        service = PremiumTrafficService()
        monkeypatch.setattr(
            'app.services.premium_traffic_service.settings', SimpleNamespace(PREMIUM_TRAFFIC_CHECK_INTERVAL_SECONDS=raw)
        )

        assert service.get_check_interval_seconds() == expected


class TestNotifications:
    async def test_nothing_is_sent_without_a_bot(self):
        service = PremiumTrafficService()

        await service._notify_exhausted(_target(), _state())  # не должно падать

    async def test_message_names_the_server(self, monkeypatch):
        """С несколькими премиум-серверами иначе не понять, на каком кончилось."""
        service = PremiumTrafficService()
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, **_kwargs):
                sent.append(text)

        service.set_bot(_Bot())

        await service._notify_exhausted(_target(display_name='📱 Мобильный резерв 2'), _state())

        assert '📱 Мобильный резерв 2' in sent[0]

    async def test_message_falls_back_to_a_generic_label(self, monkeypatch):
        """Сервер удалили из справочника, своё название не задали."""
        service = PremiumTrafficService()
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, **_kwargs):
                sent.append(text)

        service.set_bot(_Bot())

        await service._notify_exhausted(_target(display_name=''), _state())

        assert sent and 'ремиум' in sent[0]

    async def test_message_carries_used_and_limit(self, monkeypatch):
        service = PremiumTrafficService()
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, **_kwargs):
                sent.append((chat_id, text))

        service.set_bot(_Bot())
        state = _state(limit_gb=5, used_bytes=5 * BYTES_IN_GB)

        await service._notify_exhausted(_target(), state)

        assert sent and sent[0][0] == 555
        assert '5' in sent[0][1]

    async def test_delivery_failure_does_not_propagate(self, monkeypatch):
        """Упавшее уведомление не должно валить проход воркера."""
        service = PremiumTrafficService()

        class _Bot:
            async def send_message(self, *_args, **_kwargs):
                raise RuntimeError('telegram недоступен')

        service.set_bot(_Bot())

        await service._notify_exhausted(_target(), _state())

    async def test_user_without_telegram_id_is_skipped(self):
        service = PremiumTrafficService()
        service.set_bot(object())  # обращение к нему упало бы

        await service._notify_exhausted(_target(telegram_id=None), _state())


# ------------------------------------------------- осиротевшие состояния


async def _seed_subscription(
    db,
    *,
    premium_limits,
    connected=(SQUAD,),
    subscription_id=1,
    tariff_id=1,
    reset_mode='MONTH',
    panel_user_id=PANEL_USER_ID,
):
    """Подписка с тарифом: премиальный список задаётся `premium_limits`."""
    db.add(
        Tariff(
            id=tariff_id,
            name='Премиум',
            period_prices={'30': 10000},
            traffic_limit_gb=100,
            device_limit=1,
            server_traffic_limits=premium_limits,
            traffic_reset_mode=reset_mode,
        )
    )
    db.add(
        Subscription(
            id=subscription_id,
            user_id=1,
            status='active',
            tariff_id=tariff_id,
            connected_squads=list(connected),
            start_date=NOW - timedelta(days=10),
            end_date=NOW + timedelta(days=20),
            # Колонка уникальна: подписки в одном тесте должны указывать на
            # разных панельных пользователей.
            remnawave_id=panel_user_id,
            # Тоже уникальна, а server_default пустой — двум строкам нужен свой.
            remnawave_short_id=f'sid{subscription_id}',
        )
    )
    await db.commit()


async def _seed_state(db, *, subscription_id=1, squad_uuid=SQUAD, is_limited=False, baseline_bytes=None):
    state = await get_or_create_state(
        db,
        subscription_id,
        squad_uuid,
        limit_bytes=5 * BYTES_IN_GB,
        period_start_at=NOW,
    )
    state.is_limited = is_limited
    # `None` означает «поправку на первые сутки ещё не замеряли»; тестам про
    # снятие она мешает — замер пришёлся бы целиком в baseline.
    state.baseline_bytes = baseline_bytes
    await db.commit()
    return state


def _run_worker_against(monkeypatch, db, *, push=None, panel_squads=None, usage_by_node=None):
    """Подменить сессию и панель так, чтобы `process_once` шёл по нашей БД.

    `panel_squads` — что панель отдаёт в `activeInternalSquads`; `None` значит
    «панель не сказала», и сверка обязана промолчать.

    Возвращает список наборов сквадов, уехавших в панель.
    """
    pushed: list[list[str]] = []

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr('app.services.premium_traffic_service.AsyncSessionLocal', _session)

    class _Service:
        is_configured = True

        @contextlib.asynccontextmanager
        async def get_api_client(self):
            yield FakeRemnawaveApi(
                usage_by_node=usage_by_node,
                panel_user=SimpleNamespace(last_traffic_reset_at=None, active_internal_squads=panel_squads),
            )

    monkeypatch.setattr('app.services.premium_traffic_service.RemnaWaveService', _Service)

    async def _default_push(_api, _subscription_id, *, user_id, active_internal_squads):
        pushed.append(list(active_internal_squads or []))

    monkeypatch.setattr(
        'app.services.grace_access_runtime.update_panel_user_grace_safe',
        push or _default_push,
    )
    return pushed


class TestOrphanStates:
    """Состояние без конфигурации запирает клиента навсегда.

    `_collect_targets` выдаёт пару «подписка + сквад», только пока сквад
    премиальный в текущем тарифе. Стоит убрать его из премиального списка или
    сменить тариф — и ветки возврата до подписки уже не доходят: `is_limited`
    остаётся поднятым, а `effective_panel_squads` продолжает вычитать сквад из
    набора для панели.
    """

    async def test_tariff_switch_without_premium_clears_states(self, monkeypatch):
        """Переход на тариф без премиум-лимитов снимает блокировку, а не запирает клиента."""
        from app.database.crud import subscription as subscription_crud

        async with memory_session(monkeypatch, (SubscriptionPremiumTraffic.__table__,)) as db:
            await _seed_state(db, is_limited=True)

            monkeypatch.setattr('app.database.crud.subscription._lock_subscription_row', AsyncMock())
            monkeypatch.setattr('app.database.crud.subscription._housekeep_expired_purchases', AsyncMock())
            monkeypatch.setattr('app.database.crud.subscription.clear_notifications', AsyncMock())
            monkeypatch.setattr(
                'app.database.crud.tariff.get_tariff_by_id',
                AsyncMock(return_value=SimpleNamespace(is_daily=False, server_traffic_limits={})),
            )
            subscription = SimpleNamespace(
                id=1,
                user_id=7,
                status='active',
                is_trial=False,
                start_date=NOW,
                end_date=NOW + timedelta(days=1),
                tariff_id=1,
                traffic_limit_gb=10,
                traffic_used_gb=0.0,
                device_limit=1,
                connected_squads=[SQUAD],
                purchased_traffic_gb=0,
                updated_at=NOW,
            )

            await subscription_crud.extend_subscription(db, subscription, 30, tariff_id=2, commit=False)

            assert await get_state(db, 1, SQUAD) is None

    async def test_tariff_switch_keeps_states_still_premium(self, monkeypatch):
        """Сквад остался премиальным в новом тарифе — учёт периода не теряем."""
        from app.database.crud import subscription as subscription_crud

        async with memory_session(monkeypatch, (SubscriptionPremiumTraffic.__table__,)) as db:
            await _seed_state(db, is_limited=True)

            monkeypatch.setattr('app.database.crud.subscription._lock_subscription_row', AsyncMock())
            monkeypatch.setattr('app.database.crud.subscription._housekeep_expired_purchases', AsyncMock())
            monkeypatch.setattr('app.database.crud.subscription.clear_notifications', AsyncMock())
            monkeypatch.setattr(
                'app.database.crud.tariff.get_tariff_by_id',
                AsyncMock(
                    return_value=SimpleNamespace(
                        is_daily=False, server_traffic_limits={SQUAD: {'traffic_limit_gb': 10}}
                    )
                ),
            )
            subscription = SimpleNamespace(
                id=1,
                user_id=7,
                status='active',
                is_trial=False,
                start_date=NOW,
                end_date=NOW + timedelta(days=1),
                tariff_id=1,
                traffic_limit_gb=10,
                traffic_used_gb=0.0,
                device_limit=1,
                connected_squads=[SQUAD],
                purchased_traffic_gb=0,
                updated_at=NOW,
            )

            await subscription_crud.extend_subscription(db, subscription, 30, tariff_id=2, commit=False)

            assert await get_state(db, 1, SQUAD) is not None

    async def test_closed_squad_stays_closed_after_leaving_the_premium_config(self, monkeypatch):
        """Закрытие переживает и осиротение, а не только смену периода/докупку.

        Сквад мог осиротеть самым обычным путём — админ обнулил
        `traffic_limit_gb` в тарифе, и `_collect_orphans` подобрал строку. Но
        если он вдобавок закрыт вручную (`/close`), `_clear_orphan` не должен
        ни удалять строку (вместе с ней исчез бы `closed_at`), ни возвращать
        сквад в панель — это решение оператора, а не следствие того, что
        лимита в тарифе больше нет.
        """
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await _seed_subscription(db, premium_limits={})
            state = await _seed_state(db, is_limited=True)
            state.used_bytes = state.total_limit_bytes
            state.closed_at = NOW
            await db.commit()
            pushed = _run_worker_against(monkeypatch, db)

            stats = await PremiumTrafficService().process_once()

            reread = await get_state(db, 1, SQUAD)
            assert reread is not None
            assert reread.is_limited is True
            assert reread.closed_at is not None
            assert stats['cleaned'] == 0
            # Возврата в панель быть не должно — закрытие ещё в силе.
            assert pushed == []

    async def test_squad_dropped_from_premium_list_is_unlocked(self, monkeypatch):
        """Сквад убрали из премиального списка тарифа — доступ возвращается."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            # В тарифе премиум остался, но на другом скваде: подписка из обхода
            # выпала, а снятый сквад так и остался снятым.
            await _seed_subscription(db, premium_limits={OTHER_SQUAD: {'traffic_limit_gb': 5}})
            await _seed_state(db, is_limited=True)
            pushed = _run_worker_against(monkeypatch, db)

            stats = await PremiumTrafficService().process_once()

            state = await get_state(db, 1, SQUAD)
            assert state is None or state.is_limited is False
            assert stats['cleaned'] == 1
            # Возврат должен доехать до панели: без него база «забыла» о снятии,
            # а панель — нет, и клиент остался бы заперт уже без следов.
            assert pushed == [[SQUAD]]

    async def test_worker_pass_clears_states_without_config(self, monkeypatch):
        """Состояния без конфигурации подчищаются проходом воркера, а не копятся."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await _seed_subscription(db, premium_limits={})
            await _seed_state(db, is_limited=False)
            pushed = _run_worker_against(monkeypatch, db)

            stats = await PremiumTrafficService().process_once()

            assert await get_state(db, 1, SQUAD) is None
            assert stats['cleaned'] == 1
            # Сквад не снимали — трогать панель незачем.
            assert pushed == []

    async def test_panel_failure_keeps_the_state_for_the_next_pass(self, monkeypatch):
        """Панель не ответила — строку не удаляем, иначе снятие станет невидимым."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await _seed_subscription(db, premium_limits={OTHER_SQUAD: {'traffic_limit_gb': 5}})
            await _seed_state(db, is_limited=True)

            async def _broken_push(*_args, **_kwargs):
                raise RuntimeError('панель недоступна')

            _run_worker_against(monkeypatch, db, push=_broken_push)

            stats = await PremiumTrafficService().process_once()

            state = await get_state(db, 1, SQUAD)
            assert state is not None and state.is_limited is True
            assert stats['cleaned'] == 0

    async def test_deferred_restore_keeps_the_state_for_the_next_pass(self, monkeypatch):
        """Возврат отложен grace-оверлеем — строку удалять нельзя.

        Отправка не упала (для этого есть тест выше): она вернулась без
        исключения, но `update_panel_user_grace_safe` отложила
        `activeInternalSquads`, потому что над подпиской открыт grace-оверлей.
        Не различая этого, `_clear_orphan` счёл бы сквад возвращённым, а панель
        его так и не вернула бы: клиент остался бы заперт уже без единственного
        следа о том, что его ограничили.
        """
        from app.services.grace_access_runtime import DeferredPanelUpdate

        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await _seed_subscription(db, premium_limits={OTHER_SQUAD: {'traffic_limit_gb': 5}})
            await _seed_state(db, is_limited=True)

            pushed: list[list[str]] = []

            async def _deferred_push(_api, _subscription_id, *, user_id, active_internal_squads):
                pushed.append(list(active_internal_squads or []))
                return DeferredPanelUpdate(SimpleNamespace(id=user_id))

            _run_worker_against(monkeypatch, db, push=_deferred_push)

            stats = await PremiumTrafficService().process_once()

            state = await get_state(db, 1, SQUAD)
            assert state is not None and state.is_limited is True, 'строку нельзя терять до подтверждённого возврата'
            assert stats['cleaned'] == 0
            # Попытка была — просто оверлей её отложил.
            assert pushed == [[SQUAD]]

    async def test_state_of_a_squad_the_subscription_lost_needs_no_panel_call(self, monkeypatch):
        """Права на сквад нет — в панель он и так не уезжает, возвращать нечего."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await _seed_subscription(db, premium_limits={}, connected=(OTHER_SQUAD,))
            await _seed_state(db, is_limited=True)
            pushed = _run_worker_against(monkeypatch, db)

            await PremiumTrafficService().process_once()

            assert await get_state(db, 1, SQUAD) is None
            assert pushed == []

    async def test_configured_squad_survives_the_pass(self, monkeypatch):
        """Уборка не должна съедать состояния, которые всё ещё настроены."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await _seed_subscription(db, premium_limits={SQUAD: {'traffic_limit_gb': 5}})
            await _seed_state(db, is_limited=True)
            _run_worker_against(monkeypatch, db)

            stats = await PremiumTrafficService().process_once()

            assert await get_state(db, 1, SQUAD) is not None
            assert stats['cleaned'] == 0


# ---------------------------------------------- досылка снятия после сбоя


class TestLimitPushRetry:
    """Флаг и отправка — одна атомарная единица: упавшая отправка не коммитится.

    `_limit_squad` флипает флаг, делает `flush` (чтобы `effective_panel_squads`
    увидел его в той же транзакции) и отправляет в панель внутри одного
    `begin_nested()`. Если отправка падает, savepoint откатывается целиком —
    база остаётся в состоянии «не снято», как и было до прохода, и никакого
    расхождения с панелью не возникает. Периодической пересылки сквадов в
    проекте нет (`sync_users_to_panel` запускается вручную, рутинный
    мониторинг `activeInternalSquads` не трогает), поэтому без повтора на
    следующем проходе клиент пользовался бы исчерпанным премиумом бессрочно.
    """

    @staticmethod
    def _recording_push(fail_first=False):
        """Двойник панели: пишет отправленный набор, при желании роняет первую."""
        calls: list[list[str] | None] = []

        async def _push(_api, _subscription_id, *, user_id, active_internal_squads):
            calls.append(active_internal_squads)
            if fail_first and len(calls) == 1:
                raise RuntimeError('панель недоступна')

        return calls, _push

    @staticmethod
    async def _seed_exhausted(db, *, connected=(SQUAD, OTHER_SQUAD)):
        """Подписка, у которой премиум-сквад исчерпан на первом же проходе.

        Режим сброса `NO_RESET` — чтобы граница периода не зависела от даты
        прогона: иначе календарный месяц однажды перевалит за `NOW` и проход
        начнёт новый период вместо снятия.
        """
        await _seed_subscription(
            db,
            premium_limits={SQUAD: {'traffic_limit_gb': 5}},
            connected=connected,
            reset_mode='NO_RESET',
        )
        await _seed_state(db, baseline_bytes=0)

    async def test_failed_limit_push_is_retried_next_pass(self, monkeypatch):
        """Отправка снятия упала — следующий проход досылает, а не забывает."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await self._seed_exhausted(db)
            push_calls, push = self._recording_push(fail_first=True)
            _run_worker_against(
                monkeypatch,
                db,
                push=push,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}],
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )
            service = PremiumTrafficService()

            first = await service.process_once()

            # Отправка упала — savepoint откатил и флаг: база не расходится с
            # панелью, а отказ панели не считается ошибкой воркера.
            assert first['errors'] == 0
            state = await get_state(db, 1, SQUAD)
            assert state is not None and state.is_limited is False

            second = await service.process_once()

            assert len(push_calls) == 2
            assert push_calls[1] == [OTHER_SQUAD]
            assert second['limited'] == 1

    async def test_retry_sends_an_empty_set_when_every_squad_is_exhausted(self, monkeypatch):
        """Все сквады подписки премиальные и исчерпаны — досылать надо literal [].

        Пустой набор — законный результат фильтра, а не «нечего отправлять»:
        `update_user` понимает `[]` как «снять все». Ветка досылки не имеет
        права гейтить отправку непустотой набора, иначе ровно у тех подписок,
        где премиум и есть весь доступ, снятие не доедет никогда.
        """
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await self._seed_exhausted(db, connected=(SQUAD,))
            push_calls, push = self._recording_push(fail_first=True)
            _run_worker_against(
                monkeypatch,
                db,
                push=push,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}],
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )
            service = PremiumTrafficService()

            await service.process_once()
            second = await service.process_once()

            assert push_calls == [[], []]
            assert second['limited'] == 1

    async def test_deferred_push_is_repeated_while_the_panel_still_serves_the_squad(self, monkeypatch):
        """Отправка без исключения — ещё не применённая отправка.

        `update_panel_user_grace_safe` молча выбрасывает `activeInternalSquads`
        из апдейта, пока открыт grace-оверлей, и возвращается без ошибки.
        Отличить отложенное от применённого по её ответу нечем, поэтому ветка
        не имеет права ничего запоминать: единственная защита клиента —
        повторить, пока панель отдаёт сквад.
        """
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await self._seed_exhausted(db)
            # Отправка «успешна», но панель набор не меняет — как при откладывании.
            push_calls, push = self._recording_push()
            _run_worker_against(
                monkeypatch,
                db,
                push=push,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}],
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )
            service = PremiumTrafficService()

            for _ in range(3):
                await service.process_once()

            assert len(push_calls) == 3

    async def test_deferred_limit_push_leaves_the_squad_unlimited(self, monkeypatch):
        """Настоящий `DeferredPanelUpdate` — не «панель промолчала», а явный отказ.

        В отличие от теста выше (панель отвечает без ошибки, но набор не
        меняет), здесь `update_panel_user_grace_safe` возвращает маркер отложенной
        записи явно. `_limit_squad` обязан откатить флаг вместе с отправкой —
        иначе база станет утверждать снятие, которое панель не подтвердила.
        """
        from app.services.grace_access_runtime import DeferredPanelUpdate

        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await self._seed_exhausted(db)

            async def _deferred_push(_api, _subscription_id, *, user_id, active_internal_squads):
                return DeferredPanelUpdate(SimpleNamespace(id=user_id))

            _run_worker_against(
                monkeypatch,
                db,
                push=_deferred_push,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}],
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )

            stats = await PremiumTrafficService().process_once()

            state = await get_state(db, 1, SQUAD)
            assert state is not None and state.is_limited is False, 'откат не применён — база разошлась с панелью'
            assert stats['errors'] == 0
            assert stats['limited'] == 0

    async def test_applied_push_is_not_repeated(self, monkeypatch):
        """Панель сняла сквад — досылать нечего, дёргать её каждый проход незачем."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await self._seed_exhausted(db)
            panel_squads = [{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}]
            push_calls: list[list[str] | None] = []

            async def _push(_api, _subscription_id, *, user_id, active_internal_squads):
                push_calls.append(active_internal_squads)
                # Панель применила снятие: карточка теперь без премиум-сквада.
                panel_squads[:] = [{'uuid': uuid} for uuid in active_internal_squads or []]

            _run_worker_against(
                monkeypatch,
                db,
                push=_push,
                panel_squads=panel_squads,
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )
            service = PremiumTrafficService()

            for _ in range(3):
                await service.process_once()

            assert push_calls == [[OTHER_SQUAD]]

    async def test_deferred_restore_push_leaves_the_squad_limited(self, monkeypatch):
        """Зеркало теста выше для возврата: откат обязателен и для `_restore_squad`.

        Топап снял исчерпание, но панель отложила запись (открыт grace-оверлей)
        — `is_limited` обязан остаться `True`, иначе клиент решит, что доступ
        уже вернули, а панель так и не отдаст сквад.
        """
        from app.services.grace_access_runtime import DeferredPanelUpdate

        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await self._seed_exhausted(db)
            await _seed_state(db, is_limited=True, baseline_bytes=0)

            async def _deferred_push(_api, _subscription_id, *, user_id, active_internal_squads):
                return DeferredPanelUpdate(SimpleNamespace(id=user_id))

            _run_worker_against(
                monkeypatch,
                db,
                push=_deferred_push,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}],
                # Меньше лимита — топап, должна сработать ветка восстановления.
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': BYTES_IN_GB}]},
            )

            stats = await PremiumTrafficService().process_once()

            state = await get_state(db, 1, SQUAD)
            assert state is not None and state.is_limited is True, 'откат не применён — база разошлась с панелью'
            assert stats['errors'] == 0
            assert stats['restored'] == 0


# ------------------------------------------------- открытый grace-оверлей


class TestOpenGraceOverlay:
    """Пока над подпиской открыт grace-оверлей, составом сквадов владеет он.

    Grace во время инцидента держит в панели свой снимок, сверяет панель с ним
    (`panel_matches_overlay`) и возвращает своё циклом сверки. Снятие сквада
    воркером он откатит и запишет ошибку — сломается не премиум-ограничение, а
    grace: он переделает работу и отрапортует конфликт, которого не было.

    Поэтому такие подписки проход пропускает целиком: ни обращения к панели, ни
    записи состояния. Оверлей временный, следующий проход досчитает период.
    """

    @staticmethod
    def _grace_open(monkeypatch, subscription_ids):
        """Подменить резолв открытых оверлеев и считать обращения к нему."""
        calls: list[int] = []

        async def _open_ids(_db):
            calls.append(1)
            return set(subscription_ids)

        monkeypatch.setattr(
            'app.services.grace_access_runtime.get_open_grace_subscription_ids',
            _open_ids,
        )
        return calls

    @staticmethod
    async def _seed_exhausted(db, *, subscription_id=1, tariff_id=1, panel_user_id=PANEL_USER_ID):
        """Подписка, у которой премиум-сквад исчерпан на первом же проходе."""
        await _seed_subscription(
            db,
            premium_limits={SQUAD: {'traffic_limit_gb': 5}},
            connected=(SQUAD, OTHER_SQUAD),
            subscription_id=subscription_id,
            tariff_id=tariff_id,
            reset_mode='NO_RESET',
            panel_user_id=panel_user_id,
        )
        return await _seed_state(db, subscription_id=subscription_id, baseline_bytes=0)

    async def test_worker_skips_subscriptions_with_open_grace_overlay(self, monkeypatch):
        """Ни отправки в панель, ни записи состояния — вмешиваться нельзя."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            state = await self._seed_exhausted(db)
            push_squads = AsyncMock()
            _run_worker_against(
                monkeypatch,
                db,
                push=push_squads,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}],
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )
            self._grace_open(monkeypatch, {1})

            stats = await PremiumTrafficService().process_once()

            push_squads.assert_not_awaited()
            assert state.is_limited is False  # состояние тоже не трогаем
            assert stats['checked'] == 0

    async def test_the_open_overlay_set_is_resolved_once_per_pass(self, monkeypatch):
        """Один запрос на проход, а не на подписку: их десятки тысяч."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await self._seed_exhausted(db, subscription_id=1, tariff_id=1)
            await self._seed_exhausted(db, subscription_id=2, tariff_id=2, panel_user_id=PANEL_USER_ID + 1)
            _run_worker_against(
                monkeypatch,
                db,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}],
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )
            calls = self._grace_open(monkeypatch, {1, 2})

            await PremiumTrafficService().process_once()

            assert len(calls) == 1

    async def test_a_subscription_without_an_overlay_is_still_enforced(self, monkeypatch):
        """Страховка от обратного: гард не имеет права глушить весь проход."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            state = await self._seed_exhausted(db)
            # return_value=None обязателен: пустой AsyncMock() при обращении к
            # .grace_write_deferred сам создаёт правдоподобный (truthy) Mock,
            # и panel_update_was_deferred() ошибочно сочтёт запись отложенной.
            push_squads = AsyncMock(return_value=None)
            _run_worker_against(
                monkeypatch,
                db,
                push=push_squads,
                panel_squads=[{'uuid': SQUAD, 'name': 'LTE'}, {'uuid': OTHER_SQUAD, 'name': 'Базовый'}],
                usage_by_node={NODE_A: [{'id': PANEL_USER_ID, 'totalBytes': 5 * BYTES_IN_GB}]},
            )
            # Оверлей открыт над чужой подпиской.
            self._grace_open(monkeypatch, {999})

            stats = await PremiumTrafficService().process_once()

            push_squads.assert_awaited_once()
            assert state.is_limited is True
            assert stats['limited'] == 1

    async def test_orphan_cleanup_also_stands_down_under_an_open_overlay(self, monkeypatch):
        """Уборка осиротевших состояний тоже пишет сквады — и тоже ждёт."""
        async with memory_session(monkeypatch, ORPHAN_TABLES) as db:
            await _seed_subscription(db, premium_limits={OTHER_SQUAD: {'traffic_limit_gb': 5}})
            await _seed_state(db, is_limited=True)
            pushed = _run_worker_against(monkeypatch, db)
            self._grace_open(monkeypatch, {1})

            stats = await PremiumTrafficService().process_once()

            assert pushed == []
            assert stats['cleaned'] == 0
            state = await get_state(db, 1, SQUAD)
            assert state is not None and state.is_limited is True
