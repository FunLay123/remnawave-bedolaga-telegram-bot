"""Ранние выходы автоназначения промогруппы обязаны снимать блокировку строки.

``create_transaction(commit=True)`` коммитит платёж и сразу вызывает
``maybe_assign_promo_group_by_total_spent``. Та блокирует строку пользователя
(``lock_user_for_update``) и раньше возвращалась без commit/rollback, если
трат ещё нет или подходящей группы не настроено. Блокировка оставалась за
вызывающей сессией, а пост-коммит хук реферальной награды открывал вторую
сессию и ждал ту же строку. Внешний код ждал хук — никто не отпускал
блокировку; PostgreSQL-таймаут превращал успешную оплату в ``error`` без
маркера и дней.

Тесты фиксируют коммит на каждом раннем выходе. Полноценная проверка
отсутствия взаимной блокировки — в
``tests/database/test_promo_group_lock_release_postgres.py``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import promo_group_assignment


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        telegram_id=1001,
        auto_promo_group_threshold_kopeks=0,
        auto_promo_group_assigned=False,
        get_primary_promo_group=lambda: None,
    )


def _db() -> SimpleNamespace:
    db = SimpleNamespace()
    db.get = AsyncMock(return_value=_user())
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _no_real_lock(monkeypatch):
    async def _lock(_db, user):
        return user

    monkeypatch.setattr(promo_group_assignment, 'lock_user_for_update', _lock)


@pytest.mark.asyncio
async def test_zero_total_spent_commits_and_releases_the_lock(monkeypatch):
    monkeypatch.setattr(
        promo_group_assignment,
        'get_user_total_spent_kopeks',
        AsyncMock(return_value=0),
    )
    db = _db()

    result = await promo_group_assignment.maybe_assign_promo_group_by_total_spent(db, 1)

    assert result is None
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_target_group_commits_and_releases_the_lock(monkeypatch):
    monkeypatch.setattr(
        promo_group_assignment,
        'get_user_total_spent_kopeks',
        AsyncMock(return_value=150_000),
    )
    monkeypatch.setattr(
        promo_group_assignment,
        '_get_best_group_for_spending',
        AsyncMock(return_value=None),
    )
    db = _db()

    result = await promo_group_assignment.maybe_assign_promo_group_by_total_spent(db, 1)

    assert result is None
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
