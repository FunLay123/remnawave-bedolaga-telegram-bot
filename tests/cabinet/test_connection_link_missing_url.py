"""connection-link при ``subscription_url = NULL``.

``str(None)`` — это непустая строка ``"None"``, поэтому проверка «ссылка ещё не
сгенерирована» не срабатывала, и кабинет отдавал 200 с ``"None"`` вместо 404.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import status as status_module


@pytest.mark.asyncio
async def test_missing_subscription_url_is_404_not_string_none(monkeypatch):
    subscription = SimpleNamespace(subscription_url=None)
    monkeypatch.setattr(status_module, 'resolve_subscription', AsyncMock(return_value=subscription))

    with pytest.raises(HTTPException) as caught:
        await status_module.get_connection_link(
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
            subscription_id=None,
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == 'Subscription link not yet generated'
