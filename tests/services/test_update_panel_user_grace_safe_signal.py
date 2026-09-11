"""Task 7b: отложенная запись `update_panel_user_grace_safe` обязана быть
отличима от применённой.

До этой правки при открытом grace-оверлее и апдейте, состоящем только из
защищённых полей (например, одних сквадов), функция откладывала запись и
возвращала `get_user_by_id(...)` — тот же truthy панельный объект, что и при
успешном `update_user`. Ни один вызывающий не мог различить эти два случая по
возврату, включая `_clear_orphan` из `premium_traffic_service.py` (см. отдельный
тест там).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

import app.services.grace_access_runtime as grace_runtime_mod
from app.config import Settings
from app.services.grace_access_runtime import (
    GracePanelUpdateLease,
    grace_access_runtime,
    panel_update_was_deferred,
    update_panel_user_grace_safe,
)
from app.services.grace_access_service import GraceAccessMode


PANEL_ID = 777
SUBSCRIPTION_ID = 9001


class _RecordingApi:
    """Панель-двойник: считает вызовы update_user/get_user_by_id."""

    def __init__(self, *, current: Any = None) -> None:
        self.update_calls: list[dict[str, Any]] = []
        self.get_calls: list[int] = []
        self._current = current

    async def update_user(self, **kwargs: Any) -> SimpleNamespace:
        self.update_calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def get_user_by_id(self, user_id: int) -> Any:
        self.get_calls.append(user_id)
        return self._current


@pytest.fixture(autouse=True)
def _single_tariff(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)


@pytest.fixture(autouse=True)
def _grace_active(monkeypatch):
    """Режим ACTIVE — иначе функция коротким замыканием уходит в обычный PATCH."""
    monkeypatch.setattr(grace_access_runtime, '_mode', GraceAccessMode.ACTIVE)


def _use_lease(monkeypatch, *, has_open_grace: bool) -> SimpleNamespace:
    subscription = SimpleNamespace(
        id=SUBSCRIPTION_ID,
        remnawave_id=None,
        user=SimpleNamespace(remnawave_id=PANEL_ID),
    )

    @asynccontextmanager
    async def fake_lease(subscription_id):
        yield GracePanelUpdateLease(subscription=subscription, has_open_grace=has_open_grace, db=object())

    monkeypatch.setattr(grace_runtime_mod, 'grace_sensitive_panel_update', fake_lease)
    return subscription


def _payment_not_recovered(monkeypatch) -> None:
    """Заглушка на месте `apply_recovered_grace_update_locked`.

    Восстановление после оплаты — отдельный, уже покрытый путь; здесь важно
    только то, что происходит ПОСЛЕ него, когда оно ничего не завершило.
    """

    async def _not_completed(db, api, subscription_id, *, update_kwargs, source):
        return False, None

    monkeypatch.setattr(grace_runtime_mod, 'apply_recovered_grace_update_locked', _not_completed)


@pytest.mark.asyncio
async def test_deferred_write_is_distinguishable_from_applied(monkeypatch):
    """Сквад-only апдейт под открытым оверлеем — запись отложена, а не применена.

    Ровно случай из брифа: после вычитания защищённых полей остаётся один
    `user_id`, и старый код возвращал truthy-карточку, неотличимую от успеха.
    """
    _use_lease(monkeypatch, has_open_grace=True)
    _payment_not_recovered(monkeypatch)
    current = SimpleNamespace(id=PANEL_ID, active_internal_squads=[{'uuid': 'kept'}])
    api = _RecordingApi(current=current)

    with structlog.testing.capture_logs() as logs:
        result = await update_panel_user_grace_safe(
            api,
            SUBSCRIPTION_ID,
            user_id=PANEL_ID,
            active_internal_squads=['kept'],
        )

    assert panel_update_was_deferred(result) is True
    assert api.update_calls == [], 'защищённое поле не должно уйти в панель, пока оверлей открыт'
    assert api.get_calls == [PANEL_ID]
    assert any(entry['event'] == 'Deferred grace-owned fields from routine Remnawave update' for entry in logs)


@pytest.mark.asyncio
async def test_deferred_result_still_reads_like_the_panel_card(monkeypatch):
    """Безразличный вызывающий не ломается: обёртка прозрачна на чтение и на `if`."""
    _use_lease(monkeypatch, has_open_grace=True)
    _payment_not_recovered(monkeypatch)
    current = SimpleNamespace(id=PANEL_ID, subscription_url='https://panel/sub', active_internal_squads=[])
    api = _RecordingApi(current=current)

    result = await update_panel_user_grace_safe(
        api,
        SUBSCRIPTION_ID,
        user_id=PANEL_ID,
        active_internal_squads=['kept'],
    )

    assert result, 'возврат обязан остаться truthy — на него смотрят через `if` в админке'
    assert result.id == PANEL_ID
    assert result.subscription_url == 'https://panel/sub'
    assert getattr(result, 'happ_crypto_link', None) is None, 'отсутствующее поле обязано остаться отсутствующим'


@pytest.mark.asyncio
async def test_applied_write_returns_the_panel_object_unchanged(monkeypatch):
    """Без открытого оверлея возврат — объект панели как был, без обёртки.

    Это и есть граница радиуса поражения: применённая запись не меняется ни на
    байт, поэтому двадцати одному существующему упоминанию править нечего.
    """
    _use_lease(monkeypatch, has_open_grace=False)
    api = _RecordingApi()

    result = await update_panel_user_grace_safe(
        api,
        SUBSCRIPTION_ID,
        user_id=PANEL_ID,
        active_internal_squads=['kept'],
    )

    assert panel_update_was_deferred(result) is False
    assert isinstance(result, SimpleNamespace), 'применённый путь не должен ничего оборачивать'
    assert result.active_internal_squads == ['kept']
    assert api.update_calls == [{'user_id': PANEL_ID, 'active_internal_squads': ['kept']}]
    assert api.get_calls == []


@pytest.mark.asyncio
async def test_partially_applied_write_still_counts_as_deferred(monkeypatch):
    """Незащищённая часть уезжает в панель, защищённая — откладывается.

    Вызывающему, которому важна именно защищённая часть (сквады), такая запись
    тоже не применена: деление идёт по защищённым полям, а не по факту хоть
    какого-то запроса к панели.
    """
    _use_lease(monkeypatch, has_open_grace=True)
    _payment_not_recovered(monkeypatch)
    api = _RecordingApi()

    result = await update_panel_user_grace_safe(
        api,
        SUBSCRIPTION_ID,
        user_id=PANEL_ID,
        description='new',
        active_internal_squads=['kept'],
    )

    assert panel_update_was_deferred(result) is True
    assert api.update_calls == [{'user_id': PANEL_ID, 'description': 'new'}]
    assert api.get_calls == [], 'карточка уже пришла с update_user — второй запрос не нужен'


@pytest.mark.asyncio
async def test_wrapper_does_not_turn_an_empty_panel_answer_into_a_success(monkeypatch):
    """Пустой ответ панели остаётся falsy и под обёрткой.

    Часть вызывающих (админка, мониторинг) судит об успехе по `if result:`.
    Если бы обёртка была truthy сама по себе, пустой ответ прочитался бы как
    успешное обновление — поведение безразличного вызывающего изменилось бы.
    """
    _use_lease(monkeypatch, has_open_grace=True)
    _payment_not_recovered(monkeypatch)

    class _EmptyAnswerApi(_RecordingApi):
        async def update_user(self, **kwargs: Any) -> Any:
            self.update_calls.append(kwargs)
            return None

    api = _EmptyAnswerApi()

    result = await update_panel_user_grace_safe(
        api,
        SUBSCRIPTION_ID,
        user_id=PANEL_ID,
        description='new',
        active_internal_squads=['kept'],
    )

    assert panel_update_was_deferred(result) is True
    assert not result, 'обёртка обязана повторять истинность карточки, а не подменять её'


@pytest.mark.asyncio
async def test_update_without_protected_fields_is_applied_not_deferred(monkeypatch):
    """Оверлей открыт, но защищённых полей в запросе нет — откладывать нечего."""
    _use_lease(monkeypatch, has_open_grace=True)
    _payment_not_recovered(monkeypatch)
    api = _RecordingApi()

    result = await update_panel_user_grace_safe(api, SUBSCRIPTION_ID, user_id=PANEL_ID, description='new')

    assert panel_update_was_deferred(result) is False
    assert api.update_calls == [{'user_id': PANEL_ID, 'description': 'new'}]


@pytest.mark.asyncio
async def test_real_indifferent_caller_still_records_panel_identity(monkeypatch):
    """Настоящий безразличный вызывающий на отложенном возврате работает как прежде.

    `panel_sync.writer._record_identity` — самый дорогой из них: он записывает
    в подписку адрес аккаунта панели и ссылки. Потерять их из-за обёртки нельзя,
    иначе следующий проход не найдёт аккаунт и заведёт дубль.
    """
    from app.services.panel_sync.writer import _record_identity

    _use_lease(monkeypatch, has_open_grace=True)
    _payment_not_recovered(monkeypatch)
    api = _RecordingApi(
        current=SimpleNamespace(
            id=PANEL_ID,
            short_uuid='abc',
            subscription_url='https://panel/sub',
            happ_crypto_link='happ://x',
        )
    )

    result = await update_panel_user_grace_safe(
        api,
        SUBSCRIPTION_ID,
        user_id=PANEL_ID,
        active_internal_squads=['kept'],
    )

    user = SimpleNamespace(remnawave_id=None)
    subscription = SimpleNamespace(
        id=SUBSCRIPTION_ID,
        remnawave_id=None,
        remnawave_short_uuid=None,
        subscription_url=None,
        subscription_crypto_link=None,
    )
    await _record_identity(None, user, subscription, result, multi_tariff=False)

    assert subscription.remnawave_id == PANEL_ID
    assert subscription.remnawave_short_uuid == 'abc'
    assert subscription.subscription_url == 'https://panel/sub'
    assert subscription.subscription_crypto_link == 'happ://x'
    assert user.remnawave_id == PANEL_ID


def test_anything_unmarked_reads_as_applied() -> None:
    """Непомеченный объект — применённая запись, а не отложенная.

    Ровно так вело себя всё до этой правки: любой ответ читался как применённый.
    Двойникам в тестах и прямым ответам `api.update_user` опираться больше не на
    что, и предикат обязан сохранить для них прежний дефолт.
    """
    assert panel_update_was_deferred(SimpleNamespace(id=1)) is False
    assert panel_update_was_deferred(None) is False
