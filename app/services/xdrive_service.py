"""Xdrive на горячем пути выдачи подписки: цель, конфиг и кеш.

Слияние конфигов живёт в прокси страницы подписок, но решение «этой подписке
xdrive положен» принимает тот же свод правил, что и проектор PasarGuard:
``subscription_allows_xdrive`` и ``premium_state_allows_xdrive``. Здесь эти
правила применяются к строке БД, найденной по shortUuid из пути.

Провайдер конфигов кеширует ответ панели по ``(pasarguard_user_id,
cache_version)``. Версия включает поколение личности и время последнего
revoke: после ротации кредов старый конфиг клиенту уже не подходит, и кеш
обязан промахнуться, не дожидаясь TTL.

Панель может быть недоступна, а клиент — ждать. Поэтому таймаут клиента
панели ограничен несколькими секундами: GET повторяется, и три полных
таймаута подряд задержали бы выдачу подписки. Тела конфигов (в них могут быть
xdrive ``secrets`` и ``remoteFolder``) не логируются.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.crud.premium_traffic import get_state
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionPremiumTraffic, Tariff, User
from app.external.pasarguard_api import PasarguardAPI
from app.services.grace_access_runtime import get_open_grace_subscription_ids
from app.services.pasarguard_sync_service import (
    PremiumStateView,
    premium_state_allows_xdrive,
    subscription_allows_xdrive,
)
from app.utils.premium_traffic import get_premium_squads_for_tariff


logger = structlog.get_logger(__name__)

# Больше этого числа записей кеш чистится от протухших: версии после revoke
# остаются в словаре навсегда, если их не убирать.
_CACHE_PRUNE_THRESHOLD = 1024


@dataclass(frozen=True)
class XdriveTarget:
    """Подписка, которой можно добавить xdrive в ответ прокси.

    ``cache_version`` меняется при пересоздании личности и при revoke, поэтому
    провайдер не отдаст конфиг со старыми кредами.
    """

    subscription_id: int
    pasarguard_user_id: int
    cache_version: str


def _premium_view(state: SubscriptionPremiumTraffic | None) -> PremiumStateView | None:
    if state is None:
        return None
    return PremiumStateView(
        is_limited=bool(state.is_limited),
        is_exhausted=bool(state.is_exhausted),
        closed_at=state.closed_at,
        total_limit_bytes=int(state.total_limit_bytes or 0),
        used_bytes=int(state.used_bytes or 0),
    )


def _cache_version(subscription: Subscription) -> str:
    """Поколение личности: создание и последний успешный revoke."""
    created = subscription.pasarguard_created_at.isoformat() if subscription.pasarguard_created_at else ''
    revoked = subscription.pasarguard_last_revoke_at.isoformat() if subscription.pasarguard_last_revoke_at else ''
    return f'{created}|{revoked}'


async def lookup_xdrive_target(short_uuids: list[str]) -> XdriveTarget | None:
    """Найти подписку по shortUuid и решить, положен ли ей xdrive сейчас.

    Возвращает ``None`` на любую неоднозначность и любой сбой: слияние —
    добавка к рабочей подписке, а не точка отказа. Grace-оверлей делает
    подписку неeligible, как и в проекторе: иначе xdrive обходил бы лимит
    грейс-трафика.
    """
    if not short_uuids or not settings.is_xdrive_enabled():
        return None

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Subscription, User.status, Tariff.server_traffic_limits)
                .join(User, User.id == Subscription.user_id)
                .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
                .where(Subscription.remnawave_short_uuid.in_(short_uuids))
                .limit(2)
            )
            rows = result.all()
            # Два совпадения — путь неоднозначный, лучше не угадывать.
            if len(rows) != 1:
                return None

            subscription, user_status, server_traffic_limits = rows[0]
            if subscription.pasarguard_user_id is None:
                return None

            grace_ids = await get_open_grace_subscription_ids(db)
            if not subscription_allows_xdrive(
                status=subscription.status,
                end_date=subscription.end_date,
                user_status=user_status,
                grace_open=subscription.id in grace_ids,
                now=datetime.now(UTC),
            ):
                return None

            squads = get_premium_squads_for_tariff(SimpleNamespace(server_traffic_limits=server_traffic_limits))
            if len(squads) != 1:
                return None

            state = await get_state(db, subscription.id, next(iter(squads)))
            if not premium_state_allows_xdrive(state=_premium_view(state), squad_count=len(squads)):
                return None

            return XdriveTarget(
                subscription_id=subscription.id,
                pasarguard_user_id=int(subscription.pasarguard_user_id),
                cache_version=_cache_version(subscription),
            )
    except Exception as error:
        logger.warning(
            'Не удалось определить xdrive-цель подписки',
            short_uuid_count=len(short_uuids),
            error=str(error)[:200],
        )
        return None


class PasarguardConfigProvider:
    """Кеш Xray-конфига PasarGuard на короткий TTL.

    Ключ — пользователь панели и поколение личности. Тело конфига не
    логируется и не сохраняется нигде, кроме памяти процесса.
    """

    def __init__(self, api: PasarguardAPI, cache_seconds: int) -> None:
        self._api = api
        self._cache_seconds = max(0, int(cache_seconds))
        self._cache: dict[tuple[int, str], tuple[float, bytes]] = {}

    async def get(self, target: XdriveTarget) -> bytes | None:
        now = asyncio.get_running_loop().time()
        key = (target.pasarguard_user_id, target.cache_version)
        cached = self._cache.get(key)
        if cached is not None:
            if cached[0] > now:
                return cached[1]
            self._cache.pop(key, None)

        try:
            payload = await self._api.get_subscription_config(target.pasarguard_user_id, client_type='xray')
        except Exception as error:
            # Ошибку не кешируем: следующий запрос клиента повторит попытку.
            logger.warning(
                'Не удалось получить xdrive-конфиг PasarGuard',
                subscription_id=target.subscription_id,
                pasarguard_user_id=target.pasarguard_user_id,
                error=str(error)[:200],
            )
            return None

        if not payload:
            return None

        if self._cache_seconds:
            self._cache[key] = (now + self._cache_seconds, payload)
            if len(self._cache) > _CACHE_PRUNE_THRESHOLD:
                self._cache = {stale_key: entry for stale_key, entry in self._cache.items() if entry[0] > now}
        return payload

    async def aclose(self) -> None:
        await self._api.close()
