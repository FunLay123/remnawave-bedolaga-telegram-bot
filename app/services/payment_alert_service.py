"""Admin alerts about stalled payments and provider verification failures.

The module is intentionally independent from the general admin-notification
master switch: payment alerts are delivered through
``AdminNotificationService.send_payment_alert`` which bypasses
``ADMIN_NOTIFICATIONS_ENABLED`` while still requiring a configured admin chat
and the dedicated ``PAYMENT_ALERTS_ENABLED`` switch.

Repeated alerts are rate limited per ``kind/provider/local_id`` for
``PAYMENT_ALERT_REPEAT_HOURS`` hours. Redis ``SET NX`` is used when the cache
is connected; otherwise a single-process, in-memory timestamp map provides the
same behavior.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.config import settings
from app.database.models import PaymentMethod
from app.services.admin_notification_service import AdminNotificationService
from app.services.payment_verification_service import PendingPayment, method_display_name
from app.utils.cache import cache


logger = structlog.get_logger(__name__)

PENDING_KIND = 'pending'
CHECK_ERROR_KIND = 'check_error'

DEFAULT_PENDING_MINUTES = 30
DEFAULT_REPEAT_HOURS = 6
DEFAULT_MAX_PER_CYCLE = 20

# Single-process fallback when Redis is unavailable. Maps a cooldown key to the
# monotonic time at which the claim expires.
_MEMORY_CLAIMS: dict[str, float] = {}


@dataclass(slots=True, frozen=True)
class PaymentAlert:
    """Sanitized description of a payment worth telling the admins about."""

    kind: str
    method: PaymentMethod
    local_id: int
    identifier: str
    amount_kopeks: int
    status: str
    age_minutes: int


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _age_minutes(record: PendingPayment, now: datetime) -> int:
    delta = _as_aware(now) - _as_aware(record.created_at)
    return max(0, int(delta.total_seconds() // 60))


def _pending_threshold_minutes() -> int:
    try:
        return max(0, int(settings.PAYMENT_ALERT_PENDING_MINUTES))
    except (TypeError, ValueError):  # pragma: no cover - protective config guard
        return DEFAULT_PENDING_MINUTES


def _repeat_seconds() -> int:
    try:
        hours = int(settings.PAYMENT_ALERT_REPEAT_HOURS)
    except (TypeError, ValueError):  # pragma: no cover - protective config guard
        hours = DEFAULT_REPEAT_HOURS
    return max(1, hours) * 3600


def _max_per_cycle() -> int:
    try:
        limit = int(settings.PAYMENT_ALERT_MAX_PER_CYCLE)
    except (TypeError, ValueError):  # pragma: no cover - protective config guard
        limit = DEFAULT_MAX_PER_CYCLE
    return max(1, limit)


def _build_alert(record: PendingPayment, now: datetime, kind: str) -> PaymentAlert:
    return PaymentAlert(
        kind=kind,
        method=record.method,
        local_id=record.local_id,
        identifier=record.identifier,
        amount_kopeks=record.amount_kopeks,
        status=record.status,
        age_minutes=_age_minutes(record, now),
    )


def build_pending_alert(record: PendingPayment, now: datetime) -> PaymentAlert | None:
    """Return an alert for a payment pending at least the configured age."""

    age_minutes = _age_minutes(record, now)
    if age_minutes < _pending_threshold_minutes():
        return None
    return _build_alert(record, now, PENDING_KIND)


def build_check_error_alert(record: PendingPayment, now: datetime) -> PaymentAlert:
    """Return an alert for a payment whose provider check failed.

    Only data from the payment record is used; the exception object never
    reaches this function, so its payload cannot leak into the digest.
    """

    return _build_alert(record, now, CHECK_ERROR_KIND)


def _cooldown_key(alert: PaymentAlert) -> str:
    return f'payment_alert:{alert.kind}:{alert.method.value}:{alert.local_id}'


def _claim_memory(key: str, ttl_seconds: int, now_monotonic: float) -> bool:
    expires_at = _MEMORY_CLAIMS.get(key)
    if expires_at is not None and expires_at > now_monotonic:
        return False

    _MEMORY_CLAIMS[key] = now_monotonic + ttl_seconds

    # Opportunistic cleanup so the map does not grow forever in long-running
    # processes without Redis.
    if len(_MEMORY_CLAIMS) > 1024:
        for stale_key, stale_expiry in list(_MEMORY_CLAIMS.items()):
            if stale_expiry <= now_monotonic:
                _MEMORY_CLAIMS.pop(stale_key, None)

    return True


async def _try_claim(key: str, ttl_seconds: int, *, use_redis: bool) -> bool:
    if use_redis:
        return await cache.setnx(key, '1', expire=ttl_seconds)
    return _claim_memory(key, ttl_seconds, time.monotonic())


async def _release_claim(key: str, *, use_redis: bool) -> None:
    if use_redis:
        await cache.delete(key)
    else:
        _MEMORY_CLAIMS.pop(key, None)


def _format_alert_line(alert: PaymentAlert) -> str:
    if alert.kind == CHECK_ERROR_KIND:
        title = 'Ошибка проверки'
    else:
        title = 'Долго ожидает'

    method = html.escape(method_display_name(alert.method))
    identifier = html.escape(str(alert.identifier or '—'))
    status = html.escape(str(alert.status or '—'))
    amount = settings.format_price(alert.amount_kopeks)

    return (
        f'• <b>{title}</b>: {method} · <code>{identifier}</code> · '
        f'{amount} · {alert.age_minutes} мин · статус: {status}'
    )


def format_payment_alerts_digest(alerts: list[PaymentAlert]) -> str:
    """Build the admin digest, capped at the configured number of item lines."""

    limit = _max_per_cycle()
    displayed = alerts[:limit]
    overflow = len(alerts) - len(displayed)

    lines = ['<b>⚠️ Проблемы с платежами</b>', '']
    lines.extend(_format_alert_line(alert) for alert in displayed)
    if overflow > 0:
        lines.append(f'… и ещё {overflow}')
    return '\n'.join(lines)


async def dispatch_payment_alerts(bot: object | None, alerts: list[PaymentAlert]) -> bool:
    """Claim cooldowns, format a digest and send it to the admin chat.

    Returns ``True`` only when the digest was delivered. Claims are released
    when delivery fails, so the next verification cycle can retry.
    """

    if bot is None or not alerts:
        return False

    repeat_seconds = _repeat_seconds()
    use_redis = bool(getattr(cache, '_connected', False))

    unique_alerts: list[PaymentAlert] = []
    claimed_keys: list[str] = []
    seen_keys: set[str] = set()

    for alert in alerts:
        key = _cooldown_key(alert)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if await _try_claim(key, repeat_seconds, use_redis=use_redis):
            claimed_keys.append(key)
            unique_alerts.append(alert)

    if not unique_alerts:
        return False

    text = format_payment_alerts_digest(unique_alerts)
    try:
        service = AdminNotificationService(bot)
        delivered = await service.send_payment_alert(text)
    except Exception as send_error:  # pragma: no cover - defensive
        logger.warning('Ошибка отправки алерта о проблемных платежах', error=send_error)
        delivered = False

    if not delivered:
        for key in claimed_keys:
            await _release_claim(key, use_redis=use_redis)
        logger.warning(
            'Не удалось отправить алерт о проблемных платежах, повторим в следующем цикле',
            alerts_count=len(unique_alerts),
        )
        return False

    logger.info('Алерт о проблемных платежах отправлен администраторам', alerts_count=len(unique_alerts))
    return True
