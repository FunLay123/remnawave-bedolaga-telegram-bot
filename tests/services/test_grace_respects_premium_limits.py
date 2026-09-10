"""Собственные записи grace обязаны уважать снятые премиум-сквады.

Сквад, снятый воркером за перерасход премиум-лимита, из `connected_squads` не
исчезает: право на него у подписки осталось, кончился трафик. Канонический
набор для панели считает `effective_panel_squads` — вычитанием.

Grace строит цель записи из `GraceBillingState.squad_uuids`, а тот собирался из
`connected_squads` как есть. Значит любое каноническое применение биллинга —
восстановление после оплаты (`apply_recovered_grace_update_locked`), закрытие
сессии и fail-closed-ветки (`apply_billing_state`) — возвращало клиенту
исчерпанный премиум-сквад: `_serialize_panel_target` перезаписывает
`active_internal_squads` целью безусловно, поверх любого фильтра выше по стеку.

**Где стоит шов.** Ровно перед обоими вызовами `_build_billing_target`, то есть
на границе «состояние биллинга → цель записи в панель». Не дальше и не ближе:

* фильтровать сам payload нельзя — цель используется ещё и для сверки
  результата (`_panel_matches_target` сравнивает `set(snapshot.squad_uuids)` с
  `set(target.squad_uuids)`), и отфильтрованный payload при нефильтрованной
  цели превратил бы успешную запись в конфликт;
* фильтровать раньше, в самом `GraceBillingState`, тоже нельзя — это состояние
  grace сравнивает само с собой (`billing_still_matches_session`: снимок
  `billing_before` против свежего чтения), и переменчивый премиум-флаг там
  читался бы как «инцидент изменился под нами», закрывая оверлей конфликтом.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

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
from app.external.remnawave_api import UserStatus as PanelUserStatus
from app.services.grace_access_runtime import (
    RemnawaveGracePanelGateway,
    SQLAlchemyGraceBillingGateway,
    _billing_with_effective_squads,
    _build_billing_target,
    _serialize_panel_target,
)
from app.services.grace_access_service import (
    GraceAccessSession,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
    billing_still_matches_session,
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


async def _seed(db, *, limited_squads=(), status='active', end_at=None):
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
            end_date=end_at if end_at is not None else NOW + timedelta(days=20),
            remnawave_id=PANEL_ID,
        )
    )
    await db.commit()
    await _set_limited(db, limited_squads)


async def _set_limited(db, squad_uuids):
    """Проставить/снять `is_limited` — как это делают воркер и докупка."""
    from app.database.crud.premium_traffic import get_or_create_state

    for squad_uuid in (PREMIUM_SQUAD, REGULAR_SQUAD):
        state = await get_or_create_state(
            db,
            SUBSCRIPTION_ID,
            squad_uuid,
            limit_bytes=5 * GIB,
            period_start_at=NOW - timedelta(days=10),
        )
        state.is_limited = squad_uuid in squad_uuids
    await db.commit()


def _use_session(monkeypatch, db):
    """Отдать фильтру, открывающему свою сессию, нашу in-memory."""

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', _session)


# ------------------------------------------------------- цель записи в панель


@pytest.mark.asyncio
async def test_the_panel_target_drops_a_squad_whose_premium_quota_is_exhausted(monkeypatch):
    """Исчерпанный премиум-сквад из цели уходит, остальные — нет."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,))

        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)
        effective = await _billing_with_effective_squads(billing, db=db)

        assert billing.squad_uuids == (PREMIUM_SQUAD, REGULAR_SQUAD), 'права подписки не меняются'
        assert effective.squad_uuids == (REGULAR_SQUAD,)


@pytest.mark.asyncio
async def test_the_panel_target_keeps_everything_the_customer_is_still_entitled_to(monkeypatch):
    """Фильтр вычитающий: пока лимит не исчерпан, набор не меняется."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)
        effective = await _billing_with_effective_squads(billing, db=db)

        assert effective.squad_uuids == (PREMIUM_SQUAD, REGULAR_SQUAD)


@pytest.mark.asyncio
async def test_canonical_panel_payload_does_not_regrant_the_exhausted_squad(monkeypatch):
    """Payload собирается из уже отфильтрованной цели, а не из прав.

    `_serialize_panel_target` перезаписывает `active_internal_squads` целью
    безусловно — нефильтрованный набор в `base_kwargs` до панели дойти не может.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,))

        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)
        payload = _serialize_panel_target(
            PANEL_ID,
            _build_billing_target(await _billing_with_effective_squads(billing, db=db), now=NOW),
            base_kwargs={'user_id': PANEL_ID, 'active_internal_squads': [PREMIUM_SQUAD, REGULAR_SQUAD]},
        )

        assert payload['active_internal_squads'] == [REGULAR_SQUAD]


# ------------------------------------------- применение канонического биллинга


class _EchoPanelApi:
    """Панель, отвечающая ровно тем, что ей прислали: сверка обязана сойтись."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def get_user_by_id(self, user_id: int) -> SimpleNamespace:
        raise AssertionError('ACTIVE-переход читает панель только при LIMITED')

    async def update_user(self, **kwargs: Any) -> SimpleNamespace:
        self.updates.append(kwargs)
        return SimpleNamespace(
            id=kwargs['user_id'],
            status=kwargs.get('status', PanelUserStatus.ACTIVE),
            expire_at=kwargs.get('expire_at'),
            traffic_limit_bytes=kwargs.get('traffic_limit_bytes', 0),
            used_traffic_bytes=0,
            user_traffic=0,
            active_internal_squads=[{'uuid': uuid} for uuid in kwargs.get('active_internal_squads') or []],
            external_squad_uuid=kwargs.get('external_squad_uuid'),
            hwid_device_limit=kwargs.get('hwid_device_limit'),
            last_traffic_reset_at=None,
        )


def _use_panel(monkeypatch, api):
    @contextlib.asynccontextmanager
    async def _client():
        yield api

    monkeypatch.setattr(
        'app.services.remnawave_service.remnawave_service',
        SimpleNamespace(get_api_client=_client),
    )


def _overlay() -> GracePanelOverlay:
    return GracePanelOverlay(
        status='ACTIVE',
        expire_at=NOW + timedelta(hours=6),
        traffic_limit_bytes=2 * GIB,
        squad_uuids=(GRACE_SQUAD,),
        external_squad_uuid=None,
    )


@pytest.mark.asyncio
async def test_applying_canonical_billing_does_not_regrant_the_exhausted_squad(monkeypatch):
    """Полный путь `apply_billing_state`: и запись, и сверка её результата.

    Панель отвечает тем, что получила. Если бы фильтр стоял только на payload,
    сверка сравнила бы этот ответ с нефильтрованной целью и подняла бы
    `GracePanelError` — то есть тест ловит обе половины шва сразу.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,))
        _use_session(monkeypatch, db)
        api = _EchoPanelApi()
        _use_panel(monkeypatch, api)
        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)

        await RemnawaveGracePanelGateway().apply_billing_state(billing, expected_overlay=_overlay())

        assert len(api.updates) == 1
        assert api.updates[0]['active_internal_squads'] == [REGULAR_SQUAD]


@pytest.mark.asyncio
async def test_applying_canonical_billing_keeps_the_squads_still_paid_for(monkeypatch):
    """Обратная страховка: без исчерпания в панель уходит полный набор прав."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        _use_session(monkeypatch, db)
        api = _EchoPanelApi()
        _use_panel(monkeypatch, api)
        billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)

        await RemnawaveGracePanelGateway().apply_billing_state(billing, expected_overlay=_overlay())

        assert api.updates[0]['active_internal_squads'] == [PREMIUM_SQUAD, REGULAR_SQUAD]


@pytest.mark.asyncio
async def test_recovery_after_payment_does_not_regrant_the_exhausted_squad(monkeypatch):
    """Названное место бага: `apply_recovered_grace_update_locked`.

    Клиент оплатил подписку, пока над ней был открыт grace-оверлей. Сессия
    закрывается каноническим апдейтом — и именно он возвращал в панель сквад,
    премиум-лимит которого клиент исчерпал.
    """
    from app.services.grace_access_runtime import (
        SQLAlchemyGraceSessionStore,
        apply_recovered_grace_update_locked,
        grace_access_runtime,
    )
    from app.services.grace_access_service import GraceAccessMode

    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,))
        monkeypatch.setattr(grace_access_runtime, '_mode', GraceAccessMode.ACTIVE)
        # Снимок инцидента истёк раньше — свежий срок и есть признак оплаты.
        expired_billing = await SQLAlchemyGraceBillingGateway(db).get_subscription(SUBSCRIPTION_ID)
        await SQLAlchemyGraceSessionStore(db).create(
            _session_for(replace(expired_billing, end_at=NOW - timedelta(days=1)))
        )
        await db.commit()
        api = _EchoPanelApi()

        completed, updated = await apply_recovered_grace_update_locked(
            db,
            api,
            SUBSCRIPTION_ID,
            update_kwargs={'user_id': PANEL_ID, 'active_internal_squads': [PREMIUM_SQUAD, REGULAR_SQUAD]},
            source='test',
        )

        assert completed is True
        assert updated is not None
        assert api.updates[0]['active_internal_squads'] == [REGULAR_SQUAD]


# --------------------------------------------- премиум-флаг не рвёт инцидент


def _session_for(billing) -> GraceAccessSession:
    """Открытая сессия, снявшая свой снимок биллинга в момент инцидента."""
    return GraceAccessSession(
        id='11111111-2222-3333-4444-555555555555',
        subscription_id=SUBSCRIPTION_ID,
        remnawave_id=PANEL_ID,
        reason=GraceReason.EXPIRED,
        incident_key='expired:none',
        state=GraceSessionState.ACTIVE,
        billing_before=billing,
        panel_before=GracePanelSnapshot(
            remnawave_id=PANEL_ID,
            status='EXPIRED',
            expire_at=NOW - timedelta(days=1),
            traffic_limit_bytes=100 * GIB,
            used_traffic_bytes=GIB,
            squad_uuids=(REGULAR_SQUAD,),
            external_squad_uuid=None,
        ),
        overlay=_overlay(),
        started_at=NOW,
        grace_until=NOW + timedelta(hours=6),
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_a_premium_grant_during_an_overlay_does_not_conflict_the_session(monkeypatch):
    """Докупка трафика или начисление админом не должны обрывать grace.

    Обе операции снимают `is_limited` (`apply_premium_topup`,
    `admin_premium_traffic.add_extra_bytes`), и support-начисление посреди
    инцидента вероятнее самостоятельной покупки. Попади премиум-фильтр в
    `GraceBillingState`, набор сквадов у свежего чтения стал бы БОЛЬШЕ, чем в
    снимке сессии, `billing_still_matches_session` вернул бы False, и оверлей
    закрылся бы конфликтом посреди инцидента — а для EXPIRED дедуп по ключу
    инцидента больше бы его и не открыл.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, limited_squads=(PREMIUM_SQUAD,), status='expired', end_at=NOW - timedelta(days=1))
        gateway = SQLAlchemyGraceBillingGateway(db)
        session = _session_for(await gateway.get_subscription(SUBSCRIPTION_ID))

        # Клиент докупил трафик / админ начислил — блокировка снята.
        await _set_limited(db, ())
        current = await gateway.get_subscription(SUBSCRIPTION_ID)

        assert billing_still_matches_session(session, current) is True


@pytest.mark.asyncio
async def test_the_worker_limiting_mid_pass_does_not_conflict_the_session(monkeypatch):
    """Обратное направление — гонка «сессия открылась посреди прохода».

    Гард воркера резолвит открытые оверлеи в начале прохода, а `_limit_squad`
    коммитит минутами позже и не под `lock_grace_sensitive_panel_updates`.
    Сессия, открывшаяся в этом промежутке, увидела бы набор МЕНЬШЕ снимка — и это
    тоже не повод объявлять инцидент изменившимся.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, status='expired', end_at=NOW - timedelta(days=1))
        gateway = SQLAlchemyGraceBillingGateway(db)
        session = _session_for(await gateway.get_subscription(SUBSCRIPTION_ID))

        # Воркер добрался до подписки уже после открытия сессии.
        await _set_limited(db, (PREMIUM_SQUAD,))
        current = await gateway.get_subscription(SUBSCRIPTION_ID)

        assert billing_still_matches_session(session, current) is True
