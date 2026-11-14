"""Желаемое состояние пользователя PasarGuard (xdrive) — чистые функции.

Одни и те же правила нужны фоновому проектору и горячему пути выдачи
подписки: если бы каждый выводил eligibility сам, ручные правки в панели и
слияние конфигов разъезжались бы. Поэтому решения принимаются здесь, на
снимке данных: ни БД, ни API-вызовов.

``enabled`` — пользовательский рубильник ``settings.is_xdrive_enabled()``.
Выключенный флаг не no-op: известный пользователь панели должен быть
выключен, а новый — не создаваться.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import or_, select

from app.config import settings
from app.database.crud.pasarguard import (
    add_delete_intent,
    clear_pasarguard_identity,
    delete_intent,
    list_delete_intents,
    set_pasarguard_identity,
)
from app.database.database import AsyncSessionLocal
from app.database.models import (
    Subscription,
    SubscriptionPremiumTraffic,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
)
from app.external.pasarguard_api import (
    PasarguardAPI,
    PasarguardAPIError,
    PasarguardNotFoundError,
    PasarguardUser,
)
from app.services.grace_access_runtime import get_open_grace_subscription_ids
from app.utils.premium_traffic import get_premium_squads_for_tariff


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


ALLOWED_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
    }
)


@dataclass(frozen=True)
class PremiumStateView:
    """Снимок premium-пула одной подписки без ORM-зависимости.

    ``squad_connected`` — premium-сквад всё ещё входит в
    ``subscription.connected_squads``. Убранный из набора сквад панель и
    premium-воркер не обслуживают, поэтому строка состояния могла устареть:
    по ней нельзя ни включать xdrive, ни считать его расход.
    """

    is_limited: bool
    is_exhausted: bool
    closed_at: datetime | None
    total_limit_bytes: int
    used_bytes: int
    squad_connected: bool


@dataclass(frozen=True)
class SyncSubject:
    """Всё, что нужно для решения по одной подписке."""

    subscription_id: int
    subscription_status: str
    end_date: datetime | None
    user_status: str
    premium_state: PremiumStateView | None
    premium_squad_count: int
    grace_open: bool
    pasarguard_user: PasarguardUser | None


@dataclass(frozen=True)
class DesiredState:
    """Что проектор должен сделать с пользователем панели.

    ``exists`` — пользователь уже есть либо его нужно создать (eligible-подписка
    без идентичности). ``enabled=False`` означает «выключить, ничего не писать»:
    ``expire``/``data_limit``/``group_ids``/``note`` для этого состояния пусты.
    """

    exists: bool
    enabled: bool
    username: str
    expire: datetime | None
    data_limit: int | None
    group_ids: tuple[int, ...]
    note: str | None
    reason: str


def _as_utc(value: datetime | None) -> datetime | None:
    """Наивные отметки трактуем как UTC: сравнения в проекте всегда в UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def subscription_allows_xdrive(
    *,
    status: str,
    end_date: datetime | None,
    user_status: str,
    grace_open: bool,
    now: datetime,
) -> bool:
    """Даёт ли подписка вместе с пользователем право на xdrive сейчас.

    v1 не учитывает grace: пока оверлей открыт, подписка не eligible — иначе
    xdrive обходил бы лимит грейс-трафика.
    """
    if grace_open:
        return False
    if status not in ALLOWED_SUBSCRIPTION_STATUSES:
        return False
    end = _as_utc(end_date)
    if end is None or end <= _as_utc(now):
        return False
    return user_status == UserStatus.ACTIVE.value


def premium_state_allows_xdrive(*, state: PremiumStateView | None, squad_count: int) -> bool:
    """Есть ровно один positive premium-сквад, он подключён, и его пул не закрыт.

    ``squad_count`` — число premium-сквадов тарифа (с положительным лимитом).
    Больше одного — v1 выключает xdrive: непонятно, к какому пулу относить
    расход. Нулевой пул тоже не годится: ``data_limit`` по нему вышел бы нулём,
    а ноль в PasarGuard означает «без лимита».
    """
    if state is None or squad_count != 1:
        return False
    if not state.squad_connected:
        return False
    if state.is_limited or state.is_exhausted or state.closed_at is not None:
        return False
    return state.total_limit_bytes > 0


def _denied(*, exists: bool, username: str, reason: str) -> DesiredState:
    return DesiredState(
        exists=exists,
        enabled=False,
        username=username,
        expire=None,
        data_limit=None,
        group_ids=(),
        note=None,
        reason=reason,
    )


def evaluate_desired_state(
    subject: SyncSubject,
    *,
    enabled: bool,
    group_id: int,
    prefix: str,
    instance_id: str,
    now: datetime,
) -> DesiredState:
    """Свести снимок подписки в желаемое состояние пользователя PasarGuard.

    ``data_limit`` абсолютный: PasarGuard сравнивает его с накопленным
    ``used_traffic``, а не с расходом за период. Поэтому к текущему счётчику
    пользователя прибавляется остаток premium-пула.
    """
    username = f'{prefix}{subject.subscription_id}'
    note = f'xdrive-bot:{instance_id}:sub:{subject.subscription_id}'
    existing = subject.pasarguard_user is not None

    if not enabled:
        return _denied(exists=existing, username=username, reason='flag_off')

    if not subscription_allows_xdrive(
        status=subject.subscription_status,
        end_date=subject.end_date,
        user_status=subject.user_status,
        grace_open=subject.grace_open,
        now=now,
    ):
        return _denied(exists=existing, username=username, reason='subscription_ineligible')

    state = subject.premium_state
    if state is None or not premium_state_allows_xdrive(
        state=state,
        squad_count=subject.premium_squad_count,
    ):
        return _denied(exists=existing, username=username, reason='premium_ineligible')

    used_traffic = subject.pasarguard_user.used_traffic if subject.pasarguard_user is not None else 0
    remaining_bytes = max(0, state.total_limit_bytes - state.used_bytes)
    data_limit = used_traffic + remaining_bytes
    if data_limit <= 0:
        # Ноль PasarGuard трактует как «без лимита» — лучше выключить.
        return _denied(exists=existing, username=username, reason='zero_data_limit')

    return DesiredState(
        exists=True,
        enabled=True,
        username=username,
        expire=subject.end_date,
        data_limit=data_limit,
        group_ids=(group_id,),
        note=note,
        reason='enabled',
    )


# --------------------------------------------------------------------------
# Проектор: единственный писатель PasarGuard
# --------------------------------------------------------------------------

STATS_KEYS = ('checked', 'created', 'updated', 'disabled', 'enabled', 'deleted', 'errors')

# Панель агрегирует expiry/limit с секундной точностью: расхождение меньше
# секунды — не повод писать каждую проход.
_EXPIRE_TOLERANCE_SECONDS = 1.0
# Допуск на рассинхрон счётчиков панели и бота: 1 MiB или 1 %.
_DATA_LIMIT_TOLERANCE_MIN_BYTES = 1024 * 1024
# Между пачками бэкфилла пауза, чтобы старт не залил панель пачкой POST-ов.
_BACKFILL_BATCH_PAUSE_SECONDS = 0.05
# Уборка сирот — страховка для очереди удалений, чаще раза в час не нужна.
_ORPHAN_CLEANUP_INTERVAL_SECONDS = 3600.0
_PANEL_STATUSES_NEEDING_ACTIVE = frozenset({'limited', 'expired'})


def _empty_stats() -> dict[str, int]:
    return dict.fromkeys(STATS_KEYS, 0)


def _make_api(*, timeout: float | None = None, get_attempts: int | None = None) -> PasarguardAPI | None:
    """Собрать клиент панели. Без URL, ключа и группы проектор молчит."""
    if not settings.is_pasarguard_configured():
        return None
    return PasarguardAPI(
        base_url=settings.PASARGUARD_API_URL or '',
        api_key=settings.PASARGUARD_API_KEY or '',
        timeout=float(settings.PASARGUARD_API_TIMEOUT_SECONDS) if timeout is None else timeout,
        get_attempts=get_attempts,
    )


def _desired_hash(desired: DesiredState) -> str:
    """Хеш желаемого состояния — только аудит, никогда не гейт записи."""
    payload = '\x1f'.join(
        (
            desired.username,
            'enabled' if desired.enabled else 'disabled',
            desired.expire.isoformat() if desired.expire else '',
            '' if desired.data_limit is None else str(desired.data_limit),
            ','.join(str(group_id) for group_id in desired.group_ids),
            desired.note or '',
            desired.reason,
        )
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs((_as_utc(left) - _as_utc(right)).total_seconds()) < _EXPIRE_TOLERANCE_SECONDS


def _data_limit_close(actual: int | None, desired: int | None) -> bool:
    if desired is None:
        return actual is None
    if actual is None:
        return False
    tolerance = max(_DATA_LIMIT_TOLERANCE_MIN_BYTES, int(desired * 0.01))
    return abs(int(actual) - int(desired)) <= tolerance


async def _revoke_identity(db: AsyncSession, api: PasarguardAPI, subscription: Subscription, now: datetime) -> bool:
    """Ротировать креды панели; при отказе оставить durable-интент."""
    user_id = subscription.pasarguard_user_id
    if user_id is None:
        return False
    try:
        await api.revoke_sub(user_id)
    except PasarguardNotFoundError:
        # Пользователя уже нет — ротировать нечего, личность устарела.
        await clear_pasarguard_identity(db, subscription)
        logger.warning(
            'Пользователь PasarGuard исчез до revoke, личность стёрта',
            subscription_id=subscription.id,
        )
        return True
    except PasarguardAPIError as error:
        subscription.pasarguard_revoke_pending_at = now
        await db.flush()
        logger.warning(
            'PasarGuard revoke не удался, интент сохранён',
            subscription_id=subscription.id,
            error=str(error)[:200],
        )
        return False
    subscription.pasarguard_last_revoke_at = now
    subscription.pasarguard_revoke_pending_at = None
    await db.flush()
    return True


async def revoke_pasarguard_subscription(subscription: Subscription, db: AsyncSession) -> bool:
    """Синхронный revoke на пути отзыва подписки.

    Панель может быть недоступна или настроена криво: тогда фиксируем pending
    и возвращаем False, но никогда не бросаем — Remnawave-revoke уже
    закоммичен вызывающим, и потерять из-за этого новую ссылку нельзя.
    """
    now = datetime.now(UTC)
    api: PasarguardAPI | None = None
    try:
        if getattr(subscription, 'pasarguard_user_id', None) is None:
            return False
        # Пользователь ждёт новую ссылку: зависшая панель не должна задерживать
        # ответ на полный настроенный таймаут. Запись (revoke — POST) не
        # повторяется, так что одного короткого запроса достаточно.
        api = _make_api(timeout=min(float(settings.PASARGUARD_API_TIMEOUT_SECONDS), 5.0))
        if api is None:
            subscription.pasarguard_revoke_pending_at = now
            await db.flush()
            return False
        return await _revoke_identity(db, api, subscription, now)
    except Exception as error:
        logger.warning(
            'PasarGuard revoke упал, интент сохранён',
            subscription_id=subscription.id,
            error=str(error)[:200],
        )
        try:
            subscription.pasarguard_revoke_pending_at = now
            await db.flush()
        except Exception:
            # Сломанная сессия не должна уронить Remnawave-revoke вызывающего.
            logger.warning('Не удалось сохранить интент revoke PasarGuard', subscription_id=subscription.id)
        return False
    finally:
        if api is not None:
            try:
                await api.close()
            except Exception:
                logger.warning('Не удалось закрыть клиент PasarGuard', exc_info=True)


async def enqueue_pasarguard_delete(db: AsyncSession, subscription: Subscription) -> None:
    """Записать намерение удалить пользователя панели до hard-delete подписки."""
    pasarguard_user_id = getattr(subscription, 'pasarguard_user_id', None)
    pasarguard_username = getattr(subscription, 'pasarguard_username', None)
    if pasarguard_user_id is None and not pasarguard_username:
        return
    await add_delete_intent(
        db,
        subscription_id=subscription.id,
        user_id=pasarguard_user_id,
        username=pasarguard_username,
    )


class PasarguardSyncService:
    """Периодический проектор состояния бота в PasarGuard."""

    def __init__(self) -> None:
        self._running = False
        self._pass_running = False
        self._nudge_event = asyncio.Event()
        # Ошибки отдельных элементов прохода: методы обслуживания возвращают
        # только счётчики, а ``errors`` собирается здесь.
        self._pass_errors = 0
        self._last_orphan_cleanup_at: float | None = None

    # ------------------------------------------------------------- интерфейс

    def is_enabled(self) -> bool:
        return settings.is_pasarguard_configured()

    def nudge(self, subscription_id: int | None = None) -> None:
        """Разбудить цикл; проход общий, поэтому id — подсказка, не адрес."""
        self._nudge_event.set()

    async def start_monitoring(self) -> None:
        if not self.is_enabled():
            logger.info('PasarGuard не настроен — проектор xdrive не запускается')
            return
        self._running = True
        interval = settings.get_xdrive_sync_interval_seconds()
        logger.info('Запуск проектора PasarGuard', interval_seconds=interval)
        while self._running:
            try:
                stats = await self.process_once()
                if any(stats.values()):
                    logger.info('Проход проектора PasarGuard', **stats)
            except Exception as error:
                logger.error('Ошибка в цикле проектора PasarGuard', error=str(error)[:200], exc_info=True)
            try:
                await asyncio.wait_for(self._nudge_event.wait(), timeout=interval)
                self._nudge_event.clear()
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    def stop(self) -> None:
        self._running = False
        # Разбудить цикл, если он ждёт nudge/интервал.
        self._nudge_event.set()

    # --------------------------------------------------------------- главное

    async def process_once(self) -> dict[str, int]:
        """Один проход: очередь, revoke, сироты, сверка и бэкфилл."""
        stats = _empty_stats()
        if not self.is_enabled():
            logger.debug('PasarGuard не настроен, проход проектора пропущен')
            return stats
        if self._pass_running:
            logger.warning('Проход проектора PasarGuard уже идёт, новый пропущен')
            return stats

        self._pass_running = True
        self._pass_errors = 0
        api: PasarguardAPI | None = None
        try:
            api = _make_api()
            if api is None:
                return stats
            async with AsyncSessionLocal() as db:
                try:
                    panel_users = await self._load_panel_users(api)
                    stats['deleted'] += await self._drain_delete_intents(db, api)
                    stats['updated'] += await self._retry_pending_revokes(db, api)
                    if self._orphan_cleanup_due():
                        stats['deleted'] += await self._cleanup_orphans(db, api, panel_users)
                        self._last_orphan_cleanup_at = asyncio.get_running_loop().time()
                    reconcile_stats = await self._reconcile(db, api, panel_users)
                    for key, value in reconcile_stats.items():
                        stats[key] += value
                    stats['created'] += await self._backfill(db, api, panel_users)
                    stats['errors'] += self._pass_errors
                    await db.commit()
                except Exception as error:
                    await db.rollback()
                    stats['errors'] += self._pass_errors + 1
                    logger.error('Проход проектора PasarGuard прерван', error=str(error)[:200], exc_info=True)
        except Exception as error:
            stats['errors'] += 1
            logger.error('Проектор PasarGuard упал', error=str(error)[:200], exc_info=True)
        finally:
            self._pass_running = False
            if api is not None:
                try:
                    await api.close()
                except Exception:
                    logger.warning('Не удалось закрыть клиент PasarGuard', exc_info=True)
        return stats

    # ------------------------------------------------------------- панель

    async def _load_panel_users(self, api: PasarguardAPI) -> list[PasarguardUser]:
        """Все пользователи панели разом: один источник для сверки и сирот."""
        limit = self._batch_size()
        users: list[PasarguardUser] = []
        offset = 0
        while True:
            page = await api.list_users(offset=offset, limit=limit)
            users.extend(page)
            if len(page) < limit:
                break
            offset += len(page)
        return users

    # ------------------------------------------------------------ сверка

    async def _reconcile(self, db: AsyncSession, api: PasarguardAPI, users: list[PasarguardUser]) -> dict[str, int]:
        """Сверить подписки с известной личностью: обновить или погасить."""
        stats = _empty_stats()
        now = datetime.now(UTC)
        grace_ids = await get_open_grace_subscription_ids(db)
        panel_by_id = {user.id: user for user in users}
        panel_by_username = {user.username: user for user in users}
        batch_size = self._batch_size()
        last_id = 0
        while True:
            result = await db.execute(
                select(Subscription, User.status, Tariff.server_traffic_limits)
                .join(User, User.id == Subscription.user_id)
                .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
                .where(
                    Subscription.id > last_id,
                    or_(
                        Subscription.pasarguard_user_id.is_not(None),
                        Subscription.pasarguard_username.is_not(None),
                    ),
                )
                .order_by(Subscription.id)
                .limit(batch_size)
            )
            rows = result.all()
            if not rows:
                break
            last_id = rows[-1][0].id
            states_by_subscription = await self._load_premium_states(db, [row[0].id for row in rows])
            for subscription, user_status, server_traffic_limits in rows:
                stats['checked'] += 1
                existing = self._resolve_panel_user(subscription, panel_by_id, panel_by_username)
                try:
                    action = await self._apply_desired(
                        db,
                        api,
                        subscription,
                        existing,
                        user_status=user_status,
                        grace_open=subscription.id in grace_ids,
                        premium_states=states_by_subscription.get(subscription.id, {}),
                        server_traffic_limits=server_traffic_limits,
                        panel_by_id=panel_by_id,
                        panel_by_username=panel_by_username,
                        now=now,
                    )
                except Exception as error:
                    stats['errors'] += 1
                    logger.error(
                        'Не удалось применить состояние PasarGuard',
                        subscription_id=subscription.id,
                        error=str(error)[:200],
                    )
                    continue
                if action is not None:
                    stats[action] += 1
            if len(rows) < batch_size:
                break
        return stats

    @staticmethod
    def _resolve_panel_user(
        subscription: Subscription,
        panel_by_id: dict[int, PasarguardUser],
        panel_by_username: dict[str, PasarguardUser],
    ) -> PasarguardUser | None:
        if subscription.pasarguard_user_id is not None:
            user = panel_by_id.get(subscription.pasarguard_user_id)
            if user is not None:
                return user
        if subscription.pasarguard_username:
            return panel_by_username.get(subscription.pasarguard_username)
        return None

    @staticmethod
    async def _load_premium_states(
        db: AsyncSession,
        subscription_ids: list[int],
    ) -> dict[int, dict[str, SubscriptionPremiumTraffic]]:
        if not subscription_ids:
            return {}
        result = await db.execute(
            select(SubscriptionPremiumTraffic).where(SubscriptionPremiumTraffic.subscription_id.in_(subscription_ids))
        )
        states: dict[int, dict[str, SubscriptionPremiumTraffic]] = {}
        for state in result.scalars().all():
            states.setdefault(state.subscription_id, {})[state.squad_uuid] = state
        return states

    @staticmethod
    def _premium_view(
        server_traffic_limits: dict | None,
        premium_states: dict[str, SubscriptionPremiumTraffic],
        connected_squads: list[str] | None,
    ) -> tuple[PremiumStateView | None, int]:
        squads = get_premium_squads_for_tariff(SimpleNamespace(server_traffic_limits=server_traffic_limits))
        squad_count = len(squads)
        if squad_count != 1:
            return None, squad_count
        squad_uuid = next(iter(squads))
        state = premium_states.get(squad_uuid)
        if state is None:
            return None, squad_count
        return (
            PremiumStateView(
                is_limited=bool(state.is_limited),
                is_exhausted=bool(state.is_exhausted),
                closed_at=state.closed_at,
                total_limit_bytes=int(state.total_limit_bytes),
                used_bytes=int(state.used_bytes or 0),
                squad_connected=squad_uuid in set(connected_squads or ()),
            ),
            squad_count,
        )

    async def _apply_desired(
        self,
        db: AsyncSession,
        api: PasarguardAPI,
        subscription: Subscription,
        existing: PasarguardUser | None,
        *,
        user_status: str,
        grace_open: bool,
        premium_states: dict[str, SubscriptionPremiumTraffic],
        server_traffic_limits: dict | None,
        panel_by_id: dict[int, PasarguardUser],
        panel_by_username: dict[str, PasarguardUser],
        now: datetime,
    ) -> str | None:
        if existing is None:
            # Подписка без личности, но с панельным пользователем того же имени:
            # это след оборванного создания — усыновляем, а не создаём дубль.
            candidate = subscription.pasarguard_username or f'{self._prefix()}{subscription.id}'
            existing = panel_by_username.get(candidate)

        premium_state, squad_count = self._premium_view(
            server_traffic_limits,
            premium_states,
            subscription.connected_squads,
        )
        subject = SyncSubject(
            subscription_id=subscription.id,
            subscription_status=subscription.status,
            end_date=subscription.end_date,
            user_status=user_status,
            premium_state=premium_state,
            premium_squad_count=squad_count,
            grace_open=grace_open,
            pasarguard_user=existing,
        )
        desired = evaluate_desired_state(
            subject,
            enabled=settings.is_xdrive_enabled(),
            group_id=self._group_id(),
            prefix=self._prefix(),
            instance_id=self._instance_id(),
            now=now,
        )

        if not desired.enabled:
            return await self._apply_disabled(
                db, api, subscription, existing, desired, panel_by_id, panel_by_username, now
            )

        if existing is None:
            return await self._create_user(db, api, subscription, desired, panel_by_id, panel_by_username, now)

        if subscription.pasarguard_user_id != existing.id:
            await self._persist_identity(db, subscription, existing, now)
        return await self._apply_enabled(
            db,
            api,
            subscription,
            existing,
            desired,
            panel_by_id,
            panel_by_username,
            now,
        )

    async def _create_user(
        self,
        db: AsyncSession,
        api: PasarguardAPI,
        subscription: Subscription,
        desired: DesiredState,
        panel_by_id: dict[int, PasarguardUser],
        panel_by_username: dict[str, PasarguardUser],
        now: datetime,
    ) -> str:
        if desired.expire is None or desired.data_limit is None:  # pragma: no cover - гарантировано evaluator-ом
            raise RuntimeError('enabled DesiredState без expire/data_limit')
        try:
            created = await api.create_user(
                username=desired.username,
                group_ids=list(desired.group_ids),
                expire=desired.expire,
                data_limit=desired.data_limit,
                status='active',
                note=desired.note,
            )
        except PasarguardAPIError as error:
            if error.status_code != 409:
                raise
            # Гонка двух создателей: победил другой проход/инстанс — читаем его.
            created = await api.get_user_by_username(desired.username)
            self._remember_user(created, panel_by_id, panel_by_username)
            await self._persist_identity(db, subscription, created, now)
            return await self._apply_enabled(
                db,
                api,
                subscription,
                created,
                desired,
                panel_by_id,
                panel_by_username,
                now,
            )
        self._remember_user(created, panel_by_id, panel_by_username)
        await self._persist_identity(db, subscription, created, now)
        await self._mark_synced(db, subscription, desired, now)
        return 'created'

    async def _apply_enabled(
        self,
        db: AsyncSession,
        api: PasarguardAPI,
        subscription: Subscription,
        existing: PasarguardUser,
        desired: DesiredState,
        panel_by_id: dict[int, PasarguardUser],
        panel_by_username: dict[str, PasarguardUser],
        now: datetime,
    ) -> str | None:
        if existing.status != 'active':
            # Сначала expire/data_limit (статус limited/expired снимаем явно),
            # только потом cleared disabled — иначе панель оставит старое.
            fields = self._enable_fields(existing, desired)
            updated = await api.update_user(existing.id, **fields)
            self._remember_user(updated, panel_by_id, panel_by_username)
            activated = await api.set_disabled(existing.id, False)
            self._remember_user(activated, panel_by_id, panel_by_username)
            await self._mark_synced(db, subscription, desired, now)
            return 'enabled'

        fields = self._drift_fields(existing, desired)
        if not fields:
            return None
        updated = await api.update_user(existing.id, **fields)
        self._remember_user(updated, panel_by_id, panel_by_username)
        await self._mark_synced(db, subscription, desired, now)
        return 'updated'

    async def _apply_disabled(
        self,
        db: AsyncSession,
        api: PasarguardAPI,
        subscription: Subscription,
        existing: PasarguardUser | None,
        desired: DesiredState,
        panel_by_id: dict[int, PasarguardUser],
        panel_by_username: dict[str, PasarguardUser],
        now: datetime,
    ) -> str | None:
        if existing is None or existing.status == 'disabled':
            return None
        updated = await api.set_disabled(existing.id, True)
        self._remember_user(updated, panel_by_id, panel_by_username)
        await self._mark_synced(db, subscription, desired, now)
        return 'disabled'

    @staticmethod
    def _enable_fields(existing: PasarguardUser, desired: DesiredState) -> dict[str, object]:
        fields: dict[str, object] = {
            'expire': desired.expire,
            'data_limit': desired.data_limit,
            'group_ids': list(desired.group_ids),
            'note': desired.note,
        }
        if existing.status in _PANEL_STATUSES_NEEDING_ACTIVE:
            fields['status'] = 'active'
        if desired.data_limit is not None:
            fields['data_limit_reset_strategy'] = 'no_reset'
        return fields

    @staticmethod
    def _drift_fields(existing: PasarguardUser, desired: DesiredState) -> dict[str, object]:
        fields: dict[str, object] = {}
        if not _same_instant(existing.expire, desired.expire):
            fields['expire'] = desired.expire
        if not _data_limit_close(existing.data_limit, desired.data_limit):
            fields['data_limit'] = desired.data_limit
        if set(existing.group_ids or ()) != set(desired.group_ids):
            fields['group_ids'] = list(desired.group_ids)
        if existing.note != desired.note:
            fields['note'] = desired.note
        if 'data_limit' in fields:
            fields['data_limit_reset_strategy'] = 'no_reset'
        return fields

    @staticmethod
    def _remember_user(
        user: PasarguardUser,
        panel_by_id: dict[int, PasarguardUser],
        panel_by_username: dict[str, PasarguardUser],
    ) -> None:
        panel_by_id[user.id] = user
        panel_by_username[user.username] = user

    async def _persist_identity(
        self,
        db: AsyncSession,
        subscription: Subscription,
        user: PasarguardUser,
        now: datetime,
    ) -> None:
        if subscription.pasarguard_user_id == user.id and subscription.pasarguard_username:
            return
        async with db.begin_nested():
            await set_pasarguard_identity(
                db,
                subscription,
                user_id=user.id,
                username=user.username,
                created_at=now,
            )

    async def _mark_synced(
        self,
        db: AsyncSession,
        subscription: Subscription,
        desired: DesiredState,
        now: datetime,
    ) -> None:
        subscription.pasarguard_synced_at = now
        subscription.pasarguard_synced_hash = _desired_hash(desired)
        await db.flush()

    # ------------------------------------------------------------ бэкфилл

    async def _backfill(self, db: AsyncSession, api: PasarguardAPI, users: list[PasarguardUser]) -> int:
        """Создать личности eligible-подпискам без неё (пачками, resumable)."""
        created = 0
        now = datetime.now(UTC)
        grace_ids = await get_open_grace_subscription_ids(db)
        panel_by_id = {user.id: user for user in users}
        panel_by_username = {user.username: user for user in users}
        batch_size = self._batch_size()
        last_id = 0
        while True:
            result = await db.execute(
                select(Subscription, User.status, Tariff.server_traffic_limits)
                .join(User, User.id == Subscription.user_id)
                .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
                .where(
                    Subscription.id > last_id,
                    Subscription.pasarguard_user_id.is_(None),
                    Subscription.pasarguard_username.is_(None),
                    Subscription.status.in_(ALLOWED_SUBSCRIPTION_STATUSES),
                    Subscription.end_date > now,
                    User.status == UserStatus.ACTIVE.value,
                )
                .order_by(Subscription.id)
                .limit(batch_size)
            )
            rows = result.all()
            if not rows:
                break
            last_id = rows[-1][0].id
            states_by_subscription = await self._load_premium_states(db, [row[0].id for row in rows])
            for subscription, user_status, server_traffic_limits in rows:
                if subscription.id in grace_ids:
                    continue
                try:
                    action = await self._apply_desired(
                        db,
                        api,
                        subscription,
                        None,
                        user_status=user_status,
                        grace_open=False,
                        premium_states=states_by_subscription.get(subscription.id, {}),
                        server_traffic_limits=server_traffic_limits,
                        panel_by_id=panel_by_id,
                        panel_by_username=panel_by_username,
                        now=now,
                    )
                except Exception as error:
                    self._pass_errors += 1
                    logger.error(
                        'Бэкфилл PasarGuard не удался',
                        subscription_id=subscription.id,
                        error=str(error)[:200],
                    )
                    continue
                if action == 'created':
                    created += 1
            if len(rows) < batch_size:
                break
            await asyncio.sleep(_BACKFILL_BATCH_PAUSE_SECONDS)
        return created

    # ------------------------------------------------------- обслуживание

    def _orphan_cleanup_due(self) -> bool:
        if self._last_orphan_cleanup_at is None:
            return True
        elapsed = asyncio.get_running_loop().time() - self._last_orphan_cleanup_at
        return elapsed >= _ORPHAN_CLEANUP_INTERVAL_SECONDS

    def _owned_panel_users(self, users: list[PasarguardUser]) -> dict[int, PasarguardUser]:
        """Пользователи бота — только при совпадении префикса И note-маркера."""
        pattern = re.compile(rf'^{re.escape(self._prefix())}(\d+)$')
        marker = f'xdrive-bot:{self._instance_id()}:sub:'
        owned: dict[int, PasarguardUser] = {}
        for user in users:
            match = pattern.match(user.username or '')
            if match is None:
                continue
            subscription_id = int(match.group(1))
            if user.note != f'{marker}{subscription_id}':
                continue
            owned[subscription_id] = user
        return owned

    async def _cleanup_orphans(self, db: AsyncSession, api: PasarguardAPI, users: list[PasarguardUser]) -> int:
        """Удалить пользователей бота, чья подписка уже удалена."""
        owned = self._owned_panel_users(users)
        if not owned:
            return 0
        alive: set[int] = set()
        subscription_ids = list(owned)
        batch_size = self._batch_size()
        for start in range(0, len(subscription_ids), batch_size):
            chunk = subscription_ids[start : start + batch_size]
            result = await db.execute(select(Subscription.id).where(Subscription.id.in_(chunk)))
            alive.update(result.scalars().all())
        deleted = 0
        for subscription_id, user in owned.items():
            if subscription_id in alive:
                continue
            try:
                await api.delete_user(user.id)
            except PasarguardNotFoundError:
                pass
            except Exception as error:
                self._pass_errors += 1
                logger.warning(
                    'Не удалось удалить сироту PasarGuard',
                    pasarguard_user_id=user.id,
                    error=str(error)[:200],
                )
                continue
            deleted += 1
            logger.info(
                'Удалён сирота PasarGuard',
                pasarguard_user_id=user.id,
                subscription_id=subscription_id,
            )
        return deleted

    async def _drain_delete_intents(self, db: AsyncSession, api: PasarguardAPI) -> int:
        """Разобрать очередь удалений; недоступная панель оставляет интент."""
        intents = await list_delete_intents(db, limit=self._batch_size())
        drained = 0
        for intent in intents:
            try:
                await self._delete_panel_user(api, intent.pasarguard_user_id, intent.pasarguard_username)
            except PasarguardNotFoundError:
                pass
            except Exception as error:
                self._pass_errors += 1
                logger.warning(
                    'Не удалось разобрать интент удаления PasarGuard',
                    subscription_id=intent.subscription_id,
                    error=str(error)[:200],
                )
                continue
            await delete_intent(db, intent)
            drained += 1
        return drained

    @staticmethod
    async def _delete_panel_user(api: PasarguardAPI, user_id: int | None, username: str | None) -> None:
        if user_id is not None:
            try:
                await api.delete_user(user_id)
            except PasarguardNotFoundError:
                return
            return
        if username:
            user = await api.get_user_by_username(username)
            await api.delete_user(user.id)

    async def _retry_pending_revokes(self, db: AsyncSession, api: PasarguardAPI) -> int:
        """Повторить revoke, отложенный синхронным путём при недоступной панели."""
        now = datetime.now(UTC)
        result = await db.execute(
            select(Subscription)
            .where(
                Subscription.pasarguard_revoke_pending_at.is_not(None),
                Subscription.pasarguard_user_id.is_not(None),
            )
            .order_by(Subscription.id)
            .limit(self._batch_size())
        )
        revoked = 0
        for subscription in result.scalars().all():
            try:
                resolved = await _revoke_identity(db, api, subscription, now)
            except Exception as error:
                self._pass_errors += 1
                logger.warning(
                    'Не удалось повторить revoke PasarGuard',
                    subscription_id=subscription.id,
                    error=str(error)[:200],
                )
                continue
            if resolved:
                revoked += 1
        return revoked

    # -------------------------------------------------------- настройки

    @staticmethod
    def _batch_size() -> int:
        try:
            return max(1, int(settings.PASARGUARD_SYNC_BATCH_SIZE))
        except (TypeError, ValueError):
            return 200

    @staticmethod
    def _group_id() -> int:
        group_id = settings.PASARGUARD_XDRIVE_GROUP_ID
        if group_id is None:  # pragma: no cover - is_pasarguard_configured() уже проверил
            raise RuntimeError('PASARGUARD_XDRIVE_GROUP_ID не задан')
        return int(group_id)

    @staticmethod
    def _prefix() -> str:
        return (settings.PASARGUARD_USERNAME_PREFIX or 'bx').strip() or 'bx'

    @staticmethod
    def _instance_id() -> str:
        return settings.get_pasarguard_instance_id()


pasarguard_sync_service = PasarguardSyncService()


def nudge_pasarguard_projector(subscription_id: int | None = None) -> None:
    """Разбудить проектор из чужого горячего пути, никогда не роняя его.

    Nudge — подсказка о задержке, а не часть корректности: плановый проход всё
    равно сойдётся. Поэтому сбой импорта/вызова глушится с предупреждением.
    """
    try:
        pasarguard_sync_service.nudge(subscription_id)
    except Exception:
        logger.warning('Не удалось разбудить проектор PasarGuard', subscription_id=subscription_id)
