"""Прокси страницы подписок: полоска расхода в приложениях — по белым спискам.

Полоску трафика приложения берут из заголовка ``subscription-userinfo``, а его
панель строит по общему трафику аккаунта. На тарифе «безлимит + премиум-сквады
по счёту» общая полоска бесконечная, а то, что действительно кончается, —
расход премиум-сквадов — клиенту не видно.

nginx отправляет домен подписок в бота (страница подписок стоит в upstream
запасным сервером). Бот пересылает запрос странице подписок как есть и в ответе
меняет только этот заголовок: ``download`` — израсходовано по премиум-сквадам
подписки, ``total`` — их лимиты вместе с докупленным, ``expire`` остаётся от
панели. Если подменять нечего или что-то пошло не так, ответ уходит нетронутым:
полоска без белых списков лучше, чем сломанная подписка.

Запросы узнаются по заголовку Host, поэтому остальные маршруты веб-сервера бота
на домене подписок недоступны.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.database.crud.premium_traffic import get_states_for_subscription
from app.database.crud.tariff import get_tariff_by_id
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription
from app.utils.premium_traffic import get_premium_squads_for_tariff


logger = structlog.get_logger(__name__)

USERINFO_HEADER = 'subscription-userinfo'
TIMEOUT_SECONDS = 15.0

# Заголовки одного соединения (RFC 9110, 7.6.1): между nginx и ботом и между
# ботом и страницей подписок соединения разные, дальше их пересылать нельзя.
HOP_BY_HOP_HEADERS = frozenset(
    {
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'proxy-authorization',
        'te',
        'trailer',
        'transfer-encoding',
        'upgrade',
    }
)

# Сегмент пути, похожий на shortUuid. Путь подписки бывает с префиксом и с
# типом клиента после shortUuid, поэтому в базе ищутся все такие сегменты.
_SHORT_UUID_RE = re.compile(r'^[A-Za-z0-9_-]{6,64}$')

UsageLookup = Callable[[list[str]], Awaitable[tuple[int, int] | None]]


def short_uuid_candidates(path: str) -> list[str]:
    return [segment for segment in path.split('/') if _SHORT_UUID_RE.match(segment)]


def rewrite_userinfo(original: str, used_bytes: int, total_bytes: int) -> str:
    """Меняет upload/download/total, остальные поля (``expire``) оставляет."""
    fields: dict[str, str] = {}
    for part in original.split(';'):
        key, sep, value = part.strip().partition('=')
        if sep and key.strip():
            fields[key.strip().lower()] = value.strip()

    # upload=0: приложения показывают расход как сумму upload + download.
    fields.update(upload='0', download=str(used_bytes), total=str(total_bytes))
    head = [f'{key}={fields.pop(key)}' for key in ('upload', 'download', 'total')]
    return '; '.join(head + [f'{key}={value}' for key, value in fields.items()])


async def premium_usage(db: AsyncSession, short_uuids: list[str]) -> tuple[int, int] | None:
    """(израсходовано, лимит с докупленным) по премиум-сквадам подписки, в байтах.

    Сквады те же, что в строках премиума главного меню (``_premium_squad_lines``):
    премиальные в тарифе и подключённые к подписке. Несколько сквадов
    складываются в одну полоску. ``None`` — премиум-сквадов нет, подменять нечего.
    """
    # С выключенным премиумом воркер не замеряет расход, цифры в базе устарели.
    if not short_uuids or not settings.PREMIUM_TRAFFIC_ENABLED or not settings.is_tariffs_mode():
        return None

    rows = (
        (await db.execute(select(Subscription).where(Subscription.remnawave_short_uuid.in_(short_uuids)).limit(2)))
        .scalars()
        .all()
    )
    # Два совпадения — путь неоднозначный, лучше не угадывать.
    if len(rows) != 1 or not rows[0].tariff_id:
        return None
    subscription = rows[0]

    configs = get_premium_squads_for_tariff(await get_tariff_by_id(db, subscription.tariff_id))
    connected = set(subscription.connected_squads or [])
    uuids = [squad_uuid for squad_uuid in configs if squad_uuid in connected]
    if not uuids:
        return None

    states = {state.squad_uuid: state for state in await get_states_for_subscription(db, subscription.id)}
    used = total = 0
    for squad_uuid in uuids:
        state = states.get(squad_uuid)
        if state is None:
            # Воркер ещё не замерял сквад: расход ноль, потолок — лимит тарифа.
            total += configs[squad_uuid].limit_bytes
            continue
        used += state.used_bytes or 0
        total += state.total_limit_bytes
    return used, total


async def lookup_premium_usage(short_uuids: list[str]) -> tuple[int, int] | None:
    async with AsyncSessionLocal() as db:
        return await premium_usage(db, short_uuids)


def create_proxy_client() -> httpx.AsyncClient:
    # Редиректы страницы подписок отдаются клиенту, а не проходятся здесь.
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)


class SubscriptionProxyMiddleware:
    """Перехватывает запросы на домен подписок, остальное пропускает в приложение."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        hosts: Iterable[str],
        upstream: str,
        client: httpx.AsyncClient,
        lookup: UsageLookup = lookup_premium_usage,
    ) -> None:
        self.app = app
        self.hosts = {host.lower() for host in hosts}
        self.upstream = upstream.rstrip('/')
        self.client = client
        self.lookup = lookup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or not self._is_subscription_host(scope):
            await self.app(scope, receive, send)
            return

        response = await self._proxy(Request(scope, receive))
        await response(scope, receive, send)

    def _is_subscription_host(self, scope: Scope) -> bool:
        for name, value in scope.get('headers') or []:
            if name == b'host':
                return value.decode('latin-1').rsplit(':', 1)[0].lower() in self.hosts
        return False

    async def _proxy(self, request: Request) -> Response:
        # Сырой путь: без повторного кодирования, как его прислал клиент.
        raw_path = request.scope.get('raw_path') or request.url.path.encode()
        url = self.upstream + raw_path.decode('latin-1')
        if query := request.scope.get('query_string'):
            url += '?' + query.decode('latin-1')

        headers = [
            (name, value)
            for name, value in request.headers.raw
            if name.decode('latin-1').lower() not in HOP_BY_HOP_HEADERS and name.lower() != b'content-length'
        ]
        upstream_request = self.client.build_request(request.method, url, headers=headers, content=await request.body())
        try:
            upstream_response = await self.client.send(upstream_request, stream=True)
            try:
                # aiter_raw, а не .content: тело остаётся сжатым, как пришло,
                # и Content-Encoding от страницы подписок остаётся верным.
                body = b''.join([chunk async for chunk in upstream_response.aiter_raw()])
            finally:
                await upstream_response.aclose()
        except httpx.HTTPError as error:
            # 502 — сигнал nginx'у перейти на запасной сервер.
            logger.warning('Страница подписок не ответила', url=url, error=str(error))
            return Response(status_code=502)

        response_headers = [
            (name, value)
            for name, value in upstream_response.headers.raw
            if name.decode('latin-1').lower() not in HOP_BY_HOP_HEADERS
            and (request.method == 'HEAD' or name.lower() != b'content-length')
        ]
        if request.method != 'HEAD':
            response_headers.append((b'content-length', str(len(body)).encode()))

        userinfo = upstream_response.headers.get(USERINFO_HEADER)
        content_type = upstream_response.headers.get('content-type', '')
        if userinfo is not None and not content_type.startswith('text/html'):
            response_headers = await self._with_premium_userinfo(request, response_headers, userinfo)

        response = Response(content=body, status_code=upstream_response.status_code)
        response.raw_headers = response_headers
        return response

    async def _with_premium_userinfo(
        self, request: Request, headers: list[tuple[bytes, bytes]], userinfo: str
    ) -> list[tuple[bytes, bytes]]:
        try:
            usage = await self.lookup(short_uuid_candidates(request.url.path))
        except Exception as error:
            logger.warning('Не удалось посчитать расход белых списков для подписки', error=str(error))
            return headers
        if usage is None:
            return headers

        value = rewrite_userinfo(userinfo, *usage).encode('latin-1')
        return [
            (name, value if name.lower() == USERINFO_HEADER.encode() else header_value)
            for name, header_value in headers
        ]
