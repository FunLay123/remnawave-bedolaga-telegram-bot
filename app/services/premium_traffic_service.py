"""Учёт премиум-трафика по сквадам и снятие доступа при исчерпании.

Панель не умеет ограничивать трафик отдельным сквадом — у пользователя одно поле
``trafficLimitBytes`` на всю учётную запись. Этот сервис считает расход сам и сам
снимает доступ, убирая сквад из ``activeInternalSquads``.

**Как считается расход.** ``POST /api/bandwidth-stats/nodes/usage`` принимает
список нод и диапазон дат, а отдаёт расход каждого пользователя за этот
диапазон. Один запрос покрывает весь сквад и всех его подписчиков сразу, поэтому
стоимость прохода растёт от числа сквадов, а не от числа пользователей.

Диапазон у эндпоинта задаётся датами ``YYYY-MM-DD`` — время панель отвергает.
Отсюда согласованное огрубление: в первые сутки периода в расход попадает и то,
что потрачено до сброса. Раз в период это незаметно, а точности до суток хватает.

**Границы периода** считаются отдельно (``utils/premium_traffic_period``) по
режиму сброса тарифа, чтобы премиум обнулялся синхронно с общим трафиком.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.premium_traffic import (
    get_or_create_state,
    record_usage,
    start_new_period,
)
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionPremiumTraffic, SubscriptionStatus, Tariff
from app.services.remnawave_service import RemnaWaveService
from app.utils.panel_node_usage import normalize_node_usage
from app.utils.premium_traffic import (
    BYTES_IN_GB,
    PremiumSquadConfig,
    get_premium_squads_for_tariff,
    parse_premium_squads,
)
from app.utils.premium_traffic_period import period_anchor, resolve_period_start


logger = structlog.get_logger(__name__)


DEFAULT_INTERVAL_SECONDS = 300
WARNING_THRESHOLD = 0.8
# Ноды сквада меняются редко, а спрашивают их на каждом проходе по каждому
# скваду. Кеш живёт дольше интервала воркера, чтобы не дёргать панель впустую.
NODES_CACHE_TTL_SECONDS = 3600
# Карточка панельного пользователя нужна ради `lastTrafficResetAt` (досрочный
# сброс) и фактического набора сквадов (сверка). Обе величины меняются редко, а
# запрашивать их на каждого подписчика каждый проход — самая дорогая часть
# работы: при пяти тысячах подписок это больше десятка запросов в секунду
# непрерывно. Держим час, разброс не даёт всем протухнуть одновременно.
PANEL_USER_CACHE_TTL_SECONDS = 3600
PANEL_USER_CACHE_JITTER = 0.25


@dataclass
class _Target:
    """Подписка, которую проверяем по одному конкретному премиум-скваду."""

    subscription: Subscription
    config: PremiumSquadConfig
    panel_user_id: int
    # Как назвать сервер в уведомлении: своё название из тарифа, иначе имя
    # сервера. Без него человек с несколькими премиум-серверами не поймёт, на
    # каком именно кончился лимит.
    display_name: str = ''


@dataclass
class _Orphan:
    """Состояние премиум-лимита, которому больше не отвечает конфигурация.

    Появляется, когда сквад убрали из премиального списка тарифа или подписку
    перевели на тариф, где этого сквада нет. ``_collect_targets`` такие пары уже
    не выдаёт, поэтому снять с них ``is_limited`` больше некому.
    """

    subscription_id: int
    squad_uuid: str
    is_limited: bool
    # ``None`` только если строка подписки исчезла: внешний ключ стоит с
    # ``ON DELETE CASCADE``, так что в норме этого не бывает.
    subscription: Subscription | None
    panel_user_id: int | None


class _DeferredOrphanRestore(Exception):
    """Внутренний сигнал ``_clear_orphan``: возврат сквада отложен grace-оверлеем.

    Границу метода не пересекает — нужен только затем, чтобы выйти из
    ``begin_nested()`` и откатить уже выполненное удаление строки состояния,
    когда сама отправка прошла без исключения, но применена не была.
    """


class PremiumTrafficService:
    """Периодический учёт премиум-трафика."""

    def __init__(self) -> None:
        self._running = False
        self._bot: Bot | None = None
        self._nodes_cache: dict[str, tuple[float, list[str]]] = {}
        # Живёт между проходами, а не внутри одного: см. PANEL_USER_CACHE_TTL_SECONDS.
        self._panel_users: dict[int, tuple[float, Any]] = {}

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    def is_enabled(self) -> bool:
        return bool(getattr(settings, 'PREMIUM_TRAFFIC_ENABLED', True))

    def get_check_interval_seconds(self) -> int:
        raw = getattr(settings, 'PREMIUM_TRAFFIC_CHECK_INTERVAL_SECONDS', DEFAULT_INTERVAL_SECONDS)
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL_SECONDS
        # Чаще минуты смысла нет: панель агрегирует статистику с задержкой, а
        # запросов станет больше без выигрыша в точности.
        return max(60, interval)

    # ---------------------------------------------------------------- цикл

    async def start_monitoring(self) -> None:
        self._running = True
        interval = self.get_check_interval_seconds()
        logger.info('🔄 Запуск учёта премиум-трафика', interval_seconds=interval)

        while self._running:
            try:
                stats = await self.process_once()
                if stats['limited'] or stats['restored'] or stats['cleaned'] or stats['errors']:
                    logger.info('📊 Проход по премиум-трафику', **stats)
            except Exception as error:
                logger.error('Ошибка в цикле учёта премиум-трафика', error=error, exc_info=True)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------- главное

    async def process_once(self) -> dict[str, int]:
        """Один проход: посчитать расход, снять и вернуть сквады."""
        stats = {'checked': 0, 'limited': 0, 'restored': 0, 'warned': 0, 'cleaned': 0, 'errors': 0}

        service = RemnaWaveService()
        if not service.is_configured:
            logger.debug('Панель не настроена, проход по премиум-трафику пропущен')
            return stats

        async with AsyncSessionLocal() as db:
            targets = await self._collect_targets(db)
            # Осиротевшие состояния ищем всегда, даже когда целей нет: они как
            # раз и появляются там, где конфигурация исчезла, а строка осталась.
            orphans = await self._collect_orphans(db)
            if not targets and not orphans:
                return stats

            targets, orphans = await self._drop_grace_owned(db, targets, orphans)
            if not targets and not orphans:
                return stats

            now = datetime.now(UTC)
            async with service.get_api_client() as api:
                if orphans:
                    stats['cleaned'] += await self._clear_orphans(db, api, orphans)

                # Группируем по скваду и дате начала периода: у эндпоинта один
                # диапазон на запрос, а у подписок с одинаковым режимом сброса
                # он совпадает. При календарных режимах это один запрос на сквад.
                groups: dict[tuple[str, str], list[_Target]] = defaultdict(list)
                # Начало периода — своё у каждой пары «подписка + сквад»:
                # состояния одной подписки могут разойтись, если один сквад уже
                # перевалил границу, а другой ещё нет.
                period_starts: dict[tuple[int, str], datetime] = {}
                for target in targets:
                    try:
                        period_start = await self._resolve_period(db, api, target, now)
                    except Exception as error:
                        stats['errors'] += 1
                        logger.warning(
                            'Не удалось определить период премиум-лимита',
                            subscription_id=target.subscription.id,
                            squad_uuid=target.config.squad_uuid,
                            error=error,
                        )
                        continue
                    period_starts[(target.subscription.id, target.config.squad_uuid)] = period_start
                    groups[(target.config.squad_uuid, period_start.date().isoformat())].append(target)

                await db.commit()

                for (squad_uuid, start_date), group in groups.items():
                    try:
                        usage = await self._fetch_usage(api, squad_uuid, start_date, now)
                    except Exception as error:
                        # Сбой панели ничего не снимает: состояние не трогаем,
                        # следующий проход посчитает заново.
                        stats['errors'] += len(group)
                        logger.warning(
                            'Не удалось получить расход по скваду',
                            squad_uuid=squad_uuid,
                            start_date=start_date,
                            error=error,
                        )
                        continue

                    for target in group:
                        stats['checked'] += 1
                        try:
                            outcome = await self._apply_usage(
                                db,
                                api,
                                target,
                                used_bytes=usage.get(target.panel_user_id, 0),
                                period_start=period_starts[(target.subscription.id, target.config.squad_uuid)],
                                now=now,
                                panel_user=self._cached_panel_user(target.panel_user_id),
                            )
                        except Exception as error:
                            stats['errors'] += 1
                            logger.error(
                                'Ошибка обработки премиум-лимита',
                                subscription_id=target.subscription.id,
                                squad_uuid=squad_uuid,
                                error=error,
                                exc_info=True,
                            )
                            continue
                        if outcome:
                            stats[outcome] += 1

            await db.commit()

        return stats

    # ------------------------------------------------------------- сборка

    async def _collect_targets(self, db: AsyncSession) -> list[_Target]:
        """Активные подписки с премиум-сквадами в тарифе.

        Сначала отбираем тарифы, потом подписки по ним. Тарифов десятки, а
        подписок могут быть десятки тысяч — тянуть их все и отсеивать в Python
        значило бы вычитывать таблицу целиком каждые пять минут.

        Фильтровать по JSON средствами БД нельзя переносимо: `server_traffic_limits`
        хранится как JSON и в PostgreSQL, и в SQLite, а условия к нему у них разные.
        """
        tariffs = await db.execute(select(Tariff.id, Tariff.server_traffic_limits))
        premium_tariff_ids = [tariff_id for tariff_id, limits in tariffs.all() if parse_premium_squads(limits)]
        if not premium_tariff_ids:
            return []

        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
                Subscription.tariff_id.in_(premium_tariff_ids),
            )
        )
        subscriptions = list(result.scalars().all())
        squad_names = await self._squad_display_names(db, subscriptions)

        targets: list[_Target] = []
        for subscription in subscriptions:
            premium = get_premium_squads_for_tariff(subscription.tariff)
            if not premium:
                continue
            panel_user_id = self._panel_user_id(subscription)
            if not panel_user_id:
                continue
            connected = set(subscription.connected_squads or [])
            for squad_uuid, config in premium.items():
                # Сквад, на который подписка не даёт права, не наш случай даже
                # если лимит на него в тарифе задан.
                if squad_uuid in connected:
                    targets.append(
                        _Target(
                            subscription,
                            config,
                            panel_user_id,
                            config.name or squad_names.get(squad_uuid, ''),
                        )
                    )
        return targets

    async def _collect_orphans(self, db: AsyncSession) -> list[_Orphan]:
        """Состояния, для которых в тарифе больше нет премиум-лимита.

        Обратная сторона ``_collect_targets``: та выдаёт цель, только пока сквад
        премиальный в текущем тарифе подписки. Стоит убрать его из премиального
        списка или перевести подписку на другой тариф — и пара «подписка+сквад»
        выпадает из обхода вместе с ветками возврата. ``is_limited`` остаётся
        поднятым навсегда, ``effective_panel_squads`` продолжает вычитать сквад
        из набора для панели, и клиент оказывается заперт, продолжая платить.

        Набор сквадов подписки здесь намеренно не проверяется: сквад, которого
        нет в ``connected_squads``, в панель и так не уезжает, а состояние по
        нему — законный учёт периода на случай, если сквад вернут.
        """
        result = await db.execute(select(SubscriptionPremiumTraffic))
        states = list(result.scalars().all())
        if not states:
            return []

        subscriptions = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(Subscription.id.in_({state.subscription_id for state in states}))
        )
        by_id = {subscription.id: subscription for subscription in subscriptions.scalars().all()}

        orphans: list[_Orphan] = []
        for state in states:
            subscription = by_id.get(state.subscription_id)
            if subscription is not None and state.squad_uuid in get_premium_squads_for_tariff(subscription.tariff):
                continue
            orphans.append(
                _Orphan(
                    subscription_id=state.subscription_id,
                    squad_uuid=state.squad_uuid,
                    is_limited=bool(state.is_limited),
                    subscription=subscription,
                    panel_user_id=self._panel_user_id(subscription) if subscription is not None else None,
                )
            )
        return orphans

    @staticmethod
    async def _drop_grace_owned(
        db: AsyncSession,
        targets: list[_Target],
        orphans: list[_Orphan],
    ) -> tuple[list[_Target], list[_Orphan]]:
        """Убрать из прохода подписки с открытым grace-оверлеем.

        Во время инцидента составом сквадов владеет grace: он держит в панели
        свой снимок, сверяет панель с ним (`panel_matches_overlay`) и возвращает
        своё циклом сверки. Наше снятие он откатит и запишет ошибку — сломается
        не премиум-ограничение, а grace. Поэтому такие подписки проход
        пропускает целиком: ни обращения к панели, ни записи состояния. Ничего
        не теряется — оверлей временный, а следующий проход после его закрытия
        досчитает период и снимет сквад.

        Множество резолвится один раз за проход: запрос на подписку означал бы
        десятки тысяч обращений к БД каждые пять минут, а при выключенном grace
        запроса не будет вовсе — `get_open_grace_subscription_ids` в
        немутирующих режимах отвечает пустым множеством сразу.
        """
        from app.services.grace_access_runtime import get_open_grace_subscription_ids

        open_grace_ids = await get_open_grace_subscription_ids(db)
        if not open_grace_ids:
            return targets, orphans

        kept_targets = [target for target in targets if target.subscription.id not in open_grace_ids]
        kept_orphans = [orphan for orphan in orphans if orphan.subscription_id not in open_grace_ids]
        skipped = len(targets) - len(kept_targets) + len(orphans) - len(kept_orphans)
        if skipped:
            logger.info(
                'Премиум-проход пропустил подписки с открытым grace-оверлеем',
                skipped=skipped,
            )
        return kept_targets, kept_orphans

    async def _clear_orphans(self, db: AsyncSession, api: Any, orphans: list[_Orphan]) -> int:
        cleared = 0
        for orphan in orphans:
            if await self._clear_orphan(db, api, orphan):
                cleared += 1
        return cleared

    async def _clear_orphan(self, db: AsyncSession, api: Any, orphan: _Orphan) -> bool:
        """Вернуть сквад в панель и убрать осиротевшее состояние.

        Молча удалить строку нельзя: ``is_limited`` означает, что сквад снят в
        панели. Без строки база забудет об этом, а панель — нет, и запертым
        клиент останется уже без единого следа. То же самое случится, если
        возврат не упал с исключением, а был молча отложен
        ``update_panel_user_grace_safe`` из-за открытого grace-оверлея: панель
        ничего не восстановила, а строка исчезла бы как после успеха.

        Отсюда порядок: удаление внутри точки сохранения, потом отправка, и
        только потом коммит. Раньше отправить нельзя — ``effective_panel_squads``
        читает ту же строку и вернул бы сквад снова вычтенным. Сбой панели и
        отложенная запись одинаково откатывают точку сохранения: строка остаётся
        на месте, и следующий проход повторит попытку.
        """
        from app.database.crud.premium_traffic import delete_states_for_squads
        from app.services.grace_access_runtime import panel_update_was_deferred

        # Возвращать нечего, если сквад подписке и так не положен: в панель он
        # не уезжает, потому что его нет в `connected_squads`.
        needs_restore = (
            orphan.is_limited
            and orphan.subscription is not None
            and orphan.squad_uuid in (orphan.subscription.connected_squads or [])
        )
        if needs_restore and orphan.panel_user_id is None:
            # Адреса в панели нет — вернуть сквад нечем. Строку оставляем: она
            # единственное свидетельство, что сквад сняли.
            logger.warning(
                'Осиротевшее состояние премиум-лимита не снято: нет аккаунта в панели',
                subscription_id=orphan.subscription_id,
                squad_uuid=orphan.squad_uuid,
            )
            return False

        try:
            async with db.begin_nested():
                await delete_states_for_squads(db, orphan.subscription_id, {orphan.squad_uuid})
                if needs_restore:
                    result = await self._push_subscription_squads(db, api, orphan.subscription, orphan.panel_user_id)
                    if panel_update_was_deferred(result):
                        # Над подпиской открыт grace-оверлей: сквады в панель не
                        # ушли. Откатываем точку сохранения целиком — удаление
                        # строки не должно закоммититься без возврата.
                        raise _DeferredOrphanRestore
            await db.commit()
        except _DeferredOrphanRestore:
            logger.info(
                'Возврат осиротевшего сквада отложен: открыт grace-оверлей',
                subscription_id=orphan.subscription_id,
                squad_uuid=orphan.squad_uuid,
            )
            return False
        except Exception as error:
            logger.warning(
                'Не удалось снять осиротевшее состояние премиум-лимита',
                subscription_id=orphan.subscription_id,
                squad_uuid=orphan.squad_uuid,
                error=error,
            )
            return False

        logger.info(
            'Осиротевшее состояние премиум-лимита снято',
            subscription_id=orphan.subscription_id,
            squad_uuid=orphan.squad_uuid,
            restored=needs_restore,
        )
        return True

    @staticmethod
    async def _squad_display_names(db: AsyncSession, subscriptions: list[Subscription]) -> dict[str, str]:
        """Имена премиум-серверов одним запросом на весь проход."""
        from app.database.crud.server_squad import get_squad_display_names

        uuids: set[str] = set()
        for subscription in subscriptions:
            uuids.update(get_premium_squads_for_tariff(subscription.tariff))
        return await get_squad_display_names(db, sorted(uuids))

    @staticmethod
    def _panel_user_id(subscription: Subscription) -> int | None:
        if settings.is_multi_tariff_enabled():
            raw = getattr(subscription, 'remnawave_id', None)
        else:
            raw = getattr(subscription, 'remnawave_id', None) or (
                subscription.user.remnawave_id if subscription.user else None
            )
        try:
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------- период

    async def _resolve_period(
        self,
        db: AsyncSession,
        api: Any,
        target: _Target,
        now: datetime,
    ) -> datetime:
        """Определить период и, если он сменился, начать новый."""
        subscription = target.subscription
        state = await get_or_create_state(
            db,
            subscription.id,
            target.config.squad_uuid,
            limit_bytes=target.config.limit_bytes,
            period_start_at=now,
        )

        panel_user = await self._panel_user(api, target.panel_user_id)
        panel_reset_at = getattr(panel_user, 'last_traffic_reset_at', None)
        first_connected_at = getattr(panel_user, 'first_connected_at', None)
        anchor = period_anchor(first_connected_at, subscription.start_date, fallback=now)
        mode = getattr(subscription.tariff, 'traffic_reset_mode', None) or settings.DEFAULT_TRAFFIC_RESET_STRATEGY

        resolved = resolve_period_start(
            mode,
            anchor=anchor,
            now=now,
            panel_reset_at=panel_reset_at,
            acknowledged_panel_reset_at=state.panel_reset_ack_at,
        )

        if state.period_start_at is None or resolved > _as_utc(state.period_start_at):
            start_new_period(
                state,
                period_start_at=resolved,
                limit_bytes=target.config.limit_bytes,
                panel_reset_ack_at=panel_reset_at,
            )
            logger.info(
                'Новый период премиум-лимита',
                subscription_id=subscription.id,
                squad_uuid=target.config.squad_uuid,
                period_start=resolved.isoformat(),
            )
        elif state.limit_bytes != target.config.limit_bytes and not state.is_limited:
            # Лимит в тарифе поменяли посреди периода. Поднять потолок можно
            # сразу; понижение до уже снятого сквада не трогаем, чтобы правка
            # тарифа не возвращала доступ задним числом.
            state.limit_bytes = target.config.limit_bytes

        return _as_utc(state.period_start_at)

    async def _panel_user(self, api: Any, panel_user_id: int) -> Any:
        """Пользователь из панели: отметки времени и фактический набор сквадов.

        Кеш переживает проходы: запрашивать карточку на каждого подписчика раз в
        пять минут — самая дорогая часть работы, и обе нужные величины меняются
        куда реже. Немедленные действия — снятие и возврат сквада — от кеша не
        зависят, они идут по свежему замеру расхода.
        """
        cached = self._cached_panel_user(panel_user_id)
        if cached is not None:
            return cached

        panel_user = await api.get_user_by_id(panel_user_id)
        # Разброс срока жизни: иначе через час все записи протухли бы разом и
        # один проход дал бы залп на всю базу подписчиков.
        ttl = PANEL_USER_CACHE_TTL_SECONDS * (1 + random.uniform(-PANEL_USER_CACHE_JITTER, PANEL_USER_CACHE_JITTER))
        self._panel_users[panel_user_id] = (asyncio.get_running_loop().time() + ttl, panel_user)
        return panel_user

    def _cached_panel_user(self, panel_user_id: int) -> Any:
        """Непротухшая карточка из кеша либо None."""
        entry = self._panel_users.get(panel_user_id)
        if entry is None:
            return None
        expires_at, panel_user = entry
        if asyncio.get_running_loop().time() >= expires_at:
            del self._panel_users[panel_user_id]
            return None
        return panel_user

    def invalidate_panel_user(self, panel_user_id: int) -> None:
        """Сбросить кеш после того, как мы сами изменили набор сквадов."""
        self._panel_users.pop(panel_user_id, None)

    # -------------------------------------------------------------- расход

    async def _squad_nodes(self, api: Any, squad_uuid: str) -> list[str]:
        cached = self._nodes_cache.get(squad_uuid)
        now = asyncio.get_running_loop().time()
        if cached and now - cached[0] < NODES_CACHE_TTL_SECONDS:
            return cached[1]

        nodes = await api.get_internal_squad_accessible_nodes(squad_uuid)
        uuids = [node.uuid for node in nodes if getattr(node, 'uuid', None)]
        self._nodes_cache[squad_uuid] = (now, uuids)
        return uuids

    async def _fetch_usage(self, api: Any, squad_uuid: str, start_date: str, now: datetime) -> dict[int, int]:
        """Расход всех пользователей сквада за период: {panel_user_id: байты}."""
        node_uuids = await self._squad_nodes(api, squad_uuid)
        if not node_uuids:
            return {}

        # Верхняя граница — завтра: панель включает конечную дату не всегда
        # предсказуемо, а лишние сутки в диапазоне не могут занизить расход.
        end_date = (now + timedelta(days=1)).date().isoformat()
        response = await api.get_bandwidth_stats_nodes_usage(node_uuids, start_date, end_date)

        totals: dict[int, int] = defaultdict(int)
        for node_entry in (response or {}).get('nodes') or []:
            node_uuid = node_entry.get('uuid') or ''
            for item in normalize_node_usage(node_entry.get('users'), node_uuid):
                user_id = item['user_id']
                if user_id is not None:
                    totals[user_id] += item['total_bytes']
        return dict(totals)

    # ------------------------------------------------------------ решение

    async def _apply_usage(
        self,
        db: AsyncSession,
        api: Any,
        target: _Target,
        *,
        used_bytes: int,
        period_start: datetime,
        now: datetime,
        panel_user: Any = None,
    ) -> str | None:
        from app.database.crud.premium_traffic import get_state

        state = await get_state(db, target.subscription.id, target.config.squad_uuid)
        if state is None:
            return None

        record_usage(state, self._net_usage(state, used_bytes, period_start, now), checked_at=now)

        if state.is_exhausted and not state.is_limited:
            await self._limit_squad(db, api, target, state)
            return 'limited'

        if state.is_limited and not state.is_exhausted:
            await self._restore_squad(db, api, target, state)
            return 'restored'

        if not state.is_limited and not state.notified_80 and self._crossed_warning(state):
            state.notified_80 = True
            await self._notify_warning(target, state)
            return 'warned'

        # Сверка с панелью. Флаг `is_limited` могли снять не мы: докупка через
        # кабинет обнуляет его сама и сама же возвращает сквад. Если та отправка
        # не дошла — панель недоступна, оборвалась сессия, — сквад остался бы
        # снятым навсегда: ветки выше сюда уже не попадут, флаг-то снят.
        # Поэтому сверяем фактический набор с ожидаемым и досылаем расхождение.
        if not state.is_limited and self._squad_missing_in_panel(target, panel_user):
            await self._push_squads(db, api, target)
            logger.info(
                'Премиум-сквад досинхронизирован с панелью',
                subscription_id=target.subscription.id,
                squad_uuid=target.config.squad_uuid,
            )
            return 'restored'

        # Зеркальная сверка на снятие. `_limit_squad` коммитит `is_limited` до
        # отправки (иначе фильтр, читающий базу, вернул бы сквад обратно), и
        # упавшая отправка разводит базу с панелью: у нас сквад снят, у панели
        # работает. Ветки выше сюда уже не попадут — флаг-то стоит, — а
        # периодической пересылки сквадов в проекте нет: `sync_users_to_panel`
        # запускается вручную, рутинный мониторинг `activeInternalSquads` не
        # трогает. Без этой ветки клиент пользовался бы исчерпанным премиумом
        # бессрочно.
        #
        # Отправку здесь нельзя считать применённой: `update_panel_user_grace_safe`
        # молча откладывает `activeInternalSquads`, пока открыт grace-оверлей, и
        # отличить отложенное от применённого по её ответу нечем. Поэтому ветка
        # ничего не запоминает: следующий проход перечитает панель и, если сквад
        # всё ещё там, отправит снова.
        if state.is_limited and self._squad_present_in_panel(target, panel_user):
            await self._push_squads(db, api, target)
            logger.info(
                'Премиум-сквад доснят в панели повторно',
                subscription_id=target.subscription.id,
                squad_uuid=target.config.squad_uuid,
            )
            return 'limited'

        return None

    @classmethod
    def _squad_missing_in_panel(cls, target: _Target, panel_user: Any) -> bool:
        """Сквад положен подписке, но в панели его нет — надо вернуть."""
        squads = cls._panel_squads(panel_user)
        return squads is not None and target.config.squad_uuid not in squads

    @classmethod
    def _squad_present_in_panel(cls, target: _Target, panel_user: Any) -> bool:
        """Сквад снят в базе, но панель его всё ещё отдаёт — надо доснять."""
        squads = cls._panel_squads(panel_user)
        return squads is not None and target.config.squad_uuid in squads

    @staticmethod
    def _panel_squads(panel_user: Any) -> set[str] | None:
        """Фактический набор сквадов из карточки панели либо ``None``.

        Панель отдаёт сквады объектами `{uuid, name}`, а не строками. Разбор
        берём общий с grace-механизмом, чтобы обе части читали одно и то же.

        ``None`` — панель не ответила или ответила без поля: сверять не с чем, и
        обе ветки сверки обязаны промолчать. Пустой набор — ответ по существу:
        сквадов нет. Это законный случай (все сквады подписки премиальные и все
        исчерпаны), и путать его с «нет данных» нельзя.
        """
        if panel_user is None:
            return None
        raw = getattr(panel_user, 'active_internal_squads', None)
        if raw is None:
            return None

        from app.services.grace_access_runtime import _extract_panel_squads

        return set(_extract_panel_squads(raw))

    @staticmethod
    def _net_usage(state: Any, raw_bytes: int, period_start: datetime, now: datetime) -> int:
        """Расход за период с поправкой на первые сутки.

        Диапазон в статистике панели задаётся датами без времени, поэтому запрос
        за день начала периода приносит и то, что потрачено до сброса. Если
        период начался не в полночь, первый замер целиком относится к прошлому
        периоду — запоминаем его и дальше вычитаем.

        Поправку снимаем, только пока идут те же сутки, что и начало периода.
        Если воркер простоял дольше, вычитать уже нечего: первый замер включал
        бы законный расход нового периода, и мы подарили бы пользователю лимит.
        """
        if state.baseline_bytes is None:
            starts_midday = period_start.timetz().replace(tzinfo=None) != time(0, 0)
            same_day = now.date() == period_start.date()
            state.baseline_bytes = raw_bytes if (starts_midday and same_day) else 0
        return max(0, raw_bytes - (state.baseline_bytes or 0))

    @staticmethod
    def _crossed_warning(state: Any) -> bool:
        total = state.total_limit_bytes
        if total <= 0:
            return False
        return (state.used_bytes or 0) >= total * WARNING_THRESHOLD

    async def _limit_squad(self, db: AsyncSession, api: Any, target: _Target, state: Any) -> None:
        """Снять сквад: сперва отметить в базе, потом отправить в панель.

        Порядок важен. ``effective_panel_squads`` вычитает снятые сквады из
        набора, читая базу, — если отправить раньше коммита, фильтр ещё не
        увидит отметку и вернёт сквад обратно.

        Плата за такой порядок — окно, в котором база считает сквад снятым, а
        панель его ещё отдаёт: отправка после коммита может упасть. Окно
        закрывает сверка в ``_apply_usage`` (``_squad_present_in_panel``): она
        досылает снятие на следующем проходе.
        """
        state.is_limited = True
        await db.commit()

        await self._push_squads(db, api, target)
        logger.info(
            'Премиум-сквад снят за перерасход',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
            used_gb=round((state.used_bytes or 0) / BYTES_IN_GB, 2),
            limit_gb=round(state.total_limit_bytes / BYTES_IN_GB, 2),
        )
        await self._notify_exhausted(target, state)

    async def _restore_squad(self, db: AsyncSession, api: Any, target: _Target, state: Any) -> None:
        state.is_limited = False
        await db.commit()

        await self._push_squads(db, api, target)
        logger.info(
            'Премиум-сквад возвращён',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
        )

    async def _push_squads(self, db: AsyncSession, api: Any, target: _Target) -> None:
        await self._push_subscription_squads(db, api, target.subscription, target.panel_user_id)

    async def _push_subscription_squads(
        self,
        db: AsyncSession,
        api: Any,
        subscription: Subscription,
        panel_user_id: int,
    ) -> Any:
        """Отправить в панель набор сквадов подписки через общий фильтр.

        Отдельный вход от ``_push_squads`` нужен уборке осиротевших состояний: у
        неё нет ``_Target`` — сквад из конфигурации тарифа как раз и исчез.

        Возвращает ответ ``update_panel_user_grace_safe`` как есть.
        ``_limit_squad`` и ``_restore_squad`` его по-прежнему не читают — у них
        своя сверка с панелью на следующем проходе; читает только
        ``_clear_orphan``, которому терять строку состояния нельзя.
        """
        from app.services.grace_access_runtime import update_panel_user_grace_safe
        from app.utils.premium_traffic import effective_panel_squads

        result = await update_panel_user_grace_safe(
            api,
            subscription.id,
            user_id=panel_user_id,
            active_internal_squads=await effective_panel_squads(
                subscription.id, subscription.connected_squads or [], db=db
            ),
        )
        # Набор сквадов в панели только что изменился — иначе сверка на
        # следующем проходе увидела бы протухший снимок и отправила бы всё заново.
        self.invalidate_panel_user(panel_user_id)
        return result

    # ------------------------------------------------------- уведомления

    async def _notify_warning(self, target: _Target, state: Any) -> None:
        await self._notify(
            target,
            'PREMIUM_TRAFFIC_WARNING',
            (
                '⚠️ <b>Премиум-трафик заканчивается</b>\n\n'
                'Использовано {used} ГБ из {limit} ГБ.\n'
                'При исчерпании доступ к этим серверам будет приостановлен до конца периода.'
            ),
            state,
        )

    async def _notify_exhausted(self, target: _Target, state: Any) -> None:
        await self._notify(
            target,
            'PREMIUM_TRAFFIC_EXHAUSTED',
            (
                '🚫 <b>Премиум-трафик исчерпан</b>\n\n'
                'Израсходовано {limit} ГБ. Доступ к этим серверам приостановлен.\n'
                'Остальные серверы продолжают работать.'
            ),
            state,
        )

    async def _notify(self, target: _Target, key: str, default_text: str, state: Any) -> None:
        if self._bot is None:
            return
        user = target.subscription.user
        if user is None or not getattr(user, 'telegram_id', None):
            return

        from app.localization.texts import get_texts

        texts = get_texts(getattr(user, 'language', 'ru'))
        # Без названия человек с несколькими премиум-серверами не поймёт, на
        # каком именно кончился лимит.
        name = target.display_name or texts.t('PREMIUM_TRAFFIC_LABEL', 'Премиум-трафик')
        message = texts.t(key, default_text).format(
            name=name,
            used=round((state.used_bytes or 0) / BYTES_IN_GB, 1),
            limit=round(state.total_limit_bytes / BYTES_IN_GB, 1),
        )
        try:
            await self._bot.send_message(user.telegram_id, message, parse_mode='HTML')
        except Exception as error:
            logger.warning(
                'Не удалось отправить уведомление о премиум-трафике',
                telegram_id=user.telegram_id,
                error=error,
            )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


premium_traffic_service = PremiumTrafficService()
