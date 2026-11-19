# Premium Traffic Refresh After Purchase — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После покупки тарифа экран белых списков сразу показывает новый лимит, а воркер премиум-трафика проходит вне очереди, не дожидаясь пятиминутного интервала.

**Architecture:** При смене тарифа `extend_subscription` сразу обновляет снимки `limit_bytes` в `subscription_premium_traffic`. Воркер получает `asyncio.Event` и будится после успешной синхронизации с панелью при `reset_traffic=True`; проходы остаются последовательными и схлопываются.

**Tech Stack:** Python 3.14, aiogram 3, SQLAlchemy async, pytest.

**Spec:** `docs/superpowers/specs/2026-11-19-premium-traffic-refresh-after-purchase-design.md`

## Global Constraints

- Не менять `PREMIUM_TRAFFIC_CHECK_INTERVAL_SECONDS` и рубильник `PREMIUM_TRAFFIC_ENABLED`.
- Не запускать `process_once()` из хендлеров и не создавать параллельные проходы.
- Не трогать и не снимать `closed_at` — админское закрытие сильнее любых покупок.
- Побочный эффект пробуждения не должен ломать уже оплаченную покупку: только `try/except` с логом.
- После изменения добавить запись в `../claude-changes.md`.

## Review Focus

- Ручное закрытие админом (`closed_at`) и блокировка доступа переживают смену тарифа и пробуждение воркера; снимок лимита в новом периоде следует за тарифом, как у всех.
- Автоснятый за расход сквад при апгрейде на больший лимит получает право на возврат, при даунгрейде — остаётся снятым.
- Несколько покупок подряд не рождают параллельные проходы: один `Event`, один цикл, лишние пробуждения схлопываются.
- Синхронизация без `reset_traffic` (устройства, сквады, рутинный апдейт) не будит воркер.
- Выключенный `PREMIUM_TRAFFIC_ENABLED` и тариф без премиум-сквадов не будят воркер; `stop()` не оставляет спящую задачу на пять минут.

---

### Task 1: Обновлять снимки лимитов при смене тарифа

**Files:**
- Modify: `app/database/crud/premium_traffic.py` — новая функция рядом с `start_new_period`
- Modify: `app/database/crud/subscription.py` — блок `if is_tariff_change and tariff_id is not None:` в `extend_subscription` (около строки 1770)
- Test: `tests/crud/test_premium_traffic_state.py`

**Interfaces:**
- Consumes: `get_states_for_subscription(db, subscription_id)`, `get_premium_squads_for_tariff(tariff) -> dict[str, PremiumSquadConfig]`, `PremiumSquadConfig.limit_bytes`.
- Produces: `async def sync_premium_state_limits(db: AsyncSession, subscription_id: int, tariff: object | None) -> int` — число изменённых строк.

- [ ] **Step 1: Написать падающие тесты**

В `tests/crud/test_premium_traffic_state.py` добавить `SimpleNamespace` в импорты, `sync_premium_state_limits` в импорт из `app.database.crud.premium_traffic` и тесты:

```python
def _tariff(limits):
    return SimpleNamespace(server_traffic_limits=limits)


async def test_tariff_switch_refreshes_limit_and_keeps_extra(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.extra_bytes = 2 * BYTES_IN_GB
        await db.commit()

        changed = await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert changed == 1
        assert reread.limit_bytes == 20 * BYTES_IN_GB
        assert reread.extra_bytes == 2 * BYTES_IN_GB


async def test_tariff_switch_lowers_limit_of_an_open_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, limit_bytes=20 * BYTES_IN_GB)
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 5}}))
        await db.commit()

        assert (await get_state(db, 1, SQUAD)).limit_bytes == 5 * BYTES_IN_GB


async def test_tariff_switch_raises_limit_for_an_auto_limited_squad(monkeypatch):
    """Апгрейд должен вернуть воркеру право восстановить снятый сквад."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = 5 * BYTES_IN_GB
        state.is_limited = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 20 * BYTES_IN_GB
        assert reread.is_limited is True


async def test_tariff_switch_does_not_lower_limit_of_a_limited_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db, limit_bytes=20 * BYTES_IN_GB)
        state.used_bytes = 20 * BYTES_IN_GB
        state.is_limited = True
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 5}}))
        await db.commit()

        assert (await get_state(db, 1, SQUAD)).limit_bytes == 20 * BYTES_IN_GB


async def test_tariff_switch_leaves_admin_closed_squad_untouched(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)  # 5 ГБ
        state.used_bytes = state.limit_bytes
        state.is_limited = True
        state.closed_at = NOW
        await db.commit()

        await sync_premium_state_limits(db, 1, _tariff({SQUAD: {'traffic_limit_gb': 20}}))
        await db.commit()

        reread = await get_state(db, 1, SQUAD)
        assert reread.limit_bytes == 5 * BYTES_IN_GB
        assert reread.closed_at == NOW


async def test_tariff_switch_ignores_squad_absent_from_new_tariff(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db)
        await db.commit()

        changed = await sync_premium_state_limits(db, 1, _tariff({}))
        await db.commit()

        assert changed == 0
        assert (await get_state(db, 1, SQUAD)).limit_bytes == 5 * BYTES_IN_GB
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/crud/test_premium_traffic_state.py -q`
Expected: FAIL — `ImportError: cannot import name 'sync_premium_state_limits'`.

- [ ] **Step 3: Реализовать `sync_premium_state_limits`**

В `app/database/crud/premium_traffic.py` добавить после `start_new_period`:

```python
async def sync_premium_state_limits(
    db: AsyncSession,
    subscription_id: int,
    tariff: object | None,
) -> int:
    """Подтянуть снимки лимитов к новому тарифу после смены тарифа подписки.

    Сквад, оставшийся премиумным в новом тарифе, продолжает текущий период, но
    потолок берётся из нового тарифа сразу — иначе экран показывает старый лимит
    до ближайшего прохода воркера. Автоснятому скваду лимит только поднимаем:
    опускание ограничило бы возврат доступа задним числом. Вручную закрытый
    сквад (``closed_at``) не трогаем — это решение оператора.
    """
    from app.utils.premium_traffic import get_premium_squads_for_tariff

    premium = get_premium_squads_for_tariff(tariff)
    if not premium:
        return 0

    changed = 0
    for state in await get_states_for_subscription(db, subscription_id):
        config = premium.get(state.squad_uuid)
        if config is None or state.closed_at is not None:
            continue
        if state.is_limited and config.limit_bytes <= (state.limit_bytes or 0):
            continue
        if state.limit_bytes != config.limit_bytes:
            state.limit_bytes = config.limit_bytes
            changed += 1
    return changed
```

- [ ] **Step 4: Вызвать синхронизацию в `extend_subscription`**

В `app/database/crud/subscription.py` в блоке смены тарифа сразу после `await _drop_orphan_premium_states(...)`:

```python
        from app.database.crud.premium_traffic import sync_premium_state_limits

        updated_limits = await sync_premium_state_limits(db, subscription.id, new_tariff)
        if updated_limits:
            logger.info(
                '💠 Смена тарифа: лимиты премиум-сквадов обновлены',
                subscription_id=subscription.id,
                updated_count=updated_limits,
            )
```

- [ ] **Step 5: Прогнать тесты**

Run: `python -m pytest tests/crud/test_premium_traffic_state.py tests/crud/test_drop_orphan_premium_states.py -q`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add app/database/crud/premium_traffic.py app/database/crud/subscription.py tests/crud/test_premium_traffic_state.py docs/superpowers/specs/2026-11-19-premium-traffic-refresh-after-purchase-design.md docs/superpowers/plans/2026-11-19-premium-traffic-refresh-after-purchase.md
git commit -m "fix: refresh premium traffic limits on tariff change"
```

---

### Task 2: Будить воркер премиум-трафика по событию

**Files:**
- Modify: `app/services/premium_traffic_service.py` — `__init__`, `start_monitoring`, `stop`, новый `request_pass`
- Test: `tests/services/test_premium_traffic_service.py`

**Interfaces:**
- Consumes: `invalidate_panel_user(panel_user_id)`.
- Produces: `PremiumTrafficService.request_pass(*, panel_user_id: int | None = None) -> None`; `stop()` завершает спящий цикл; `start_monitoring()` реагирует на событие раньше интервала.

- [ ] **Step 1: Написать падающие тесты**

В `tests/services/test_premium_traffic_service.py` добавить `import asyncio` в начало и тесты:

```python
def _empty_stats():
    return {'checked': 0, 'limited': 0, 'restored': 0, 'warned': 0, 'cleaned': 0, 'errors': 0}


async def test_request_pass_runs_the_next_pass_without_waiting_for_the_interval(monkeypatch):
    service = PremiumTrafficService()
    monkeypatch.setattr(service, 'get_check_interval_seconds', lambda: 3600)
    first_pass = asyncio.Event()
    second_pass = asyncio.Event()
    passes = 0

    async def _process_once():
        nonlocal passes
        passes += 1
        if passes == 1:
            first_pass.set()
        elif passes == 2:
            second_pass.set()
        return _empty_stats()

    monkeypatch.setattr(service, 'process_once', _process_once)
    task = asyncio.create_task(service.start_monitoring())
    try:
        await asyncio.wait_for(first_pass.wait(), timeout=1)
        service.request_pass()
        service.request_pass()
        await asyncio.wait_for(second_pass.wait(), timeout=1)
    finally:
        service.stop()
        await asyncio.wait_for(task, timeout=1)

    assert passes == 2  # два вызова подряд схлопнулись в один проход


async def test_stop_interrupts_the_sleep_and_ends_the_loop(monkeypatch):
    service = PremiumTrafficService()
    monkeypatch.setattr(service, 'get_check_interval_seconds', lambda: 3600)
    first_pass = asyncio.Event()

    async def _process_once():
        first_pass.set()
        return _empty_stats()

    monkeypatch.setattr(service, 'process_once', _process_once)
    task = asyncio.create_task(service.start_monitoring())
    await asyncio.wait_for(first_pass.wait(), timeout=1)
    service.stop()
    await asyncio.wait_for(task, timeout=1)


def test_request_pass_invalidates_the_panel_user_cache():
    service = PremiumTrafficService()
    service._panel_users[42] = (0.0, object())

    service.request_pass(panel_user_id=42)

    assert 42 not in service._panel_users
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/services/test_premium_traffic_service.py -q`
Expected: FAIL — `AttributeError: 'PremiumTrafficService' object has no attribute 'request_pass'` (и тест `stop` висит до таймаута).

- [ ] **Step 3: Реализовать событие и `request_pass`**

В `PremiumTrafficService.__init__` рядом с `self._running` добавить:

```python
        self._wake_event = asyncio.Event()
```

Заменить хвост `start_monitoring`:

```python
        while self._running:
            self._wake_event.clear()
            try:
                stats = await self.process_once()
                if stats['limited'] or stats['restored'] or stats['cleaned'] or stats['errors']:
                    logger.info('📊 Проход по премиум-трафику', **stats)
            except Exception as error:
                logger.error('Ошибка в цикле учёта премиум-трафика', error=error, exc_info=True)
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
            except TimeoutError:
                pass
```

Добавить метод рядом с `is_enabled`:

```python
    def request_pass(self, *, panel_user_id: int | None = None) -> None:
        """Разбудить цикл для внеочередного прохода.

        Вызывается после покупки, когда панель уже получила новый тариф и
        сбросила трафик. Будится единственный цикл: конкурентных проходов не
        появляется, а несколько вызовов подряд схлопываются в один.
        """
        if panel_user_id is not None:
            self.invalidate_panel_user(panel_user_id)
        self._wake_event.set()
```

Заменить `stop`:

```python
    def stop(self) -> None:
        self._running = False
        self._wake_event.set()
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m pytest tests/services/test_premium_traffic_service.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add app/services/premium_traffic_service.py tests/services/test_premium_traffic_service.py
git commit -m "feat: wake premium traffic worker on demand"
```

---

### Task 3: Будить воркер после синхронизации покупки с панелью

**Files:**
- Modify: `app/services/subscription_service.py` — новый helper после `logger`; вызовы после `db.commit()` в `create_remnawave_user` (около строки 298) и `update_remnawave_user` (около строки 708)
- Test: `tests/services/test_subscription_service_premium_refresh.py` (новый)

**Interfaces:**
- Consumes: `get_premium_squads_for_tariff(tariff)`, `premium_traffic_service.is_enabled()`, `premium_traffic_service.request_pass(panel_user_id=...)`.
- Produces: `_request_premium_traffic_refresh(subscription: Subscription, panel_user_id: int | None, *, reset_traffic: bool) -> None`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/services/test_subscription_service_premium_refresh.py`:

```python
"""Пробуждение воркера премиум-трафика после покупки."""

from types import SimpleNamespace

from app.services import subscription_service


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'


def _subscription(limits):
    return SimpleNamespace(tariff=SimpleNamespace(server_traffic_limits=limits))


def _fake_service(monkeypatch, *, enabled=True, calls=None):
    calls = [] if calls is None else calls
    fake = SimpleNamespace(
        is_enabled=lambda: enabled,
        request_pass=lambda *, panel_user_id: calls.append(panel_user_id),
    )
    monkeypatch.setattr('app.services.premium_traffic_service.premium_traffic_service', fake)
    return calls


def test_premium_tariff_wakes_the_worker_with_the_panel_id(monkeypatch):
    calls = _fake_service(monkeypatch)

    subscription_service._request_premium_traffic_refresh(
        _subscription({SQUAD: {'traffic_limit_gb': 20}}),
        42,
        reset_traffic=True,
    )

    assert calls == [42]


def test_sync_without_traffic_reset_does_not_wake_the_worker(monkeypatch):
    calls = _fake_service(monkeypatch)

    subscription_service._request_premium_traffic_refresh(
        _subscription({SQUAD: {'traffic_limit_gb': 20}}),
        42,
        reset_traffic=False,
    )

    assert calls == []


def test_tariff_without_premium_squads_does_not_wake_the_worker(monkeypatch):
    calls = _fake_service(monkeypatch)

    subscription_service._request_premium_traffic_refresh(_subscription({}), 42, reset_traffic=True)

    assert calls == []


def test_disabled_feature_does_not_wake_the_worker(monkeypatch):
    calls = _fake_service(monkeypatch, enabled=False)

    subscription_service._request_premium_traffic_refresh(
        _subscription({SQUAD: {'traffic_limit_gb': 20}}),
        42,
        reset_traffic=True,
    )

    assert calls == []
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/services/test_subscription_service_premium_refresh.py -q`
Expected: FAIL — `AttributeError: module 'app.services.subscription_service' has no attribute '_request_premium_traffic_refresh'`.

- [ ] **Step 3: Реализовать helper**

В `app/services/subscription_service.py` после `logger = structlog.get_logger(__name__)` добавить:

```python
def _request_premium_traffic_refresh(
    subscription: Subscription,
    panel_user_id: int | None,
    *,
    reset_traffic: bool,
) -> None:
    """Разбудить воркер премиум-трафика после синхронизации покупки с панелью.

    Вызывается после коммита и после сброса трафика в панели: воркер увидит
    новый ``lastTrafficResetAt`` и начнёт новый премиум-период сразу, а не через
    интервал. Без премиум-сквадов в тарифе проход не нужен. Сбой пробуждения не
    должен ломать уже оплаченную покупку.
    """
    if not reset_traffic:
        return
    try:
        from app.utils.premium_traffic import get_premium_squads_for_tariff

        if not get_premium_squads_for_tariff(getattr(subscription, 'tariff', None)):
            return
        from app.services.premium_traffic_service import premium_traffic_service

        if not premium_traffic_service.is_enabled():
            return
        premium_traffic_service.request_pass(panel_user_id=panel_user_id)
    except Exception as error:
        logger.warning('Не удалось запросить внеочередной проход премиум-трафика', error=error)
```

- [ ] **Step 4: Вызвать helper в обеих точках синхронизации**

В `create_remnawave_user` после `await db.commit()` перед `logger.info('✅ Создан/обновлен RemnaWave пользователь для подписки', ...)`:

```python
                _request_premium_traffic_refresh(
                    subscription,
                    updated_user.id,
                    reset_traffic=reset_traffic,
                )
```

В `update_remnawave_user` после `await db.commit()` перед `status_text = ...`:

```python
                _request_premium_traffic_refresh(
                    subscription,
                    remnawave_id,
                    reset_traffic=reset_traffic,
                )
```

Ранние ветки `preserve_open_grace` не трогаем: панель там не сбрасывает трафик, а премиум-воркер и так пропускает подписки с открытым grace-оверлеем.

- [ ] **Step 5: Прогнать тесты**

Run: `python -m pytest tests/services/test_subscription_service_premium_refresh.py tests/services/test_premium_squads_are_filtered_everywhere.py -q`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add app/services/subscription_service.py tests/services/test_subscription_service_premium_refresh.py
git commit -m "feat: request premium traffic refresh after purchase sync"
```

---

### Task 4: Документация, changelog и общий прогон

**Files:**
- Modify: `docs/premium-traffic-limits.md` — разделы «Данные» (`limit_bytes`) и «Воркер»
- Modify: `../claude-changes.md` — запись в конец файла

- [ ] **Step 1: Обновить документацию**

В `docs/premium-traffic-limits.md` в описании `limit_bytes` добавить предложение:

```markdown
   При смене тарифа подписки снимок обновляется сразу
   (`sync_premium_state_limits` в `app/database/crud/premium_traffic.py`):
   сквады, оставшиеся премиумными, получают новый потолок, вручную закрытые
   (`closed_at`) не трогаются.
```

В разделе «Воркер» после абзаца про интервал добавить:

```markdown
После успешной синхронизации покупки с панелью (`reset_traffic=True` в
`SubscriptionService.create_remnawave_user` / `update_remnawave_user`) сервис
будится вне очереди через `request_pass()`: цикл ждёт `asyncio.Event` с
таймаутом интервала вместо `sleep`. Повторные вызовы схлопываются в один
проход, параллельных проходов не появляется. Это нужно, чтобы после покупки
тарифа белые списки получали новый лимит и возвращались из снятых за расход
сразу, а не через пять минут.
```

- [ ] **Step 2: Добавить запись в `../claude-changes.md`**

В конец файла:

```markdown
## 2026-11-19 — Белые списки: обновление после покупки тарифа

**Scope:** `app/services/premium_traffic_service.py`, `app/services/subscription_service.py`, `app/database/crud/premium_traffic.py`, `app/database/crud/subscription.py`, `docs/premium-traffic-limits.md`, тесты `tests/crud/test_premium_traffic_state.py`, `tests/services/test_premium_traffic_service.py`, `tests/services/test_subscription_service_premium_refresh.py`.
**What:** при смене тарифа снимки `subscription_premium_traffic.limit_bytes` обновляются сразу (кроме вручную закрытых сквадов); воркер премиум-трафика получил `asyncio.Event` и `request_pass()` и будится после успешной синхронизации покупки с панелью при `reset_traffic=True`.
**Why:** после покупки тарифа с 20 ГБ белых списков экран до пяти минут показывал триальные 3 ГБ, а снятый за расход сквад оставался недоступным до ближайшего прохода воркера.
```

- [ ] **Step 3: Общий прогон**

Run:
```bash
python -m pytest tests/crud/test_premium_traffic_state.py tests/crud/test_drop_orphan_premium_states.py tests/services/test_premium_traffic_service.py tests/services/test_subscription_service_premium_refresh.py tests/services/test_premium_squads_are_filtered_everywhere.py -q
```
Expected: PASS, без новых warning'ов про необработанные задачи.

- [ ] **Step 4: Коммит**

```bash
git add docs/premium-traffic-limits.md docs/superpowers/specs/2026-11-19-premium-traffic-refresh-after-purchase-design.md docs/superpowers/plans/2026-11-19-premium-traffic-refresh-after-purchase.md
git commit -m "docs: describe premium traffic refresh after purchase"
```

---

## Fix pass after review

- Снимок лимита вынесен в `refresh_premium_states_for_tariff` и подключён во все
  пути смены тарифа: `extend_subscription`, `replace_subscription` (guest-покупка)
  и кабинетный `tariff_switch`.
- `request_premium_traffic_refresh` разделён на публичный helper без гейта и
  приватную reset-обёртку; guest-доставка зовёт публичный после своего коммита.
- Добавлены интеграционные тесты на реальные `extend_subscription` и
  `replace_subscription`, а также тесты call-site для `create_remnawave_user` и
  `update_remnawave_user`.
- Семантика `closed_at`: закрытие и блокировка переживают смену тарифа; снимок
  лимита в новом периоде следует за тарифом. Закреплено тестом
  `test_new_period_updates_closed_limit_but_keeps_access_blocked`.
- Отложенные Minor: сброс `notified_80/90` при поднятии лимита снятому скваду;
  транзиентная перезапись снимка уже идущим проходом; `try/finally` в тесте
  `stop`.
