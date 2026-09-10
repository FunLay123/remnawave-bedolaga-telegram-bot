"""Настройки премиум-сквада должны переживать сохранение тарифа из админки.

Кабинет гоняет `server_traffic_limits` через схему в обе стороны: на чтении
`admin_tariffs._get_tariff_detail` собирает `ServerTrafficLimit(**limit_data)`,
на записи `update_tariff` кладёт обратно `limit.model_dump()`. Pydantic по
умолчанию отбрасывает незадекларированные ключи, поэтому поле, которое есть в
JSON, но не объявлено в схеме, молча исчезает при первом же сохранении тарифа —
админ правит название, а вместе с ним теряет цены докупки премиум-трафика.

Тест воспроизводит этот круг целиком.
"""

import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import admin_tariffs as route
from app.cabinet.schemas.tariffs import ServerTrafficLimit
from app.database.models import PromoGroup, ServerSquad, Subscription, Tariff, tariff_promo_groups
from app.utils.premium_traffic import parse_premium_squad
from tests.fixtures.sqlite_memory import memory_session


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'

STORED = {
    'traffic_limit_gb': 5,
    'name': 'Мобильный резерв',
    'sort_order': 2,
    'topup_enabled': True,
    'topup_packages': {'1': 500, '5': 2000},
    'max_topup_gb': 20,
}


def _roundtrip(stored: dict) -> dict:
    """Прогнать запись через чтение и запись ровно так, как это делает кабинет."""
    read_back = ServerTrafficLimit(**stored)  # admin_tariffs.py, сборка ответа
    return read_back.model_dump()  # admin_tariffs.py, сохранение


def test_premium_topup_settings_survive_a_tariff_save():
    saved = _roundtrip(STORED)

    assert saved['traffic_limit_gb'] == 5
    assert saved['name'] == 'Мобильный резерв'
    assert saved['sort_order'] == 2
    assert saved['topup_enabled'] is True
    assert saved['topup_packages'] == {'1': 500, '5': 2000}
    assert saved['max_topup_gb'] == 20


def test_every_stored_key_is_declared_in_the_schema():
    """Страховка на будущее: новый ключ в JSON без поля в схеме — потеря данных."""
    saved = _roundtrip(STORED)

    assert set(STORED) <= set(saved), f'схема теряет ключи: {set(STORED) - set(saved)}'


def test_saved_form_is_still_readable_by_the_domain_parser():
    """Круг через кабинет не должен ломать разбор на стороне воркера."""
    config = parse_premium_squad(SQUAD, _roundtrip(STORED))

    assert config is not None
    assert config.limit_gb == 5
    assert config.name == 'Мобильный резерв'
    assert config.topup_enabled is True
    assert config.topup_packages == {1: 500, 5: 2000}
    assert config.max_topup_gb == 20


def test_legacy_record_without_topup_fields_gets_safe_defaults():
    """Тарифы, заведённые до появления премиум-докупки, не должны падать."""
    saved = _roundtrip({'traffic_limit_gb': 5})

    assert saved['topup_enabled'] is False
    assert saved['topup_packages'] == {}
    assert saved['max_topup_gb'] == 0
    assert parse_premium_squad(SQUAD, saved).limit_gb == 5


# --- Кабинетный API тарифа: ответ и запись через настоящий роутер --------
#
# Прямой вызов обработчика (как в _roundtrip выше) не проходит через
# response_model и request-валидацию — здесь запрос идёт через тот же роутер
# и те же зависимости, что и в проде, иначе рассинхрон между сырым значением и
# разбором, а также молчаливый merge/replace конфигурации не были бы видны.

OTHER_SQUAD = '82a12389-14d6-40c6-b320-4674f6bbb344'
ADMIN = SimpleNamespace(id=1, telegram_id=777)

_TABLES = (
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    ServerSquad.__table__,
    Subscription.__table__,
)


def _override_permission_dependencies(app: FastAPI) -> None:
    """Пропустить RBAC, оставив всё остальное настоящим (см. test_admin_grace_access_http.py)."""
    for candidate in route.router.routes:
        for dependant in candidate.dependant.dependencies:
            call = dependant.call
            if getattr(call, '__name__', '') == 'dependency' and getattr(call, '__module__', '').endswith(
                'cabinet.dependencies'
            ):
                app.dependency_overrides[call] = lambda: ADMIN


@contextlib.asynccontextmanager
async def _tariff_app(monkeypatch, *, server_traffic_limits=None):
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(
            Tariff(
                id=1,
                name='Pro',
                is_active=True,
                allowed_squads=[SQUAD, OTHER_SQUAD],
                server_traffic_limits=server_traffic_limits or {},
            )
        )
        await db.flush()

        app = FastAPI()
        app.include_router(route.router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: db
        _override_permission_dependencies(app)

        with TestClient(app) as http:
            yield http


@pytest.mark.asyncio
async def test_get_reports_topup_disabled_when_no_packages(monkeypatch):
    """topup_enabled=true без пакетов — это выключенная докупка, так и отдаём."""
    limits = {SQUAD: {'traffic_limit_gb': 5, 'topup_enabled': True, 'topup_packages': {}}}
    async with _tariff_app(monkeypatch, server_traffic_limits=limits) as http:
        response = http.get('/cabinet/admin/tariffs/1')

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['server_traffic_limits'][SQUAD]['topup_enabled'] is False


@pytest.mark.asyncio
async def test_partial_update_keeps_other_squads(monkeypatch):
    """PATCH одного сквада не должен стирать настройки остальных."""
    limits = {
        SQUAD: {'traffic_limit_gb': 5},
        OTHER_SQUAD: {'traffic_limit_gb': 10, 'topup_enabled': True, 'topup_packages': {'5': 100}},
    }
    async with _tariff_app(monkeypatch, server_traffic_limits=limits) as http:
        response = http.put(
            '/cabinet/admin/tariffs/1',
            json={'server_traffic_limits': {SQUAD: {'traffic_limit_gb': 7}}},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['server_traffic_limits'][SQUAD]['traffic_limit_gb'] == 7
    assert OTHER_SQUAD in body['server_traffic_limits']
    assert body['server_traffic_limits'][OTHER_SQUAD]['traffic_limit_gb'] == 10
    assert body['server_traffic_limits'][OTHER_SQUAD]['topup_enabled'] is True


@pytest.mark.asyncio
async def test_write_rejects_malformed_packages(monkeypatch):
    """Мусор в пакетах докупки отклоняем на записи, а не терпим до следующего чтения."""
    async with _tariff_app(monkeypatch) as http:
        response = http.put(
            '/cabinet/admin/tariffs/1',
            json={
                'server_traffic_limits': {
                    SQUAD: {
                        'traffic_limit_gb': 5,
                        'topup_enabled': True,
                        'topup_packages': {'5': -100},
                    }
                }
            },
        )

    assert response.status_code == 422
