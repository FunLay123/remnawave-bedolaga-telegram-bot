"""Раздел премиум-трафика через настоящий роутер и настоящую require_permission.

`test_admin_premium_traffic.py` бьёт только по внутренним функциям
(``_reset_premium``, ``_close_access``, ``_reopen_access``, ``_reset_regular``),
минуя FastAPI-роутер и ``require_permission`` целиком — ни один тест там не
проверяет, что запрос без права ``traffic:read``/``traffic:manage`` вообще
отклоняется по HTTP. Здесь запрос идёт через настоящий роутер с настоящей
``require_permission``: подменяется только движок RBAC
(``PermissionService.check_permission``/``log_action``), а не сама зависимость
``require_permission`` — в отличие от `_override_permission_dependencies` в
`test_admin_grace_access_http.py`, которая для остальных тестов той же
проверки нарочно исключает (там предмет теста — сериализация ответа, а не
авторизация). Правило разрешения роль -> право проверяется отдельно, в
`test_permission_service.py`; здесь важно только то, что роут действительно
зовёт `require_permission` и уважает её отказ, а не тихо пропускает запрос.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import admin_premium_traffic as route
from app.database.models import SubscriptionPremiumTraffic
from app.services.permission_service import PermissionService
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__,)

# Не легаси-админ (см. _is_legacy_admin) — иначе check_permission вернул бы
# True ещё до вызова нашей подмены, и тест ничего бы не доказывал.
ADMIN = SimpleNamespace(id=1, telegram_id=999)


def _denying_app(monkeypatch, db) -> tuple[FastAPI, list[str]]:
    """Настоящий роутер и require_permission; RBAC форсированно отказывает.

    ``log_action`` тоже подменяется заглушкой: в проде отказ пишется в журнал
    аудита (``AuditLogCRUD``), но это отдельная гарантия, не предмет этого
    теста, и таскать ради неё ещё одну таблицу здесь незачем.
    """
    checked: list[str] = []

    async def _deny(_db, _user, permission, *, ip_address=None):
        checked.append(permission)
        return False, 'нет активных ролей'

    async def _log_action(_db, **_kwargs):
        return None

    monkeypatch.setattr(PermissionService, 'check_permission', _deny)
    monkeypatch.setattr(PermissionService, 'log_action', _log_action)

    app = FastAPI()
    app.include_router(route.router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: db
    app.dependency_overrides[get_current_cabinet_user] = lambda: ADMIN
    return app, checked


@pytest.mark.asyncio
async def test_get_states_refuses_without_traffic_read(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        app, checked = _denying_app(monkeypatch, db)
        with TestClient(app) as http:
            response = http.get('/cabinet/admin/premium-traffic/1')

    assert response.status_code == 403
    assert checked == ['traffic:read']


@pytest.mark.parametrize(
    ('path', 'body'),
    [
        ('/cabinet/admin/premium-traffic/1/reset', {'scope': 'premium'}),
        ('/cabinet/admin/premium-traffic/1/grant', {'squad_uuid': 'x', 'gb': 1}),
        ('/cabinet/admin/premium-traffic/1/close', {'squad_uuid': 'x'}),
        ('/cabinet/admin/premium-traffic/1/reopen', {'squad_uuid': 'x'}),
    ],
)
@pytest.mark.asyncio
async def test_manage_endpoints_refuse_without_traffic_manage(monkeypatch, path, body):
    async with memory_session(monkeypatch, TABLES) as db:
        app, checked = _denying_app(monkeypatch, db)
        with TestClient(app) as http:
            response = http.post(path, json=body)

    assert response.status_code == 403
    assert checked == ['traffic:manage']
