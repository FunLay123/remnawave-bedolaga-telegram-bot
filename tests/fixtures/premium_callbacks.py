"""In-memory replacement for the external Telegram callback cache in tests."""

import pytest

from app.utils import premium_callbacks


@pytest.fixture
def premium_callback_cache(monkeypatch):
    class Cache:
        entries: dict = {}

        async def set(self, key, value, expire):
            assert expire > 0
            self.entries[key] = value
            return True

        async def get(self, key):
            return self.entries.get(key)

        async def getdel(self, key):
            return self.entries.pop(key, None)

    cache = Cache()
    monkeypatch.setattr(premium_callbacks, 'cache', cache)
    return cache
