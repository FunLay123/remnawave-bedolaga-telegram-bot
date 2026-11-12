"""Уникальный индекс маркера первой покупки: модель, миграция, свежая установка.

Маркер ``referral_first_purchase_registered`` — одна строка на приглашённого.
SELECT-before-INSERT в приложении сужает окно гонки, но не закрывает его;
партиальный UNIQUE-индекс делает дубель структурно невозможным. Эти тесты
фиксируют паритет «свежая установка = миграция»: индекс объявлен в метаданных
и создаётся ``create_all``, а миграция 0136 чистит исторические дубли и
переживает повторный запуск.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, ReferralEarning


INDEX_NAME = 'uq_referral_earnings_first_purchase_registered'
REASON = 'referral_first_purchase_registered'
PENDING_INDEX_NAME = 'uq_referral_earnings_registration_pending'
PENDING_REASON = 'referral_registration_pending'
MIGRATION = (
    Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0136_referral_first_purchase_marker_unique.py'
)


def _load_migration():
    spec = importlib.util.spec_from_file_location('migration_0136', MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_table(conn) -> None:
    """Схема до миграции: таблица без нового индекса."""
    conn.execute(
        sa.text(
            """
            CREATE TABLE referral_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                referral_id INTEGER NOT NULL,
                amount_kopeks INTEGER NOT NULL,
                reason VARCHAR(100) NOT NULL,
                reward_type VARCHAR(10) NOT NULL DEFAULT 'money',
                level INTEGER NOT NULL DEFAULT 1,
                days_granted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
    )


def _seed_duplicates(conn) -> None:
    conn.execute(
        sa.text(
            """
            INSERT INTO referral_earnings (id, user_id, referral_id, amount_kopeks, reason)
            VALUES
                (1, 10, 1, 0, :marker),
                (2, 20, 1, 0, :marker),
                (3, 30, 1, 0, 'referral_registration_days_only'),
                (4, 10, 1, 0, :marker),
                (5, 20, 1, 0, 'referral_first_purchase')
            """
        ),
        {'marker': REASON},
    )


def _index_sql(conn) -> str | None:
    return conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
        {'name': INDEX_NAME},
    ).scalar_one_or_none()


def _marker_ids(conn) -> list[int]:
    rows = conn.execute(
        sa.text('SELECT id FROM referral_earnings WHERE reason = :reason ORDER BY id'),
        {'reason': REASON},
    ).fetchall()
    return [row[0] for row in rows]


def test_model_declares_partial_unique_first_purchase_index():
    """Fresh-install parity: индекс объявлен в модели, а не только в миграции."""
    indexes = {index.name: index for index in ReferralEarning.__table__.indexes}
    index = indexes[INDEX_NAME]
    assert index.unique is True
    assert [column.name for column in index.columns] == ['user_id']
    for dialect in ('postgresql', 'sqlite'):
        where_clause = str(index.dialect_kwargs[f'{dialect}_where'])
        assert f"reason = '{REASON}'" in where_clause


def test_model_declares_partial_unique_pending_index():
    """Fresh-install parity для миграции 0085: pending-строка одна на пару."""
    indexes = {index.name: index for index in ReferralEarning.__table__.indexes}
    index = indexes[PENDING_INDEX_NAME]
    assert index.unique is True
    assert [column.name for column in index.columns] == ['user_id', 'referral_id']
    for dialect in ('postgresql', 'sqlite'):
        where_clause = str(index.dialect_kwargs[f'{dialect}_where'])
        assert f"reason = '{PENDING_REASON}'" in where_clause


def test_create_all_creates_partial_unique_pending_index_on_sqlite():
    """create_all на свежей БД создаёт pending-индекс, а не только миграция 0085."""
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn, tables=[ReferralEarning.__table__])

            index_sql = conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
                {'name': PENDING_INDEX_NAME},
            ).scalar_one_or_none()
            assert index_sql is not None, 'create_all не создал pending-индекс'
            assert 'UNIQUE INDEX' in index_sql.upper()
            assert f"WHERE reason = '{PENDING_REASON}'" in index_sql
    finally:
        engine.dispose()


def test_migration_0136_is_chained_after_0135():
    migration = _load_migration()
    assert migration.revision == '0136'
    assert migration.down_revision == '0135'


def test_create_all_creates_partial_unique_index_on_sqlite():
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn, tables=[ReferralEarning.__table__])

            index_sql = _index_sql(conn)
            assert index_sql is not None, 'create_all не создал индекс маркера'
            assert 'UNIQUE INDEX' in index_sql.upper()
            assert f"WHERE reason = '{REASON}'" in index_sql
    finally:
        engine.dispose()


def test_migration_0136_upgrade_dedupes_and_is_idempotent():
    """Свежие дубли схлопываются до MIN(id), повторный upgrade не падает."""
    migration = _load_migration()
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as conn:
            _create_legacy_table(conn)
            _seed_duplicates(conn)

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

            assert _marker_ids(conn) == [1, 2]
            # Чужие причины не трогаются: миграция чистит только маркер.
            assert conn.execute(sa.text('SELECT COUNT(*) FROM referral_earnings')).scalar_one() == 4

            index_sql = _index_sql(conn)
            assert index_sql is not None
            assert 'UNIQUE INDEX' in index_sql.upper()
            assert f"WHERE reason = '{REASON}'" in index_sql

        # Повторный запуск: CREATE UNIQUE INDEX IF NOT EXISTS идемпотентен.
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

            assert _marker_ids(conn) == [1, 2]
            assert _index_sql(conn) is not None

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.downgrade()

            assert _index_sql(conn) is None
    finally:
        engine.dispose()
