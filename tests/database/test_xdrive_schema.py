"""Схема PasarGuard-идентичности: колонки, частичный уникальный индекс, интенты.

Свежая установка получает схему из моделей, обновлённая — из миграции 0137.
Проверяем обе стороны: модельные таблицы в SQLite-памяти и прогон миграции на
«старой» базе, где таблицы уже существуют без новых колонок.
"""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.database.models import PasarguardDeleteIntent, Subscription, SubscriptionPremiumTraffic
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

SUBSCRIPTION_COLUMNS = {
    'pasarguard_user_id',
    'pasarguard_username',
    'pasarguard_created_at',
    'pasarguard_last_revoke_at',
    'pasarguard_revoke_pending_at',
    'pasarguard_synced_at',
    'pasarguard_synced_hash',
}
PREMIUM_COLUMNS = {'pasarguard_baseline_bytes', 'pasarguard_identity_created_at'}
DELETE_INTENT_COLUMNS = {
    'id',
    'subscription_id',
    'pasarguard_user_id',
    'pasarguard_username',
    'created_at',
}

VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION_FILE = '0137_xdrive_pasarguard.py'


async def _column_names(db, table_name: str) -> set[str]:
    connection = await db.connection()

    def inspect_table(sync_connection):
        return {column['name'] for column in sa.inspect(sync_connection).get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def test_subscription_has_pasarguard_columns(monkeypatch):
    async with memory_session(monkeypatch, (Subscription.__table__,)) as db:
        assert await _column_names(db, 'subscriptions') >= SUBSCRIPTION_COLUMNS


async def test_premium_traffic_has_pasarguard_baseline_columns(monkeypatch):
    tables = (Subscription.__table__, SubscriptionPremiumTraffic.__table__)
    async with memory_session(monkeypatch, tables) as db:
        assert await _column_names(db, 'subscription_premium_traffic') >= PREMIUM_COLUMNS


async def test_delete_intent_table_has_expected_columns(monkeypatch):
    async with memory_session(monkeypatch, (PasarguardDeleteIntent.__table__,)) as db:
        assert await _column_names(db, 'pasarguard_delete_intents') == DELETE_INTENT_COLUMNS


async def test_pasarguard_user_id_is_unique_when_set(monkeypatch):
    """Два непустых одинаковых id запрещены, а непривязанных строк может быть много."""
    async with memory_session(monkeypatch, (Subscription.__table__,)) as db:
        db.add_all(
            [
                Subscription(user_id=1, end_date=NOW, remnawave_short_id='one', pasarguard_user_id=42),
                Subscription(user_id=1, end_date=NOW, remnawave_short_id='two', pasarguard_user_id=42),
            ]
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        db.add_all(
            [
                Subscription(user_id=1, end_date=NOW, remnawave_short_id='three'),
                Subscription(user_id=1, end_date=NOW, remnawave_short_id='four'),
            ]
        )
        await db.commit()

        count = await db.scalar(sa.select(sa.func.count()).select_from(Subscription.__table__))
        assert count == 2


def _load_migration():
    spec = importlib.util.spec_from_file_location('migration_0137', VERSIONS / MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            _load_migration().upgrade()


def _legacy_engine(path: pathlib.Path) -> sa.Engine:
    """База до миграции: таблицы есть, новых колонок нет."""
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE subscriptions (id INTEGER PRIMARY KEY)'))
        connection.execute(sa.text('CREATE TABLE subscription_premium_traffic (id INTEGER PRIMARY KEY)'))
    return engine


def test_migration_adds_pasarguard_schema_to_existing_database(tmp_path):
    engine = _legacy_engine(tmp_path / 'upgraded.sqlite')
    _run_upgrade(engine)

    inspector = sa.inspect(engine)
    subscription_columns = {column['name'] for column in inspector.get_columns('subscriptions')}
    premium_columns = {column['name'] for column in inspector.get_columns('subscription_premium_traffic')}
    intent_columns = {column['name'] for column in inspector.get_columns('pasarguard_delete_intents')}
    assert subscription_columns >= SUBSCRIPTION_COLUMNS
    assert premium_columns >= PREMIUM_COLUMNS
    assert intent_columns == DELETE_INTENT_COLUMNS

    # Частичный уникальный индекс: одинаковые непустые id запрещены, NULL — нет.
    with engine.begin() as connection:
        connection.execute(sa.text('INSERT INTO subscriptions (id, pasarguard_user_id) VALUES (1, 42)'))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(sa.text('INSERT INTO subscriptions (id, pasarguard_user_id) VALUES (2, 42)'))
    with engine.begin() as connection:
        connection.execute(sa.text('INSERT INTO subscriptions (id, pasarguard_user_id) VALUES (3, NULL)'))
        connection.execute(sa.text('INSERT INTO subscriptions (id, pasarguard_user_id) VALUES (4, NULL)'))


def test_migration_is_idempotent_on_an_already_migrated_database(tmp_path):
    engine = _legacy_engine(tmp_path / 'twice.sqlite')
    _run_upgrade(engine)
    with engine.begin() as connection:
        connection.execute(sa.text('INSERT INTO subscriptions (id, pasarguard_user_id) VALUES (1, 42)'))
        connection.execute(
            sa.text(
                "INSERT INTO pasarguard_delete_intents (subscription_id, pasarguard_user_id, pasarguard_username) "
                "VALUES (1, 42, 'bx1')"
            )
        )

    _run_upgrade(engine)  # повторный прогон не должен падать на готовых колонках и таблице

    with engine.connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM subscriptions')).scalar() == 1
        assert connection.execute(sa.text('SELECT count(*) FROM pasarguard_delete_intents')).scalar() == 1
