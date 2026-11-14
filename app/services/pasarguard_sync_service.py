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

from dataclasses import dataclass
from datetime import UTC, datetime

from app.database.models import SubscriptionStatus, UserStatus
from app.external.pasarguard_api import PasarguardUser


ALLOWED_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
    }
)


@dataclass(frozen=True)
class PremiumStateView:
    """Снимок premium-пула одной подписки без ORM-зависимости."""

    is_limited: bool
    is_exhausted: bool
    closed_at: datetime | None
    total_limit_bytes: int
    used_bytes: int


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
    """Есть ровно один positive premium-сквад, и его пул не закрыт.

    ``squad_count`` — число premium-сквадов тарифа (с положительным лимитом).
    Больше одного — v1 выключает xdrive: непонятно, к какому пулу относить
    расход. Нулевой пул тоже не годится: ``data_limit`` по нему вышел бы нулём,
    а ноль в PasarGuard означает «без лимита».
    """
    if state is None or squad_count != 1:
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
