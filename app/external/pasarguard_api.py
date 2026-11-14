"""Тонкий async-клиент PasarGuard API.

Один метод на эндпоинт, без бизнес-логики: PasarGuard — раб, все решения
принимает проектор в боте. Панель авторизует по ``X-Api-Key``.

Маршруты PasarGuard (сверено с ``panel/app/routers/user.py``):

* ``POST /api/user`` — создание;
* ``GET /api/user/by-id/{id}`` / ``GET /api/user/by-username/{username}``;
* ``GET /api/users?offset=&limit=`` — список ``{"users": [...], "total": N}``,
  оба счётчика трафика приходят сразу (``used_traffic`` и
  ``lifetime_used_traffic``);
* ``PUT /api/user/by-id/{id}`` — изменение;
* ``PUT /api/user/by-id/{id}/disabled`` — тело ``{"disabled": bool}``;
* ``POST /api/user/by-id/{id}/revoke_sub`` — ротация VLESS UUID и токена;
* ``DELETE /api/user/by-id/{id}`` — 204 без тела;
* ``GET /api/user/{id}/subscription/{client_type}`` — конфиг через админский
  API, без токена подписки.

Ошибки: 404 → :class:`PasarguardNotFoundError`, 5xx/таймаут/обрыв связи →
:class:`PasarguardTransientError`, прочие 4xx → :class:`PasarguardAPIError`.
Повторяются только идемпотентные GET: запись панель не умеет дедуплицировать
(Idempotency-Key у неё нет), и повторный POST создал бы второго пользователя.

Заголовки, тела запросов/ответов и ключ API в логи не попадают: только
идентификаторы, имена и счётчики.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog


logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0

# GET можно повторять: читать безопасно. Пауза короткая — проектор опрашивает
# панель пачками, а зависший запрос не должен задерживать весь проход.
GET_RETRY_ATTEMPTS = 3
GET_RETRY_BASE_DELAY_SECONDS = 0.2


@dataclass(frozen=True)
class PasarguardUser:
    """Пользователь панели в форме, нужной проектору.

    ``data_limit`` — абсолютный лимит: PasarGuard сравнивает его с накопленным
    ``used_traffic``, а не с расходом за период. ``0`` означает «без лимита»,
    поэтому ноль сохраняется как есть, а не превращается в ``None``.
    """

    id: int
    username: str
    status: str
    expire: datetime | None
    data_limit: int | None
    used_traffic: int
    lifetime_used_traffic: int
    group_ids: list[int]
    note: str | None


class PasarguardAPIError(Exception):
    """Отказ панели или негодный ответ."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PasarguardTransientError(PasarguardAPIError):
    """Временный сбой: таймаут, обрыв связи или 5xx после повторов GET."""


class PasarguardNotFoundError(PasarguardAPIError):
    """Панель ответила 404 — пользователя (или маршрута) нет."""


def _optional_int(value: Any) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_expire(value: Any) -> datetime | None:
    """``expire`` панели: ISO-строка, unix-время или ``null``/``0`` (бессрочно)."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # 0 у PasarGuard — «без срока»; отрицательное время смысла не имеет.
        return datetime.fromtimestamp(value, tz=UTC) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None


def _parse_user(payload: dict[str, Any]) -> PasarguardUser:
    return PasarguardUser(
        id=int(payload['id']),
        username=str(payload['username']),
        status=str(payload.get('status') or ''),
        expire=_parse_expire(payload.get('expire')),
        data_limit=_optional_int(payload.get('data_limit')),
        used_traffic=_optional_int(payload.get('used_traffic')) or 0,
        lifetime_used_traffic=_optional_int(payload.get('lifetime_used_traffic')) or 0,
        group_ids=[int(group_id) for group_id in (payload.get('group_ids') or [])],
        note=payload.get('note'),
    )


class PasarguardAPI:
    """HTTP-клиент панели. Создаётся один раз на процесс, закрывается ``close()``."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={'X-Api-Key': api_key},
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Выполнить запрос и разложить отказ по типам ошибок.

        GET повторяется при 5xx/таймауте/обрыве связи; запись — ровно один раз.
        """
        attempts = GET_RETRY_ATTEMPTS if method == 'GET' else 1
        for attempt in range(attempts):
            last_attempt = attempt + 1 >= attempts
            try:
                response = await self._client.request(method, path, params=params, json=json_body)
            except httpx.TimeoutException as exc:
                if not last_attempt:
                    await asyncio.sleep(GET_RETRY_BASE_DELAY_SECONDS * (2**attempt))
                    continue
                logger.warning('PasarGuard request timed out', method=method, path=path)
                raise PasarguardTransientError(f'Request timed out: {method} {path}') from exc
            except httpx.TransportError as exc:
                if not last_attempt:
                    await asyncio.sleep(GET_RETRY_BASE_DELAY_SECONDS * (2**attempt))
                    continue
                logger.warning(
                    'PasarGuard request failed (panel unreachable)',
                    method=method,
                    path=path,
                    error=str(exc)[:200],
                )
                raise PasarguardTransientError(f'Request failed: {method} {path}') from exc

            if response.status_code >= 500:
                if not last_attempt:
                    await asyncio.sleep(GET_RETRY_BASE_DELAY_SECONDS * (2**attempt))
                    continue
                logger.warning(
                    'PasarGuard panel error',
                    method=method,
                    path=path,
                    status=response.status_code,
                )
                raise PasarguardTransientError(
                    f'HTTP {response.status_code}: {method} {path}',
                    status_code=response.status_code,
                )
            if response.status_code == 404:
                # Штатный путь create-if-missing: GET по имени сначала получает 404.
                logger.debug('PasarGuard not found', method=method, path=path)
                raise PasarguardNotFoundError(
                    f'Not found: {method} {path}',
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                logger.warning(
                    'PasarGuard API error',
                    method=method,
                    path=path,
                    status=response.status_code,
                )
                raise PasarguardAPIError(
                    f'HTTP {response.status_code}: {method} {path}',
                    status_code=response.status_code,
                )
            return response

        raise AssertionError('unreachable')  # pragma: no cover - цикл всегда возвращает или бросает

    def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise PasarguardAPIError(
                'Invalid JSON in PasarGuard response',
                status_code=response.status_code,
            ) from exc

    async def get_user_by_id(self, user_id: int) -> PasarguardUser:
        response = await self._request('GET', f'/api/user/by-id/{user_id}')
        return _parse_user(self._json(response))

    async def get_user_by_username(self, username: str) -> PasarguardUser:
        response = await self._request('GET', f'/api/user/by-username/{username}')
        return _parse_user(self._json(response))

    async def list_users(self, offset: int = 0, limit: int = 200) -> list[PasarguardUser]:
        """Одна страница списка: оба счётчика трафика приходят без поюзерных запросов."""
        response = await self._request('GET', '/api/users', params={'offset': offset, 'limit': limit})
        payload = self._json(response)
        users = [_parse_user(item) for item in payload.get('users') or []]
        logger.debug('PasarGuard users listed', offset=offset, limit=limit, count=len(users))
        return users

    async def create_user(
        self,
        username: str,
        group_ids: list[int],
        expire: datetime,
        data_limit: int,
        status: str = 'active',
        note: str | None = None,
    ) -> PasarguardUser:
        """Создать пользователя с абсолютным ``data_limit`` и стратегией ``no_reset``."""
        body: dict[str, Any] = {
            'username': username,
            'group_ids': group_ids,
            'expire': expire.isoformat(),
            'data_limit': data_limit,
            'data_limit_reset_strategy': 'no_reset',
            'status': status,
            'note': note,
        }
        response = await self._request('POST', '/api/user', json_body=body)
        user = _parse_user(self._json(response))
        logger.info('PasarGuard user created', pasarguard_user_id=user.id, username=user.username)
        return user

    async def update_user(self, user_id: int, **fields: Any) -> PasarguardUser:
        """Изменить пользователя; ``None``-поля не отправляются (панель: не менять)."""
        body: dict[str, Any] = {}
        for key, value in fields.items():
            if value is None:
                continue
            body[key] = value.isoformat() if isinstance(value, datetime) else value
        response = await self._request('PUT', f'/api/user/by-id/{user_id}', json_body=body)
        user = _parse_user(self._json(response))
        logger.info('PasarGuard user updated', pasarguard_user_id=user.id, fields=sorted(body))
        return user

    async def set_disabled(self, user_id: int, disabled: bool) -> PasarguardUser:
        response = await self._request(
            'PUT',
            f'/api/user/by-id/{user_id}/disabled',
            json_body={'disabled': disabled},
        )
        user = _parse_user(self._json(response))
        logger.info('PasarGuard user disabled toggled', pasarguard_user_id=user.id, disabled=disabled)
        return user

    async def revoke_sub(self, user_id: int) -> PasarguardUser:
        """Ротация VLESS UUID и токена подписки; старые креды становятся недействительны."""
        response = await self._request('POST', f'/api/user/by-id/{user_id}/revoke_sub')
        user = _parse_user(self._json(response))
        logger.info('PasarGuard subscription revoked', pasarguard_user_id=user.id)
        return user

    async def delete_user(self, user_id: int) -> None:
        await self._request('DELETE', f'/api/user/by-id/{user_id}')
        logger.info('PasarGuard user deleted', pasarguard_user_id=user_id)

    async def get_subscription_config(self, user_id: int, client_type: str = 'xray') -> bytes:
        """Конфиг через админский API: сырые байты, без токена подписки и HWID.

        Тело может содержать xdrive ``secrets``/``remoteFolder`` — не логируется
        и не сохраняется, только передаётся вызывающему.
        """
        response = await self._request('GET', f'/api/user/{user_id}/subscription/{client_type}')
        return response.content
