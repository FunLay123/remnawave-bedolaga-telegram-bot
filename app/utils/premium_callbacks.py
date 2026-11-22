"""Short-lived Telegram callback contexts bound to the actor and action."""

import re
from secrets import token_hex

from app.utils.cache import cache


_TOKEN_PATTERN = re.compile(r'[0-9a-f]{24}')
_TOKEN_TTL_SECONDS = 600


async def store_premium_callback(actor_id: int, action: str, payload: dict) -> str | None:
    token = token_hex(12)
    key = f'premium_callback:{actor_id}:{action}:{token}'
    return token if await cache.set(key, payload, _TOKEN_TTL_SECONDS) else None


async def read_premium_callback(actor_id: int, action: str, token: str, *, consume: bool = False) -> dict | None:
    if not _TOKEN_PATTERN.fullmatch(token):
        return None
    key = f'premium_callback:{actor_id}:{action}:{token}'
    payload = await (cache.getdel(key) if consume else cache.get(key))
    return payload if isinstance(payload, dict) else None
