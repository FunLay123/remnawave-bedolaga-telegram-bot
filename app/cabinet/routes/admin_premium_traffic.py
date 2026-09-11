"""Админские операции с премиум-трафиком подписки.

Просмотр остатка, ручное начисление и сброс периода.

**Про выбор при сбросе.** Общий и премиум-трафик считаются из разных
источников: общий из счётчика пользователя в панели, премиум из истории
bandwidth-stats за период. ``reset-traffic`` обнуляет счётчик, но историю по
нодам не трогает — проверено на панели 3.4.3: за дни до сброса статистика
пользователя видна. Поэтому сброс общего трафика премиум не задевает, и
наоборот; раз операции независимы, админ выбирает область явно:

* ``regular`` — как раньше, дёргаем панель. Значение ``lastTrafficResetAt``
  записывается в ``panel_reset_ack_at``, чтобы воркер не принял этот сброс за
  досрочный и не сдвинул премиум-период следом;
* ``premium`` — панель не трогаем, начинаем премиум-период заново;
* ``both`` — и то, и другое.

**Про закрытие доступа отдельно от лимита.** ``traffic_limit_gb == 0`` в
конфигурации тарифа означает «отдельного лимита нет», то есть безлимит внутри
сквада — ``parse_premium_squad`` такие сквады из премиальных не считает. Если
бы «закрыть доступ» означало занулить этот же лимит, оператор, отбирающий
доступ, получил бы обратное: учёт выключился бы, а сквад остался в панели.
Поэтому закрытие — отдельное действие (``/close``), которое не трогает лимит
тарифа, а доводит состояние подписки до исчерпанного: ``used_bytes``
подтягивается минимум до ``total_limit_bytes``, ``is_limited`` ставится сразу,
а ``closed_at`` фиксирует причину отдельно от следствия. Причина нужна не для
отчётности: без неё смена периода (``start_new_period``) и доначисление
трафика (``add_extra_bytes``) сами сняли бы закрытие — оба видят только
``is_limited`` и не отличили бы «админ закрыл» от «кончился лимит». Поэтому обе
операции теперь проверяют ``closed_at`` и, пока он стоит, оставляют
``is_limited`` как есть (при смене периода — заново форсируя ``used_bytes`` до
нового лимита, чтобы воркер не открыл сквад сам в тот же проход). Как и при
выдаче трафика (``grant``), панель здесь не дёргаем — снятие сквада в панели
остаётся заботой воркера (``PremiumTrafficService``), который делает это через
единый путь отказа и уважает открытый grace-оверлей. Единственный способ снять
закрытие — ``/reopen``: она возвращает ``used_bytes`` к нулю, снимает
``is_limited`` и очищает ``closed_at``, а не подделывает расход — следующий
проход воркера пересчитает его от статистики панели заново.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.premium_traffic import (
    add_extra_bytes,
    get_or_create_state,
    get_state,
    get_states_for_subscription,
    start_new_period,
)
from app.database.crud.server_squad import get_squad_display_names
from app.database.crud.subscription import get_subscription_by_id
from app.database.models import User
from app.services.remnawave_service import RemnaWaveService
from app.utils.premium_traffic import BYTES_IN_GB, get_premium_squads_for_tariff

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/premium-traffic', tags=['Cabinet Admin Premium Traffic'])


class PremiumTrafficStateResponse(BaseModel):
    """Состояние премиум-лимита по одному скваду."""

    squad_uuid: str
    name: str | None = None
    limit_gb: float
    extra_gb: float
    used_gb: float
    remaining_gb: float
    is_limited: bool
    period_start_at: datetime | None = None
    last_checked_at: datetime | None = None
    # Воркер ещё не создавал состояние — показываем настройки тарифа как есть.
    has_state: bool = True


class PremiumTrafficResetRequest(BaseModel):
    """Что именно сбросить."""

    scope: Literal['premium', 'regular', 'both'] = 'premium'
    # Только для premium/both: если не задан, период начинается заново у всех
    # премиум-сквадов подписки.
    squad_uuid: str | None = Field(None, max_length=64)


class PremiumTrafficGrantRequest(BaseModel):
    """Ручное начисление премиум-гигабайтов."""

    squad_uuid: str = Field(..., min_length=1, max_length=64)
    gb: int = Field(..., ge=1, le=100_000)


class PremiumTrafficSquadRequest(BaseModel):
    """Один премиум-сквад подписки — для закрытия и открытия доступа."""

    squad_uuid: str = Field(..., min_length=1, max_length=64)


async def _load_subscription(db: AsyncSession, subscription_id: int):
    subscription = await get_subscription_by_id(db, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')
    return subscription


@router.get('/{subscription_id}', response_model=list[PremiumTrafficStateResponse])
async def get_premium_traffic_states(
    subscription_id: int,
    admin: User = Depends(require_permission('traffic:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Остаток по премиум-сквадам подписки."""
    subscription = await _load_subscription(db, subscription_id)
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    if not configs:
        return []

    states = {state.squad_uuid: state for state in await get_states_for_subscription(db, subscription_id)}
    squad_names = await get_squad_display_names(db, list(configs))

    result: list[PremiumTrafficStateResponse] = []
    for squad_uuid, config in configs.items():
        state = states.get(squad_uuid)
        limit_bytes = state.limit_bytes if state else config.limit_bytes
        extra_bytes = (state.extra_bytes or 0) if state else 0
        used_bytes = (state.used_bytes or 0) if state else 0
        result.append(
            PremiumTrafficStateResponse(
                squad_uuid=squad_uuid,
                name=config.name or squad_names.get(squad_uuid),
                limit_gb=round((limit_bytes or 0) / BYTES_IN_GB, 2),
                extra_gb=round(extra_bytes / BYTES_IN_GB, 2),
                used_gb=round(used_bytes / BYTES_IN_GB, 2),
                remaining_gb=round(max(0, (limit_bytes or 0) + extra_bytes - used_bytes) / BYTES_IN_GB, 2),
                is_limited=bool(state.is_limited) if state else False,
                period_start_at=state.period_start_at if state else None,
                last_checked_at=state.last_checked_at if state else None,
                has_state=state is not None,
            )
        )
    return result


@router.post('/{subscription_id}/reset')
async def reset_premium_traffic(
    subscription_id: int,
    request: PremiumTrafficResetRequest,
    admin: User = Depends(require_permission('traffic:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Сбросить трафик подписки: премиум, обычный или оба."""
    subscription = await _load_subscription(db, subscription_id)
    now = datetime.now(UTC)
    result: dict[str, object] = {'scope': request.scope, 'regular_reset': False, 'premium_squads': []}

    if request.scope in ('regular', 'both'):
        result['regular_reset'] = await _reset_regular(db, subscription, request.scope, now)

    if request.scope in ('premium', 'both'):
        result['premium_squads'] = await _reset_premium(db, subscription, request.squad_uuid, now)

    await db.commit()
    logger.info(
        'Админ сбросил трафик подписки',
        admin_id=admin.id,
        subscription_id=subscription_id,
        scope=request.scope,
        premium_squads=result['premium_squads'],
    )
    return result


async def _reset_regular(db: AsyncSession, subscription, scope: str, now: datetime) -> bool:
    """Сбросить общий трафик в панели."""
    service = RemnaWaveService()
    if not service.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={'code': 'panel_unavailable', 'message': 'Панель Remnawave не настроена'},
        )

    panel_user_id = getattr(subscription, 'remnawave_id', None) or (
        subscription.user.remnawave_id if subscription.user else None
    )
    if not panel_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={'code': 'no_panel_user', 'message': 'У подписки нет пользователя в панели'},
        )

    async with service.get_api_client() as api:
        await api.reset_user_traffic(panel_user_id)
        panel_user = await api.get_user_by_id(panel_user_id)

    if scope == 'both':
        # Премиум и так сбрасывается ниже — отмечать нечего.
        return True

    # Отмечаем сброс как учтённый, иначе воркер примет его за досрочный сброс
    # панели и обнулит премиум-период следом — ровно то, чего админ не просил.
    ack = getattr(panel_user, 'last_traffic_reset_at', None) or now
    for state in await get_states_for_subscription(db, subscription.id):
        state.panel_reset_ack_at = ack
    return True


async def _reset_premium(db: AsyncSession, subscription, squad_uuid: str | None, now: datetime) -> list[str]:
    """Начать премиум-период заново, вернув снятые сквады."""
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    if not configs:
        return []

    targets = [squad_uuid] if squad_uuid else list(configs)
    reset: list[str] = []
    for uuid in targets:
        config = configs.get(uuid)
        if config is None:
            continue
        state = await get_or_create_state(
            db,
            subscription.id,
            uuid,
            limit_bytes=config.limit_bytes,
            period_start_at=now,
        )
        start_new_period(state, period_start_at=now, limit_bytes=config.limit_bytes)
        reset.append(uuid)
    return reset


@router.post('/{subscription_id}/grant')
async def grant_premium_traffic(
    subscription_id: int,
    request: PremiumTrafficGrantRequest,
    admin: User = Depends(require_permission('traffic:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Начислить премиум-гигабайты вручную, без оплаты.

    Потолок ``max_topup_gb`` здесь не действует: он ограничивает покупку
    пользователем, а решение админа — последняя инстанция.
    """
    subscription = await _load_subscription(db, subscription_id)
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    config = configs.get(request.squad_uuid)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'not_a_premium_squad', 'message': 'В тарифе нет премиум-лимита для этого сервера'},
        )

    state = await get_or_create_state(
        db,
        subscription.id,
        request.squad_uuid,
        limit_bytes=config.limit_bytes,
        period_start_at=datetime.now(UTC),
    )
    was_limited = bool(state.is_limited)
    add_extra_bytes(state, request.gb * BYTES_IN_GB)
    await db.commit()

    logger.info(
        'Админ начислил премиум-трафик',
        admin_id=admin.id,
        subscription_id=subscription_id,
        squad_uuid=request.squad_uuid,
        gb=request.gb,
    )
    return {
        'success': True,
        'squad_uuid': request.squad_uuid,
        'gb': request.gb,
        'extra_gb': round((state.extra_bytes or 0) / BYTES_IN_GB, 2),
        # Сквад вернёт ближайший проход воркера — отдельный PATCH панели здесь
        # не делаем, чтобы админский запрос не зависел от её доступности.
        'squad_restored': was_limited and not state.is_limited,
    }


@router.post('/{subscription_id}/close')
async def close_premium_access(
    subscription_id: int,
    request: PremiumTrafficSquadRequest,
    admin: User = Depends(require_permission('traffic:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Закрыть доступ к премиум-скваду, не трогая лимит тарифа.

    См. описание модуля: зануление ``traffic_limit_gb`` не годится для этого —
    оно означает «лимита нет», а не «доступ закрыт».
    """
    subscription = await _load_subscription(db, subscription_id)
    state = await _close_access(db, subscription, request.squad_uuid, datetime.now(UTC))
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'not_a_premium_squad', 'message': 'В тарифе нет премиум-лимита для этого сервера'},
        )
    await db.commit()

    logger.info(
        'Админ закрыл доступ к премиум-скваду',
        admin_id=admin.id,
        subscription_id=subscription_id,
        squad_uuid=request.squad_uuid,
    )
    return {'success': True, 'squad_uuid': request.squad_uuid, 'is_limited': state.is_limited}


async def _close_access(
    db: AsyncSession,
    subscription,
    squad_uuid: str,
    now: datetime,
) -> SubscriptionPremiumTraffic | None:
    """Довести состояние подписки по скваду до исчерпанного и пометить его закрытым.

    Возвращает ``None``, если у сквада в тарифе нет положительного лимита —
    закрывать нечего: `is_exhausted` при нулевом лимите всегда `False`, и
    подделать это подъёмом ``used_bytes`` было бы обманом счётчика, а не
    закрытием доступа.

    ``closed_at`` — причина закрытия, отдельная от ``is_limited`` (следствия):
    её проверяют `start_new_period` (не снимать закрытие при смене периода) и
    `add_extra_bytes` (не снимать закрытие доначислением трафика). Без неё обе
    операции читали бы одно и то же значение ``is_limited``, что и у обычного
    исчерпания за расход, и не могли бы отличить «админ закрыл» от «кончился
    лимит» — то самое смешение, ради которого писалась вся задача.

    ``used_bytes`` поднимаем минимум до ``total_limit_bytes`` — просто
    поставить ``is_limited = True`` недостаточно: воркер на следующем проходе
    пересчитает ``is_exhausted`` от реального (более низкого) расхода и сам же
    откроет сквад обратно (``is_limited and not is_exhausted`` -> restore).
    Расход не может упасть ниже уже записанного (`record_usage` берёт максимум),
    поэтому закрытие держится, пока не начнётся новый период или админ не
    откроет доступ явно через ``/reopen``.
    """
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    config = configs.get(squad_uuid)
    if config is None:
        return None

    state = await get_or_create_state(
        db,
        subscription.id,
        squad_uuid,
        limit_bytes=config.limit_bytes,
        period_start_at=now,
    )
    state.used_bytes = max(state.used_bytes or 0, state.total_limit_bytes)
    state.is_limited = True
    state.closed_at = now
    return state


@router.post('/{subscription_id}/reopen')
async def reopen_premium_access(
    subscription_id: int,
    request: PremiumTrafficSquadRequest,
    admin: User = Depends(require_permission('traffic:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Открыть ранее закрытый доступ к премиум-скваду.

    Единственный путь наружу из закрытия: ни смена периода (`start_new_period`),
    ни доначисление трафика (`add_extra_bytes`) его больше не снимают — обе
    нарочно проверяют ``closed_at`` и оставляют закрытие как есть. Без явного
    reopen оно было бы ловушкой, которую оператор сам не может отменить.
    """
    subscription = await _load_subscription(db, subscription_id)
    state = await _reopen_access(db, subscription, request.squad_uuid)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={'code': 'no_state', 'message': 'Состояние премиум-лимита для этого сквада не найдено'},
        )
    await db.commit()

    logger.info(
        'Админ открыл доступ к премиум-скваду',
        admin_id=admin.id,
        subscription_id=subscription_id,
        squad_uuid=request.squad_uuid,
    )
    return {'success': True, 'squad_uuid': request.squad_uuid, 'is_limited': state.is_limited}


async def _reopen_access(
    db: AsyncSession,
    subscription,
    squad_uuid: str,
) -> SubscriptionPremiumTraffic | None:
    """Снять закрытие: обнулить расход и вернуть сквад панели.

    Расход не восстанавливаем в «реальное» значение — оно потеряно тем же
    подъёмом, которым закрытие форсировало исчерпание (см. `_close_access`).
    Обнуление здесь не подделка: `bandwidth-stats` отдаёт накопленное с начала
    периода, а не приращение, поэтому следующий проход воркера всё равно
    перечитает истинный расход за период целиком и, если он и правда выше
    лимита, снимет сквад заново — уже не по нашему решению, а по факту.

    Период не перезапускаем (в отличие от ``/reset``): это открытие доступа,
    а не начало нового периода, — докупленный трафик и отметка 80% остаются
    как были. ``closed_at`` снимаем последним: пока он стоит,
    `start_new_period` и `add_extra_bytes` не тронут ``is_limited``, а нам
    нужно ровно обратное.
    """
    state = await get_state(db, subscription.id, squad_uuid)
    if state is None:
        return None
    state.used_bytes = 0
    state.is_limited = False
    state.closed_at = None
    return state
