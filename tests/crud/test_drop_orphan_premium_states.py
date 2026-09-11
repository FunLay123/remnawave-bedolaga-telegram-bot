"""Уборка премиум-состояний при смене тарифа подписки (`_drop_orphan_premium_states`).

Пятый писатель/удалитель, найденный при повторном сквозном ревью задачи про
разведение «выключить учёт» и «закрыть доступ»: смена тарифа подписки
(`extend_subscription`) убирает состояния сквадов, которых нет в новом
тарифе, точно тем же способом, что и осиротение из-за правки самого тарифа
(`PremiumTrafficService._clear_orphan`) — но раньше не знала про `closed_at`
вообще.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.database.crud.premium_traffic import get_or_create_state, get_state
from app.database.crud.subscription import _drop_orphan_premium_states
from app.database.models import SubscriptionPremiumTraffic
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _tariff(limits):
    return SimpleNamespace(server_traffic_limits=limits)


def _subscription(subscription_id=1):
    return SimpleNamespace(id=subscription_id)


async def test_closed_state_survives_tariff_switch_that_drops_the_squad(monkeypatch):
    """Закрытый вручную сквад не должен возвращаться в панель тихой сменой тарифа.

    До фикса удаление строки стирало ``closed_at`` навсегда, а
    `effective_panel_squads`, лишившись строки, переставал вычитать сквад —
    т.е. смена тарифа тем же ходом, что и обычная уборка, тихо снимала
    административное закрытие.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        state.used_bytes = state.total_limit_bytes
        state.is_limited = True
        state.closed_at = NOW
        await db.commit()

        # Новый тариф вообще не содержит этот сквад среди премиальных.
        await _drop_orphan_premium_states(db, _subscription(), _tariff({}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread is not None
        assert reread.is_limited is True
        assert reread.closed_at is not None


async def test_ordinary_orphan_cleanup_on_tariff_switch_still_works(monkeypatch):
    """Некрытый (обычный) сквад продолжает убираться при смене тарифа как раньше."""
    async with memory_session(monkeypatch, TABLES) as db:
        await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        await db.commit()

        await _drop_orphan_premium_states(db, _subscription(), _tariff({}))
        await db.commit()

        assert await get_state(db, 1, SQUAD) is None


async def test_squad_still_in_new_tariff_is_left_alone(monkeypatch):
    """Сквад, оставшийся премиальным и в новом тарифе, уборку не затрагивает."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        await db.commit()

        await _drop_orphan_premium_states(db, _subscription(), _tariff({SQUAD: {'traffic_limit_gb': 10}}))

        reread = await get_state(db, 1, SQUAD)
        assert reread is not None
        assert reread.id == state.id
