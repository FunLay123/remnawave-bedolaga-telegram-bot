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
from collections.abc import Awaitable, Callable
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


def panel_user_id_for_subscription(subscription: Any) -> int | None:
    """Аккаунт в панели, к которому относятся расход и сквады подписки.

    В мультиподписках у каждой подписки свой аккаунт, и подстановка аккаунта
    пользователя увела бы расход, снятие сквада или сброс трафика на соседний
    тариф. Поэтому там — только аккаунт самой подписки, даже если его ещё нет.
    """
    if settings.is_multi_tariff_enabled():
        raw = getattr(subscription, 'remnawave_id', None)
    else:
        user = getattr(subscription, 'user', None)
        raw = getattr(subscription, 'remnawave_id', None) or (user.remnawave_id if user else None)
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


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
    # Причина снятия отдельно от следствия (``is_limited``, см. модель
    # ``SubscriptionPremiumTraffic``). Сквад может осиротеть и обычным путём —
    # админ обнулил ``traffic_limit_gb`` в тарифе («отдельного лимита нет», см.
    # ``app/utils/premium_traffic.py``), — но если он ещё и закрыт вручную
    # (``/close``), это не «сквад больше не премиальный», а прежнее решение
    # оператора отобрать доступ. Осиротение не должно его снимать.
    closed_at: datetime | None


class _PanelWriteNotApplied(Exception):
    """Внутренний сигнал: панель отложила запись, хотя отправка не упала.

    Раньше у каждой точки был свой способ решить, что отправка не применена:
    где-то это было исключение, где-то — расхождение, замеченное только на
    следующем проходе. Этот сигнал сводит именно случай «отправка прошла без
    исключения, но ``update_panel_user_grace_safe`` отложила
    ``activeInternalSquads`` из-за открытого grace-оверлея» к тому же виду,
    что и настоящий отказ панели: оба ловит ``_log_panel_failure`` одной
    парой ``except`` и откатывает точку сохранения, открытую вокруг них.
    Границу метода-обёртки не пересекает.
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
            # До сбора целей: если премиум убрали из всех тарифов, целей не будет,
            # а снятые сквады всё равно надо вернуть.
            stats['restored'] += await self._reconcile_limited_states(db, service)

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
                    closed_at=state.closed_at,
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

        Отсюда порядок: удаление внутри точки сохранения, потом отправка.
        Раньше отправить нельзя — ``effective_panel_squads`` читает ту же
        строку и вернул бы сквад снова вычтенным. Сбой панели и отложенная
        запись — общий случай ``_log_panel_failure``: обе откатывают точку
        сохранения целиком, строка остаётся на месте, и следующий проход
        повторит попытку.

        Закрытые администратором строки (``orphan.closed_at is not None``)
        обрабатываются отдельной веткой в начале функции — ни удаления, ни
        возврата в панель для них нет, см. её комментарий.
        """
        from app.database.crud.premium_traffic import delete_states_for_squads

        if orphan.closed_at is not None:
            # Сквад мог осиротеть самым обычным путём — админ обнулил
            # `traffic_limit_gb` в тарифе («отдельного лимита нет», это
            # сквозной кейс `parse_premium_squad`), — но если он вдобавок
            # закрыт вручную (`/close`), это решение оператора, а не следствие
            # того, что лимита в тарифе больше нет. Ни удалять строку (вместе
            # с ней исчез бы `closed_at` — единственный признак причины), ни
            # возвращать сквад в панель нельзя: закрытие переживает
            # осиротение точно так же, как переживает смену периода
            # (`start_new_period`) и доначисление трафика (`add_extra_bytes`).
            # Снять его может только явный `/reopen`.
            logger.info(
                'Осиротевшее состояние премиум-лимита пропущено: закрыто администратором',
                subscription_id=orphan.subscription_id,
                squad_uuid=orphan.squad_uuid,
            )
            return False

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

        async def _do() -> None:
            async with db.begin_nested():
                await delete_states_for_squads(db, orphan.subscription_id, {orphan.squad_uuid})
                if needs_restore:
                    self._raise_if_deferred(
                        await self._push_subscription_squads(db, api, orphan.subscription, orphan.panel_user_id)
                    )

        applied = await self._log_panel_failure(
            _do,
            fail_event='Не удалось снять осиротевшее состояние премиум-лимита',
            defer_event='Возврат осиротевшего сквада отложен: открыт grace-оверлей',
            subscription_id=orphan.subscription_id,
            squad_uuid=orphan.squad_uuid,
        )
        if not applied:
            return False

        await db.commit()
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
        return panel_user_id_for_subscription(subscription)

    # ------------------------------------------------- отменённые лимиты

    async def _reconcile_limited_states(self, db: AsyncSession, service: Any) -> int:
        """Вернуть право на снятый сквад, если его стёрло чтение панели.

        Полная синхронизация переносит набор сквадов панели в
        ``connected_squads`` (`panel_sync.projection`), а снятый за перерасход
        сквад мы из панели убрали — синхронизация вычёркивает его из права
        подписки. После этого сквад не вернулся бы даже сбросом периода:
        отправка берёт набор именно из права. Тариф всё ещё должен его давать
        — иначе право восстанавливать не за чем, см. ``_restore_lost_entitlement``.

        Отменённый лимит (сквад пропал из премиум-списка тарифа) сюда не
        относится — та же проверка, `_limit_no_longer_applies`, что и в
        ``_collect_orphans``, но с удалением строки, возвратом сквада в
        панель, ``closed_at`` и учётом отложенной записи. Раньше эта функция
        делала это сама, и в одном проходе воркера сквад уезжал в панель
        дважды: один раз отсюда, второй раз — из ``_clear_orphan`` следом,
        когда запись оставалась ``is_limited`` (отправка не подтвердилась —
        упала или была отложена оверлеем).
        """
        limited = await self._limited_states(db)
        if not limited:
            return 0

        restored = 0
        for state, subscription in limited:
            if state.closed_at is not None:
                # Закрыто администратором намеренно, а не за перерасход — эту
                # запись не трогаем, иначе решение админа развалилось бы само
                # собой на следующем проходе воркера.
                continue
            if self._limit_no_longer_applies(state, subscription):
                # Снятие и возврат в панель — забота `_collect_orphans` этим
                # же проходом, повторно ходить в панель здесь незачем.
                continue
            if self._restore_lost_entitlement(state, subscription):
                restored += 1
        # Возвращённые права — в базу, даже если восстанавливать было нечего.
        await db.commit()
        return restored

    @staticmethod
    def _restore_lost_entitlement(state: Any, subscription: Any) -> bool:
        """Вернуть право на снятый сквад, если его стёрла синхронизация с панелью.

        Чтение панели переносит её набор сквадов в `connected_squads`
        (`panel_sync.projection`), а снятый за перерасход сквад мы из панели
        убрали — полная синхронизация вычёркивает его из права подписки. После
        этого сквад не вернулся бы и по сбросу периода: возвращать было бы
        нечего, отправка берёт набор именно из права.

        Право сверяем с тарифом: сквад возвращаем, только если тариф его
        по-прежнему даёт.
        """
        tariff = getattr(subscription, 'tariff', None)
        allowed = getattr(tariff, 'allowed_squads', None) or []
        connected = list(subscription.connected_squads or [])
        if state.squad_uuid not in allowed or state.squad_uuid in connected:
            return False

        subscription.connected_squads = [*connected, state.squad_uuid]
        logger.info(
            'Право на премиум-сквад восстановлено: его стёрло чтение панели',
            subscription_id=subscription.id,
            squad_uuid=state.squad_uuid,
        )
        return True

    @staticmethod
    async def _limited_states(db: AsyncSession) -> list[tuple[Any, Subscription]]:
        """Снятые записи вместе с подписками. Их единицы, выборка по флагу дешёвая."""
        result = await db.execute(
            select(SubscriptionPremiumTraffic, Subscription)
            .join(Subscription, Subscription.id == SubscriptionPremiumTraffic.subscription_id)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(SubscriptionPremiumTraffic.is_limited.is_(True))
        )
        return list(result.tuples().all())

    @staticmethod
    def _limit_no_longer_applies(state: Any, subscription: Any) -> bool:
        return state.squad_uuid not in get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))

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

        if state.last_checked_at is None:
            # Запись ни разу не замерялась — её начало периода временное. Создают
            # её «с этой секунды» и воркер, и докупка, и выдача админом: верное
            # начало без карточки из панели не посчитать. Проверка на смену
            # периода ниже его не подхватит — верное начало всегда раньше
            # «сейчас», — и расход с начала периода до включения лимита терялся бы.
            # Счётчики не обнуляем: докупленное до первого замера должно остаться.
            state.period_start_at = resolved
            state.baseline_bytes = None
            if panel_reset_at is not None:
                state.panel_reset_ack_at = panel_reset_at
        elif resolved > _as_utc(state.period_start_at):
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
            return 'limited' if await self._limit_squad(db, api, target, state) else None

        if state.is_limited and not state.is_exhausted:
            return 'restored' if await self._restore_squad(db, api, target, state) else None

        if not state.is_limited and not state.notified_80 and self._crossed_warning(state):
            state.notified_80 = True
            await self._notify_warning(target, state)
            return 'warned'

        # Сверка с панелью. Флаг `is_limited` могли снять не мы: докупка через
        # кабинет обнуляет его сама и сама же возвращает сквад. Если та отправка
        # не дошла — панель недоступна, оборвалась сессия, — сквад остался бы
        # снятым навсегда: ветки выше сюда уже не попадут, флаг-то снят.
        # Поэтому сверяем фактический набор с ожидаемым и досылаем расхождение.
        # Состояние здесь уже верное (флаг не меняем), поэтому откатывать
        # нечего — `_log_panel_failure` просто не даёт отказу или отсрочке
        # выдать себя за успех: следующий проход перечитает панель и, если
        # сквад всё ещё расходится, повторит попытку.
        if not state.is_limited and self._squad_missing_in_panel(target, panel_user):

            async def _push_missing() -> None:
                self._raise_if_deferred(await self._push_squads(db, api, target))

            if not await self._log_panel_failure(
                _push_missing,
                fail_event='Не удалось досинхронизировать премиум-сквад с панелью',
                defer_event='Досинхронизация премиум-сквада отложена: открыт grace-оверлей',
                subscription_id=target.subscription.id,
                squad_uuid=target.config.squad_uuid,
            ):
                return None
            logger.info(
                'Премиум-сквад досинхронизирован с панелью',
                subscription_id=target.subscription.id,
                squad_uuid=target.config.squad_uuid,
                direction='снят' if state.is_limited else 'возвращён',
            )
            return 'limited' if state.is_limited else 'restored'

        # Зеркальная сверка на снятие. Ветки выше сюда уже не попадут — флаг-то
        # стоит, — а периодической пересылки сквадов в проекте нет:
        # `sync_users_to_panel` запускается вручную, рутинный мониторинг
        # `activeInternalSquads` не трогает. Без этой ветки клиент пользовался
        # бы исчерпанным премиумом бессрочно.
        if state.is_limited and self._squad_present_in_panel(target, panel_user):

            async def _push_present() -> None:
                self._raise_if_deferred(await self._push_squads(db, api, target))

            if not await self._log_panel_failure(
                _push_present,
                fail_event='Не удалось доснять премиум-сквад в панели',
                defer_event='Повторное снятие премиум-сквада отложено: открыт grace-оверлей',
                subscription_id=target.subscription.id,
                squad_uuid=target.config.squad_uuid,
            ):
                return None
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

    async def _limit_squad(self, db: AsyncSession, api: Any, target: _Target, state: Any) -> bool:
        """Снять сквад: изменить базу и отправить в панель одной точкой сохранения.

        Раньше флаг коммитился до отправки — так ``effective_panel_squads``
        видел снятие раньше, чем оно доезжало до панели, ценой окна, в котором
        упавшая или отложенная отправка разводила базу с панелью: у нас сквад
        снят, у панели — работает. Теперь то же самое достигается внутри
        ``begin_nested()`` — смена флага видна фильтру за счёт автосброса в той
        же транзакции, — а отказ или отсрочка откатывают точку сохранения
        целиком через ``_log_panel_failure``: базе нечего утверждать сверх
        того, что панель подтвердила, и следующий проход честно повторит
        попытку с нуля.
        """

        async def _do() -> None:
            async with db.begin_nested():
                state.is_limited = True
                # `autoflush=False` в проде: без явного flush фильтр ниже
                # прочитал бы из базы старое значение и не исключил бы сквад.
                await db.flush()
                self._raise_if_deferred(await self._push_squads(db, api, target))

        applied = await self._log_panel_failure(
            _do,
            fail_event='Не удалось снять премиум-сквад в панели',
            defer_event='Снятие премиум-сквада отложено: открыт grace-оверлей',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
        )
        if not applied:
            return False

        await db.commit()
        logger.info(
            'Премиум-сквад снят за перерасход',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
            used_gb=round((state.used_bytes or 0) / BYTES_IN_GB, 2),
            limit_gb=round(state.total_limit_bytes / BYTES_IN_GB, 2),
        )
        await self._notify_exhausted(target, state)
        return True

    async def _restore_squad(self, db: AsyncSession, api: Any, target: _Target, state: Any) -> bool:
        """Вернуть сквад: тот же контракт, что и у ``_limit_squad``, только флаг снимается."""

        async def _do() -> None:
            async with db.begin_nested():
                state.is_limited = False
                # См. `_limit_squad`: без явного flush фильтр ниже не увидел бы
                # возврат и продолжил бы исключать сквад из набора.
                await db.flush()
                self._raise_if_deferred(await self._push_squads(db, api, target))

        applied = await self._log_panel_failure(
            _do,
            fail_event='Не удалось вернуть премиум-сквад в панели',
            defer_event='Возврат премиум-сквада отложен: открыт grace-оверлей',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
        )
        if not applied:
            return False

        await db.commit()
        logger.info(
            'Премиум-сквад возвращён',
            subscription_id=target.subscription.id,
            squad_uuid=target.config.squad_uuid,
        )
        return True

    @staticmethod
    def _raise_if_deferred(result: Any) -> None:
        """Поднять ``_PanelWriteNotApplied``, если панель отложила запись.

        Настоящий отказ панели поднимается сам — эта проверка нужна только для
        второго исхода ``update_panel_user_grace_safe``: ответ без исключения,
        но с открытым grace-оверлеем, отложившим ``activeInternalSquads``. Не
        различая их, вызывающий счёл бы отправку применённой и закоммитил бы
        то, чего панель не сделала.
        """
        from app.services.grace_access_runtime import panel_update_was_deferred

        if panel_update_was_deferred(result):
            raise _PanelWriteNotApplied

    @staticmethod
    async def _log_panel_failure(
        action: Callable[[], Awaitable[None]],
        *,
        fail_event: str,
        defer_event: str,
        **log_kwargs: Any,
    ) -> bool:
        """Выполнить ``action``, свести отказ панели и отсрочку к одному ответу.

        Единый контракт для каждой точки этого сервиса, которая пишет
        ``activeInternalSquads``: раньше они расходились — где-то отказ поднимал
        исключение и валил проход по этой цели, где-то отложенная запись из-за
        открытого grace-оверлея проходила молча как успех. Здесь оба случая
        логируются одинаково и останавливают попытку без исключения наружу:
        вызывающий получает ``False`` и не имеет права писать состояние
        дальше — следующий проход перечитает панель и, если нужно, повторит.
        """
        try:
            await action()
        except _PanelWriteNotApplied:
            logger.info(defer_event, **log_kwargs)
            return False
        except Exception as error:
            logger.warning(fail_event, error=error, **log_kwargs)
            return False
        return True

    async def _push_squads(self, db: AsyncSession, api: Any, target: _Target) -> Any:
        """Отправить в панель набор сквадов цели; см. ``_push_subscription_squads``."""
        return await self._push_subscription_squads(db, api, target.subscription, target.panel_user_id)

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

        Фильтр вызывается здесь напрямую, с явной ``db``, а не через
        ``patch_panel_squads``: у него своей сессии нет и открыть её пришлось бы
        заново, а вызывающие (`_limit_squad`/`_restore_squad`) меняют
        ``is_limited`` в этой же сессии внутри ``db.begin_nested()`` и только
        ``flush``-ят, не коммитя. Отдельная сессия эту незакоммиченную запись
        не увидела бы и отфильтровала бы сквад по устаревшему флагу.

        Возвращает ответ ``update_panel_user_grace_safe`` как есть: каждая
        точка, что пишет сквады, читает его через ``_raise_if_deferred`` —
        отличить отложенную запись от применённой иначе нечем.
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
