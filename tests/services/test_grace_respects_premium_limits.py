"""Собственные записи grace обязаны уважать снятые премиум-сквады.

Сквад, снятый воркером за перерасход премиум-лимита, из `connected_squads` не
исчезает: право на него у подписки осталось, кончился трафик. Канонический
набор для панели считает `effective_panel_squads` — вычитанием.

Grace строит цель записи из `GraceBillingState.squad_uuids`, а тот собирался из
`connected_squads` как есть. Значит любое каноническое применение биллинга —
восстановление после оплаты (`apply_recovered_grace_update_locked`), закрытие
сессии (`apply_billing_state`) — возвращало клиенту исчерпанный премиум-сквад:
`_serialize_panel_target` перезаписывает `active_internal_squads` целью
безусловно, поверх любого фильтра, наложенного выше по стеку.

Фильтр стоит у самого чтения, в `SQLAlchemyGraceBillingGateway`, а не на
границе отправки: из одного и того же `billing.squad_uuids` растут и цель, и
сверка её результата с панелью, и снимок `billing_before` в сессии. Фильтруй
мы только отправку — сверка сравнивала бы панель с нефильтрованной целью и
считала бы совпадение расхождением.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import (
    GraceAccessSessionModel,
    PromoGroup,
    Subscription,
    SubscriptionPremiumTraffic,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services.grace_access_runtime import (
    SQLAlchemyGraceBillingGateway,
    _build_billing_target,
    _serialize_panel_target,
)
from app.services.grace_access_service import (
    GraceAccessPolicy,
    GraceAccessService,
    GraceAccessSession,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
    GraceStartDecision,
)
from tests.fixtures.sqlite_memory import memory_session


GIB = 1024**3
PANEL_ID = 4242
SUBSCRIPTION_ID = 1
# Премиальный сквад с посквадным лимитом — тот, который воркер снимает.
PREMIUM_SQUAD = '11111111-1111-1111-1111-111111111111'
# Обычный сквад той же подписки: право на него никуда не девается, и ни один
# фильтр не имеет права его тронуть.
REGULAR_SQUAD = '22222222-2222-2222-2222-222222222222'
GRACE_SQUAD = '33333333-3333-3333-3333-333333333333'
NOW = datetime.now(UTC).replace(microsecond=0)

TABLES = (
    User.__table__,
    PromoGroup.__table__,
    Tariff.__table__,
    # Тариф тянет промогруппы жадно — без связки запрос к подпискам падает.
    tariff_promo_groups,
    Subscription.__table__,
    SubscriptionPremiumTraffic.__table__,
    GraceAccessSessionModel.__table__,
)


async def _seed(db, *, limited_squads=(), status='expired'):
    """Подписка на премиум-тарифе с двумя сквадами и снятыми состояниями."""
    db.add(User(id=1, telegram_id=100, status='active', remnawave_id=PANEL_ID))
    db.add(
        Tariff(
            id=1,
            name='Премиум',
            period_prices={'30': 10000},
            traffic_limit_gb=100,
            device_limit=1,
            server_traffic_limits={PREMIUM_SQUAD: {'traffic_limit_gb': 5}},
        )
    )
    db.add(
        Subscription(
            id=SUBSCRIPTION_ID,
            user_id=1,
            status=status,
            # Явно платная: у тестовых grace выдаётся только при своём флаге.
            is_trial=False,
            tariff_id=1,
            connected_squads=[PREMIUM_SQUAD, REGULAR_SQUAD],
            traffic_limit_gb=100,
            traffic_used_gb=0.0,
            device_limit=1,
            start_date=NOW - timedelta(days=40),
            end_date=NOW - timedelta(days=1),
            remnawave_id=PANEL_ID,
        )
    )
    for squad_uuid in limited_squads:
        db.add(
            SubscriptionPremiumTraffic(
                subscription_id=SUBSCRIPTION_ID,
                squad_uuid=squad_uuid,
                limit_bytes=5 * GIB,
                used_bytes=5 * GIB,
                period_start_at=NOW - timedelta(days=10),
                is_limited=True,
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_billing_state_drops_a_squad_whose_premium_quota_is_exhausted(monkeypatch):
    """Исчерпанный премиум-сквад из канонического набора уходит, остальные — нет."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,))

        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)

        assert billing is not None
        assert billing.squad_uuids == (REGULAR_SQUAD,)


@pytest.mark.asyncio
async def test_billing_state_keeps_everything_the_customer_is_still_entitled_to(monkeypatch):
    """Фильтр вычитающий: пока лимит не исчерпан, набор прав не меняется."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)

        assert billing is not None
        assert billing.squad_uuids == (PREMIUM_SQUAD, REGULAR_SQUAD)


@pytest.mark.asyncio
async def test_canonical_panel_payload_does_not_regrant_the_exhausted_squad(monkeypatch):
    """Тот же путь, что у восстановления после оплаты, — до самого payload.

    `apply_recovered_grace_update_locked` собирает payload ровно так:
    `_build_billing_target` из состояния биллинга, затем `_serialize_panel_target`,
    который перезаписывает `active_internal_squads` целью безусловно. Фильтр,
    наложенный вызывающим выше по стеку, здесь бы и потерялся.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,), status='active')

        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)
        payload = _serialize_panel_target(
            PANEL_ID,
            _build_billing_target(billing, now=NOW),
            base_kwargs={'user_id': PANEL_ID, 'active_internal_squads': [PREMIUM_SQUAD, REGULAR_SQUAD]},
        )

        assert payload['active_internal_squads'] == [REGULAR_SQUAD]


class _MemoryStore:
    """Хранилище сессий в памяти: тесту нужна только созданная сессия."""

    def __init__(self) -> None:
        self.sessions: dict[str, GraceAccessSession] = {}

    async def get_open(self, subscription_id: int) -> GraceAccessSession | None:
        return next(
            (
                session
                for session in self.sessions.values()
                if session.subscription_id == subscription_id and session.state is not GraceSessionState.COMPLETED
            ),
            None,
        )

    async def get_by_incident(self, subscription_id: int, incident_key: str) -> GraceAccessSession | None:
        return next(
            (
                session
                for session in self.sessions.values()
                if session.subscription_id == subscription_id and session.incident_key == incident_key
            ),
            None,
        )

    async def create(self, session: GraceAccessSession) -> GraceAccessSession:
        self.sessions[session.id] = session
        return session

    async def save(self, session: GraceAccessSession) -> GraceAccessSession:
        self.sessions[session.id] = session
        return session

    async def list_open(self, *, limit: int) -> list[GraceAccessSession]:
        return [session for session in self.sessions.values()][:limit]


class _FakePanel:
    """Панель-заглушка: снимок задан, применение оверлея всегда удаётся."""

    def __init__(self, snapshot: GracePanelSnapshot) -> None:
        self.snapshot = snapshot
        self.applied_billing: list = []

    async def read_snapshot(self, remnawave_id: int) -> GracePanelSnapshot | None:
        return self.snapshot if remnawave_id == self.snapshot.remnawave_id else None

    async def apply_overlay(self, remnawave_id: int, overlay) -> None:
        return None

    async def restore_snapshot(self, remnawave_id: int, snapshot, expected_overlay):
        raise AssertionError('восстановление в этом сценарии не ожидается')

    async def apply_billing_state(self, billing, *, expected_overlay) -> None:
        self.applied_billing.append(billing)


@pytest.mark.asyncio
async def test_overlay_snapshot_excludes_the_exhausted_squad_and_keeps_the_rest(monkeypatch):
    """Снимок биллинга внутри оверлея — то, к чему grace вернёт клиента.

    Попади туда исчерпанный премиум-сквад — любое закрытие сессии (оплата,
    таймаут, конфликт) вернуло бы его в панель, и ограничение бы развалилось.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,))
        store = _MemoryStore()
        panel = _FakePanel(
            GracePanelSnapshot(
                remnawave_id=PANEL_ID,
                status='EXPIRED',
                expire_at=NOW - timedelta(days=1),
                traffic_limit_bytes=100 * GIB,
                used_traffic_bytes=GIB,
                squad_uuids=(REGULAR_SQUAD,),
                external_squad_uuid=None,
            )
        )
        service = GraceAccessService(
            store=store,
            panel=panel,
            billing=SQLAlchemyGraceBillingGateway(db),
            policy=GraceAccessPolicy(
                duration=timedelta(hours=6),
                expired_squad_uuid=GRACE_SQUAD,
                limited_squad_uuid=GRACE_SQUAD,
            ),
            clock=lambda: NOW,
        )
        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)

        result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

        assert result.decision is GraceStartDecision.STARTED
        assert result.session is not None
        assert result.session.billing_before.squad_uuids == (REGULAR_SQUAD,)


@pytest.mark.asyncio
async def test_overlay_snapshot_keeps_both_squads_while_the_quota_holds(monkeypatch):
    """Обратная сторона: не исчерпан — из снимка ничего не пропадает."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        store = _MemoryStore()
        panel = _FakePanel(
            GracePanelSnapshot(
                remnawave_id=PANEL_ID,
                status='EXPIRED',
                expire_at=NOW - timedelta(days=1),
                traffic_limit_bytes=100 * GIB,
                used_traffic_bytes=GIB,
                squad_uuids=(PREMIUM_SQUAD, REGULAR_SQUAD),
                external_squad_uuid=None,
            )
        )
        service = GraceAccessService(
            store=store,
            panel=panel,
            billing=SQLAlchemyGraceBillingGateway(db),
            policy=GraceAccessPolicy(
                duration=timedelta(hours=6),
                expired_squad_uuid=GRACE_SQUAD,
                limited_squad_uuid=GRACE_SQUAD,
            ),
            clock=lambda: NOW,
        )
        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)

        result = await service.start_if_eligible(billing, GraceReason.EXPIRED)

        assert result.session is not None
        assert result.session.billing_before.squad_uuids == (PREMIUM_SQUAD, REGULAR_SQUAD)
