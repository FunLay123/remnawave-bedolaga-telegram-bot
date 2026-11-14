"""CRUD PasarGuard-идентичности подписки и очереди отложенных удалений."""

from datetime import UTC, datetime

from app.database.crud.pasarguard import (
    add_delete_intent,
    clear_pasarguard_identity,
    delete_intent,
    list_delete_intents,
    set_pasarguard_identity,
)
from app.database.models import PasarguardDeleteIntent, Subscription
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

IDENTITY_ATTRIBUTES = (
    'pasarguard_user_id',
    'pasarguard_username',
    'pasarguard_created_at',
    'pasarguard_last_revoke_at',
    'pasarguard_revoke_pending_at',
    'pasarguard_synced_at',
    'pasarguard_synced_hash',
)

TABLES = (Subscription.__table__, PasarguardDeleteIntent.__table__)


async def _subscription(db) -> Subscription:
    subscription = Subscription(user_id=1, end_date=NOW, remnawave_short_id='sub1')
    db.add(subscription)
    await db.commit()
    return subscription


async def test_set_and_clear_identity(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await _subscription(db)

        await set_pasarguard_identity(db, subscription, user_id=42, username='bx12', created_at=NOW)
        assert subscription.pasarguard_user_id == 42
        assert subscription.pasarguard_username == 'bx12'
        assert subscription.pasarguard_created_at == NOW

        subscription.pasarguard_last_revoke_at = NOW
        subscription.pasarguard_revoke_pending_at = NOW
        subscription.pasarguard_synced_at = NOW
        subscription.pasarguard_synced_hash = 'a' * 64
        await db.commit()

        await clear_pasarguard_identity(db, subscription)
        await db.commit()
        await db.refresh(subscription)

        assert {attribute: getattr(subscription, attribute) for attribute in IDENTITY_ATTRIBUTES} == dict.fromkeys(
            IDENTITY_ATTRIBUTES
        )


async def test_delete_intent_round_trip(monkeypatch):
    async with memory_session(monkeypatch, (PasarguardDeleteIntent.__table__,)) as db:
        await add_delete_intent(db, subscription_id=7, user_id=42, username='bx7')
        await db.commit()

        intents = await list_delete_intents(db)
        assert len(intents) == 1
        intent = intents[0]
        assert intent.subscription_id == 7
        assert intent.pasarguard_user_id == 42
        assert intent.pasarguard_username == 'bx7'

        await delete_intent(db, intent)
        await db.commit()

        assert await list_delete_intents(db) == []
