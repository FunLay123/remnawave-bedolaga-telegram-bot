"""Короткоживущий снимок пользователя панели для экрана подписки.

Экран подписки только читает данные, но открывается часто, и каждое открытие
ходило в панель за одним и тем же пользователем дважды: ``sync_subscription_usage``
и ``ensure_subscription_synced`` делали по GET ``/api/users/{id}``. Снимок на
30 секунд убирает повторные запросы при переоткрытии экрана.

Любая запись в панель (не-GET в ``RemnaWaveAPI._make_request``) сбрасывает кэш
целиком: после продления, сброса трафика, изменения устройств или удаления
аккаунта старый снимок не должен дожить до следующего открытия экрана.

Кэш используется только для read-only экрана подписки и никогда — для решений
о начислениях и доступе.
"""

import time
from typing import Any


PANEL_SNAPSHOT_TTL_SECONDS = 30
MAX_ENTRIES = 512

# panel_user_id -> (fetched_at_monotonic, panel_user, devices_info)
_cache: dict[int, tuple[float, Any, Any]] = {}


def get(panel_user_id: int) -> tuple[Any, Any] | None:
    """Вернуть ``(user, devices)``, если снимок свежий; просроченный выбросить."""
    entry = _cache.get(panel_user_id)
    if entry is None:
        return None

    fetched_at, user, devices = entry
    if time.monotonic() - fetched_at > PANEL_SNAPSHOT_TTL_SECONDS:
        _cache.pop(panel_user_id, None)
        return None

    return user, devices


def put(panel_user_id: int, user: Any, devices: Any) -> None:
    """Положить снимок, сначала вычистив просроченные записи.

    Если после чистки кэш всё ещё полон, выбрасывается самая старая вставка.
    Повторный ``put`` двигает запись в конец очереди: «самый старый» — это
    «дольше всего не обновлялся», а не «впервые положен».
    """
    now = time.monotonic()
    _prune_expired(now)

    _cache.pop(panel_user_id, None)
    if len(_cache) >= MAX_ENTRIES:
        oldest_key = next(iter(_cache), None)
        if oldest_key is not None:
            _cache.pop(oldest_key, None)

    _cache[panel_user_id] = (now, user, devices)


def invalidate_all() -> None:
    """Сбросить весь кэш — вызывается перед любой записью в панель."""
    _cache.clear()


def invalidate(panel_user_id: int) -> None:
    """Сбросить снимок одного пользователя."""
    _cache.pop(panel_user_id, None)


def _prune_expired(now: float) -> None:
    expired = [key for key, (fetched_at, _, _) in _cache.items() if now - fetched_at > PANEL_SNAPSHOT_TTL_SECONDS]
    for key in expired:
        _cache.pop(key, None)
